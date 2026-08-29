"""Tests for the filename helpers and the retag entry points."""

from __future__ import annotations

import pytest
from mutagen.id3 import ID3, TRCK

from app import downloader
from app.tagger import TrackMeta, write_id3


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("2024-05-01 - Bon Iver - Skinny Love.mp3", "Bon Iver - Skinny Love"),
        ("2024-05-01 - Sleater-Kinney - Jumpers.mp3", "Sleater-Kinney - Jumpers"),
        ("no-date-prefix.mp3", "no-date-prefix"),
    ],
)
def test_title_from_filename(filename, expected):
    assert downloader.title_from_filename(filename) == expected


def test_retag_file_rejects_path_traversal(tmp_path):
    result = downloader.retag_file(tmp_path, "../secrets.mp3")
    assert result["status"] == "error"
    assert "Invalid" in result["message"]


def test_retag_file_reports_missing_file(tmp_path):
    result = downloader.retag_file(tmp_path, "nope.mp3")
    assert result["status"] == "error"
    assert result["message"] == "File not found"


def test_retag_file_runs_the_tagger(tmp_path, monkeypatch):
    path = tmp_path / "2024-05-01 - Bon Iver - Skinny Love.mp3"
    path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 2048)
    tags = ID3()
    tags.add(TRCK(encoding=3, text="8"))
    tags.save(path)

    seen = {}

    def fake_tag(p, rss_title, *, log):
        seen["title"] = rss_title
        write_id3(p, TrackMeta(title="Skinny Love", artist="Bon Iver", track_number="3"))
        return {"status": "tagged"}

    monkeypatch.setattr(downloader, "tag_downloaded_file", fake_tag)
    result = downloader.retag_file(tmp_path, path.name)

    assert result["status"] == "ok"
    assert seen["title"] == "Bon Iver - Skinny Love"
    assert str(ID3(path)["TRCK"]) == "3"


def test_retag_all_walks_every_mp3(tmp_path, monkeypatch):
    for name in ("2024-05-01 - A - B.mp3", "2024-05-02 - C - D.mp3"):
        (tmp_path / name).write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 512)
    (tmp_path / "notes.txt").write_text("ignore me")

    monkeypatch.setattr(
        downloader, "tag_downloaded_file", lambda p, t, *, log: {"status": "tagged"}
    )
    result = downloader.retag_all(tmp_path)
    assert result == {"status": "ok", "total": 2, "tagged": 2, "failed": 0}
