# Song of the Day Docker Job for 89.3 The Current

Docker app that downloads [89.3 The Current](https://www.thecurrent.org/) Song of the Day every day and serves a small web UI on your NAS.

## What it does

- Pulls MP3s from the official podcast RSS feed: `https://feeds.publicradio.org/public_feeds/song-of-the-day`
- Runs a scheduled download daily at **7:00 AM America/Chicago** (configurable)
- Saves files as `YYYY-MM-DD - Artist - Title.mp3` into a mounted folder
- After each new download, looks up the track on [MusicBrainz](https://musicbrainz.org/) and writes a full ID3 tag: title, artist, album, album artist, **track and disc numbers**, release date, original release date, genre, label, catalog number, barcode, ISRC, composer/lyricist, MBIDs and cover art
- Existing files can be re-tagged from the UI (**Re-tag** per file, or **Re-tag all**)
- UI at port **8080**: local library, this week’s available feed episodes, manual download, and logs

The RSS feed only keeps about a week of episodes. The **Downloaded on NAS** list grows over time as the daily job runs.

## Quick start (build from source)

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

4. Click **Download today’s song** once to verify, then check the Logs panel for `TAGGED` or `MusicBrainz` lines.

## Alternate setup (Docker Hub image only)

Use this on a NAS when you only want a compose file — no repo clone or local build. Pulls the published image [`mattdgregg/current-sotd`](https://hub.docker.com/r/mattdgregg/current-sotd).

Create a `docker-compose.yml`:

```yaml
services:
  sotd:
    image: mattdgregg/current-sotd:latest
    ports:
      - "8080:8080"
    environment:
      TZ: America/Chicago
      DOWNLOAD_DIR: /downloads
      LOG_DIR: /data
      CRON: "0 7 * * *"
      MUSICBRAINZ_ENABLED: "true"
      MUSICBRAINZ_DEEP_METADATA: "true"
      ID3_VERSION: "3"
      # Optional: email/URL for MusicBrainz API User-Agent
      # MUSICBRAINZ_CONTACT: you@example.com
    volumes:
      # Change this host path to your NAS shared music folder
      - /volume1/music/song-of-the-day:/downloads
      - sotd-data:/data
    restart: unless-stopped

volumes:
  sotd-data:
```

Then start:

```bash
docker compose up -d
```

Update later with:

```bash
docker compose pull
docker compose up -d
```

Open `http://<nas-ip>:8080` and use **Download today’s song** once to verify.

## Configuration

| Environment variable | Default | Meaning |
|---|---|---|
| `TZ` | `America/Chicago` | Timezone for the schedule |
| `CRON` | `0 7 * * *` | When to download (5-field cron) |
| `DOWNLOAD_DIR` | `/downloads` | Inside-container MP3 path |
| `LOG_DIR` | `/data` | Log file directory |
| `MUSICBRAINZ_ENABLED` | `true` | Tag new downloads via MusicBrainz + mutagen |
| `MUSICBRAINZ_CONTACT` | _(empty)_ | Optional email/URL included in the MusicBrainz User-Agent |
| `MUSICBRAINZ_DEEP_METADATA` | `true` | Extra lookups for label, catalog number, barcode, genre and composer. Set `false` for faster, thinner tagging |
| `ID3_VERSION` | `3` | ID3v2 minor version to write. `3` is the most compatible with NAS media servers; `4` if your player prefers it |

## Local development

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
set DOWNLOAD_DIR=.\downloads
set LOG_DIR=.\data
uvicorn app.main:app --reload --port 8080
```

Run the tests with:

```bash
python -m pytest -q
```

## API

- `GET /` — UI
- `GET /api/status` — schedule + last run
- `GET /api/downloads` — local MP3 list
- `GET /api/feed` — current RSS items + downloaded flag
- `POST /api/download/latest` — download newest episode
- `POST /api/download/{guid}` — download one episode still in the feed
- `POST /api/retag/{filename}` — re-run the MusicBrainz lookup for one local file
- `POST /api/retag-all` — re-tag the whole library in the background
- `GET /api/logs` — recent log lines
- `GET /files/{filename}` — stream a saved MP3

## Tagging notes

Podcast MP3s arrive with the publisher's own ID3 tag already on them, including
a track number that has nothing to do with the song. The tagger therefore
*replaces* the tag rather than merging into it, and drops any ID3v1 trailer, so
nothing stale survives.

Track and disc numbers come from the release MusicBrainz says the recording sits
on. Releases are ranked before that number is read — official studio releases
beat live bootlegs and date-titled concert recordings, and where two pressings
tie, the original issue wins over later reissues. Candidate recordings are also
checked for title similarity against the feed title, so a loose search hit is
dropped instead of being written to the file.

Lookups are spaced out to respect the MusicBrainz ~1 request/second limit, so a
single track takes roughly ten seconds to tag. Setting `MUSICBRAINZ_CONTACT` is
appreciated by MusicBrainz and gives you a less-throttled User-Agent.
