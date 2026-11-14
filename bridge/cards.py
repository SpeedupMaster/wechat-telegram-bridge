"""Render WeChat link cards into compact WeChat-style PNG cards for Telegram."""
from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import stickers

CARD_WIDTH = 800
PAD_X = 34
PAD_TOP = 30
PAD_BOTTOM = 26
TITLE_SIZE = 36
TITLE_LINE_H = 48
DESC_SIZE = 26
DESC_LINE_H = 38
SOURCE_SIZE = 24
BADGE_SIZE = 30
THUMB_SIZE = 136
THUMB_RADIUS = 12
THUMB_GAP = 24
SECTION_GAP = 14
FOOTER_GAP = 18

BG_COLOR = (255, 255, 255)
TITLE_COLOR = (25, 25, 25)
DESC_COLOR = (122, 122, 122)
SOURCE_COLOR = (122, 122, 122)
BORDER_COLOR = (234, 234, 234)

_REGULAR_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "C:/Windows/Fonts/msyh.ttc",
)

_BOLD_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansSC-Bold.otf",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
)

_EMOJI_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
)

_LATIN_FALLBACK_REGULAR = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)
_LATIN_FALLBACK_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)

_VARIATION_SELECTORS = ("\ufe0e", "\ufe0f")
_SKIN_TONE_MODIFIERS = set(chr(cp) for cp in range(0x1F3FB, 0x1F400))
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")


@dataclass(frozen=True)
class LinkSource:
    label: str
    brand: str
    color: tuple[int, int, int]
    button_label: str


def _clean_host(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host.removeprefix("www.")


def identify_link_source(link: dict | None, app_type: int | None = None) -> LinkSource:
    """Resolve display source name, brand badge style, and Telegram CTA button label."""
    link = link or {}
    source_name = " ".join((link.get("sourceName") or "").split())
    url = (link.get("url") or "").strip()
    host = _clean_host(url)

    def has_domain(*domains: str) -> bool:
        return any(host == d or host.endswith("." + d) for d in domains)

    if has_domain("xiaohongshu.com", "xhslink.com") or source_name == "小红书":
        return LinkSource("小红书", "xhs", (255, 36, 66), "📕 打开小红书")
    if has_domain("mp.weixin.qq.com"):
        return LinkSource(source_name or "微信公众号", "wxmp", (7, 193, 96), "📄 阅读全文")
    if has_domain("bilibili.com", "b23.tv"):
        return LinkSource(source_name or "哔哩哔哩", "bili", (251, 114, 153), "📺 打开哔哩哔哩")
    if has_domain("zhihu.com"):
        return LinkSource(source_name or "知乎", "zhihu", (0, 102, 255), "💡 打开知乎")
    if has_domain("douyin.com", "iesdouyin.com"):
        return LinkSource(source_name or "抖音", "douyin", (24, 24, 32), "🎬 打开抖音")
    if has_domain("weibo.com", "weibo.cn"):
        return LinkSource(source_name or "微博", "weibo", (230, 22, 45), "🔗 打开微博")
    if has_domain("github.com"):
        return LinkSource(source_name or "GitHub", "github", (36, 41, 47), "🔗 打开 GitHub")
    if has_domain("youtube.com", "youtu.be"):
        return LinkSource(source_name or "YouTube", "video", (255, 0, 0), "🎬 打开视频")
    if has_domain("music.163.com", "y.qq.com") or app_type == 3:
        return LinkSource(source_name or host or "音乐", "music", (230, 57, 70), "🎵 打开音乐")
    if app_type == 4:
        return LinkSource(source_name or host or "视频", "video", (59, 130, 246), "🎬 打开视频")
    return LinkSource(source_name or host or "网页链接", "link", (87, 107, 149), "🔗 打开链接")


def _ttc_sc_index(path: str) -> int:
    if not path.lower().endswith(".ttc"):
        return 0
    for idx in range(6):
        try:
            f = ImageFont.truetype(path, 24, index=idx)
            name = " ".join(str(x) for x in f.getname()).upper()
            if "SC" in name or "PINGFANG" in name or "MICROHEI" in name or "ZENHEI" in name:
                return idx
        except Exception:
            break
    return 0


@lru_cache(maxsize=1)
def _font_paths() -> tuple[str | None, str | None, str | None, bool]:
    reg = next((p for p in _REGULAR_FONT_CANDIDATES if os.path.isfile(p)), None)
    bold = next((p for p in _BOLD_FONT_CANDIDATES if os.path.isfile(p)), None) or reg
    has_cjk = reg is not None
    if reg is None:
        reg = next((p for p in _LATIN_FALLBACK_REGULAR if os.path.isfile(p)), None)
    if bold is None:
        bold = next((p for p in _LATIN_FALLBACK_BOLD if os.path.isfile(p)), None) or reg
    emoji = next((p for p in _EMOJI_FONT_CANDIDATES if os.path.isfile(p)), None)
    return reg, bold, emoji, has_cjk


@lru_cache(maxsize=16)
def _load_font(path: str | None, size: int, *, index: int | None = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if path:
        idx = _ttc_sc_index(path) if index is None else index
        try:
            return ImageFont.truetype(path, size, index=idx)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


@lru_cache(maxsize=1)
def _emoji_font() -> ImageFont.FreeTypeFont | None:
    _, _, emoji_path, _ = _font_paths()
    if not emoji_path:
        return None
    for strike in (109, 128, 72, 64):
        try:
            return ImageFont.truetype(emoji_path, strike)
        except Exception:
            continue
    return None


def _is_emoji_base(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or cp in (0x231A, 0x231B, 0x23E9, 0x23EA, 0x23EB, 0x23EC, 0x23F0, 0x23F3,
                  0x25FD, 0x25FE, 0x2B50, 0x2B55, 0x3030, 0x303D, 0x3297, 0x3299)
    )


def _iter_clusters(text: str) -> list[tuple[str, bool]]:
    """Split text into grapheme-like clusters tagged with whether they are emoji."""
    out: list[tuple[str, bool]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _VARIATION_SELECTORS or ch == "\u200d":
            i += 1
            continue
        is_emoji = _is_emoji_base(ch)
        cluster = ch
        i += 1
        while i < n:
            nxt = text[i]
            if nxt in _VARIATION_SELECTORS:
                if nxt == "\ufe0f" and (is_emoji or ord(ch) >= 0x2190):
                    is_emoji = True
                cluster += nxt
                i += 1
            elif nxt in _SKIN_TONE_MODIFIERS and is_emoji:
                cluster += nxt
                i += 1
            elif nxt == "\u200d" and i + 1 < n:
                is_emoji = True
                cluster += nxt + text[i + 1]
                i += 2
            else:
                break
        out.append((cluster, is_emoji))
    return out


@lru_cache(maxsize=256)
def _render_emoji_glyph(cluster: str, size: int) -> Image.Image | None:
    efont = _emoji_font()
    if efont is None:
        return None
    canvas = Image.new("RGBA", (180, 180), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    try:
        draw.text((20, 20), cluster, font=efont, embedded_color=True)
    except Exception:
        return None
    bbox = canvas.getbbox()
    if not bbox:
        return None
    cropped = canvas.crop(bbox)
    cw, ch = cropped.size
    target = max(1, round(size * 0.96))
    scale = target / max(cw, ch)
    nw, nh = max(1, round(cw * scale)), max(1, round(ch * scale))
    return cropped.convert("RGBa").resize((nw, nh), Image.LANCZOS).convert("RGBA")


def _cluster_advance(cluster: str, is_emoji: bool, font, size: int) -> float:
    if is_emoji:
        if _render_emoji_glyph(cluster, size) is not None:
            return size * 1.08
        return 0.0
    return float(font.getlength(cluster))


def _measure_text(text: str, font, size: int) -> float:
    return sum(_cluster_advance(c, e, font, size) for c, e in _iter_clusters(text))


def _wrap_lines(text: str, font, size: int, max_width: int, max_lines: int) -> list[str]:
    clean = " ".join((text or "").split())
    if not clean or max_lines <= 0:
        return []
    clusters = _iter_clusters(clean)
    # Remove unrenderable emoji clusters up front so truncation & wrapping stay accurate.
    kept: list[tuple[str, bool, float]] = []
    for cl, is_emoji in clusters:
        adv = _cluster_advance(cl, is_emoji, font, size)
        if adv > 0 or cl == " ":
            kept.append((cl, is_emoji, adv))
    if not kept:
        return []

    lines: list[list[tuple[str, bool, float]]] = []
    cur: list[tuple[str, bool, float]] = []
    cur_w = 0.0
    ell_w = float(font.getlength("…"))

    i = 0
    while i < len(kept):
        cl, is_emoji, adv = kept[i]
        if not cur and cl == " ":
            i += 1
            continue
        # Keep short ASCII/hashtag tokens together on a new line when possible.
        if cl.isascii() and cl not in (" ", "#") and (not cur or cur[-1][0] in (" ", "#")):
            j = i
            tok_w = 0.0
            while j < len(kept) and kept[j][0].isascii() and kept[j][0] != " ":
                tok_w += kept[j][2]
                j += 1
            if cur and cur_w + tok_w > max_width and tok_w <= max_width:
                lines.append(cur)
                cur = []
                cur_w = 0.0
                if len(lines) == max_lines:
                    break
                continue
        if cur_w + adv <= max_width or not cur:
            cur.append((cl, is_emoji, adv))
            cur_w += adv
            i += 1
        else:
            lines.append(cur)
            cur = []
            cur_w = 0.0
            if len(lines) == max_lines:
                break

    if len(lines) < max_lines and cur:
        lines.append(cur)
        truncated = False
    else:
        truncated = i < len(kept)

    if truncated and lines:
        last = lines[-1]
        while last and last[-1][0] == " ":
            last.pop()
        last_w = sum(x[2] for x in last)
        while last and last_w + ell_w > max_width:
            popped = last.pop()
            last_w -= popped[2]
        while last and last[-1][0] == " ":
            last.pop()
        last.append(("…", False, ell_w))

    return ["".join(c for c, _, _ in ln).rstrip() for ln in lines if ln]


def _draw_mixed_text(canvas: Image.Image, draw: ImageDraw.ImageDraw, pos: tuple[int, int],
                     text: str, font, size: int, fill: tuple[int, int, int]) -> None:
    x, y = float(pos[0]), float(pos[1])
    buf: list[str] = []

    def flush() -> None:
        nonlocal x
        if not buf:
            return
        chunk = "".join(buf)
        draw.text((round(x), round(y)), chunk, font=font, fill=fill)
        x += float(font.getlength(chunk))
        buf.clear()

    for cl, is_emoji in _iter_clusters(text):
        if is_emoji:
            glyph = _render_emoji_glyph(cl, size)
            if glyph is None:
                continue
            flush()
            gx = round(x + max(0.0, (size * 1.08 - glyph.width) / 2))
            gy = round(y + max(0.0, (size * 1.08 - glyph.height) / 2))
            canvas.paste(glyph, (gx, gy), glyph)
            x += size * 1.08
        else:
            buf.append(cl)
    flush()


def _decode_thumb(data: bytes) -> Image.Image | None:
    try:
        if data.startswith(b"wxgf"):
            ffmpeg = stickers.find_ffmpeg()
            if not ffmpeg:
                return None
            im = stickers.wxgf_to_image(data, ffmpeg)
        else:
            im = Image.open(io.BytesIO(data))
            im.load()
            im = ImageOps.exif_transpose(im)
        return stickers.on_white(im)
    except Exception:
        return None


def _draw_rounded_thumb(canvas: Image.Image, thumb: Image.Image, box: tuple[int, int, int, int], radius: int) -> None:
    x, y, size = box[0], box[1], box[2] - box[0]
    scale = 4
    fitted = ImageOps.fit(thumb, (size, size), Image.LANCZOS)
    mask = Image.new("L", (size * scale, size * scale), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle((0, 0, size * scale - 1, size * scale - 1), radius=radius * scale, fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    canvas.paste(fitted, (x, y), mask)

    border = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(border)
    bdraw.rounded_rectangle(
        (0, 0, size * scale - 1, size * scale - 1),
        radius=radius * scale,
        outline=(*BORDER_COLOR, 255),
        width=scale,
    )
    border = border.resize((size, size), Image.LANCZOS)
    canvas.paste(border, (x, y), border)


def _draw_brand_badge(canvas: Image.Image, x: int, y: int, size: int, source: LinkSource,
                      bold_path: str | None, has_cjk: bool) -> None:
    scale = 4
    s = size * scale
    badge = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(badge)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=round(s * 0.24), fill=(*source.color, 255))

    white = (255, 255, 255, 255)
    if source.brand == "xhs":
        if has_cjk and bold_path:
            f = _load_font(bold_path, round(s * 0.28))
            d.text((s // 2, round(s * 0.52)), "小红书", font=f, fill=white, anchor="mm")
        else:
            f = _load_font(bold_path, round(s * 0.34))
            d.text((s // 2, round(s * 0.52)), "RED", font=f, fill=white, anchor="mm")
    elif source.brand == "zhihu" and (has_cjk or not source.label.isascii()):
        f = _load_font(bold_path, round(s * 0.54))
        d.text((s // 2, round(s * 0.50)), "知", font=f, fill=white, anchor="mm")
    elif source.brand == "wxmp":
        for idx, ratio in enumerate((0.34, 0.50, 0.66)):
            right = 0.56 if idx == 2 else 0.74
            d.rounded_rectangle(
                (round(s * 0.26), round(s * ratio - s * 0.045), round(s * right), round(s * ratio + s * 0.045)),
                radius=round(s * 0.04),
                fill=white,
            )
    elif source.brand in ("bili", "video", "douyin"):
        pts = [(round(s * 0.38), round(s * 0.30)), (round(s * 0.38), round(s * 0.70)), (round(s * 0.72), round(s * 0.50))]
        d.polygon(pts, fill=white)
    elif source.brand == "music":
        d.ellipse((round(s * 0.28), round(s * 0.56), round(s * 0.48), round(s * 0.74)), fill=white)
        d.ellipse((round(s * 0.54), round(s * 0.50), round(s * 0.74), round(s * 0.68)), fill=white)
        d.rectangle((round(s * 0.43), round(s * 0.28), round(s * 0.48), round(s * 0.65)), fill=white)
        d.rectangle((round(s * 0.69), round(s * 0.24), round(s * 0.74), round(s * 0.59)), fill=white)
        d.polygon([(round(s * 0.43), round(s * 0.28)), (round(s * 0.74), round(s * 0.22)),
                   (round(s * 0.74), round(s * 0.31)), (round(s * 0.43), round(s * 0.37))], fill=white)
    else:
        # Clean diagonal link arrow
        w = max(2, round(s * 0.09))
        d.line([(round(s * 0.32), round(s * 0.68)), (round(s * 0.68), round(s * 0.32))], fill=white, width=w)
        d.line([(round(s * 0.44), round(s * 0.32)), (round(s * 0.68), round(s * 0.32))], fill=white, width=w)
        d.line([(round(s * 0.68), round(s * 0.32)), (round(s * 0.68), round(s * 0.56))], fill=white, width=w)

    badge = badge.resize((size, size), Image.LANCZOS)
    canvas.paste(badge, (x, y), badge)


def render_link_card(
    *,
    title: str | None,
    description: str | None,
    url: str | None,
    source_name: str | None = None,
    app_type: int | None = None,
    thumbnail_bytes: bytes | None = None,
) -> bytes | None:
    """Render a WeChat-style horizontal link card PNG, or None if CJK text lacks a CJK font."""
    reg_path, bold_path, _, has_cjk = _font_paths()
    source = identify_link_source({"sourceName": source_name, "url": url}, app_type)
    title_text = " ".join((title or "").split()) or source.label
    desc_text = " ".join((description or "").split())
    if not desc_text and url:
        host = _clean_host(url)
        if host and host != source.label.lower():
            desc_text = host

    if not has_cjk and any(_NON_ASCII_RE.search(t) for t in (title_text, desc_text, source.label)):
        return None

    title_font = _load_font(bold_path, TITLE_SIZE)
    desc_font = _load_font(reg_path, DESC_SIZE)
    source_font = _load_font(reg_path, SOURCE_SIZE)

    thumb = _decode_thumb(thumbnail_bytes) if thumbnail_bytes else None

    inner_w = CARD_WIDTH - PAD_X * 2
    title_lines = _wrap_lines(title_text, title_font, TITLE_SIZE, inner_w, max_lines=2)
    desc_w = (inner_w - THUMB_SIZE - THUMB_GAP) if thumb is not None else inner_w
    desc_lines = _wrap_lines(desc_text, desc_font, DESC_SIZE, desc_w, max_lines=3) if desc_text else []

    title_h = len(title_lines) * TITLE_LINE_H
    desc_h = len(desc_lines) * DESC_LINE_H
    body_h = max(desc_h, THUMB_SIZE if thumb is not None else 0)
    has_body = body_h > 0
    total_h = (
        PAD_TOP
        + title_h
        + (SECTION_GAP + body_h if has_body else 0)
        + FOOTER_GAP
        + BADGE_SIZE
        + PAD_BOTTOM
    )

    canvas = Image.new("RGB", (CARD_WIDTH, total_h), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    y = PAD_TOP
    for line in title_lines:
        _draw_mixed_text(canvas, draw, (PAD_X, y), line, title_font, TITLE_SIZE, TITLE_COLOR)
        y += TITLE_LINE_H

    if has_body:
        y += SECTION_GAP
        dy = y
        for line in desc_lines:
            _draw_mixed_text(canvas, draw, (PAD_X, dy), line, desc_font, DESC_SIZE, DESC_COLOR)
            dy += DESC_LINE_H
        if thumb is not None:
            tx = CARD_WIDTH - PAD_X - THUMB_SIZE
            _draw_rounded_thumb(canvas, thumb, (tx, y, tx + THUMB_SIZE, y + THUMB_SIZE), THUMB_RADIUS)
        y += body_h

    y += FOOTER_GAP
    _draw_brand_badge(canvas, PAD_X, y, BADGE_SIZE, source, bold_path, has_cjk)
    try:
        bbox = source_font.getbbox(source.label)
        ink_mid = (bbox[1] + bbox[3]) / 2
        source_y = round(y + BADGE_SIZE / 2 - ink_mid)
    except Exception:
        source_y = y + max(0, (BADGE_SIZE - SOURCE_SIZE) // 2) - 3
    _draw_mixed_text(canvas, draw, (PAD_X + BADGE_SIZE + 12, source_y), source.label,
                     source_font, SOURCE_SIZE, SOURCE_COLOR)

    out = io.BytesIO()
    canvas.save(out, "PNG", optimize=True)
    return out.getvalue()
