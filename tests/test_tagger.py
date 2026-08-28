"""Tests for the MusicBrainz matcher and the ID3 writer."""

from __future__ import annotations

import json

import httpx
import pytest
from mutagen.id3 import ID3, TRCK, TALB, TCON, TIT2

from app import tagger
from app.tagger import TrackMeta, _MBSession
from tests import fixtures


# --------------------------------------------------------------------------
# Fake MusicBrainz transport
# --------------------------------------------------------------------------


class FakeMB:
    """Routes ws/2 paths to canned payloads and records what was requested."""

    def __init__(self, *, recordings=None, release=None, work=None, release_group=None,
                 search=None):
        self.recordings = recordings if recordings is not None else {
            "rec-studio": fixtures.RECORDING_STUDIO,
            "rec-live": fixtures.RECORDING_LIVE,
        }
        self.release = release if release is not None else fixtures.RELEASE_DETAIL
        self.work = work if work is not None else fixtures.WORK_DETAIL
        self.release_group = (
            release_group if release_group is not None else fixtures.RELEASE_GROUP_DETAIL
        )
        self.search = search if search is not None else fixtures.SEARCH_RESPONSE
        self.requests: list[httpx.URL] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url)
        path = request.url.path

        if path.endswith("/ws/2/recording"):
            return httpx.Response(200, json=self.search)
        if "/ws/2/recording/" in path:
            mbid = path.rsplit("/", 1)[-1]
            payload = self.recordings.get(mbid)
            if payload is None:
                return httpx.Response(404, json={"error": "Not Found"})
            return httpx.Response(200, json=payload)
        if "/ws/2/release-group/" in path:
            return httpx.Response(200, json=self.release_group)
        if "/ws/2/release/" in path:
            return httpx.Response(200, json=self.release)
        if "/ws/2/work/" in path:
            return httpx.Response(200, json=self.work)
        if "coverartarchive" in request.url.host:
            return httpx.Response(404)
        return httpx.Response(404, json={"error": f"unrouted {path}"})

    def paths(self) -> list[str]:
        return [u.path for u in self.requests]


def run_search(fake: FakeMB, artist="Bon Iver", title="Skinny Love", **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    session = _MBSession(client, sleep=lambda _s: None)
    try:
        return tagger.search_recording(artist, title, session=session, **kwargs)
    finally:
        client.close()


# --------------------------------------------------------------------------
# Feed title parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Bon Iver - Skinny Love", ("Bon Iver", "Skinny Love")),
        ("Bon Iver – Skinny Love", ("Bon Iver", "Skinny Love")),
        ("Bon Iver — Skinny Love", ("Bon Iver", "Skinny Love")),
        ("Song of the Day: Bon Iver - Skinny Love", ("Bon Iver", "Skinny Love")),
        ("  Bon Iver   -   Skinny Love  ", ("Bon Iver", "Skinny Love")),
        ('Bon Iver - "Skinny Love"', ("Bon Iver", "Skinny Love")),
        # Song title containing a dash keeps the remainder intact.
        ("Sleater-Kinney - Jumpers - Live", ("Sleater-Kinney", "Jumpers - Live")),
        ("No separator here", ("", "No separator here")),
        ("", ("", "")),
    ],
)
def test_parse_artist_title(raw, expected):
    assert tagger.parse_artist_title(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Run the Jewels feat. Zack de la Rocha", "Run the Jewels"),
        ("Sylvan Esso ft. Flock of Dimes", "Sylvan Esso"),
        # Ampersands/slashes are part of the name, not a guest credit.
        ("Simon & Garfunkel", "Simon & Garfunkel"),
        ("Florence and the Machine", "Florence and the Machine"),
        ("AC/DC", "AC/DC"),
    ],
)
def test_primary_artist(raw, expected):
    assert tagger._primary_artist(raw) == expected


# --------------------------------------------------------------------------
# Track position extraction -- the "always track 8" bug
# --------------------------------------------------------------------------


def test_extract_track_info_uses_printed_number():
    info = tagger._extract_track_info(fixtures.RECORDING_STUDIO["releases"][1])
    assert info["number"] == "3"
    assert info["track_total"] == "9"
    assert info["disc_number"] == "1"
    assert info["track_mbid"] == "trk-3"


def test_extract_track_info_falls_back_to_track_offset():
    release = {
        "media": [
            {
                "position": 2,
                "track-count": 11,
                "track-offset": 6,
                "track": [{"id": "t", "title": "x"}],
            }
        ]
    }
    info = tagger._extract_track_info(release)
    assert info["number"] == "7"  # track-offset is zero-based
    assert info["disc_number"] == "2"


def test_extract_track_info_empty_when_no_media():
    assert tagger._extract_track_info({"media": []}) == {}


# --------------------------------------------------------------------------
# Release ranking
# --------------------------------------------------------------------------


def test_studio_album_outranks_live_bootleg():
    releases = fixtures.RECORDING_STUDIO["releases"]
    chosen = tagger._pick_release(releases)
    assert chosen["id"] == "rel-album"


def test_original_pressing_wins_over_reissue():
    rg = {
        "id": "rg",
        "primary-type": "Album",
        "secondary-types": [],
        "first-release-date": "2001-05-01",
    }
    original = {"id": "orig", "status": "Official", "date": "2001-05-01", "release-group": rg}
    reissue = {"id": "reissue", "status": "Official", "date": "2015-09-01", "release-group": rg}
    assert tagger._pick_release([reissue, original])["id"] == "orig"


def test_release_is_usable_rejects_bootleg():
    assert not tagger._release_is_usable(
        tagger._release_rank(fixtures.RECORDING_STUDIO["releases"][0])
    )


# --------------------------------------------------------------------------
# End-to-end matcher
# --------------------------------------------------------------------------


def test_search_recording_picks_studio_track_not_bootleg_track_eight():
    fake = FakeMB()
    meta = run_search(fake, deep=True)

    assert meta is not None
    assert meta.track_number == "3"
    assert meta.track_number != "8"
    assert meta.track_total == "9"
    assert meta.disc_number == "1"
    assert meta.disc_total == "1"
    assert meta.album == "For Emma, Forever Ago"
    assert meta.release_mbid == "rel-album"


def test_search_recording_fills_out_full_metadata():
    fake = FakeMB()
    meta = run_search(fake, deep=True)

    assert meta.title == "Skinny Love"
    assert meta.artist == "Bon Iver"
    assert meta.album_artist == "Bon Iver"
    assert meta.artist_sort == "Bon Iver"
    assert meta.date == "2008-02-19"
    assert meta.original_date == "2007-07-08"
    assert meta.year == "2008"
    assert meta.original_year == "2007"
    assert meta.genre == "Indie Folk"  # highest vote count wins
    assert meta.label == "Jagjaguwar"
    assert meta.catalog_number == "JAG115"
    assert meta.barcode == "656605213729"
    assert meta.isrc == "USJAY0700018"
    assert meta.media_format == "CD"
    assert meta.release_country == "US"
    assert meta.release_status == "Official"
    assert meta.release_type == "Album"
    assert meta.composer == "Justin Vernon"
    assert meta.lyricist == "Justin Vernon"
    assert meta.length_ms == 238866
    assert meta.recording_mbid == "rec-studio"
    assert meta.release_group_mbid == "rg-1"
    assert meta.release_track_mbid == "trk-3"
    assert meta.work_mbid == "work-1"
    assert meta.artist_mbids == ["art-1"]
    assert meta.album_artist_mbids == ["art-1"]


def test_recording_lookup_requests_rich_includes():
    fake = FakeMB()
    run_search(fake, deep=True)
    lookup = next(u for u in fake.requests if "/ws/2/recording/" in u.path)
    inc = lookup.params["inc"]
    for part in ("releases", "media", "isrcs", "genres", "work-rels", "artist-credits"):
        assert part in inc


def test_deep_lookups_skipped_when_disabled():
    fake = FakeMB()
    meta = run_search(fake, deep=False)
    paths = fake.paths()
    assert not any("/ws/2/work/" in p for p in paths)
    assert not any(p.startswith("/ws/2/release/") for p in paths)
    # Shallow data still comes through from the recording lookup.
    assert meta.track_number == "3"
    assert meta.album == "For Emma, Forever Ago"
    assert meta.label == ""


def test_release_group_consulted_when_recording_has_no_genre():
    recording = json.loads(json.dumps(fixtures.RECORDING_STUDIO))
    recording.pop("genres")
    fake = FakeMB(recordings={"rec-studio": recording, "rec-live": fixtures.RECORDING_LIVE})
    meta = run_search(fake, deep=True)
    assert meta.genre == "Indie Folk"
    assert any("/ws/2/release-group/" in p for p in fake.paths())


def test_uncurated_tags_are_not_used_as_genre():
    recording = json.loads(json.dumps(fixtures.RECORDING_STUDIO))
    recording.pop("genres")
    recording["tags"] = [{"name": "seen live", "count": 40}]
    rg = json.loads(json.dumps(fixtures.RELEASE_GROUP_DETAIL))
    rg.pop("genres")
    fake = FakeMB(
        recordings={"rec-studio": recording, "rec-live": fixtures.RECORDING_LIVE},
        release_group=rg,
    )
    meta = run_search(fake, deep=True)
    assert meta.genre == ""


def test_no_match_when_title_is_unrelated():
    fake = FakeMB()
    assert run_search(fake, artist="Bon Iver", title="Totally Different Song") is None


def test_falls_back_to_search_hit_when_lookup_fails():
    fake = FakeMB(recordings={})  # every lookup 404s
    meta = run_search(fake, deep=True)
    assert meta is not None
    assert meta.title == "Skinny Love"
    assert meta.artist == "Bon Iver"


def test_low_score_hits_are_ignored():
    search = {"recordings": [{"id": "x", "score": 10, "title": "Skinny Love"}]}
    fake = FakeMB(search=search)
    assert run_search(fake) is None


def test_query_escapes_lucene_specials():
    queries = tagger._build_queries("AC/DC", "Who Made Who (Live)")
    assert queries[0] == 'recording:"Who Made Who \\(Live\\)" AND artist:"AC\\/DC"'
    # A decoration-free variant is tried as a fallback.
    assert any(q.startswith('recording:"Who Made Who"') for q in queries)
    # Title-only is the last resort.
    assert queries[-1] == 'recording:"Who Made Who"' 


def test_rate_limiter_spaces_requests():
    waits: list[float] = []
    fake = FakeMB()
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    session = _MBSession(client, min_interval=1.1, sleep=waits.append)
    try:
        tagger.search_recording("Bon Iver", "Skinny Love", session=session, deep=True)
    finally:
        client.close()
    assert len(waits) >= 3
    assert all(0 < w <= 1.1 for w in waits)


# --------------------------------------------------------------------------
# ID3 writing
# --------------------------------------------------------------------------


def _make_mp3(tmp_path, frames=()):
    path = tmp_path / "2024-05-01 - Bon Iver - Skinny Love.mp3"
    path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 2048)
    if frames:
        tags = ID3()
        for frame in frames:
            tags.add(frame)
        tags.save(path)
    return path


def test_write_id3_clears_inherited_podcast_track_number(tmp_path):
    path = _make_mp3(
        tmp_path,
        frames=[
            TRCK(encoding=3, text="8"),
            TALB(encoding=3, text="Song of the Day"),
            TCON(encoding=3, text="Podcast"),
            TIT2(encoding=3, text="Song of the Day"),
        ],
    )
    assert str(ID3(path)["TRCK"]) == "8"

    meta = TrackMeta(
        title="Skinny Love",
        artist="Bon Iver",
        album="For Emma, Forever Ago",
        track_number="3",
        track_total="9",
    )
    tagger.write_id3(path, meta)

    tags = ID3(path)
    assert str(tags["TRCK"]) == "3/9"
    assert str(tags["TALB"]) == "For Emma, Forever Ago"
    assert str(tags["TIT2"]) == "Skinny Love"
    assert "TCON" not in tags  # stale podcast genre is gone


def test_write_id3_writes_full_frame_set(tmp_path):
    path = _make_mp3(tmp_path)
    fake = FakeMB()
    meta = run_search(fake, deep=True)
    tagger.write_id3(path, meta, cover=b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    tags = ID3(path)
    assert str(tags["TIT2"]) == "Skinny Love"
    assert str(tags["TPE1"]) == "Bon Iver"
    assert str(tags["TPE2"]) == "Bon Iver"
    assert str(tags["TALB"]) == "For Emma, Forever Ago"
    assert str(tags["TRCK"]) == "3/9"
    assert str(tags["TPOS"]) == "1/1"
    assert str(tags["TCON"]) == "Indie Folk"
    assert str(tags["TPUB"]) == "Jagjaguwar"
    assert str(tags["TSRC"]) == "USJAY0700018"
    assert str(tags["TCOM"]) == "Justin Vernon"
    assert str(tags["TEXT"]) == "Justin Vernon"
    assert str(tags["TMED"]) == "CD"
    assert str(tags["TLEN"]) == "238866"
    assert str(tags["TDRC"]).startswith("2008")
    assert tags.getall("APIC")[0].mime == "image/png"

    txxx = {frame.desc: str(frame) for frame in tags.getall("TXXX")}
    assert txxx["MusicBrainz Album Id"] == "rel-album"
    assert txxx["MusicBrainz Release Group Id"] == "rg-1"
    assert txxx["MusicBrainz Release Track Id"] == "trk-3"
    assert txxx["MusicBrainz Artist Id"] == "art-1"
    assert txxx["MusicBrainz Album Artist Id"] == "art-1"
    assert txxx["MusicBrainz Album Type"] == "Album"
    assert txxx["MusicBrainz Album Status"] == "Official"
    assert txxx["MusicBrainz Album Release Country"] == "US"
    assert txxx["CATALOGNUMBER"] == "JAG115"
    assert txxx["BARCODE"] == "656605213729"
    assert txxx["originalyear"] == "2007"

    ufid = tags.getall("UFID")[0]
    assert ufid.data == b"rec-studio"


def test_write_id3_removes_id3v1_trailer(tmp_path):
    path = tmp_path / "song.mp3"
    path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 2048)
    tags = ID3()
    tags.add(TRCK(encoding=3, text="8"))
    tags.add(TIT2(encoding=3, text="Old"))
    tags.save(path, v1=2)
    assert path.read_bytes()[-128:].startswith(b"TAG")

    tagger.write_id3(path, TrackMeta(title="New", artist="Someone", track_number="4"))
    assert not path.read_bytes()[-128:].startswith(b"TAG")
    assert str(ID3(path)["TRCK"]) == "4"


def test_write_id3_omits_missing_fields(tmp_path):
    path = _make_mp3(tmp_path)
    tagger.write_id3(path, TrackMeta(title="Untitled", artist="Nobody"))
    tags = ID3(path)
    assert "TRCK" not in tags
    assert "TALB" not in tags
    assert "TPOS" not in tags
