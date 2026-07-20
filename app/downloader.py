"""Fetch Song of the Day from The Current RSS feed and save MP3s."""

from __future__ import annotations

import email.utils
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

import httpx

from app.tagger import tag_downloaded_file

FEED_URL = "https://feeds.publicradio.org/public_feeds/song-of-the-day"
USER_AGENT = "current-sotd/1.0 (+https://www.thecurrent.org/song-of-the-day)"

LogFn = Callable[[str], None]


@dataclass
class Episode:
    guid: str
    title: str
    pub_date: datetime
    enclosure_url: str
    link: str = ""

    @property
    def date_str(self) -> str:
        return self.pub_date.strftime("%Y-%m-%d")

    @property
    def filename(self) -> str:
        return make_filename(self.date_str, self.title)


def _noop_log(msg: str) -> None:
    pass


def make_filename(date_str: str, title: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip()
    safe = re.sub(r"\s+", " ", safe)
    safe = safe.rstrip(". ")
    if not safe:
        safe = "unknown"
    return f"{date_str} - {safe}.mp3"


def parse_pub_date(raw: str) -> datetime:
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def fetch_feed(client: httpx.Client | None = None) -> list[Episode]:
    own_client = client is None
    if own_client:
        client = httpx.Client(follow_redirects=True, timeout=60.0, headers={"User-Agent": USER_AGENT})
    assert client is not None
    try:
        resp = client.get(FEED_URL)
        resp.raise_for_status()
        return parse_feed_xml(resp.text)
    finally:
        if own_client:
            client.close()


def parse_feed_xml(xml_text: str) -> list[Episode]:
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return []

    episodes: list[Episode] = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        guid = (item.findtext("guid") or title).strip()
        link = (item.findtext("link") or "").strip()
        pub_raw = item.findtext("pubDate") or ""
        enclosure = item.find("enclosure")
        if enclosure is None:
            continue
        url = enclosure.get("url") or ""
        if not url:
            continue
        episodes.append(
            Episode(
                guid=guid,
                title=title,
                pub_date=parse_pub_date(pub_raw),
                enclosure_url=url,
                link=link,
            )
        )
    return episodes


def find_existing_for_date(download_dir: Path, date_str: str) -> Path | None:
    prefix = f"{date_str} - "
    if not download_dir.exists():
        return None
    for path in download_dir.iterdir():
        if path.is_file() and path.name.startswith(prefix) and path.suffix.lower() == ".mp3":
            return path
    return None


def list_local_downloads(download_dir: Path) -> list[dict]:
    download_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for path in sorted(download_dir.glob("*.mp3"), reverse=True):
        if not path.is_file():
            continue
        stat = path.stat()
        rows.append(
            {
                "filename": path.name,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return rows


def episode_is_downloaded(download_dir: Path, episode: Episode) -> bool:
    target = download_dir / episode.filename
    if target.exists() and target.stat().st_size > 0:
        return True
    return find_existing_for_date(download_dir, episode.date_str) is not None


def download_episode(
    episode: Episode,
    download_dir: Path,
    *,
    log: LogFn = _noop_log,
    client: httpx.Client | None = None,
) -> dict:
    download_dir.mkdir(parents=True, exist_ok=True)
    dest = download_dir / episode.filename

    existing = find_existing_for_date(download_dir, episode.date_str)
    if existing is not None:
        log(f"SKIP already have {existing.name}")
        return {
            "status": "skipped",
            "filename": existing.name,
            "guid": episode.guid,
            "title": episode.title,
            "date": episode.date_str,
            "message": f"Already downloaded: {existing.name}",
        }

    log(f"DOWNLOAD {episode.date_str} — {episode.title}")
    own_client = client is None
    if own_client:
        client = httpx.Client(follow_redirects=True, timeout=120.0, headers={"User-Agent": USER_AGENT})
    assert client is not None

    tmp = dest.with_suffix(".mp3.partial")
    try:
        with client.stream("GET", episode.enclosure_url) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)
        tmp.replace(dest)
        size = dest.stat().st_size
        log(f"OK saved {dest.name} ({size} bytes)")
        tag_result = tag_downloaded_file(dest, episode.title, log=log)
        return {
            "status": "downloaded",
            "filename": dest.name,
            "guid": episode.guid,
            "title": episode.title,
            "date": episode.date_str,
            "size": size,
            "message": f"Downloaded {dest.name}",
            "tags": tag_result,
        }
    except Exception as exc:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        log(f"ERROR {episode.title}: {exc}")
        return {
            "status": "error",
            "guid": episode.guid,
            "title": episode.title,
            "date": episode.date_str,
            "message": str(exc),
        }
    finally:
        if own_client:
            client.close()


def download_latest(download_dir: Path, *, log: LogFn = _noop_log) -> dict:
    episodes = fetch_feed()
    if not episodes:
        log("ERROR feed returned no episodes")
        return {"status": "error", "message": "Feed returned no episodes"}
    return download_episode(episodes[0], download_dir, log=log)


def download_by_guid(guid: str, download_dir: Path, *, log: LogFn = _noop_log) -> dict:
    decoded = unquote(guid)
    episodes = fetch_feed()
    match = next((e for e in episodes if e.guid == decoded or e.guid == guid), None)
    if match is None:
        log(f"ERROR guid not in current feed: {decoded}")
        return {
            "status": "error",
            "message": "Episode not in current feed (may have aged out after ~1 week)",
            "guid": decoded,
        }
    return download_episode(match, download_dir, log=log)


def feed_with_status(download_dir: Path) -> list[dict]:
    download_dir.mkdir(parents=True, exist_ok=True)
    episodes = fetch_feed()
    rows: list[dict] = []
    for ep in episodes:
        existing = find_existing_for_date(download_dir, ep.date_str)
        rows.append(
            {
                "guid": ep.guid,
                "title": ep.title,
                "date": ep.date_str,
                "link": ep.link,
                "enclosure_url": ep.enclosure_url,
                "downloaded": existing is not None,
                "filename": existing.name if existing else ep.filename,
            }
        )
    return rows


def get_download_dir() -> Path:
    return Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
