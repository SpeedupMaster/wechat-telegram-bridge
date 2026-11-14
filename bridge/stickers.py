"""Bidirectional sticker conversion between Telegram and WeChat (agent-wechat >= 2.4.0).

Telegram -> WeChat (native sticker, type=47):
  agent-wechat 2.4.0 sends stickers as validated GIFs (<= 240x240, <= 1 MB, <= 200 frames, <= 10 s):
  - static stickers (.webp)          -> <= 240x240 transparent GIF
  - animated stickers (.tgs Lottie)  -> rendered with rlottie into <= 240x240 transparent GIF
  - video stickers (.webm, VP9)      -> transcoded with libvpx-vp9 into <= 240x240 transparent GIF
  Legacy fallback (for agent-wechat < 2.4.0) converts to white-background JPEG or MP4 via `convert()`.

WeChat -> Telegram (native Telegram sticker):
  - static images / WXGF preview     -> 512 px RGBA .webp
  - animated GIF                     -> 512 px VP9 .webm (<= 3 s, <= 30 fps, <= 256 KB, alpha kept)
"""
from __future__ import annotations

import asyncio
import bisect
import gzip
import io
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time

from PIL import Image, ImageSequence

log = logging.getLogger("stickers")

SIZE = 384          # legacy MP4 output size
MIN_SECONDS = 3.0   # legacy MP4 repeat target
MAX_LOOPS = 4

WX_STICKER_SIZE = 240
WX_STICKER_MAX_BYTES = 1024 * 1024
# agent-wechat uploads stickers to WeChat in 8 KB sequential chunks, so staying compact keeps sends fast.
WX_STICKER_FAST_BYTES = 450 * 1024
WX_STICKER_MAX_FRAMES = 200
WX_STICKER_MAX_SECONDS = 9.5

_WX_GIF_ATTEMPTS = (
    (WX_STICKER_SIZE, 20.0, 256, "full", "none"),
    (WX_STICKER_SIZE, 1000.0 / 60.0, 256, "full", "none"),
    (WX_STICKER_SIZE, 12.5, 256, "full", "none"),
    (WX_STICKER_SIZE, 12.5, 160, "full", "bayer:bayer_scale=4"),
    (WX_STICKER_SIZE, 10.0, 128, "full", "bayer:bayer_scale=4"),
    (192, 12.5, 128, "full", "bayer:bayer_scale=4"),
    (160, 10.0, 64, "full", "bayer:bayer_scale=3"),
)

TG_STICKER_SIZE = 512
TG_VIDEO_STICKER_MAX_BYTES = 256 * 1024
TG_VIDEO_STICKER_MAX_SECONDS = 3.0
TG_VIDEO_STICKER_MAX_FPS = 30


def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:  # optional, for running without a system ffmpeg (e.g. local tests on Windows)
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def find_gifsicle() -> str | None:
    return shutil.which("gifsicle")


def on_white(im: Image.Image) -> Image.Image:
    im = im.convert("RGBA")
    bg = Image.new("RGB", im.size, "white")
    bg.paste(im, mask=im.getchannel("A"))
    return bg


def static_to_jpeg(data: bytes) -> bytes:
    im = Image.open(io.BytesIO(data))
    im.load()
    out = io.BytesIO()
    on_white(im).save(out, "JPEG", quality=92)
    return out.getvalue()


def _tgs_size(anim, max_edge: int) -> tuple[int, int]:
    try:
        orig_w, orig_h = anim.lottie_animation_get_size()
    except Exception:
        orig_w, orig_h = max_edge, max_edge
    return _fit_size(orig_w, orig_h, max_edge)


def _render_tgs_rgba(anim, frame_num: int, width: int, height: int) -> Image.Image:
    # rlottie renders premultiplied BGRA bytes; unpremultiplying avoids dark halos on translucent pixels.
    buf = anim.lottie_animation_render(frame_num=frame_num, width=width, height=height)
    return Image.frombuffer("RGBa", (width, height), buf, "raw", "BGRa").convert("RGBA")


def _resize_rgba(im: Image.Image, width: int, height: int) -> Image.Image:
    rgba = im.convert("RGBA")
    if rgba.size == (width, height):
        return rgba
    # Premultiply before LANCZOS so transparent (0,0,0,0) pixels do not bleed dark fringes into edges.
    return rgba.convert("RGBa").resize((width, height), Image.LANCZOS).convert("RGBA")


def tgs_first_frame_jpeg(data: bytes) -> bytes:
    from rlottie_python import LottieAnimation
    anim = LottieAnimation.from_data(gzip.decompress(data).decode("utf-8"))
    w, h = _tgs_size(anim, SIZE)
    out = io.BytesIO()
    on_white(_render_tgs_rgba(anim, 0, w, h)).save(out, "JPEG", quality=92)
    return out.getvalue()


def _wxgf_partition(data: bytes) -> bytes:
    """Largest HEVC partition of a WeChat "wxgf" (v2) image, as an Annex B stream.

    Same discovery as agent-wechat's wechat-media/src/wxgf.rs (after sjzar/chatlog): a header whose
    length is byte 4, then partitions that are a u32-BE length followed by NAL units starting with a
    VPS/SPS/PPS."""
    if len(data) < 15 or not data.startswith(b"wxgf") or data[5:7] != b"\x00\x02" or not 15 <= data[4] < len(data):
        raise ValueError("not a wxgf v2 image")

    def start_code(i: int) -> int:
        return 4 if data[i:i + 4] == b"\0\0\0\x01" else 3 if data[i:i + 3] == b"\0\0\x01" else 0

    pos, best = data[4] + 4, None
    while pos < len(data):
        code = start_code(pos)
        if not code:
            pos += 1
            continue
        length = int.from_bytes(data[pos - 4:pos], "big")
        end = pos + length
        nal = data[pos + code:pos + code + 2]
        parameter_set = len(nal) == 2 and not nal[0] & 0x80 and nal[1] & 7 and 32 <= (nal[0] >> 1) & 0x3F <= 34
        if length < code + 2 or end > len(data) or not parameter_set:
            pos += 1
            continue
        if best is None or length > best[1] - best[0]:
            best = (pos, end)
        pos = end
    if not best:
        raise ValueError("wxgf image has no HEVC partition")
    return data[best[0]:best[1]]


def wxgf_to_image(data: bytes, ffmpeg: str) -> Image.Image:
    """First frame of a WeChat wxgf image (used for avatars and stickers that come in this format)."""
    with tempfile.TemporaryDirectory() as tmp:
        src, out = os.path.join(tmp, "in.hevc"), os.path.join(tmp, "out.png")
        with open(src, "wb") as f:
            f.write(_wxgf_partition(data))
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "hevc", "-i", src,
                        "-frames:v", "1", out], check=True, capture_output=True, timeout=30)
        im = Image.open(out)
        im.load()
        return im


def _fit_size(w: int, h: int, max_edge: int, *, even: bool = False, upscale: bool = False) -> tuple[int, int]:
    w, h = max(1, w), max(1, h)
    longest = max(w, h)
    if not upscale and longest <= max_edge:
        nw, nh = w, h
    else:
        scale = max_edge / longest
        nw = max(1, round(w * scale))
        nh = max(1, round(h * scale))
    if even:
        nw = max(2, min(max_edge if max_edge % 2 == 0 else max_edge - 1, (nw // 2) * 2 or 2))
        nh = max(2, min(max_edge if max_edge % 2 == 0 else max_edge - 1, (nh // 2) * 2 or 2))
    return nw, nh


def _rgba_frame_to_pal(im: Image.Image, max_colors: int = 255) -> tuple[Image.Image, bool]:
    im = im.convert("RGBA")
    alpha = im.getchannel("A")
    has_alpha = alpha.getextrema()[0] < 128
    if not has_alpha:
        return im.convert("RGB").quantize(colors=min(256, max_colors + 1), method=Image.Quantize.MEDIANCUT), False
    mask = alpha.point(lambda a: 255 if a < 128 else 0)
    pal = im.convert("RGB").quantize(colors=min(255, max_colors), method=Image.Quantize.MEDIANCUT)
    p = pal.getpalette() or []
    if len(p) < 768:
        p = p + [0] * (768 - len(p))
        pal.putpalette(p)
    pal.paste(255, mask=mask)
    pal.info["transparency"] = 255
    return pal, True


def _rgba_to_transparent_gif_pillow(frames: list[tuple[Image.Image, int]], max_edge: int = WX_STICKER_SIZE,
                                    max_colors: int = 255) -> bytes:
    pals: list[Image.Image] = []
    durations: list[int] = []
    any_alpha = False
    for frame, delay_ms in frames:
        im = frame.convert("RGBA")
        if max(im.size) > max_edge:
            w, h = _fit_size(im.size[0], im.size[1], max_edge)
            im = _resize_rgba(im, w, h)
        pal, has_alpha = _rgba_frame_to_pal(im, max_colors=max_colors)
        any_alpha = any_alpha or has_alpha
        pals.append(pal)
        durations.append(max(20, delay_ms or 50))
    out = io.BytesIO()
    kw: dict = {"disposal": 2}
    if any_alpha:
        kw["transparency"] = 255
    if len(pals) == 1:
        pals[0].save(out, "GIF", **kw)
    else:
        pals[0].save(out, "GIF", save_all=True, append_images=pals[1:], duration=durations, loop=0, **kw)
    return out.getvalue()


def _gif_palette_vf(max_colors: int, stats_mode: str, dither: str) -> str:
    use_opts = f"dither={dither}:alpha_threshold=128"
    if stats_mode == "single":
        use_opts = "new=1:" + use_opts
    else:
        use_opts = "diff_mode=rectangle:" + use_opts
    return (f"split[s0][s1];[s0]palettegen=max_colors={max_colors}:reserve_transparent=1:stats_mode={stats_mode}[p];"
            f"[s1][p]paletteuse={use_opts}")


def _ffmpeg_rgba_to_gif(frames: list[bytes], width: int, height: int, fps: float, ffmpeg: str,
                        *, max_colors: int = 256, stats_mode: str = "full",
                        dither: str = "none") -> bytes:
    vf = _gif_palette_vf(max_colors, stats_mode, dither)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out.gif")
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{width}x{height}", "-r", f"{fps:g}", "-i", "pipe:0",
               "-vf", vf, "-loop", "0", out]
        res = subprocess.run(cmd, input=b"".join(frames), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.decode(errors="replace").strip() or f"ffmpeg exited {res.returncode}")
        with open(out, "rb") as f:
            return f.read()


def _optimize_gif(data: bytes, target_bytes: int, gifsicle: str | None) -> bytes:
    if not gifsicle:
        return data
    best = data
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.gif")
        out = os.path.join(tmp, "out.gif")
        with open(src, "wb") as f:
            f.write(data)
        res = subprocess.run([gifsicle, "-O3", src, "-o", out], capture_output=True, timeout=60)
        if res.returncode == 0 and os.path.exists(out):
            with open(out, "rb") as f:
                opt = f.read()
            if 0 < len(opt) < len(best):
                best = opt
        if len(best) <= target_bytes:
            return best
        for lossy in (20, 35):
            if lossy > 20 and len(best) <= WX_STICKER_MAX_BYTES:
                break
            res = subprocess.run([gifsicle, "-O3", f"--lossy={lossy}", src, "-o", out],
                                 capture_output=True, timeout=60)
            if res.returncode == 0 and os.path.exists(out):
                with open(out, "rb") as f:
                    opt = f.read()
                if 0 < len(opt) < len(best):
                    best = opt
            if len(best) <= target_bytes:
                break
    return best


def _gif_attempts(duration: float) -> tuple[tuple[int, float, int, str, str], ...]:
    if duration > 6.0:
        return _WX_GIF_ATTEMPTS[2:]
    if duration > 4.5:
        return _WX_GIF_ATTEMPTS[1:]
    return _WX_GIF_ATTEMPTS


def webp_to_wechat_gif(data: bytes, ffmpeg: str | None) -> bytes:
    im = Image.open(io.BytesIO(data))
    im.load()
    gifsicle = find_gifsicle()
    n_frames = getattr(im, "n_frames", 1)
    if n_frames <= 1:
        for edge, colors in ((WX_STICKER_SIZE, 255), (192, 127), (144, 63)):
            out = _optimize_gif(_rgba_to_transparent_gif_pillow([(im, 0)], max_edge=edge, max_colors=colors),
                                WX_STICKER_FAST_BYTES, gifsicle)
            if len(out) <= WX_STICKER_MAX_BYTES:
                return out
        return out

    src_frames: list[Image.Image] = []
    ends_ms: list[int] = []
    acc = 0
    max_ms = int(WX_STICKER_MAX_SECONDS * 1000)
    for frame in ImageSequence.Iterator(im):
        d = max(20, int(frame.info.get("duration") or 50))
        if acc + d > max_ms and src_frames:
            break
        src_frames.append(frame.convert("RGBA"))
        acc += d
        ends_ms.append(acc)

    duration = max(0.05, acc / 1000.0)
    src_fps = len(src_frames) / duration
    orig_w, orig_h = im.size
    raw_cache: dict[tuple[int, int, int], bytes] = {}
    pil_cache: dict[tuple[int, int, int], Image.Image] = {}
    seen: set[tuple] = set()
    best: bytes | None = None
    best_is_hq = False

    def get_pil(idx: int, w: int, h: int) -> Image.Image:
        key = (w, h, idx)
        if key not in pil_cache:
            pil_cache[key] = _resize_rgba(src_frames[idx], w, h)
        return pil_cache[key]

    def get_raw(idx: int, w: int, h: int) -> bytes:
        key = (w, h, idx)
        if key not in raw_cache:
            raw_cache[key] = get_pil(idx, w, h).tobytes()
        return raw_cache[key]

    for edge, max_fps, colors, stats_mode, dither in _gif_attempts(duration):
        is_hq = edge == WX_STICKER_SIZE and colors == 256
        if best is not None and best_is_hq and not is_hq:
            return best
        w, h = _fit_size(orig_w, orig_h, edge)
        target_fps = min(max_fps, src_fps, 180.0 / max(duration, 0.1))
        n_out = max(1, min(180, round(duration * target_fps)))
        actual_fps = max(1.0, n_out / duration)
        indices = [
            min(len(src_frames) - 1, bisect.bisect_right(ends_ms, (i + 0.5) * acc / n_out))
            for i in range(n_out)
        ]
        spec = (w, h, tuple(indices), colors, stats_mode, dither)
        if spec in seen:
            continue
        seen.add(spec)
        if ffmpeg:
            raw = [get_raw(idx, w, h) for idx in indices]
            out = _ffmpeg_rgba_to_gif(raw, w, h, actual_fps, ffmpeg, max_colors=colors,
                                      stats_mode=stats_mode, dither=dither)
        else:
            delay_ms = max(20, round(1000.0 / actual_fps))
            pil_frames = [(get_pil(idx, w, h), delay_ms) for idx in indices]
            out = _rgba_to_transparent_gif_pillow(pil_frames, max_edge=edge, max_colors=min(255, colors))
        out = _optimize_gif(out, WX_STICKER_FAST_BYTES, gifsicle)
        if len(out) <= WX_STICKER_MAX_BYTES:
            if best is None or len(out) < len(best):
                best, best_is_hq = out, is_hq
            if len(best) <= WX_STICKER_FAST_BYTES or not is_hq:
                return best
    if best is not None:
        return best
    raise ValueError("sticker_output_size_limit")


def tgs_to_wechat_gif(data: bytes, ffmpeg: str | None) -> bytes:
    from rlottie_python import LottieAnimation
    anim = LottieAnimation.from_data(gzip.decompress(data).decode("utf-8"))
    gifsicle = find_gifsicle()
    total = max(1, anim.lottie_animation_get_totalframe())
    src_fps = anim.lottie_animation_get_framerate() or 30
    if total == 1:
        w, h = _tgs_size(anim, WX_STICKER_SIZE)
        frame = _render_tgs_rgba(anim, 0, w, h)
        return _optimize_gif(_rgba_to_transparent_gif_pillow([(frame, 0)], max_edge=WX_STICKER_SIZE),
                             WX_STICKER_FAST_BYTES, gifsicle)

    duration = min(WX_STICKER_MAX_SECONDS, total / src_fps)
    usable_src_frames = max(1, min(total, round(duration * src_fps)))
    pil_cache: dict[tuple[int, int, int], Image.Image] = {}
    raw_cache: dict[tuple[int, int, int], bytes] = {}
    seen: set[tuple] = set()
    best: bytes | None = None
    best_is_hq = False

    def get_pil(idx: int, w: int, h: int) -> Image.Image:
        key = (w, h, idx)
        if key not in pil_cache:
            pil_cache[key] = _render_tgs_rgba(anim, idx, w, h)
        return pil_cache[key]

    def get_raw(idx: int, w: int, h: int) -> bytes:
        key = (w, h, idx)
        if key not in raw_cache:
            raw_cache[key] = get_pil(idx, w, h).tobytes()
        return raw_cache[key]

    for edge, max_fps, colors, stats_mode, dither in _gif_attempts(duration):
        is_hq = edge == WX_STICKER_SIZE and colors == 256
        if best is not None and best_is_hq and not is_hq:
            return best
        w, h = _tgs_size(anim, edge)
        target_fps = min(max_fps, src_fps, 180.0 / max(duration, 0.1))
        n_out = max(1, min(180, round(duration * target_fps)))
        actual_fps = max(1.0, n_out / max(duration, 0.05))
        indices = [min(usable_src_frames - 1, int(i * usable_src_frames / n_out)) for i in range(n_out)]
        spec = (w, h, tuple(indices), colors, stats_mode, dither)
        if spec in seen:
            continue
        seen.add(spec)
        if ffmpeg:
            raw = [get_raw(idx, w, h) for idx in indices]
            out = _ffmpeg_rgba_to_gif(raw, w, h, actual_fps, ffmpeg, max_colors=colors,
                                      stats_mode=stats_mode, dither=dither)
        else:
            delay_ms = max(20, round(1000.0 / actual_fps))
            pil_frames = [(get_pil(idx, w, h), delay_ms) for idx in indices]
            out = _rgba_to_transparent_gif_pillow(pil_frames, max_edge=edge, max_colors=min(255, colors))
        out = _optimize_gif(out, WX_STICKER_FAST_BYTES, gifsicle)
        if len(out) <= WX_STICKER_MAX_BYTES:
            if best is None or len(out) < len(best):
                best, best_is_hq = out, is_hq
            if len(best) <= WX_STICKER_FAST_BYTES or not is_hq:
                return best
    if best is not None:
        return best
    raise ValueError("sticker_output_size_limit")


def webm_to_wechat_gif(data: bytes, ffmpeg: str) -> bytes:
    gifsicle = find_gifsicle()
    with tempfile.TemporaryDirectory() as tmp:
        src, out_path = os.path.join(tmp, "in.webm"), os.path.join(tmp, "out.gif")
        with open(src, "wb") as f:
            f.write(data)
        best: bytes | None = None
        best_is_hq = False
        for edge, fps, colors, stats_mode, dither in _WX_GIF_ATTEMPTS:
            is_hq = edge == WX_STICKER_SIZE and colors == 256
            if best is not None and best_is_hq and not is_hq:
                return best
            vf = (f"fps={fps:g},format=rgba,premultiply=inplace=1,"
                  f"scale={edge}:{edge}:force_original_aspect_ratio=decrease:flags=lanczos,"
                  f"unpremultiply=inplace=1,"
                  + _gif_palette_vf(colors, stats_mode, dither))
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-c:v", "libvpx-vp9", "-i", src, "-t", f"{WX_STICKER_MAX_SECONDS:g}",
                   "-vf", vf, "-loop", "0", out_path]
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
            with open(out_path, "rb") as f:
                out = f.read()
            out = _optimize_gif(out, WX_STICKER_FAST_BYTES, gifsicle)
            if len(out) <= WX_STICKER_MAX_BYTES:
                if best is None or len(out) < len(best):
                    best, best_is_hq = out, is_hq
                if len(best) <= WX_STICKER_FAST_BYTES or not is_hq:
                    return best
        if best is not None:
            return best
    raise ValueError("sticker_output_size_limit")


async def to_wechat_sticker(data: bytes, mime: str) -> tuple[str, bytes] | str:
    """Convert a Telegram sticker (.webp / .tgs / .webm) to a <=240px <=1MB transparent GIF for WeChat."""
    t0 = time.monotonic()
    try:
        ffmpeg = find_ffmpeg()
        if mime == "image/webp":
            out = await asyncio.to_thread(webp_to_wechat_gif, data, ffmpeg)
        elif mime == "application/x-tgsticker":
            out = await asyncio.to_thread(tgs_to_wechat_gif, data, ffmpeg)
        elif mime == "video/webm":
            if not ffmpeg:
                return "需要 ffmpeg 才能转换视频贴纸"
            out = await asyncio.to_thread(webm_to_wechat_gif, data, ffmpeg)
        else:
            return f"不支持的贴纸格式 {mime}"
        try:
            gim = Image.open(io.BytesIO(out))
            log.info("Converted %s sticker (%d B) -> %dx%d GIF (%d frames, %d B) in %.2fs",
                     mime, len(data), gim.size[0], gim.size[1], getattr(gim, "n_frames", 1),
                     len(out), time.monotonic() - t0)
        except Exception:
            pass
        return "sticker", out
    except Exception as e:
        log.warning("WeChat sticker conversion failed (%s): %s", mime, e)
        return f"贴纸转换失败：{type(e).__name__}"


def wechat_image_to_tg_webp(im: Image.Image) -> tuple[bytes, int, int]:
    rgba = im.convert("RGBA")
    w, h = _fit_size(rgba.size[0], rgba.size[1], TG_STICKER_SIZE, upscale=True)
    if (w, h) != rgba.size:
        rgba = _resize_rgba(rgba, w, h)
    out = io.BytesIO()
    rgba.save(out, "WEBP", quality=92, method=6)
    return out.getvalue(), w, h


def wechat_gif_to_tg_webm(data: bytes, ffmpeg: str) -> tuple[bytes, int, int, float] | None:
    im = Image.open(io.BytesIO(data))
    n_frames = getattr(im, "n_frames", 1)
    if n_frames <= 1:
        return None
    total_ms = 0
    for frame in ImageSequence.Iterator(im):
        total_ms += max(20, int(frame.info.get("duration") or 100))
    dur_s = max(0.1, total_ms / 1000.0)
    out_dur = min(TG_VIDEO_STICKER_MAX_SECONDS, dur_s)
    target_fps = min(TG_VIDEO_STICKER_MAX_FPS, max(1, round(n_frames / dur_s)))
    out_w, out_h = _fit_size(im.size[0], im.size[1], TG_STICKER_SIZE, even=True, upscale=True)
    with tempfile.TemporaryDirectory() as tmp:
        src, out_path = os.path.join(tmp, "in.gif"), os.path.join(tmp, "out.webm")
        with open(src, "wb") as f:
            f.write(data)
        vf = f"fps={target_fps},scale={out_w}:{out_h}:flags=lanczos,format=yuva420p"
        for crf, bitrate in ((20, "650k"), (26, "450k"), (34, "280k"), (42, "150k")):
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-i", src, "-t", f"{TG_VIDEO_STICKER_MAX_SECONDS:g}", "-vf", vf,
                   "-c:v", "libvpx-vp9", "-b:v", bitrate, "-crf", str(crf),
                   "-deadline", "good", "-cpu-used", "2",
                   "-auto-alt-ref", "0", "-an", out_path]
            res = subprocess.run(cmd, capture_output=True, timeout=120)
            if res.returncode != 0:
                return None
            with open(out_path, "rb") as f:
                out = f.read()
            if len(out) <= TG_VIDEO_STICKER_MAX_BYTES:
                return out, out_w, out_h, out_dur
    return None


def from_wechat_sticker(data: bytes, mime: str) -> tuple[str, bytes, dict] | None:
    """Convert a downloaded WeChat sticker to a Telegram static (.webp) or video (.webm) sticker.

    Returns ("static", bytes, {"w", "h"}) or ("video", bytes, {"w", "h", "duration"}) or None.
    """
    ffmpeg = find_ffmpeg()
    try:
        if mime == "image/x-wxgf":
            if not ffmpeg:
                return None
            im = wxgf_to_image(data, ffmpeg)
            webp, w, h = wechat_image_to_tg_webp(im)
            return "static", webp, {"w": w, "h": h}
        if mime == "image/gif":
            im = Image.open(io.BytesIO(data))
            if getattr(im, "n_frames", 1) > 1:
                if not ffmpeg:
                    return None
                res = wechat_gif_to_tg_webm(data, ffmpeg)
                if not res:
                    return None
                webm, w, h, dur = res
                return "video", webm, {"w": w, "h": h, "duration": dur}
            im.load()
            webp, w, h = wechat_image_to_tg_webp(im)
            return "static", webp, {"w": w, "h": h}
        if mime in ("image/png", "image/jpeg", "image/webp"):
            im = Image.open(io.BytesIO(data))
            im.load()
            webp, w, h = wechat_image_to_tg_webp(im)
            return "static", webp, {"w": w, "h": h}
    except Exception as e:
        log.info("Could not convert WeChat sticker (%s) to Telegram sticker: %s", mime, e)
    return None


def _encode_cmd(ffmpeg: str, source: list[str], out: str, fps: float) -> list[str]:
    """Legacy MP4 encoder: Input 0 is a white background, input 1 the sticker (with alpha), centred and padded."""
    graph = (f"[1:v]scale={SIZE}:{SIZE}:force_original_aspect_ratio=decrease,"
             f"pad={SIZE}:{SIZE}:(ow-iw)/2:(oh-ih)/2:color=black@0,format=rgba[fg];"
             "[0:v][fg]overlay=shortest=1,format=yuv420p")
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=white:s={SIZE}x{SIZE}:r={fps:g}",
            *source, "-filter_complex", graph, "-r", f"{fps:g}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-movflags", "+faststart", "-an", out]


def webm_to_mp4(data: bytes, ffmpeg: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        src, out = os.path.join(tmp, "in.webm"), os.path.join(tmp, "out.mp4")
        with open(src, "wb") as f:
            f.write(data)
        # libvpx-vp9 (not ffmpeg's native vp9 decoder) keeps the alpha channel
        source = ["-stream_loop", str(MAX_LOOPS - 2), "-c:v", "libvpx-vp9", "-i", src]
        subprocess.run(_encode_cmd(ffmpeg, source, out, 30), check=True, capture_output=True, timeout=120)
        with open(out, "rb") as f:
            return f.read()


def tgs_to_mp4(data: bytes, ffmpeg: str) -> bytes:
    from rlottie_python import LottieAnimation
    anim = LottieAnimation.from_data(gzip.decompress(data).decode("utf-8"))
    total = max(1, anim.lottie_animation_get_totalframe())
    fps = anim.lottie_animation_get_framerate() or 30
    loops = min(MAX_LOOPS, max(1, math.ceil(MIN_SECONDS * fps / total)))
    w, h = _tgs_size(anim, SIZE)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out.mp4")
        source = ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-r", f"{fps:g}", "-i", "pipe:0"]
        frames = [_render_tgs_rgba(anim, i, w, h).tobytes() for i in range(total)]
        raw = b"".join(frames) * loops
        res = subprocess.run(_encode_cmd(ffmpeg, source, out, fps), input=raw,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.decode(errors="replace").strip() or f"ffmpeg exited {res.returncode}")
        with open(out, "rb") as f:
            return f.read()


async def convert(data: bytes, mime: str) -> tuple[str, bytes] | str:
    """Legacy fallback for agent-wechat < 2.4.0: ("image" | "video", bytes), or error reason."""
    try:
        if mime == "image/webp":
            return "image", await asyncio.to_thread(static_to_jpeg, data)
        ffmpeg = find_ffmpeg()
        if mime == "video/webm":
            if not ffmpeg:
                return "需要 ffmpeg 才能转换视频贴纸"
            return "video", await asyncio.to_thread(webm_to_mp4, data, ffmpeg)
        if mime == "application/x-tgsticker":
            if ffmpeg:
                try:
                    return "video", await asyncio.to_thread(tgs_to_mp4, data, ffmpeg)
                except Exception as e:
                    log.warning("Could not render animated sticker as video (%s); sending its first frame", e)
            return "image", await asyncio.to_thread(tgs_first_frame_jpeg, data)
    except Exception as e:
        log.warning("Sticker conversion failed (%s): %s", mime, e)
        return f"贴纸转换失败：{type(e).__name__}"
    return f"不支持的贴纸格式 {mime}"
