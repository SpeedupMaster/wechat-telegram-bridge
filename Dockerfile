FROM python:3.13-slim AS builder

# Stage runtime tools (only /usr/bin/gifsicle & dynamic /usr/bin/tini — drops ~2.6 MiB X11 from
# gifview and ~0.7 MiB tini-static) and fonts (Noto Color Emoji + SubsetOTF Simplified Chinese
# Noto Sans SC instead of the ~38 MiB multi-region NotoSansCJK TTC bundle).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        binutils ca-certificates curl fonts-noto-color-emoji gifsicle tini \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /stage/usr/bin \
        /stage/usr/share/fonts/opentype/noto \
        /stage/usr/share/fonts/truetype/noto \
        /stage/app/data /stage/app/config \
    && cp /usr/bin/gifsicle /usr/bin/tini /stage/usr/bin/ \
    && cp /usr/share/fonts/truetype/noto/NotoColorEmoji.ttf /stage/usr/share/fonts/truetype/noto/ \
    && curl -fsSL --retry 3 --retry-delay 2 \
        -o /stage/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf \
        https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf \
    && curl -fsSL --retry 3 --retry-delay 2 \
        -o /stage/usr/share/fonts/opentype/noto/NotoSansSC-Bold.otf \
        https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004/Sans/SubsetOTF/SC/NotoSansSC-Bold.otf \
    && printf '%s\n' \
        'faa6c9df652116dde789d351359f3d7e5d2285a2b2a1f04a2d7244df706d5ea9  /stage/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf' \
        'c6cb5a93abaa9edc8ee7463b7ebb7f42d618d40e6ed2f7a5371c97b0b64767c0  /stage/usr/share/fonts/opentype/noto/NotoSansSC-Bold.otf' \
        | sha256sum -c -

# Install Python deps without .pyc bytecode, strip unstripped DWARF debug info from wheel .so
# binaries (notably curl_cffi/_wrapper.abi3.so, aiohttp, Pillow), and deduplicate rlottie copies.
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --no-compile --disable-pip-version-check \
        --target /opt/dist-packages -r requirements.txt \
    && find /opt/dist-packages -type f \( -name '*.so' -o -name '*.so.*' \) -exec strip --strip-unneeded {} + \
    && rm -rf /opt/dist-packages/bin \
    && find /opt/dist-packages -type d -name '__pycache__' -exec rm -rf {} + \
    && rl=/opt/dist-packages/rlottie_python \
    && real=$(find "$rl" -maxdepth 1 -type f -name 'librlottie.so.*.*' | head -n 1) \
    && if [ -n "$real" ]; then \
        base=$(basename "$real"); \
        for dup in "$rl"/librlottie.so*; do \
            if [ "$dup" != "$real" ] && [ -f "$dup" ] && cmp -s "$dup" "$real"; then \
                ln -sf "$base" "$dup"; \
            fi; \
        done; \
    fi

FROM gcr.io/distroless/python3-debian13:nonroot

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    MALLOC_TRIM_THRESHOLD_=65536 \
    BRIDGE_DATA_DIR=/app/data \
    HOME=/app

# Static ffmpeg avoids ~430 MB of Debian Mesa/LLVM/X11 transitive deps.
COPY --from=mwader/static-ffmpeg:7.0-1 /ffmpeg /usr/local/bin/ffmpeg
COPY --from=builder /stage/usr/bin/ /usr/bin/
COPY --from=builder /stage/usr/share/fonts/ /usr/share/fonts/
COPY --from=builder /opt/dist-packages /usr/local/lib/python3.13/dist-packages

WORKDIR /app
COPY bridge ./bridge

# Runtime state (bridge.db, Telegram sessions) lives in /app/data, mounted as a volume (UID 10002).
# /app/config may hold an optional config.toml with advanced settings.
COPY --from=builder --chown=10002:10002 /stage/app/data /app/data
COPY --from=builder /stage/app/config /app/config
USER 10002:10002

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/bin/python", "-m", "bridge", "/app/config/config.toml"]
