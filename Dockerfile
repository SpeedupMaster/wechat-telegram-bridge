FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BRIDGE_DATA_DIR=/app/data

# Static ffmpeg avoids ~430 MB of Debian Mesa/LLVM/X11 transitive deps;
# gifsicle optimizes stickers; Noto Sans CJK + Color Emoji render link cards (drop unused ~51 MB Noto Serif CJK).
COPY --from=mwader/static-ffmpeg:7.0-1 /ffmpeg /usr/local/bin/
RUN apt-get update \
    && apt-get install -y --no-install-recommends gifsicle tini fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /usr/share/fonts/opentype/noto/NotoSerifCJK* /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge ./bridge

# Runtime state (bridge.db, Telegram sessions) lives in /app/data, mounted as a volume.
# /app/config may hold an optional config.toml with advanced settings.
RUN useradd --system --uid 10002 --home /app bridge \
    && mkdir -p /app/data /app/config && chown bridge:bridge /app/data
USER bridge

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "bridge", "/app/config/config.toml"]
