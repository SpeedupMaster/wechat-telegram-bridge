"""Telegram stickers -> something WeChat can show.

agent-wechat cannot send WeChat stickers (表情), and it re-encodes every image it sends as JPEG,
which drops transparency and animation. So:
  - static stickers (.webp)        -> JPEG on a white background, sent as an image
  - video stickers (.webm, VP9)    -> short MP4 on white, sent as a video
  - animated stickers (.tgs Lottie) -> rendered with rlottie into a short MP4, sent as a video
"""
from __future__ import annotations

import asyncio
import gzip
import io
import logging
import math
import os
import shutil
import subprocess
import tempfile

from PIL import Image

log = logging.getLogger("stickers")

SIZE = 384          # output video is SIZE x SIZE; Telegram stickers are at most 512 x 512
MIN_SECONDS = 3.0   # short animations are repeated until the video lasts about this long
MAX_LOOPS = 4


def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:  # optional, for running without a system ffmpeg (e.g. local tests on Windows)
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


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


def tgs_first_frame_jpeg(data: bytes) -> bytes:
    from rlottie_python import LottieAnimation
    anim = LottieAnimation.from_data(gzip.decompress(data).decode("utf-8"))
    out = io.BytesIO()
    on_white(anim.render_pillow_frame(frame_num=0, width=SIZE, height=SIZE)).save(out, "JPEG", quality=92)
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


def _encode_cmd(ffmpeg: str, source: list[str], out: str, fps: float) -> list[str]:
    """Input 0 is a white background, input 1 the sticker (with alpha), centred and padded."""
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
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out.mp4")
        source = ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{SIZE}x{SIZE}", "-r", f"{fps:g}", "-i", "pipe:0"]
        proc = subprocess.Popen(_encode_cmd(ffmpeg, source, out, fps), stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            frames = [anim.render_pillow_frame(frame_num=i, width=SIZE, height=SIZE).convert("RGBA").tobytes()
                      for i in range(total)]
            for _ in range(loops):
                for frame in frames:
                    proc.stdin.write(frame)
            proc.stdin.close()
            _, err = proc.communicate(timeout=120)
        except BaseException:
            proc.kill()
            raise
        if proc.returncode != 0:
            raise RuntimeError(err.decode(errors="replace").strip() or f"ffmpeg exited {proc.returncode}")
        with open(out, "rb") as f:
            return f.read()


async def convert(data: bytes, mime: str) -> tuple[str, bytes] | str:
    """("image" | "video", bytes) ready for WeChat, or a reason it could not be converted."""
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
