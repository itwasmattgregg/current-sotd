FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Chicago \
    DOWNLOAD_DIR=/downloads \
    LOG_DIR=/data \
    CRON="0 7 * * *" \
    MUSICBRAINZ_ENABLED=true \
    MUSICBRAINZ_DEEP_METADATA=true \
    ID3_VERSION=3

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /downloads /data

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

