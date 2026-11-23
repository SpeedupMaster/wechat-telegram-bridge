FROM --platform=$BUILDPLATFORM python:3.13-slim AS ffmpeg-builder

ARG BUILDARCH
ARG TARGETARCH
ARG FFMPEG_VERSION=7.1.5
ARG FFMPEG_SHA256=de668509caf9e35e3cd162473441fdb29538c6d96ed080292b3cf9e6fc5d558f

# Build a minimal ffmpeg containing only the codecs/muxers/filters used by voice notes,
# WXGF/WebM/GIF stickers, and video thumbnails (cross-compiled natively on $BUILDPLATFORM
# so multi-arch arm64 builds avoid slow QEMU compilation).
RUN set -eux; \
    build_arch="${BUILDARCH:-$(dpkg --print-architecture)}"; \
    target_arch="${TARGETARCH:-$build_arch}"; \
    case "$target_arch" in \
        amd64) ff_arch=x86_64;  gnu_host=x86_64-linux-gnu;  asm_pkg=nasm ;; \
        arm64) ff_arch=aarch64; gnu_host=aarch64-linux-gnu; asm_pkg= ;; \
        *) echo "Unsupported TARGETARCH: $target_arch" >&2; exit 1 ;; \
    esac; \
    if [ "$build_arch" = "$target_arch" ]; then \
        apt-get update; \
        apt-get install -y --no-install-recommends \
            binutils ca-certificates curl gcc libc6-dev make pkgconf xz-utils $asm_pkg \
            libdav1d-dev libopus-dev libvpx-dev zlib1g-dev; \
        cross_args=""; \
        strip_bin="strip"; \
        readelf_bin="readelf"; \
    else \
        dpkg --add-architecture "$target_arch"; \
        apt-get update; \
        apt-get install -y --no-install-recommends \
            "binutils-${gnu_host}" ca-certificates curl "gcc-${gnu_host}" make pkgconf xz-utils $asm_pkg \
            "libc6-dev:${target_arch}" "libdav1d-dev:${target_arch}" \
            "libopus-dev:${target_arch}" "libvpx-dev:${target_arch}" "zlib1g-dev:${target_arch}"; \
        cross_args="--enable-cross-compile --arch=${ff_arch} --target-os=linux --cross-prefix=${gnu_host}-"; \
        strip_bin="${gnu_host}-strip"; \
        readelf_bin="${gnu_host}-readelf"; \
    fi; \
    rm -rf /var/lib/apt/lists/*; \
    mkdir -p /tmp/ffmpeg-src /stage/usr/local/lib/ffmpeg; \
    curl -fsSL --retry 3 --retry-delay 2 -o /tmp/ffmpeg.tar.xz \
        "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz"; \
    echo "${FFMPEG_SHA256}  /tmp/ffmpeg.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg-src --strip-components=1; \
    rm -f /tmp/ffmpeg.tar.xz; \
    cd /tmp/ffmpeg-src; \
    unset PKG_CONFIG_PATH; \
    export PKG_CONFIG_LIBDIR="/usr/lib/${gnu_host}/pkgconfig:/usr/share/pkgconfig"; \
    ./configure \
        --prefix=/stage/usr/local \
        --pkg-config=pkgconf \
        $cross_args \
        --extra-cflags="-O2 -ffunction-sections -fdata-sections" \
        --extra-ldflags="-Wl,--gc-sections -Wl,-rpath,/usr/local/lib/ffmpeg" \
        --disable-doc \
        --disable-htmlpages \
        --disable-manpages \
        --disable-podpages \
        --disable-txtpages \
        --disable-ffplay \
        --disable-ffprobe \
        --disable-avdevice \
        --disable-postproc \
        --disable-network \
        --disable-autodetect \
        --disable-debug \
        --disable-everything \
        --enable-pthreads \
        --enable-ffmpeg \
        --enable-swscale \
        --enable-swresample \
        --enable-zlib \
        --enable-libopus \
        --enable-libvpx \
        --enable-libdav1d \
        --enable-protocol=file,pipe \
        --enable-demuxer=hevc,h264,mov,matroska,gif,wav,pcm_s16le,ogg,mp3,aac,amr,avi,flv,mpegts,image2,image2pipe,rawvideo \
        --enable-muxer=ogg,webm,gif,image2,image2pipe,rawvideo,null \
        --enable-decoder=hevc,h264,vp8,vp9,libvpx_vp9,libdav1d,mpeg4,h263,mpeg2video,mpeg1video,gif,png,pam,mjpeg,webp,bmp,rawvideo,pcm_s16le,pcm_s16be,pcm_s24le,pcm_s32le,pcm_u8,pcm_f32le,pcm_f64le,pcm_alaw,pcm_mulaw,adpcm_ima_wav,adpcm_ms,mp3,aac,opus,vorbis,amrnb,amrwb \
        --enable-encoder=libopus,libvpx_vp9,gif,pam,png,mjpeg,rawvideo \
        --enable-parser=hevc,h264,vp8,vp9,av1,mpeg4video,mpegaudio,aac,opus,png,gif,mjpeg,bmp \
        --enable-bsf=null,extract_extradata,vp9_superframe,vp9_superframe_split,vp9_raw_reorder \
        --enable-filter=scale,fps,format,aformat,crop,alphamerge,split,palettegen,paletteuse,premultiply,unpremultiply,transpose,hflip,vflip,rotate,trim,atrim,null,anull,aresample; \
    make -j"$(nproc)"; \
    make install; \
    rm -rf /stage/usr/local/include /stage/usr/local/share /stage/usr/local/lib/pkgconfig /stage/usr/local/lib/*.a /tmp/ffmpeg-src; \
    cp -a \
        "/usr/lib/${gnu_host}"/libdav1d.so.* \
        "/usr/lib/${gnu_host}"/libopus.so.* \
        "/usr/lib/${gnu_host}"/libvpx.so.* \
        /stage/usr/local/lib/ffmpeg/; \
    "$strip_bin" --strip-unneeded /stage/usr/local/bin/ffmpeg; \
    find /stage/usr/local/lib/ffmpeg -type f -exec "$strip_bin" --strip-unneeded {} +; \
    LC_ALL=C "$readelf_bin" -d /stage/usr/local/bin/ffmpeg | grep -F '[/usr/local/lib/ffmpeg]'; \
    for elf in /stage/usr/local/bin/ffmpeg /stage/usr/local/lib/ffmpeg/*.so.*; do \
        [ -f "$elf" ] && [ ! -L "$elf" ] || continue; \
        for soname in $(LC_ALL=C "$readelf_bin" -d "$elf" | sed -n 's/.*Shared library: \[\(.*\)\].*/\1/p'); do \
            case "$soname" in \
                libc.so.*|libm.so.*|libpthread.so.*|libdl.so.*|librt.so.*|libz.so.*|libgcc_s.so.*|libstdc++.so.*|ld-linux*.so.*) ;; \
                *) [ -e "/stage/usr/local/lib/ffmpeg/$soname" ] || { echo "Missing runtime lib: $soname (from $elf)" >&2; exit 1; } ;; \
            esac; \
        done; \
    done; \
    if [ "$build_arch" = "$target_arch" ]; then \
        head -c 3200 /dev/zero | env LD_LIBRARY_PATH=/stage/usr/local/lib/ffmpeg \
            /stage/usr/local/bin/ffmpeg -loglevel error \
            -f s16le -ar 16000 -ac 1 -i pipe:0 -c:a libopus -b:a 32k -f ogg pipe:1 >/dev/null; \
        head -c 4096 /dev/zero | env LD_LIBRARY_PATH=/stage/usr/local/lib/ffmpeg \
            /stage/usr/local/bin/ffmpeg -loglevel error -y \
            -f rawvideo -pix_fmt rgba -s 32x32 -r 10 -i pipe:0 \
            -vf "scale=16:16:flags=lanczos,format=yuva420p" -c:v libvpx-vp9 -an /tmp/smoke.webm; \
        head -c 4096 /dev/zero | env LD_LIBRARY_PATH=/stage/usr/local/lib/ffmpeg \
            /stage/usr/local/bin/ffmpeg -loglevel error -y \
            -f rawvideo -pix_fmt rgba -s 32x32 -r 10 -i pipe:0 \
            -vf "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" /tmp/smoke.gif; \
        rm -f /tmp/smoke.webm /tmp/smoke.gif; \
    fi

FROM python:3.13-slim AS builder

# Stage runtime tools (only /usr/bin/gifsicle & dynamic /usr/bin/tini) and slimmed fonts:
# - Strip unreachable non-cmap GSUB ligature glyphs from NotoColorEmoji.ttf (11.1 MB -> 3.0 MB,
#   pixel-identical under Pillow without libraqm).
# - Ship only NotoSansSC-Regular.otf (~8.3 MB); bridge/cards.py synthesizes Bold via 2x supersampling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        binutils ca-certificates curl fonts-noto-color-emoji gifsicle python3-fonttools tini \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /stage/usr/bin \
        /stage/usr/share/fonts/opentype/noto \
        /stage/usr/share/fonts/truetype/noto \
        /stage/app/data /stage/app/config \
    && cp /usr/bin/gifsicle /usr/bin/tini /stage/usr/bin/ \
    && python3 -m fontTools.subset /usr/share/fonts/truetype/noto/NotoColorEmoji.ttf \
        "--unicodes=*" "--layout-features=" \
        --output-file=/stage/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf \
    && curl -fsSL --retry 3 --retry-delay 2 \
        -o /stage/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf \
        https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf \
    && echo 'faa6c9df652116dde789d351359f3d7e5d2285a2b2a1f04a2d7244df706d5ea9  /stage/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf' \
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

COPY --from=ffmpeg-builder /stage/usr/local/ /usr/local/
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
