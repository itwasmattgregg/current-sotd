# Song of the Day NAS Downloader

Docker app that downloads [89.3 The Current](https://www.thecurrent.org/) Song of the Day every day and serves a small web UI on your NAS.

## What it does

- Pulls MP3s from the official podcast RSS feed: `https://feeds.publicradio.org/public_feeds/song-of-the-day`
- Runs a scheduled download daily at **7:00 AM America/Chicago** (configurable)
- Saves files as `YYYY-MM-DD - Artist - Title.mp3` into a mounted folder
- UI at port **8080**: local library, this week’s available feed episodes, manual download, and logs

The RSS feed only keeps about a week of episodes. The **Downloaded on NAS** list grows over time as the daily job runs.

## Quick start (NAS)

1. Edit `docker-compose.yml` and point the `/downloads` volume at your shared music folder, for example:

   ```yaml
   volumes:
     - /volume1/music/song-of-the-day:/downloads
   ```

2. Build and start:

   ```bash
   docker compose up -d --build
   ```

3. Open `http://<nas-ip>:8080`

4. Click **Download today’s song** once to verify, then check the Logs panel.

## Configuration

| Environment variable | Default | Meaning |
|---|---|---|
| `TZ` | `America/Chicago` | Timezone for the schedule |
| `CRON` | `0 7 * * *` | When to download (5-field cron) |
| `DOWNLOAD_DIR` | `/downloads` | Inside-container MP3 path |
| `LOG_DIR` | `/data` | Log file directory |

## Local development

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
set DOWNLOAD_DIR=.\downloads
set LOG_DIR=.\data
uvicorn app.main:app --reload --port 8080
```

## API

- `GET /` — UI
- `GET /api/status` — schedule + last run
- `GET /api/downloads` — local MP3 list
- `GET /api/feed` — current RSS items + downloaded flag
- `POST /api/download/latest` — download newest episode
- `POST /api/download/{guid}` — download one episode still in the feed
- `GET /api/logs` — recent log lines
- `GET /files/{filename}` — stream a saved MP3
