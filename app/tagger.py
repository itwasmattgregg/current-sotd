"""Tag downloaded MP3s via MusicBrainz lookup + mutagen ID3 writes.

The lookup runs in three passes:

1. ``/ws/2/recording?query=...`` to find candidate recordings.
2. ``/ws/2/recording/<mbid>?inc=...+releases+media`` for each candidate, which
   returns the releases the recording appears on *and* the medium/track entry
   for it -- that is where the real track number comes from.
3. Targeted lookups on the winning release / release-group / work to pick up
   label, barcode, catalog number, genre and composer credits.

MusicBrainz asks for roughly one request per second, so every call goes through
``_MBSession`` which spaces requests out.
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

import httpx
from mutagen.id3 import (
    APIC,
    ID3,
    TALB,
    TCOM,
    TCON,
    TDOR,
    TDRC,
    TEXT,
    TIT2,
    TLEN,
    TMED,
    TPE1,
    TPE2,
    TPOS,
    TPUB,
    TRCK,
    TSO2,
    TSOP,
    TSRC,
    TXXX,
    UFID,
    ID3NoHeaderError,
)

LogFn = Callable[[str], None]

MB_BASE = "https://musicbrainz.org/ws/2"
COVER_BASE = "https://coverartarchive.org"

MIN_SCORE = 60
#: How close a candidate title has to be to the one from the RSS feed.
MIN_TITLE_SIMILARITY = 0.55
#: Candidates pulled from the search result and fully looked up.
MAX_CANDIDATES = 5
#: MusicBrainz rate limit: ~1 request/second.
MB_MIN_INTERVAL = 1.1

RECORDING_INC = (
    "artists+artist-credits+releases+release-groups+media+isrcs+genres+work-rels"
)
# ``media`` is part of the default release response, so it is not requested as
# an inc -- fewer inc values means fewer ways for the request to 400.
RELEASE_INC = "artist-credits+labels+release-groups"
RELEASE_GROUP_INC = "genres"
WORK_INC = "artist-rels"

_LEADING_NOISE = re.compile(
    r"^(?:song of the day|sotd|the current)\s*[:\-–—]\s*", re.IGNORECASE
)
_SEPARATORS = (" - ", " – ", " — ", " ‒ ")
_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)')


def _noop_log(msg: str) -> None:
    pass


def musicbrainz_enabled() -> bool:
    return os.environ.get("MUSICBRAINZ_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def deep_metadata_enabled() -> bool:
    """Follow-up lookups for label/genre/composer. On by default."""
    return os.environ.get("MUSICBRAINZ_DEEP_METADATA", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def id3_version() -> int:
    """ID3v2 minor version to write. v2.3 is the most NAS-compatible."""
    raw = os.environ.get("ID3_VERSION", "3").strip()
    return 4 if raw == "4" else 3


def _user_agent() -> str:
    contact = os.environ.get("MUSICBRAINZ_CONTACT", "").strip()
    base = "current-sotd/1.0 (https://github.com/current-sotd)"
    if contact:
        return f"{base}; contact={contact}"
    return base


@dataclass
class TrackMeta:
    """Everything we were able to pull for one song."""

    title: str
    artist: str
    album: str = ""
    album_artist: str = ""
    date: str = ""
    original_date: str = ""
    track_number: str = ""
    track_total: str = ""
    disc_number: str = ""
    disc_total: str = ""
    genre: str = ""
    label: str = ""
    catalog_number: str = ""
    barcode: str = ""
    isrc: str = ""
    media_format: str = ""
    release_country: str = ""
    release_status: str = ""
    release_type: str = ""
    composer: str = ""
    lyricist: str = ""
    artist_sort: str = ""
    album_artist_sort: str = ""
    length_ms: int = 0
    recording_mbid: str = ""
    release_mbid: str = ""
    release_group_mbid: str = ""
    release_track_mbid: str = ""
    work_mbid: str = ""
    artist_mbids: list[str] = field(default_factory=list)
    album_artist_mbids: list[str] = field(default_factory=list)
    score: int = 0

    @property
    def year(self) -> str:
        return (self.date or self.original_date or "")[:4]

    @property
    def original_year(self) -> str:
        return (self.original_date or self.date or "")[:4]

    def filled_fields(self) -> list[str]:
        """Names of the populated fields, for logging."""
        out = []
        for name, value in vars(self).items():
            if name == "score":
                continue
            if value:
                out.append(name)
        return out


# --------------------------------------------------------------------------
# Feed title parsing
# --------------------------------------------------------------------------


def parse_artist_title(rss_title: str) -> tuple[str, str]:
    """Split feed titles like 'Artist - Song' into artist and title."""
    raw = re.sub(r"\s+", " ", (rss_title or "").strip())
    raw = _LEADING_NOISE.sub("", raw).strip()
    raw = raw.strip("“”\"'")

    for sep in _SEPARATORS:
        if sep in raw:
            artist, title = raw.split(sep, 1)
            artist, title = artist.strip(), title.strip()
            if artist and title:
                return artist, _strip_quotes(title)
    return "", _strip_quotes(raw)


def _strip_quotes(value: str) -> str:
    return value.strip().strip("“”\"'").strip()


def _strip_decorations(title: str) -> str:
    """Drop trailing '(Live at ...)' / '[Radio Edit]' style qualifiers."""
    cleaned = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", title).strip()
    return cleaned or title


def _primary_artist(artist: str) -> str:
    """'A feat. B' -> 'A'.

    Only guest-credit markers are stripped. Splitting on ``&``/``and``/``/``
    would wreck names like 'Simon & Garfunkel', 'Florence and the Machine' or
    'AC/DC', so those are left alone.
    """
    parts = re.split(
        r"\s+(?:feat\.?|featuring|ft\.?|w/|with)\s+",
        artist,
        flags=re.IGNORECASE,
    )
    return parts[0].strip() if parts else artist.strip()


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "").lower()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\b(?:the|a|an)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def _similarity(a: str, b: str) -> float:
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.92
    return SequenceMatcher(None, na, nb).ratio()


def _lucene_escape(value: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", value)


def _format_artist_credit(credit: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for part in credit or []:
        name = part.get("name") or (part.get("artist") or {}).get("name") or ""
        parts.append(str(name))
        parts.append(str(part.get("joinphrase") or ""))
    return "".join(parts).strip()


def _artist_sort(credit: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for part in credit or []:
        artist = part.get("artist") or {}
        name = artist.get("sort-name") or artist.get("name") or part.get("name") or ""
        parts.append(str(name))
        parts.append(str(part.get("joinphrase") or ""))
    return "".join(parts).strip()


def _artist_ids(credit: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for part in credit or []:
        mbid = (part.get("artist") or {}).get("id")
        if mbid and mbid not in ids:
            ids.append(str(mbid))
    return ids


def _pick_genre(entity: dict[str, Any]) -> str:
    """Highest-voted genre.

    Only the curated ``genres`` list is used. Raw ``tags`` are the same data
    before curation and are full of things like "seen live", which make for a
    worse genre than none at all.
    """
    items = [item for item in (entity.get("genres") or []) if item.get("name")]
    if not items:
        return ""
    best = max(items, key=lambda item: int(item.get("count") or 0))
    return str(best["name"]).strip().title()


# --------------------------------------------------------------------------
# Release ranking
# --------------------------------------------------------------------------


def _date_key(value: str) -> int:
    """'2004-05-24' -> 20040524, missing/partial dates sort last."""
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return 99999999
    digits = (digits + "0101")[:8]
    try:
        return int(digits)
    except ValueError:
        return 99999999


def _release_rank(release: dict[str, Any]) -> int:
    """Prefer official studio albums over bootlegs/live comps."""
    score = 0
    status = (release.get("status") or "").lower()
    if status == "official":
        score += 100
    elif status == "bootleg":
        score -= 100
    elif status in {"promotion", "pseudo-release"}:
        score -= 40

    rg = release.get("release-group") or {}
    primary = (rg.get("primary-type") or "").lower()
    if primary == "album":
        score += 40
    elif primary == "single":
        score += 30
    elif primary == "ep":
        score += 20

    secondary = {str(s).lower() for s in (rg.get("secondary-types") or [])}
    if "live" in secondary:
        score -= 80
    if "compilation" in secondary:
        score -= 20
    if "remix" in secondary or "dj-mix" in secondary:
        score -= 40
    if "demo" in secondary:
        score -= 30
    if "interview" in secondary or "spokenword" in secondary:
        score -= 80

    title = f"{release.get('title') or ''} {rg.get('title') or ''}"
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", title):
        score -= 80
    if re.search(r"\blive\b", title, re.IGNORECASE):
        score -= 40

    if release.get("date"):
        score += 10

    # The pressing that matches the release group's first release is the
    # original issue rather than a later reissue.
    first = (rg.get("first-release-date") or "")[:10]
    if first and first == (release.get("date") or "")[:10]:
        score += 15

    if _extract_track_info(release):
        score += 5

    return score


def _release_is_usable(rank: int) -> bool:
    """Good enough to write album/track tags from."""
    return rank > 0


def _pick_release(releases: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not releases:
        return None
    # Highest rank wins; ties go to the earliest release (the original issue).
    return max(
        releases,
        key=lambda r: (_release_rank(r), -_date_key(str(r.get("date") or ""))),
    )


def _extract_track_info(release: dict[str, Any]) -> dict[str, Any]:
    """Pull the medium/track entry MusicBrainz returns for the recording.

    A recording lookup with ``inc=releases+media`` embeds only the medium and
    track the recording actually sits on, which is where the real track number
    comes from. ``track-offset`` is zero-based.
    """
    for medium in release.get("media") or []:
        tracks = medium.get("track") or medium.get("tracks") or []
        if not tracks:
            continue
        track = tracks[0] or {}

        position = track.get("position")
        if position is None:
            offset = medium.get("track-offset")
            if offset is not None:
                try:
                    position = int(offset) + 1
                except (TypeError, ValueError):
                    position = None

        number = str(track.get("number") or "").strip()
        if not number and position is not None:
            number = str(position)

        if not number:
            continue

        return {
            "number": number,
            "position": position,
            "track_total": str(medium.get("track-count") or "").strip(),
            "disc_number": str(medium.get("position") or "").strip(),
            "format": str(medium.get("format") or "").strip(),
            "track_mbid": str(track.get("id") or "").strip(),
            "track_title": str(track.get("title") or "").strip(),
            "length": track.get("length"),
        }
    return {}


# --------------------------------------------------------------------------
# HTTP session with MusicBrainz rate limiting
# --------------------------------------------------------------------------


class _MBSession:
    """httpx wrapper that keeps MusicBrainz calls ~1/second."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        log: LogFn = _noop_log,
        min_interval: float = MB_MIN_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._log = log
        self._min_interval = min_interval
        self._sleep = sleep
        self._last_call = 0.0

    def _throttle(self) -> None:
        if self._last_call:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = time.monotonic()

    def get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        self._throttle()
        try:
            resp = self._client.get(f"{MB_BASE}{path}", params={"fmt": "json", **params})
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._log(f"MusicBrainz request failed ({path}): {exc}")
            return None


# --------------------------------------------------------------------------
# Search + candidate selection
# --------------------------------------------------------------------------


def _build_queries(artist: str, title: str) -> list[str]:
    """Progressively looser Lucene queries, best first."""
    queries: list[str] = []
    title_q = _lucene_escape(title)
    bare_title = _strip_decorations(title)
    bare_title_q = _lucene_escape(bare_title)
    primary_q = _lucene_escape(_primary_artist(artist))
    artist_q = _lucene_escape(artist)

    def add(query: str) -> None:
        if query and query not in queries:
            queries.append(query)

    if title and artist:
        add(f'recording:"{title_q}" AND artist:"{artist_q}"')
        if primary_q and primary_q != artist_q:
            add(f'recording:"{title_q}" AND artist:"{primary_q}"')
        if bare_title_q != title_q:
            add(f'recording:"{bare_title_q}" AND artist:"{primary_q or artist_q}"')
        add(f'recording:"{title_q}" AND artistname:"{primary_q or artist_q}"')
    if title:
        add(f'recording:"{title_q}"')
        if bare_title_q != title_q:
            add(f'recording:"{bare_title_q}"')
    return queries


def _candidate_score(
    recording: dict[str, Any],
    *,
    artist: str,
    title: str,
    release_rank: int,
) -> float:
    mb_score = int(recording.get("score") or 0)
    title_sim = _similarity(str(recording.get("title") or ""), title)
    credit = recording.get("artist-credit") or []
    artist_sim = 0.0
    if artist:
        candidates = [_format_artist_credit(credit)] + [
            str((part.get("artist") or {}).get("name") or part.get("name") or "")
            for part in credit
        ]
        artist_sim = max((_similarity(name, artist) for name in candidates), default=0.0)
    else:
        artist_sim = 0.6  # unknown, stay neutral

    return (
        0.45 * mb_score
        + 30.0 * title_sim
        + 25.0 * artist_sim
        + 0.25 * max(release_rank, -100)
    )


def _lookup_recording(session: _MBSession, mbid: str) -> dict[str, Any] | None:
    return session.get_json(f"/recording/{mbid}", {"inc": RECORDING_INC})


def _search_candidates(
    session: _MBSession,
    artist: str,
    title: str,
    *,
    log: LogFn,
) -> list[dict[str, Any]]:
    for query in _build_queries(artist, title):
        log(f"MusicBrainz search: {query}")
        payload = session.get_json("/recording", {"query": query, "limit": 10})
        if not payload:
            continue
        recordings = payload.get("recordings") or []
        candidates = [
            rec
            for rec in recordings
            if rec.get("id") and int(rec.get("score") or 0) >= MIN_SCORE
        ]
        if candidates:
            return candidates[:MAX_CANDIDATES]
        if recordings:
            log(f"MusicBrainz: no recordings at or above score {MIN_SCORE}")
    return []


def search_recording(
    artist: str,
    title: str,
    *,
    client: httpx.Client | None = None,
    session: _MBSession | None = None,
    log: LogFn = _noop_log,
    deep: bool | None = None,
) -> TrackMeta | None:
    """Find the best MusicBrainz match and build a fully populated TrackMeta."""
    if session is None:
        if client is None:
            raise ValueError("search_recording needs a client or a session")
        session = _MBSession(client, log=log)
    if deep is None:
        deep = deep_metadata_enabled()

    if not title and not artist:
        return None

    candidates = _search_candidates(session, artist, title, log=log)
    if not candidates:
        log("MusicBrainz: no recordings found")
        return None

    ranked: list[tuple[float, dict[str, Any], dict[str, Any] | None, int]] = []
    for rec in candidates:
        full = _lookup_recording(session, str(rec["id"])) or rec
        # The search hit carries the relevance score; the lookup does not.
        full.setdefault("score", rec.get("score"))
        releases = full.get("releases") or rec.get("releases") or []
        chosen = _pick_release(releases)
        rank = _release_rank(chosen) if chosen else -999
        total = _candidate_score(full, artist=artist, title=title, release_rank=rank)
        ranked.append((total, full, chosen, rank))

    ranked.sort(key=lambda row: row[0], reverse=True)
    total, best, chosen, rank = ranked[0]

    title_sim = _similarity(str(best.get("title") or ""), title)
    if title and title_sim < MIN_TITLE_SIMILARITY:
        log(
            f"MusicBrainz: best match {best.get('title')!r} too far from "
            f"{title!r} (similarity={title_sim:.2f}); skipping"
        )
        return None

    log(
        f"MusicBrainz match: {best.get('title')!r} "
        f"(mbid={best.get('id')}, score={best.get('score')}, "
        f"rank={rank}, confidence={total:.1f})"
    )
    return _build_meta(
        session,
        best,
        chosen,
        rank,
        fallback_artist=artist,
        fallback_title=title,
        deep=deep,
        log=log,
    )


# --------------------------------------------------------------------------
# Metadata assembly
# --------------------------------------------------------------------------


def _build_meta(
    session: _MBSession,
    recording: dict[str, Any],
    release: dict[str, Any] | None,
    release_rank: int,
    *,
    fallback_artist: str,
    fallback_title: str,
    deep: bool,
    log: LogFn,
) -> TrackMeta:
    credit = recording.get("artist-credit") or []
    isrcs = [str(i) for i in (recording.get("isrcs") or []) if i]

    meta = TrackMeta(
        title=str(recording.get("title") or fallback_title).strip(),
        artist=(_format_artist_credit(credit) or fallback_artist).strip(),
        artist_sort=_artist_sort(credit),
        artist_mbids=_artist_ids(credit),
        isrc=isrcs[0] if isrcs else "",
        length_ms=int(recording.get("length") or 0),
        genre=_pick_genre(recording),
        recording_mbid=str(recording.get("id") or "").strip(),
        score=int(recording.get("score") or 0),
    )

    work = _find_work(recording)
    if work:
        meta.work_mbid = str(work.get("id") or "")

    if release is None:
        return meta

    if not _release_is_usable(release_rank):
        log(
            f"MusicBrainz: weak release match (rank={release_rank}); "
            "tagging artist/title only"
        )
        return meta

    _apply_release(meta, release)

    track = _extract_track_info(release)
    if track:
        meta.track_number = track["number"]
        meta.track_total = track["track_total"]
        meta.disc_number = track["disc_number"]
        meta.media_format = track["format"]
        meta.release_track_mbid = track["track_mbid"]
        if not meta.length_ms and track.get("length"):
            meta.length_ms = int(track["length"] or 0)
    else:
        log("MusicBrainz: release has no track position for this recording")

    if deep:
        _enrich(session, meta, release, work, log=log)

    return meta


def _apply_release(meta: TrackMeta, release: dict[str, Any]) -> None:
    rg = release.get("release-group") or {}
    meta.album = str(rg.get("title") or release.get("title") or "").strip()
    meta.release_mbid = str(release.get("id") or "").strip()
    meta.release_group_mbid = str(rg.get("id") or "").strip()
    meta.date = str(release.get("date") or "").strip()
    meta.original_date = str(rg.get("first-release-date") or meta.date).strip()
    meta.release_country = str(release.get("country") or "").strip()
    meta.release_status = str(release.get("status") or "").strip()

    types = [t for t in [rg.get("primary-type")] if t]
    types += [str(t) for t in (rg.get("secondary-types") or []) if t]
    meta.release_type = "/".join(types)

    release_credit = release.get("artist-credit") or []
    if release_credit:
        meta.album_artist = _format_artist_credit(release_credit)
        meta.album_artist_sort = _artist_sort(release_credit)
        meta.album_artist_mbids = _artist_ids(release_credit)


def _find_work(recording: dict[str, Any]) -> dict[str, Any] | None:
    for rel in recording.get("relations") or []:
        work = rel.get("work")
        if work and work.get("id"):
            return work
    return None


def _enrich(
    session: _MBSession,
    meta: TrackMeta,
    release: dict[str, Any],
    work: dict[str, Any] | None,
    *,
    log: LogFn,
) -> None:
    """Follow-up lookups for label, barcode, disc totals, genre and composer."""
    if meta.release_mbid:
        full = session.get_json(f"/release/{meta.release_mbid}", {"inc": RELEASE_INC})
        if full:
            _apply_release_details(meta, full)

    if not meta.genre and meta.release_group_mbid:
        rg = session.get_json(
            f"/release-group/{meta.release_group_mbid}", {"inc": RELEASE_GROUP_INC}
        )
        if rg:
            meta.genre = _pick_genre(rg)
            if not meta.original_date:
                meta.original_date = str(rg.get("first-release-date") or "").strip()

    if work and work.get("id"):
        full_work = session.get_json(f"/work/{work['id']}", {"inc": WORK_INC})
        if full_work:
            _apply_work(meta, full_work)


def _apply_release_details(meta: TrackMeta, release: dict[str, Any]) -> None:
    label_info = release.get("label-info") or []
    labels = [
        str((info.get("label") or {}).get("name") or "").strip()
        for info in label_info
        if (info.get("label") or {}).get("name")
    ]
    catalogs = [
        str(info.get("catalog-number") or "").strip()
        for info in label_info
        if info.get("catalog-number")
    ]
    if labels and not meta.label:
        meta.label = labels[0]
    if catalogs and not meta.catalog_number:
        meta.catalog_number = catalogs[0]
    if release.get("barcode"):
        meta.barcode = str(release["barcode"]).strip()
    if release.get("country") and not meta.release_country:
        meta.release_country = str(release["country"]).strip()
    if release.get("date") and not meta.date:
        meta.date = str(release["date"]).strip()
    if release.get("status") and not meta.release_status:
        meta.release_status = str(release["status"]).strip()

    credit = release.get("artist-credit") or []
    if credit and not meta.album_artist:
        meta.album_artist = _format_artist_credit(credit)
        meta.album_artist_sort = _artist_sort(credit)
        meta.album_artist_mbids = _artist_ids(credit)

    rg = release.get("release-group") or {}
    if rg.get("first-release-date") and not meta.original_date:
        meta.original_date = str(rg["first-release-date"]).strip()
    if rg.get("title") and not meta.album:
        meta.album = str(rg["title"]).strip()

    media = release.get("media") or []
    if media:
        meta.disc_total = str(len(media))
        index = 0
        if meta.disc_number.isdigit():
            index = max(int(meta.disc_number) - 1, 0)
        medium = media[index] if index < len(media) else media[0]
        if medium.get("track-count") and not meta.track_total:
            meta.track_total = str(medium["track-count"])
        if medium.get("format") and not meta.media_format:
            meta.media_format = str(medium["format"]).strip()
        if not meta.disc_number and len(media) == 1:
            meta.disc_number = "1"


def _apply_work(meta: TrackMeta, work: dict[str, Any]) -> None:
    composers: list[str] = []
    lyricists: list[str] = []
    for rel in work.get("relations") or []:
        rel_type = str(rel.get("type") or "").lower()
        name = str((rel.get("artist") or {}).get("name") or "").strip()
        if not name:
            continue
        if rel_type in {"composer", "writer"} and name not in composers:
            composers.append(name)
        elif rel_type == "lyricist" and name not in lyricists:
            lyricists.append(name)
    if composers and not meta.composer:
        meta.composer = "; ".join(composers)
    if lyricists and not meta.lyricist:
        meta.lyricist = "; ".join(lyricists)


# --------------------------------------------------------------------------
# Cover art
# --------------------------------------------------------------------------


def fetch_cover_art(
    release_mbid: str,
    *,
    client: httpx.Client,
    log: LogFn = _noop_log,
    release_group_mbid: str = "",
) -> bytes | None:
    targets = []
    if release_mbid:
        targets.append(f"{COVER_BASE}/release/{release_mbid}/front-500")
    if release_group_mbid:
        targets.append(f"{COVER_BASE}/release-group/{release_group_mbid}/front-500")
    for url in targets:
        try:
            resp = client.get(url)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            if resp.content:
                return resp.content
        except httpx.HTTPError as exc:
            log(f"Cover Art Archive error: {exc}")
    if targets:
        log("Cover Art Archive: no front cover")
    return None


# --------------------------------------------------------------------------
# ID3 writing
# --------------------------------------------------------------------------


def _txxx(tags: ID3, desc: str, value: str) -> None:
    if value:
        tags.add(TXXX(encoding=3, desc=desc, text=value))


def write_id3(path: Path, meta: TrackMeta, cover: bytes | None = None) -> None:
    """Replace the file's ID3 tag with one built purely from ``meta``.

    Existing frames are cleared rather than merged. Podcast MP3s arrive with
    the publisher's own tags (including a track number that has nothing to do
    with the song), and leaving those in place is what made every file look
    like the same track.
    """
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    tags.clear()

    tags.add(TIT2(encoding=3, text=meta.title))
    if meta.artist:
        tags.add(TPE1(encoding=3, text=meta.artist))
    if meta.album:
        tags.add(TALB(encoding=3, text=meta.album))
    if meta.album_artist:
        tags.add(TPE2(encoding=3, text=meta.album_artist))
    if meta.artist_sort:
        tags.add(TSOP(encoding=3, text=meta.artist_sort))
    if meta.album_artist_sort:
        tags.add(TSO2(encoding=3, text=meta.album_artist_sort))

    if meta.track_number:
        track = meta.track_number
        if meta.track_total:
            track = f"{track}/{meta.track_total}"
        tags.add(TRCK(encoding=3, text=track))
    if meta.disc_number:
        disc = meta.disc_number
        if meta.disc_total:
            disc = f"{disc}/{meta.disc_total}"
        tags.add(TPOS(encoding=3, text=disc))

    if meta.date:
        tags.add(TDRC(encoding=3, text=meta.date))
    elif meta.original_date:
        tags.add(TDRC(encoding=3, text=meta.original_date))
    if meta.original_date:
        tags.add(TDOR(encoding=3, text=meta.original_date))

    if meta.genre:
        tags.add(TCON(encoding=3, text=meta.genre))
    if meta.label:
        tags.add(TPUB(encoding=3, text=meta.label))
    if meta.isrc:
        tags.add(TSRC(encoding=3, text=meta.isrc))
    if meta.composer:
        tags.add(TCOM(encoding=3, text=meta.composer))
    if meta.lyricist:
        tags.add(TEXT(encoding=3, text=meta.lyricist))
    if meta.media_format:
        tags.add(TMED(encoding=3, text=meta.media_format))
    if meta.length_ms:
        tags.add(TLEN(encoding=3, text=str(meta.length_ms)))

    if meta.recording_mbid:
        tags.add(
            UFID(owner="http://musicbrainz.org", data=meta.recording_mbid.encode("ascii"))
        )
    # Picard-compatible TXXX frames so other taggers/players agree with us.
    _txxx(tags, "MusicBrainz Recording Id", meta.recording_mbid)
    _txxx(tags, "MusicBrainz Track Id", meta.recording_mbid)
    _txxx(tags, "MusicBrainz Release Track Id", meta.release_track_mbid)
    _txxx(tags, "MusicBrainz Album Id", meta.release_mbid)
    _txxx(tags, "MusicBrainz Release Group Id", meta.release_group_mbid)
    _txxx(tags, "MusicBrainz Work Id", meta.work_mbid)
    _txxx(tags, "MusicBrainz Artist Id", "/".join(meta.artist_mbids))
    _txxx(tags, "MusicBrainz Album Artist Id", "/".join(meta.album_artist_mbids))
    _txxx(tags, "MusicBrainz Album Type", meta.release_type)
    _txxx(tags, "MusicBrainz Album Status", meta.release_status)
    _txxx(tags, "MusicBrainz Album Release Country", meta.release_country)
    _txxx(tags, "CATALOGNUMBER", meta.catalog_number)
    _txxx(tags, "BARCODE", meta.barcode)
    _txxx(tags, "originalyear", meta.original_year)

    if cover:
        mime = "image/png" if cover.startswith(b"\x89PNG") else "image/jpeg"
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover))

    # v1=0 drops any ID3v1 trailer the publisher left behind; it can carry its
    # own stale track number that some scanners prefer.
    tags.save(path, v1=0, v2_version=id3_version())


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def tag_downloaded_file(
    path: Path,
    rss_title: str,
    *,
    log: LogFn = _noop_log,
) -> dict:
    """Look up MusicBrainz metadata and write ID3 tags. Never raises."""
    if not musicbrainz_enabled():
        return {"status": "disabled"}
    if not path.is_file():
        return {"status": "error", "message": "file missing"}

    artist, title = parse_artist_title(rss_title)
    headers = {"User-Agent": _user_agent(), "Accept": "application/json"}

    try:
        with httpx.Client(follow_redirects=True, timeout=30.0, headers=headers) as client:
            session = _MBSession(client, log=log)
            meta = search_recording(artist, title, session=session, log=log)
            if meta is None and artist and title:
                # Fallback: title-only search if artist+title missed.
                meta = search_recording("", title, session=session, log=log)
            if meta is None:
                return {"status": "no_match", "query_artist": artist, "query_title": title}

            cover = fetch_cover_art(
                meta.release_mbid,
                client=client,
                log=log,
                release_group_mbid=meta.release_group_mbid,
            )

            write_id3(path, meta, cover)
            track_desc = meta.track_number or "?"
            if meta.track_total:
                track_desc = f"{track_desc}/{meta.track_total}"
            log(
                f"TAGGED {path.name}: {meta.artist} — {meta.title}"
                f" [{meta.album or 'no album'} track {track_desc}]"
                f" (score={meta.score}, cover={'yes' if cover else 'no'},"
                f" fields={len(meta.filled_fields())})"
            )
            return {
                "status": "tagged",
                "artist": meta.artist,
                "title": meta.title,
                "album": meta.album,
                "album_artist": meta.album_artist,
                "date": meta.date,
                "original_date": meta.original_date,
                "track": meta.track_number,
                "track_total": meta.track_total,
                "disc": meta.disc_number,
                "disc_total": meta.disc_total,
                "genre": meta.genre,
                "label": meta.label,
                "catalog_number": meta.catalog_number,
                "barcode": meta.barcode,
                "isrc": meta.isrc,
                "composer": meta.composer,
                "recording_mbid": meta.recording_mbid,
                "release_mbid": meta.release_mbid,
                "release_group_mbid": meta.release_group_mbid,
                "score": meta.score,
                "cover": bool(cover),
            }
    except Exception as exc:
        log(f"MusicBrainz tag error for {path.name}: {exc}")
        return {"status": "error", "message": str(exc)}
