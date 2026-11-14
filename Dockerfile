FROM python:3.13-slim

# ffmpeg and gifsicle convert/optimize stickers and voice notes; noto fonts render link cards
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gifsicle tini fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge ./bridge

# Runtime state (bridge.db, Telegram sessions) lives in /app/data, mounted as a volume.
# /app/config may hold an optional config.toml with advanced settings.
RUN useradd --system --uid 10002 --home /app bridge \
    && mkdir -p /app/data /app/config && chown bridge:bridge /app/data
USER bridge
ENV PYTHONUNBUFFERED=1 BRIDGE_DATA_DIR=/app/data

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "bridge", "/app/config/config.toml"]
