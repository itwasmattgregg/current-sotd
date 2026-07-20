"""Song of the Day NAS downloader — FastAPI UI + APScheduler."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.downloader import (
    download_by_guid,
    download_latest,
    feed_with_status,
    get_download_dir,
    list_local_downloads,
)
from app.logging_util import get_log_lines, setup_logging

APP_DIR = Path(__file__).resolve().parent
TZ_NAME = os.environ.get("TZ", "America/Chicago")
CRON = os.environ.get("CRON", "0 7 * * *")
LOG_DIR = Path(os.environ.get("LOG_DIR", "/data"))

logger = setup_logging(LOG_DIR)
scheduler = BackgroundScheduler()
last_run: dict[str, Any] = {"at": None, "result": None, "trigger": None}


def _log(msg: str) -> None:
    logger.info(msg)


def _parse_cron(expr: str) -> CronTrigger:
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"CRON must have 5 fields, got: {expr!r}")
    minute, hour, day, month, day_of_week = parts
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone=ZoneInfo(TZ_NAME),
    )


def scheduled_download() -> None:
    _log("Scheduled job starting (download latest)")
    result = download_latest(get_download_dir(), log=_log)
    last_run["at"] = datetime.now(ZoneInfo(TZ_NAME)).isoformat()
    last_run["result"] = result
    last_run["trigger"] = "schedule"


def next_run_iso() -> str | None:
    job = scheduler.get_job("daily_download")
    if job is None or job.next_run_time is None:
        return None
    return job.next_run_time.isoformat()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_download_dir().mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        trigger = _parse_cron(CRON)
    except ValueError as exc:
        _log(f"Invalid CRON {CRON!r}, falling back to 0 7 * * *: {exc}")
        trigger = _parse_cron("0 7 * * *")

    scheduler.add_job(
        scheduled_download,
        trigger=trigger,
        id="daily_download",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _log(f"Scheduler started TZ={TZ_NAME} CRON={CRON} next={next_run_iso()}")
    yield
    scheduler.shutdown(wait=False)
    _log("Scheduler stopped")


app = FastAPI(title="Song of the Day", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"tz": TZ_NAME, "cron": CRON},
    )


@app.get("/api/status")
def api_status():
    return {
        "timezone": TZ_NAME,
        "cron": CRON,
        "next_run": next_run_iso(),
        "last_run": last_run,
        "download_dir": str(get_download_dir()),
    }


@app.get("/api/downloads")
def api_downloads():
    return {"items": list_local_downloads(get_download_dir())}


@app.get("/api/feed")
def api_feed():
    try:
        items = feed_with_status(get_download_dir())
    except Exception as exc:
        _log(f"ERROR fetching feed: {exc}")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"items": items}


@app.post("/api/download/latest")
def api_download_latest():
    _log("Manual trigger: download latest")
    result = download_latest(get_download_dir(), log=_log)
    last_run["at"] = datetime.now(ZoneInfo(TZ_NAME)).isoformat()
    last_run["result"] = result
    last_run["trigger"] = "manual"
    return result


@app.post("/api/download/{guid:path}")
def api_download_guid(guid: str):
    _log(f"Manual trigger: download guid={guid}")
    result = download_by_guid(guid, get_download_dir(), log=_log)
    last_run["at"] = datetime.now(ZoneInfo(TZ_NAME)).isoformat()
    last_run["result"] = result
    last_run["trigger"] = "manual"
    if result.get("status") == "error" and "not in current feed" in result.get("message", ""):
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@app.get("/api/logs")
def api_logs():
    return {"lines": get_log_lines()}


@app.get("/files/{filename}")
def serve_file(filename: str):
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = get_download_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    # Ensure resolved path stays inside download dir
    try:
        path.resolve().relative_to(get_download_dir().resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc
    return FileResponse(path, media_type="audio/mpeg", filename=filename)
