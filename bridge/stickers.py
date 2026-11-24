"""Bidirectional sticker conversion between Telegram and WeChat.

Telegram -> WeChat (native sticker, type=47):
  - static stickers (.webp, 1-frame .tgs) -> <= 512px <= 1 MB transparent GIF
  - animated stickers (.tgs/.webm/.webp) & GIFs -> <= 384px <= 1 MB transparent GIF

WeChat -> Telegram (native Telegram sticker):
  - static images / 1-frame WXGF     -> 512 px RGBA .webp
  - animated GIF / WXGF              -> 512 px VP9 .webm (<= 3 s, <= 30 fps, <= 256 KB, alpha kept)
  WXGF is WeChat's HEVC format: colour frames plus a gray alpha-mask stream. agent-wechat's own preview
  of it is a single still frame, so stickers are fetched as the original bytes and decoded here.
"""
from __future__ import annotations

import asyncio
import atexit
import bisect
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .mem import maybe_trim_memory

if TYPE_CHECKING:  # pragma: no cover
    from PIL import Image, ImageChops, ImageSequence, ImageStat
else:
    Image = ImageChops = ImageSequence = ImageStat = None


def _ensure_pil() -> None:
    """Lazily import Pillow on first sticker, avatar, or video-thumbnail operation."""
    global Image, ImageChops, ImageSequence, ImageStat
    if Image is None:
        from PIL import (
            Image as _Image,
            ImageChops as _ImageChops,
            ImageSequence as _ImageSequence,
            ImageStat as _ImageStat,
        )
        Image, ImageChops, ImageSequence, ImageStat = _Image, _ImageChops, _ImageSequence, _ImageStat

log = logging.getLogger("stickers")

_MEDIA_EXECUTOR_LOCK = threading.Lock()
_MEDIA_EXECUTOR: ThreadPoolExecutor | None = None


def get_media_executor() -> ThreadPoolExecutor:
    """Dedicated bounded thread pool for heavy sticker transcoding and video thumbnail extraction."""
    global _MEDIA_EXECUTOR
    with _MEDIA_EXECUTOR_LOCK:
        if _MEDIA_EXECUTOR is None:
            _MEDIA_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bridge-media")
        return _MEDIA_EXECUTOR


def shutdown_media_executor(*, wait: bool = False) -> None:
    global _MEDIA_EXECUTOR
    with _MEDIA_EXECUTOR_LOCK:
        ex, _MEDIA_EXECUTOR = _MEDIA_EXECUTOR, None
    if ex is not None:
        ex.shutdown(wait=wait)


atexit.register(shutdown_media_executor)


async def run_in_media_executor(fn: Callable, *args):
    """Run a heavy media/transcode function in the isolated 2-worker media executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(get_media_executor(), fn, *args)

STICKER_CACHE_VERSION = 3
STICKER_CACHE_MAX_BYTES = 100 * 1024 * 1024

WX_STICKER_SIZE = 384
WX_STICKER_STATIC_SIZE = 512
WX_STICKER_MAX_BYTES = 1024 * 1024
WX_STICKER_FAST_BYTES = 550 * 1024
WX_STICKER_MAX_FRAMES = 200
WX_STICKER_MAX_SECONDS = 9.5


def default_fast_bytes(max_edge: int = WX_STICKER_SIZE) -> int:
    """Return the early-stop byte budget for WeChat sticker GIF encoding."""
    del max_edge
    return WX_STICKER_FAST_BYTES


def _static_attempts(max_edge: int) -> tuple[tuple[int, int], ...]:
    fallbacks = ((384, 255), (288, 191), (192, 95))
    return ((max_edge, 255), *(step for step in fallbacks if step[0] < max_edge))


def _gif_attempts(duration: float, max_edge: int = WX_STICKER_SIZE,
                  *, video: bool = False) -> tuple[tuple[int, float, int, str, str], ...]:
    """Prioritize crisp target-edge frames and exact 10ms-aligned GIF cadence before reducing colors or size."""
    if video:
        head: tuple[tuple[int, float, int, str, str], ...] = (
            (max_edge, 12.5, 256, "diff", "bayer:bayer_scale=5"),
            (max_edge, 10.0, 256, "diff", "bayer:bayer_scale=5"),
            (max_edge, 1000.0 / 120.0, 192, "diff", "bayer:bayer_scale=4"),
            (max_edge, 1000.0 / 120.0, 128, "diff", "bayer:bayer_scale=4"),
        )
    else:
        head = (
            (max_edge, 1000.0 / 60.0, 256, "diff", "bayer:bayer_scale=5"),
            (max_edge, 12.5, 256, "diff", "bayer:bayer_scale=5"),
            (max_edge, 10.0, 256, "diff", "bayer:bayer_scale=4"),
            (max_edge, 10.0, 192, "diff", "bayer:bayer_scale=4"),
            (max_edge, 1000.0 / 120.0, 128, "diff", "bayer:bayer_scale=3"),
        )
        if duration > 6.0:
            head = head[2:]
        elif duration > 4.5:
            head = head[1:]
    fallbacks = (
        (320, 10.0, 192, "diff", "bayer:bayer_scale=4"),
        (240, 10.0, 128, "diff", "bayer:bayer_scale=3"),
        (176, 1000.0 / 120.0, 96, "diff", "bayer:bayer_scale=3"),
    )
    return head + tuple(step for step in fallbacks if step[0] < max_edge)

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
    _ensure_pil()
    im = im.convert("RGBA")
    bg = Image.new("RGB", im.size, "white")
    bg.paste(im, mask=im.getchannel("A"))
    return bg


def _tgs_size(anim, max_edge: int) -> tuple[int, int]:
    try:
        orig_w, orig_h = anim.lottie_animation_get_size()
    except Exception:
        orig_w, orig_h = max_edge, max_edge
    return _fit_size(orig_w, orig_h, max_edge)


def _render_tgs_rgba(anim, frame_num: int, width: int, height: int) -> Image.Image:
    _ensure_pil()
    # rlottie renders premultiplied BGRA bytes; unpremultiplying avoids dark halos on translucent pixels.
    buf = anim.lottie_animation_render(frame_num=frame_num, width=width, height=height)
    return Image.frombuffer("RGBa", (width, height), buf, "raw", "BGRa").convert("RGBA")


def _resize_rgba(im: Image.Image, width: int, height: int) -> Image.Image:
    _ensure_pil()
    rgba = im.convert("RGBA")
    if rgba.size == (width, height):
        return rgba
    # Premultiply before LANCZOS so transparent (0,0,0,0) pixels do not bleed dark fringes into edges.
    return rgba.convert("RGBa").resize((width, height), Image.LANCZOS).convert("RGBA")


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


def _hevc_nal_header_ok(nal: bytes) -> bool:
    return len(nal) == 2 and not nal[0] & 0x80 and bool(nal[1] & 7) and (nal[0] >> 1) & 0x3F <= 40


def _wxgf_all_partitions(data: bytes) -> list[bytes]:
    """Every HEVC partition of a wxgf v2 image, in file order.

    An animated wxgf stores each frame as its own partition, alternating between an alpha-mask stream
    (HEVC Rext, gray) and the colour stream; only a stream's first frames carry VPS/SPS/PPS. The first
    partition must open with a parameter set, so stray start codes in the header are not mistaken for one."""
    if len(data) < 15 or not data.startswith(b"wxgf") or data[5:7] != b"\x00\x02" or not 15 <= data[4] < len(data):
        raise ValueError("not a wxgf v2 image")
    parts: list[bytes] = []
    pos = data[4] + 4
    while pos < len(data):
        code = 4 if data[pos:pos + 4] == b"\0\0\0\x01" else 3 if data[pos:pos + 3] == b"\0\0\x01" else 0
        if not code:
            pos += 1
            continue
        length = int.from_bytes(data[pos - 4:pos], "big")
        end = pos + length
        nal = data[pos + code:pos + code + 2]
        ok = length >= code + 2 and end <= len(data) and _hevc_nal_header_ok(nal)
        if ok and not parts:
            ok = 32 <= (nal[0] >> 1) & 0x3F <= 34
        if not ok:
            pos += 1
            continue
        parts.append(data[pos:end])
        pos = end
    if not parts:
        raise ValueError("wxgf image has no HEVC partition")
    return parts


def _hevc_sps_chroma_format(partition: bytes) -> int | None:
    """chroma_format_idc of the first SPS in an Annex B partition (0 = monochrome mask), or None without one."""
    for m in re.finditer(rb"\x00\x00\x01", partition):
        start = m.end()
        if len(partition) < start + 2 or (partition[start] >> 1) & 0x3F != 33:
            continue
        end = partition.find(b"\x00\x00\x01", start)
        rbsp = re.sub(rb"\x00\x00\x03", b"\x00\x00", partition[start + 2:end if end != -1 else len(partition)])
        bits = "".join(f"{b:08b}" for b in rbsp[:64])
        pos = 0

        def u(n: int) -> int:
            nonlocal pos
            if pos + n > len(bits):
                raise ValueError("truncated SPS")
            v = int(bits[pos:pos + n], 2) if n else 0
            pos += n
            return v

        def ue() -> int:
            nonlocal pos
            zeros = 0
            while u(1) == 0:
                zeros += 1
                if zeros > 31:
                    raise ValueError("bad exp-golomb")
            return (1 << zeros) - 1 + u(zeros)

        try:
            u(4)                      # sps_video_parameter_set_id
            max_sub_layers = u(3)     # sps_max_sub_layers_minus1
            u(1)                      # sps_temporal_id_nesting_flag
            u(96)                     # general profile_tier_level
            flags = [(u(1), u(1)) for _ in range(max_sub_layers)]
            if max_sub_layers:
                u(2 * (8 - max_sub_layers))
            for profile_present, level_present in flags:
                u(88 * profile_present + 8 * level_present)
            ue()                      # sps_seq_parameter_set_id
            return ue()               # chroma_format_idc
        except ValueError:
            return None
    return None


def _wxgf_streams(data: bytes) -> tuple[list[bytes], list[bytes]]:
    """Split a wxgf into (colour partitions, alpha-mask partitions), one partition per frame."""
    parts = _wxgf_all_partitions(data)
    chroma = [_hevc_sps_chroma_format(p) for p in parts]
    if not any(c == 0 for c in chroma):
        return parts, []
    if all(c is not None for c in chroma):
        return [p for p, c in zip(parts, chroma) if c != 0], [p for p, c in zip(parts, chroma) if c == 0]
    mask_parity = {i % 2 for i, c in enumerate(chroma) if c == 0}
    colour_parity = {i % 2 for i, c in enumerate(chroma) if c is not None and c != 0}
    if len(mask_parity) != 1 or mask_parity & colour_parity:
        raise ValueError("wxgf mask and colour frames do not alternate")
    mp = mask_parity.pop()
    colour = [p for i, p in enumerate(parts) if i % 2 != mp]
    if not colour:
        raise ValueError("wxgf image has no colour frames")
    return colour, [p for i, p in enumerate(parts) if i % 2 == mp]


_TBR_RE = re.compile(r"Video:.*?([\d.]+) tbr")
WX_ANIMATION_DEFAULT_FPS = 25.0


def _hevc_frame_rate(path: str, ffmpeg: str) -> float:
    """Frame rate from the stream's VUI timing (ffmpeg's `tbr` for raw HEVC), else ffmpeg's 25 fps default."""
    res = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-f", "hevc", "-i", path, "-map", "0:v:0",
                          "-c", "copy", "-f", "null", "-"],
                         capture_output=True, text=True, errors="replace", timeout=30)
    m = _TBR_RE.search(res.stderr or "")
    try:
        rate = float(m.group(1)) if m else 0.0
    except ValueError:
        rate = 0.0
    return rate if 1.0 <= rate <= 120.0 else WX_ANIMATION_DEFAULT_FPS


def wxgf_to_tg_sticker(data: bytes, ffmpeg: str) -> tuple[str, bytes, dict]:
    """Decode a (possibly animated, possibly transparent) wxgf into a Telegram sticker.

    Returns ("video", webm, {"w", "h", "duration"}) for animations and ("static", webp, {"w", "h"}) otherwise."""
    colour, mask = _wxgf_streams(data)
    width, height = int.from_bytes(data[7:9], "big"), int.from_bytes(data[9:11], "big")
    with tempfile.TemporaryDirectory() as tmp:
        colour_path, mask_path = os.path.join(tmp, "colour.hevc"), os.path.join(tmp, "mask.hevc")
        with open(colour_path, "wb") as f:
            f.write(b"".join(colour))
        if mask:
            with open(mask_path, "wb") as f:
                f.write(b"".join(mask))
        fps = _hevc_frame_rate(colour_path, ffmpeg)
        # Odd canvases decode one pixel larger in 4:2:0 HEVC; crop back to the header's canvas.
        crop = f"crop='min(iw,{width})':'min(ih,{height})':0:0," if width and height else ""
        animated = len(colour) > 1

        def inputs(with_mask: bool) -> list[str]:
            args = ["-framerate", f"{fps:g}", "-f", "hevc", "-i", colour_path]
            if with_mask:
                args += ["-framerate", f"{fps:g}", "-f", "hevc", "-i", mask_path]
            return args

        def merge(with_mask: bool) -> str:
            # The mask's luma is the alpha channel (white = opaque).
            return "[0:v][1:v]alphamerge," if with_mask else "[0:v]format=rgba,"

        attempts = (True, False) if mask else (False,)
        for with_mask in attempts:
            if animated:
                out_w, out_h = _fit_size(width or TG_STICKER_SIZE, height or TG_STICKER_SIZE, TG_STICKER_SIZE,
                                         even=True, upscale=True)
                graph = (f"{merge(with_mask)}{crop}fps={min(fps, TG_VIDEO_STICKER_MAX_FPS):g},"
                         f"scale={out_w}:{out_h}:flags=lanczos,format=yuva420p[v]")
                webm = _encode_tg_webm(ffmpeg, inputs(with_mask), ["-filter_complex", graph, "-map", "[v]"],
                                       os.path.join(tmp, "out.webm"))
                if webm is not None:
                    duration = min(TG_VIDEO_STICKER_MAX_SECONDS, len(colour) / fps)
                    return "video", webm, {"w": out_w, "h": out_h, "duration": duration}
                continue
            png = os.path.join(tmp, "frame.png")
            res = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                                  *inputs(with_mask), "-filter_complex", f"{merge(with_mask)}{crop}format=rgba[v]",
                                  "-map", "[v]", "-frames:v", "1", png], capture_output=True, timeout=30)
            if res.returncode == 0 and os.path.exists(png):
                _ensure_pil()
                with Image.open(png) as im:
                    im.load()
                    webp, w, h = wechat_image_to_tg_webp(im)
                return "static", webp, {"w": w, "h": h}
            log.info("wxgf still decode failed (mask=%s): %s", with_mask, res.stderr.decode(errors="replace")[-300:])
    if animated:
        # The video path failed (e.g. the webm stays over Telegram's size cap): fall back to a still.
        webp, w, h = wechat_image_to_tg_webp(wxgf_to_image(data, ffmpeg))
        return "static", webp, {"w": w, "h": h}
    raise ValueError("wxgf decode failed")


def wxgf_to_image(data: bytes, ffmpeg: str) -> Image.Image:
    """First frame of a WeChat wxgf image (used for avatars and stickers that come in this format)."""
    _ensure_pil()
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
    _ensure_pil()
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
    _ensure_pil()
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


_ALPHA_KEEP_LUT = [0 if a < 128 else 255 for a in range(256)]
_OPAQUE_ALPHA_TABLE = bytes(range(128, 256))
_GIFSICLE_FAST_SKIP_BYTES = 96 * 1024


def _clean_rgba_alpha(im: Image.Image) -> Image.Image:
    """Zero RGB on sub-128 alpha pixels so faint edge halos and invisible jitter vanish cleanly."""
    _ensure_pil()
    rgba = im.convert("RGBA")
    lo, _ = rgba.getchannel("A").getextrema()
    if lo >= 128:
        return rgba
    clean = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    clean.paste(rgba, mask=rgba.getchannel("A").point(_ALPHA_KEEP_LUT))
    return clean


def _rgba_frames_have_alpha(frames: Sequence[Image.Image | bytes]) -> bool:
    _ensure_pil()
    for frame in frames:
        if isinstance(frame, Image.Image):
            rgba = frame if frame.mode == "RGBA" else frame.convert("RGBA")
            lo, _ = rgba.getchannel("A").getextrema()
            if lo < 128:
                return True
        elif len(frame) >= 4 and frame[3::4].translate(None, _OPAQUE_ALPHA_TABLE):
            return True
    return False


def _dedup_sampled_frames(
    indices: list[int],
    total_cs: int,
    width: int,
    height: int,
    get_pil: Callable[[int, int, int], Image.Image],
    *,
    max_diff_thresh: int,
    rms_thresh: float,
) -> tuple[list[int], list[Image.Image], list[int]]:
    """Collapse consecutive identical or visually still sampled frames and accumulate their durations."""
    _ensure_pil()
    n_out = len(indices)
    kept_indices: list[int] = []
    kept_pils: list[Image.Image] = []
    kept_cs: list[int] = []

    for i, idx in enumerate(indices):
        dur_cs = max(2, round((i + 1) * total_cs / n_out) - round(i * total_cs / n_out))
        if kept_indices and idx == kept_indices[-1]:
            kept_cs[-1] = min(65535, kept_cs[-1] + dur_cs)
            continue
        im = get_pil(idx, width, height)
        if kept_pils:
            diff = ImageChops.difference(im, kept_pils[-1])
            max_diff = max(hi for _, hi in diff.getextrema())
            same = max_diff == 0 or (
                max_diff_thresh > 0
                and max_diff <= max_diff_thresh
                and max(ImageStat.Stat(diff).rms) <= rms_thresh
            )
            if same and kept_cs[-1] + dur_cs <= 65535:
                kept_cs[-1] += dur_cs
                continue
        kept_indices.append(idx)
        kept_pils.append(im)
        kept_cs.append(dur_cs)
    return kept_indices, kept_pils, kept_cs


def _skip_gif_subblocks(buf: bytearray, pos: int) -> int:
    while pos < len(buf):
        size = buf[pos]
        pos += 1
        if size == 0:
            return pos
        pos += size
    return pos


def _set_gif_frame_delays(gif_bytes: bytes, delays_cs: list[int]) -> bytes:
    """Patch GIF Graphic Control Extension per-frame centisecond delays without re-encoding pixels."""
    if len(delays_cs) <= 1 or len(gif_bytes) < 14 or gif_bytes[:6] not in (b"GIF87a", b"GIF89a"):
        return gif_bytes
    out = bytearray(gif_bytes)
    pos = 13
    packed = out[10]
    if packed & 0x80:
        pos += 3 * (1 << ((packed & 0x07) + 1))
    offsets: list[int] = []
    while pos < len(out):
        tag = out[pos]
        if tag == 0x3B:
            break
        if tag == 0x21:
            if pos + 7 < len(out) and out[pos + 1] == 0xF9 and out[pos + 2] == 4:
                offsets.append(pos + 4)
                pos += 8
            else:
                pos = _skip_gif_subblocks(out, pos + 2)
        elif tag == 0x2C:
            if pos + 10 >= len(out):
                return gif_bytes
            ipacked = out[pos + 9]
            pos += 10
            if ipacked & 0x80:
                pos += 3 * (1 << ((ipacked & 0x07) + 1))
            pos += 1
            pos = _skip_gif_subblocks(out, pos)
        else:
            return gif_bytes
    if len(offsets) != len(delays_cs):
        return gif_bytes
    for off, delay in zip(offsets, delays_cs):
        cs = max(2, min(65535, delay))
        out[off] = cs & 0xFF
        out[off + 1] = (cs >> 8) & 0xFF
    return bytes(out)


def _gif_palette_vf(max_colors: int, stats_mode: str, dither: str, *, transparent: bool = True) -> str:
    use_opts = f"dither={dither}" + (":alpha_threshold=128" if transparent else "")
    if stats_mode == "single":
        use_opts = "new=1:" + use_opts
    else:
        use_opts = "diff_mode=rectangle:" + use_opts
    reserve = 1 if transparent else 0
    return (f"split[s0][s1];[s0]palettegen=max_colors={max_colors}:reserve_transparent={reserve}:stats_mode={stats_mode}[p];"
            f"[s1][p]paletteuse={use_opts}")


def _ffmpeg_rgba_to_gif(frames: Sequence[Image.Image | bytes], width: int, height: int, fps: float, ffmpeg: str,
                        *, max_colors: int = 256, stats_mode: str = "full",
                        dither: str = "none") -> bytes:
    _ensure_pil()
    transparent = _rgba_frames_have_alpha(frames)
    vf = _gif_palette_vf(max_colors, stats_mode, dither, transparent=transparent)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out.gif")
        err_path = os.path.join(tmp, "ffmpeg.err")
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{width}x{height}", "-r", f"{fps:g}", "-i", "pipe:0",
               "-vf", vf, "-loop", "0", out]
        with open(err_path, "wb") as err_f:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err_f)
            timed_out = False

            def _kill_on_timeout() -> None:
                nonlocal timed_out
                timed_out = True
                try:
                    proc.kill()
                except OSError:
                    pass

            timer = threading.Timer(120.0, _kill_on_timeout)
            timer.start()
            try:
                assert proc.stdin is not None
                try:
                    for frame in frames:
                        raw = (frame if frame.mode == "RGBA" else frame.convert("RGBA")).tobytes() if isinstance(frame, Image.Image) else frame
                        proc.stdin.write(raw)
                except BrokenPipeError:
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
                returncode = proc.wait()
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            finally:
                timer.cancel()
        if timed_out:
            raise subprocess.TimeoutExpired(cmd, 120)
        if returncode != 0:
            with open(err_path, "rb") as err_f:
                stderr = err_f.read().decode(errors="replace").strip()
            raise RuntimeError(stderr or f"ffmpeg exited {returncode}")
        with open(out, "rb") as f:
            return f.read()


def _optimize_gif(data: bytes, target_bytes: int, gifsicle: str | None) -> bytes:
    if not gifsicle or (len(data) <= _GIFSICLE_FAST_SKIP_BYTES and len(data) <= target_bytes):
        return data
    best = data
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.gif")
        out = os.path.join(tmp, "out.gif")
        with open(src, "wb") as f:
            f.write(data)
        for flags in (("-O2",), ("-O2", "--lossy=15"), ("-O2", "--lossy=35")):
            if flags[-1].startswith("--lossy=") and len(best) <= target_bytes:
                break
            if flags[-1] == "--lossy=35" and len(best) <= WX_STICKER_MAX_BYTES:
                break
            res = subprocess.run([gifsicle, *flags, src, "-o", out], capture_output=True, timeout=60)
            if res.returncode == 0 and os.path.exists(out):
                with open(out, "rb") as f:
                    opt = f.read()
                if 0 < len(opt) < len(best):
                    best = opt
            if len(best) <= target_bytes:
                break
    return best


def _encode_static_rgba_to_gif(im: Image.Image, max_edge: int, fast_bytes: int, gifsicle: str | None) -> bytes:
    """Encode a single RGBA frame into a <=max_edge <=1MB transparent GIF."""
    cleaned = _clean_rgba_alpha(im)
    out = b""
    for edge, colors in _static_attempts(max_edge):
        out = _optimize_gif(
            _rgba_to_transparent_gif_pillow([(cleaned, 0)], max_edge=edge, max_colors=colors),
            fast_bytes, gifsicle,
        )
        if len(out) <= WX_STICKER_MAX_BYTES:
            return out
    return out


def _encode_rgba_attempts_to_gif(
    attempts: tuple[tuple[int, float, int, str, str], ...],
    duration: float,
    src_fps: float,
    size_for_edge: Callable[[int], tuple[int, int]],
    sample_indices: Callable[[int], list[int]],
    load_frame: Callable[[int, int, int], Image.Image],
    ffmpeg: str | None,
    gifsicle: str | None,
    *,
    fast_bytes: int | None = None,
    static_max_edge: int | None = None,
    max_diff_thresh: int = 4,
    rms_thresh: float = 0.6,
    precleaned_size: tuple[int, int] | None = None,
) -> bytes:
    """Encode an RGBA animation using target-edge-first quality preservation, still-frame merging, and fast convergence."""
    pil_cache: dict[tuple[int, int, int], Image.Image] = {}
    cached_size: tuple[int, int] | None = None
    seen: set[tuple] = set()
    best: bytes | None = None
    best_is_hq = False
    total_cs = max(2, round(duration * 100))
    target_edge = attempts[0][0]
    eff_fast_bytes = default_fast_bytes(target_edge) if fast_bytes is None else fast_bytes

    def get_pil(idx: int, w: int, h: int) -> Image.Image:
        if precleaned_size is not None and (w, h) == precleaned_size:
            return load_frame(idx, w, h)
        key = (w, h, idx)
        if key not in pil_cache:
            pil_cache[key] = _clean_rgba_alpha(load_frame(idx, w, h))
        return pil_cache[key]

    for edge, max_fps, colors, stats_mode, dither in attempts:
        is_hq = edge == target_edge and colors == 256
        if best is not None and best_is_hq and not is_hq:
            return best
        w, h = size_for_edge(edge)
        if cached_size is not None and cached_size != (w, h):
            # Attempts step monotonically down in edge size; drop larger cached frames immediately.
            pil_cache.clear()
        cached_size = (w, h)
        target_fps = min(max_fps, src_fps, 180.0 / max(duration, 0.1))
        n_out = max(1, min(180, round(duration * target_fps)))
        indices = sample_indices(n_out)
        kept_indices, kept_pils, kept_cs = _dedup_sampled_frames(
            indices, total_cs, w, h, get_pil,
            max_diff_thresh=max_diff_thresh, rms_thresh=rms_thresh,
        )
        # Lottie exports of static stickers often use ip=0, op=1 (totalframe=2 in rlottie) or identical frames.
        if best is None and len(kept_pils) == 1:
            static_edge = max(edge, static_max_edge or edge)
            if static_edge > edge:
                sw, sh = size_for_edge(static_edge)
                static_im = load_frame(kept_indices[0], sw, sh)
            else:
                static_im = kept_pils[0]
            eff_static_fast = default_fast_bytes(static_edge) if fast_bytes is None else fast_bytes
            return _encode_static_rgba_to_gif(static_im, static_edge, eff_static_fast, gifsicle)
        spec = (w, h, tuple(kept_indices), tuple(kept_cs), colors, stats_mode, dither)
        if spec in seen:
            continue
        seen.add(spec)
        if ffmpeg:
            actual_fps = max(1.0, len(kept_pils) / max(duration, 0.05))
            out = _ffmpeg_rgba_to_gif(kept_pils, w, h, actual_fps, ffmpeg, max_colors=colors,
                                      stats_mode=stats_mode, dither=dither)
            if len(kept_cs) < len(indices) or len(set(kept_cs)) > 1:
                out = _set_gif_frame_delays(out, kept_cs)
        else:
            pil_frames = [(im, cs * 10) for im, cs in zip(kept_pils, kept_cs)]
            out = _rgba_to_transparent_gif_pillow(pil_frames, max_edge=edge, max_colors=min(255, colors))
        out = _optimize_gif(out, eff_fast_bytes, gifsicle)
        if len(out) <= WX_STICKER_MAX_BYTES:
            if best is None or len(out) < len(best):
                best, best_is_hq = out, is_hq
            if len(best) <= eff_fast_bytes or not is_hq:
                return best
    if best is not None:
        return best
    raise ValueError("sticker_output_size_limit")


def webp_to_wechat_gif(
    data: bytes,
    ffmpeg: str | None,
    *,
    max_edge: int = WX_STICKER_SIZE,
    static_max_edge: int = WX_STICKER_STATIC_SIZE,
    fast_bytes: int | None = None,
) -> bytes:
    _ensure_pil()
    gifsicle = find_gifsicle()
    with Image.open(io.BytesIO(data)) as im:
        n_frames = getattr(im, "n_frames", 1)
        if n_frames <= 1:
            im.load()
            eff_fast = default_fast_bytes(static_max_edge) if fast_bytes is None else fast_bytes
            return _encode_static_rgba_to_gif(im, static_max_edge, eff_fast, gifsicle)

        orig_w, orig_h = im.size
        base_w, base_h = _fit_size(orig_w, orig_h, max_edge)
        src_frames: list[Image.Image] = []
        ends_ms: list[int] = []
        acc = 0
        max_ms = int(WX_STICKER_MAX_SECONDS * 1000)
        for frame in ImageSequence.Iterator(im):
            frame.load()
            d = max(20, int(frame.info.get("duration") or 50))
            if acc + d > max_ms and src_frames:
                break
            src_frames.append(_clean_rgba_alpha(_resize_rgba(frame, base_w, base_h)))
            acc += d
            ends_ms.append(acc)

    def load_scaled(idx: int, w: int, h: int) -> Image.Image:
        if (w, h) == (base_w, base_h):
            return src_frames[idx]
        if w > base_w or h > base_h:
            with Image.open(io.BytesIO(data)) as raw_im:
                if idx:
                    raw_im.seek(idx)
                return _resize_rgba(raw_im, w, h)
        return _resize_rgba(src_frames[idx], w, h)

    duration = max(0.05, acc / 1000.0)
    src_fps = len(src_frames) / duration
    return _encode_rgba_attempts_to_gif(
        _gif_attempts(duration, max_edge),
        duration,
        src_fps,
        lambda edge: _fit_size(orig_w, orig_h, edge),
        lambda n_out: [
            min(len(src_frames) - 1, bisect.bisect_right(ends_ms, (i + 0.5) * acc / n_out))
            for i in range(n_out)
        ],
        load_scaled,
        ffmpeg,
        gifsicle,
        fast_bytes=fast_bytes,
        static_max_edge=static_max_edge,
        precleaned_size=(base_w, base_h),
    )


def tgs_to_wechat_gif(
    data: bytes,
    ffmpeg: str | None,
    *,
    max_edge: int = WX_STICKER_SIZE,
    static_max_edge: int = WX_STICKER_STATIC_SIZE,
    fast_bytes: int | None = None,
) -> bytes:
    from rlottie_python import LottieAnimation
    anim = LottieAnimation.from_data(gzip.decompress(data).decode("utf-8"))
    try:
        try:
            # Flush and disable rlottie's internal C++ model cache so parsed animations do not linger in RAM.
            anim.lottie_configure_model_cache_size(0)
        except Exception:
            pass
        gifsicle = find_gifsicle()
        total = max(1, anim.lottie_animation_get_totalframe())
        src_fps = anim.lottie_animation_get_framerate() or 30
        if total == 1:
            w, h = _tgs_size(anim, static_max_edge)
            frame = _render_tgs_rgba(anim, 0, w, h)
            eff_fast = default_fast_bytes(static_max_edge) if fast_bytes is None else fast_bytes
            return _encode_static_rgba_to_gif(frame, static_max_edge, eff_fast, gifsicle)

        duration = min(WX_STICKER_MAX_SECONDS, total / src_fps)
        usable_src_frames = max(1, min(total, round(duration * src_fps)))
        return _encode_rgba_attempts_to_gif(
            _gif_attempts(duration, max_edge),
            duration,
            src_fps,
            lambda edge: _tgs_size(anim, edge),
            lambda n_out: [min(usable_src_frames - 1, int(i * usable_src_frames / n_out)) for i in range(n_out)],
            lambda idx, w, h: _render_tgs_rgba(anim, idx, w, h),
            ffmpeg,
            gifsicle,
            fast_bytes=fast_bytes,
            static_max_edge=static_max_edge,
        )
    finally:
        try:
            anim.lottie_animation_destroy()
        except Exception:
            pass


def _read_exact(stream, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = stream.read(size - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _read_pam_rgba_frames(stream, *, allow_empty: bool = False,
                          clean_alpha: bool = False) -> tuple[list[Image.Image], int, int]:
    _ensure_pil()
    frames: list[Image.Image] = []
    width, height = 0, 0
    while True:
        meta: dict[str, str] = {}
        saw_endhdr = False
        while True:
            line = stream.readline()
            if not line:
                break
            text = line.decode("ascii", errors="replace").strip()
            if text == "ENDHDR":
                saw_endhdr = True
                break
            if " " in text:
                k, v = text.split(None, 1)
                meta[k] = v
        if not saw_endhdr:
            break
        w = int(meta["WIDTH"])
        h = int(meta["HEIGHT"])
        depth = int(meta["DEPTH"])
        size = w * h * depth
        px = _read_exact(stream, size)
        if len(px) < size:
            break
        mode = "RGBA" if depth == 4 else "RGB"
        im = Image.frombytes(mode, (w, h), px)
        if mode != "RGBA":
            im = im.convert("RGBA")
        elif clean_alpha:
            im = _clean_rgba_alpha(im)
        width, height = w, h
        frames.append(im)
    if not frames and not allow_empty:
        raise ValueError("empty_video_stream")
    return frames, width, height


def _decode_pam_rgba_frames(buf: bytes) -> tuple[list[Image.Image], int, int]:
    return _read_pam_rgba_frames(io.BytesIO(buf))


def _decode_video_rgba_frames(
    data: bytes,
    ffmpeg: str,
    *,
    ext: str,
    input_args: tuple[str, ...],
    preserve_alpha: bool,
    max_edge: int,
    base_fps: float,
) -> tuple[list[Image.Image], int, int]:
    """Decode and scale video frames once into RGBA images so re-attempts avoid repeating video decode."""
    scale = (f"scale='min(iw\\,{max_edge})':'min(ih\\,{max_edge})'"
             ":force_original_aspect_ratio=decrease:flags=lanczos")
    if preserve_alpha:
        vf = f"fps={base_fps:g},format=rgba,premultiply=inplace=1,{scale},unpremultiply=inplace=1"
    else:
        vf = f"fps={base_fps:g},{scale},format=rgba"
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, f"in{ext}")
        err_path = os.path.join(tmp, "ffmpeg.err")
        with open(src, "wb") as f:
            f.write(data)
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               *input_args, "-i", src, "-t", f"{WX_STICKER_MAX_SECONDS:g}",
               "-vf", vf, "-c:v", "pam", "-f", "image2pipe", "pipe:1"]
        with open(err_path, "wb") as err_f:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_f)
            timed_out = False

            def _kill_on_timeout() -> None:
                nonlocal timed_out
                timed_out = True
                try:
                    proc.kill()
                except OSError:
                    pass

            timer = threading.Timer(120.0, _kill_on_timeout)
            timer.start()
            try:
                assert proc.stdout is not None
                try:
                    frames, width, height = _read_pam_rgba_frames(
                        proc.stdout, allow_empty=True, clean_alpha=preserve_alpha
                    )
                finally:
                    proc.stdout.close()
                returncode = proc.wait()
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            finally:
                timer.cancel()
        if timed_out:
            raise subprocess.TimeoutExpired(cmd, 120)
        if returncode != 0:
            with open(err_path, "rb") as err_f:
                stderr = err_f.read()
            raise subprocess.CalledProcessError(returncode, cmd, stderr=stderr)
        if not frames:
            raise ValueError("empty_video_stream")
        return frames, width, height


def _transcode_video_to_wechat_gif(
    data: bytes,
    ffmpeg: str,
    *,
    ext: str,
    input_args: tuple[str, ...] = (),
    preserve_alpha: bool = True,
    max_edge: int = WX_STICKER_SIZE,
    video_attempts: bool = False,
    fast_bytes: int | None = None,
) -> bytes:
    gifsicle = find_gifsicle()
    attempts = _gif_attempts(0.0, max_edge, video=video_attempts)
    base_fps = attempts[0][1]
    base_edge = attempts[0][0]
    src_frames, orig_w, orig_h = _decode_video_rgba_frames(
        data, ffmpeg, ext=ext, input_args=input_args,
        preserve_alpha=preserve_alpha, max_edge=base_edge, base_fps=base_fps,
    )
    duration = max(0.05, len(src_frames) / base_fps)
    if not video_attempts:
        attempts = _gif_attempts(duration, max_edge, video=False)
    return _encode_rgba_attempts_to_gif(
        attempts,
        duration,
        base_fps,
        lambda edge: _fit_size(orig_w, orig_h, edge),
        lambda n_out: [min(len(src_frames) - 1, int(i * len(src_frames) / n_out)) for i in range(n_out)],
        lambda idx, w, h: src_frames[idx] if (w, h) == (orig_w, orig_h) else _resize_rgba(src_frames[idx], w, h),
        ffmpeg,
        gifsicle,
        fast_bytes=fast_bytes,
        max_diff_thresh=8,
        rms_thresh=1.2,
        precleaned_size=(orig_w, orig_h),
    )


def webm_to_wechat_gif(
    data: bytes,
    ffmpeg: str,
    *,
    max_edge: int = WX_STICKER_SIZE,
    fast_bytes: int | None = None,
) -> bytes:
    return _transcode_video_to_wechat_gif(
        data, ffmpeg, ext=".webm", input_args=("-c:v", "libvpx-vp9"), preserve_alpha=True,
        max_edge=max_edge, fast_bytes=fast_bytes,
    )


def gif_media_to_wechat_gif(
    data: bytes,
    mime: str,
    ffmpeg: str | None,
    *,
    max_edge: int = WX_STICKER_SIZE,
    static_max_edge: int = WX_STICKER_STATIC_SIZE,
    fast_bytes: int | None = None,
) -> bytes:
    """Convert a Telegram GIF (MP4, WebM, or GIF) into a <=max_edge <=1MB WeChat sticker GIF."""
    eff_fast = default_fast_bytes(max_edge) if fast_bytes is None else fast_bytes
    if mime == "image/gif" or data[:6] in (b"GIF87a", b"GIF89a"):
        _ensure_pil()
        im = Image.open(io.BytesIO(data))
        n_frames = getattr(im, "n_frames", 1)
        if n_frames <= 1:
            return webp_to_wechat_gif(
                data, ffmpeg, max_edge=max_edge, static_max_edge=static_max_edge, fast_bytes=fast_bytes,
            )
        total_ms = sum(max(20, int(f.info.get("duration") or 50)) for f in ImageSequence.Iterator(im))
        compliant = (
            max(im.size) <= max_edge
            and len(data) <= WX_STICKER_MAX_BYTES
            and n_frames <= WX_STICKER_MAX_FRAMES
            and total_ms <= int(WX_STICKER_MAX_SECONDS * 1000)
        )
        if compliant and len(data) <= eff_fast:
            return data
        if ffmpeg:
            out = _transcode_video_to_wechat_gif(
                data, ffmpeg, ext=".gif", preserve_alpha=True, max_edge=max_edge, fast_bytes=fast_bytes,
            )
        else:
            out = webp_to_wechat_gif(
                data, None, max_edge=max_edge, static_max_edge=static_max_edge, fast_bytes=fast_bytes,
            )
        return data if compliant and len(data) <= len(out) else out
    if not ffmpeg:
        raise ValueError("ffmpeg_missing")
    if mime == "video/webm":
        return webm_to_wechat_gif(data, ffmpeg, max_edge=max_edge, fast_bytes=fast_bytes)
    return _transcode_video_to_wechat_gif(
        data, ffmpeg, ext=".mp4", preserve_alpha=False, max_edge=max_edge, video_attempts=True,
        fast_bytes=fast_bytes,
    )


class StickerDiskCache:
    """Bounded on-disk LRU cache mapping Telegram media digests to converted WeChat sticker GIFs."""

    def __init__(self, root: str | os.PathLike[str], max_bytes: int = STICKER_CACHE_MAX_BYTES):
        self.root = Path(root)
        self.max_bytes = max(0, max_bytes)

    def _path_for(self, key: str) -> Path:
        digest = hashlib.sha256(f"v{STICKER_CACHE_VERSION}:{key}".encode("utf-8")).hexdigest()[:32]
        return self.root / f"v{STICKER_CACHE_VERSION}_{digest}.gif"

    def get(self, key: str) -> bytes | None:
        if self.max_bytes <= 0:
            return None
        path = self._path_for(key)
        try:
            st = path.stat()
            if not (0 < st.st_size <= WX_STICKER_MAX_BYTES):
                return None
            data = path.read_bytes()
            os.utime(path, None)
            return data
        except OSError:
            return None

    def put(self, key: str, data: bytes) -> None:
        if self.max_bytes <= 0 or not (0 < len(data) <= WX_STICKER_MAX_BYTES):
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            dst = self._path_for(key)
            fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".gif", dir=self.root)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp_name, dst)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            self._prune()
        except OSError as e:
            log.debug("Sticker disk cache write failed: %s", e)

    def _prune(self) -> None:
        try:
            entries: list[tuple[int, int, Path]] = []
            total = 0
            for p in self.root.iterdir():
                if not p.is_file() or p.suffix != ".gif" or p.name.startswith(".tmp-"):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                total += st.st_size
                entries.append((st.st_mtime_ns, st.st_size, p))
            if total <= self.max_bytes:
                return
            entries.sort(key=lambda item: item[0])
            for _, size, p in entries:
                if total <= self.max_bytes:
                    break
                try:
                    p.unlink()
                    total -= size
                except OSError:
                    pass
        except OSError:
            pass


async def _run_wechat_sticker_convert(
    label: str,
    mime: str,
    data: bytes,
    fn: Callable[[], bytes],
    *,
    cache: StickerDiskCache | None = None,
    cache_key: str | None = None,
) -> tuple[str, bytes] | str:
    key = cache_key or f"{label}:{mime}:{hashlib.sha256(data).hexdigest()}"
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            log.debug("Sticker cache hit for %s %s (%d B)", mime, label, len(cached))
            return "sticker", cached
    t0 = time.monotonic()
    try:
        out = await run_in_media_executor(fn)
        if cache is not None:
            cache.put(key, out)
        try:
            _ensure_pil()
            with Image.open(io.BytesIO(out)) as gim:
                log.info("Converted %s %s (%d B) -> %dx%d GIF (%d frames, %d B) in %.2fs",
                         mime, label, len(data), gim.size[0], gim.size[1], getattr(gim, "n_frames", 1),
                         len(out), time.monotonic() - t0)
        except Exception:
            pass
        return "sticker", out
    except Exception as e:
        log.warning("WeChat %s conversion failed (%s): %s", label, mime, e)
        return f"贴纸转换失败：{type(e).__name__}"
    finally:
        maybe_trim_memory()


async def to_wechat_sticker(
    data: bytes,
    mime: str,
    *,
    cache: StickerDiskCache | None = None,
    cache_key: str | None = None,
    max_edge: int = WX_STICKER_SIZE,
    static_max_edge: int = WX_STICKER_STATIC_SIZE,
    fast_bytes: int | None = None,
) -> tuple[str, bytes] | str:
    """Convert a Telegram sticker (.webp / .tgs / .webm) to a transparent GIF for WeChat."""
    ffmpeg = find_ffmpeg()
    eff_fast = default_fast_bytes(max_edge) if fast_bytes is None else fast_bytes
    key = cache_key or f"sticker:{mime}:{max_edge}:{static_max_edge}:{eff_fast}:{hashlib.sha256(data).hexdigest()}"
    if mime == "image/webp":
        return await _run_wechat_sticker_convert(
            "sticker", mime, data,
            lambda: webp_to_wechat_gif(
                data, ffmpeg, max_edge=max_edge, static_max_edge=static_max_edge, fast_bytes=fast_bytes,
            ),
            cache=cache, cache_key=key,
        )
    if mime == "application/x-tgsticker":
        return await _run_wechat_sticker_convert(
            "sticker", mime, data,
            lambda: tgs_to_wechat_gif(
                data, ffmpeg, max_edge=max_edge, static_max_edge=static_max_edge, fast_bytes=fast_bytes,
            ),
            cache=cache, cache_key=key,
        )
    if mime == "video/webm":
        if not ffmpeg:
            return "需要 ffmpeg 才能转换视频贴纸"
        return await _run_wechat_sticker_convert(
            "sticker", mime, data,
            lambda: webm_to_wechat_gif(data, ffmpeg, max_edge=max_edge, fast_bytes=fast_bytes),
            cache=cache, cache_key=key,
        )
    return f"不支持的贴纸格式 {mime}"


async def gif_to_wechat_sticker(
    data: bytes,
    mime: str,
    *,
    cache: StickerDiskCache | None = None,
    cache_key: str | None = None,
    max_edge: int = WX_STICKER_SIZE,
    static_max_edge: int = WX_STICKER_STATIC_SIZE,
    fast_bytes: int | None = None,
) -> tuple[str, bytes] | str:
    """Convert a Telegram GIF (MP4/GIF/WebM) to a WeChat sticker GIF."""
    ffmpeg = find_ffmpeg()
    is_gif_bytes = mime == "image/gif" or data[:6] in (b"GIF87a", b"GIF89a")
    if not is_gif_bytes and not ffmpeg:
        return "需要 ffmpeg 才能转换动态表情"
    eff_fast = default_fast_bytes(max_edge) if fast_bytes is None else fast_bytes
    key = cache_key or f"GIF:{mime}:{max_edge}:{static_max_edge}:{eff_fast}:{hashlib.sha256(data).hexdigest()}"
    return await _run_wechat_sticker_convert(
        "GIF", mime, data,
        lambda: gif_media_to_wechat_gif(
            data, mime, ffmpeg, max_edge=max_edge, static_max_edge=static_max_edge, fast_bytes=fast_bytes,
        ),
        cache=cache, cache_key=key,
    )


def wechat_image_to_tg_webp(im: Image.Image) -> tuple[bytes, int, int]:
    rgba = im.convert("RGBA")
    w, h = _fit_size(rgba.size[0], rgba.size[1], TG_STICKER_SIZE, upscale=True)
    if (w, h) != rgba.size:
        rgba = _resize_rgba(rgba, w, h)
    out = io.BytesIO()
    rgba.save(out, "WEBP", quality=92, method=6)
    return out.getvalue(), w, h


def wechat_gif_to_tg_webm(data: bytes, ffmpeg: str) -> tuple[bytes, int, int, float] | None:
    _ensure_pil()
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
        src = os.path.join(tmp, "in.gif")
        with open(src, "wb") as f:
            f.write(data)
        vf = f"fps={target_fps},scale={out_w}:{out_h}:flags=lanczos,format=yuva420p"
        out = _encode_tg_webm(ffmpeg, ["-i", src], ["-vf", vf], os.path.join(tmp, "out.webm"))
    return (out, out_w, out_h, out_dur) if out is not None else None


def _encode_tg_webm(ffmpeg: str, input_args: list[str], filter_args: list[str], out_path: str) -> bytes | None:
    """Encode to a <= 3 s VP9 webm (alpha kept), lowering quality until it fits Telegram's 256 KB cap."""
    for crf, bitrate in ((20, "650k"), (26, "450k"), (34, "280k"), (42, "150k")):
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               *input_args, "-t", f"{TG_VIDEO_STICKER_MAX_SECONDS:g}", *filter_args,
               "-c:v", "libvpx-vp9", "-b:v", bitrate, "-crf", str(crf),
               "-deadline", "good", "-cpu-used", "2",
               "-auto-alt-ref", "0", "-an", out_path]
        res = subprocess.run(cmd, capture_output=True, timeout=120)
        if res.returncode != 0:
            log.info("Telegram video sticker encode failed: %s", res.stderr.decode(errors="replace").strip()[-300:])
            return None
        with open(out_path, "rb") as f:
            out = f.read()
        if len(out) <= TG_VIDEO_STICKER_MAX_BYTES:
            return out
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
            try:
                return wxgf_to_tg_sticker(data, ffmpeg)
            except Exception as e:
                log.info("Could not decode wxgf sticker animation (%s); sending its first frame", e)
            webp, w, h = wechat_image_to_tg_webp(wxgf_to_image(data, ffmpeg))
            return "static", webp, {"w": w, "h": h}
        if mime == "image/gif":
            _ensure_pil()
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
            _ensure_pil()
            im = Image.open(io.BytesIO(data))
            im.load()
            webp, w, h = wechat_image_to_tg_webp(im)
            return "static", webp, {"w": w, "h": h}
    except Exception as e:
        log.info("Could not convert WeChat sticker (%s) to Telegram sticker: %s", mime, e)
    return None


_FFMPEG_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def extract_video_meta_and_thumb(data: bytes, filename: str = "video.mp4", *,
                                 path: str | None = None) -> tuple[int, int, float, bytes] | None:
    """Extract display (width, height, duration_s, jpeg_thumb_bytes) from a video using ffmpeg.

    Reads the video from `path` when given (large downloads stay on disk), else from `data`.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg or not (data or path):
        return None
    ext = os.path.splitext(filename)[1].lower() if "." in filename else ".mp4"
    if not ext or not re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
        ext = ".mp4"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            frame = os.path.join(tmp, "frame.jpg")
            if path:
                src = path
            else:
                src = os.path.join(tmp, f"in{ext}")
                with open(src, "wb") as f:
                    f.write(data)
            res = subprocess.run(
                [ffmpeg, "-hide_banner", "-y", "-i", src, "-frames:v", "1", "-q:v", "3", frame],
                capture_output=True, text=True, errors="replace", timeout=30,
            )
            if res.returncode != 0 or not os.path.exists(frame):
                return None
            _ensure_pil()
            with Image.open(frame) as im:
                im.load()
                w, h = im.size
                thumb_im = im.convert("RGB")
                thumb_im.thumbnail((320, 320), Image.LANCZOS)
                buf = io.BytesIO()
                thumb_im.save(buf, "JPEG", quality=85)
                thumb_bytes = buf.getvalue()
        m = _FFMPEG_DURATION_RE.search(res.stderr or "")
        duration = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 0.0
        return w, h, duration, thumb_bytes
    except Exception as e:
        log.info("Could not extract video metadata/thumbnail: %s", e)
        return None
