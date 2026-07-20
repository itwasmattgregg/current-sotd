"""Tag downloaded MP3s via MusicBrainz lookup + mutagen ID3 writes."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TXXX, UFID, ID3NoHeaderError

LogFn = Callable[[str], None]

MB_BASE = "https://musicbrainz.org/ws/2"
COVER_BASE = "https://coverartarchive.org"
MIN_SCORE = 60


def _noop_log(msg: str) -> None:
    pass


def musicbrainz_enabled() -> bool:
    return os.environ.get("MUSICBRAINZ_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _user_agent() -> str:
    contact = os.environ.get("MUSICBRAINZ_CONTACT", "").strip()
    base = "current-sotd/1.0 (https://github.com/current-sotd)"
    if contact:
        return f"{base}; contact={contact}"
    return base


@dataclass
class TrackMeta:
    title: str
    artist: str
    album: str = ""
    date: str = ""
    recording_mbid: str = ""
    release_mbid: str = ""
    score: int = 0


def parse_artist_title(rss_title: str) -> tuple[str, str]:
    """Split feed titles like 'Artist - Song' into artist and title."""
    raw = re.sub(r"\s+", " ", (rss_title or "").strip())
    if " - " in raw:
        artist, title = raw.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", raw


def _format_artist_credit(credit: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for part in credit:
        name = part.get("name") or (part.get("artist") or {}).get("name") or ""
        parts.append(str(name))
        parts.append(str(part.get("joinphrase") or ""))
    return "".join(parts).strip()


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

    title = f"{release.get('title') or ''} {rg.get('title') or ''}"
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", title):
        score -= 80
    if re.search(r"\blive\b", title, re.IGNORECASE):
        score -= 40

    if release.get("date"):
        score += 10

    return score


def _pick_release(releases: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not releases:
        return None
    return max(releases, key=_release_rank)


def _build_query(artist: str, title: str) -> str:
    parts: list[str] = []
    if title:
        parts.append(f'recording:"{title}"')
    if artist:
        parts.append(f'artist:"{artist}"')
    return " AND ".join(parts)


def _lookup_recording(
    recording_mbid: str,
    *,
    client: httpx.Client,
) -> dict[str, Any]:
    resp = client.get(
        f"{MB_BASE}/recording/{recording_mbid}",
        params={"fmt": "json", "inc": "artists+releases+release-groups"},
    )
    resp.raise_for_status()
    return resp.json()


def search_recording(
    artist: str,
    title: str,
    *,
    client: httpx.Client,
    log: LogFn = _noop_log,
) -> TrackMeta | None:
    query = _build_query(artist, title)
    if not query:
        return None

    url = f"{MB_BASE}/recording"
    params = {"query": query, "fmt": "json", "limit": 8}
    log(f"MusicBrainz search: {query}")
    resp = client.get(url, params=params)
    resp.raise_for_status()
    recordings = resp.json().get("recordings") or []
    if not recordings:
        log("MusicBrainz: no recordings found")
        return None

    candidates = [
        rec
        for rec in recordings
        if int(rec.get("score") or 0) >= MIN_SCORE and rec.get("id")
    ][:5]
    if not candidates:
        log(f"MusicBrainz: no recordings at or above score {MIN_SCORE}")
        return None

    ranked: list[tuple[int, int, dict[str, Any], dict[str, Any] | None]] = []
    for i, rec in enumerate(candidates):
        if i:
            time.sleep(1.1)  # MusicBrainz ~1 req/sec
        try:
            full = _lookup_recording(str(rec["id"]), client=client)
        except httpx.HTTPError as exc:
            log(f"MusicBrainz lookup error for {rec['id']}: {exc}")
            full = rec
        chosen = _pick_release(full.get("releases") or rec.get("releases") or [])
        release_rank = _release_rank(chosen) if chosen else -999
        ranked.append((release_rank, int(rec.get("score") or 0), full, chosen))

    release_rank, score, best, chosen = max(ranked, key=lambda row: (row[0], row[1]))
    mb_artist = _format_artist_credit(best.get("artist-credit") or [])

    album = ""
    release_mbid = ""
    date = ""
    # Only attach album/cover when the release looks like a real commercial release.
    if chosen and release_rank >= 100:
        rg = chosen.get("release-group") or {}
        album = (rg.get("title") or chosen.get("title") or "").strip()
        release_mbid = (chosen.get("id") or "").strip()
        date = (chosen.get("date") or rg.get("first-release-date") or "")[:4]
    elif chosen:
        log(
            f"MusicBrainz: weak release match (rank={release_rank}); "
            "tagging artist/title only"
        )

    return TrackMeta(
        title=(best.get("title") or title).strip(),
        artist=(mb_artist or artist).strip(),
        album=album,
        date=date,
        recording_mbid=(best.get("id") or "").strip(),
        release_mbid=release_mbid,
        score=score,
    )


def fetch_cover_art(
    release_mbid: str,
    *,
    client: httpx.Client,
    log: LogFn = _noop_log,
) -> bytes | None:
    if not release_mbid:
        return None
    url = f"{COVER_BASE}/release/{release_mbid}/front-500"
    try:
        resp = client.get(url)
        if resp.status_code == 404:
            log("Cover Art Archive: no front cover")
            return None
        resp.raise_for_status()
        return resp.content
    except httpx.HTTPError as exc:
        log(f"Cover Art Archive error: {exc}")
        return None


def write_id3(path: Path, meta: TrackMeta, cover: bytes | None = None) -> None:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    tags.delall("TIT2")
    tags.delall("TPE1")
    tags.delall("TALB")
    tags.delall("TDRC")
    tags.delall("APIC")
    tags.delall("UFID:http://musicbrainz.org")
    tags.delall("TXXX:MusicBrainz Recording Id")
    tags.delall("TXXX:MusicBrainz Album Id")

    tags.add(TIT2(encoding=3, text=meta.title))
    if meta.artist:
        tags.add(TPE1(encoding=3, text=meta.artist))
    if meta.album:
        tags.add(TALB(encoding=3, text=meta.album))
    if meta.date:
        tags.add(TDRC(encoding=3, text=meta.date))
    if meta.recording_mbid:
        tags.add(
            UFID(owner="http://musicbrainz.org", data=meta.recording_mbid.encode("ascii"))
        )
        tags.add(
            TXXX(encoding=3, desc="MusicBrainz Recording Id", text=meta.recording_mbid)
        )
    if meta.release_mbid:
        tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=meta.release_mbid))
    if cover:
        mime = "image/png" if cover.startswith(b"\x89PNG") else "image/jpeg"
        tags.add(
            APIC(
                encoding=3,
                mime=mime,
                type=3,
                desc="Cover",
                data=cover,
            )
        )

    tags.save(path, v2_version=3)


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
            # MusicBrainz asks for ~1 req/sec; one search per download is fine.
            meta = search_recording(artist, title, client=client, log=log)
            if meta is None:
                # Fallback: title-only search if artist+title missed.
                if artist and title:
                    time.sleep(1.1)
                    meta = search_recording("", title, client=client, log=log)
            if meta is None:
                return {"status": "no_match", "query_artist": artist, "query_title": title}

            cover = None
            if meta.release_mbid:
                time.sleep(0.2)  # Cover Art Archive is a different host
                cover = fetch_cover_art(meta.release_mbid, client=client, log=log)

            write_id3(path, meta, cover)
            log(
                f"TAGGED {path.name}: {meta.artist} — {meta.title}"
                f" (score={meta.score}, cover={'yes' if cover else 'no'})"
            )
            return {
                "status": "tagged",
                "artist": meta.artist,
                "title": meta.title,
                "album": meta.album,
                "date": meta.date,
                "recording_mbid": meta.recording_mbid,
                "release_mbid": meta.release_mbid,
                "score": meta.score,
                "cover": bool(cover),
            }
    except Exception as exc:
        log(f"MusicBrainz tag error for {path.name}: {exc}")
        return {"status": "error", "message": str(exc)}
