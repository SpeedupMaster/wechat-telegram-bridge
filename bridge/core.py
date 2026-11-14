"""WeChat (agent-wechat) <-> Telegram bridge: one Telegram group per WeChat chat."""
from __future__ import annotations

import asyncio
import html
import io
import logging
import re
import secrets
import shutil
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

import httpx
from telethon import Button, TelegramClient, errors, events, functions, types, utils

from .config import Config
from . import cards, stickers
from .login import LoginMixin
from .store import Store
from .wechat import (APP, APP_CHANNELS, APP_FILE, APP_QUOTE, APP_UPLOAD_NOTICE, IMAGE, LOCATION, SEND_OK, STICKER,
                     SYSTEM, TEXT, VIDEO, VOICE, CursorExpired, Media, WeChatClient, WeChatError)

log = logging.getLogger("bridge")

TG_TEXT_LIMIT = 4000
TG_CAPTION_LIMIT = 1000
DEAD_CHAT_ERRORS = (errors.ChatWriteForbiddenError, errors.ChatIdInvalidError, errors.PeerIdInvalidError,
                    errors.ChannelPrivateError, errors.ChatAdminRequiredError)


def _dead_chat(e: Exception) -> bool:
    # Telethon raises ValueError("Could not find the input entity ...") for chats it cannot reach.
    return isinstance(e, DEAD_CHAT_ERRORS) or (isinstance(e, ValueError) and "input entity" in str(e))


async def wav_to_ogg(data: bytes) -> bytes | None:
    """Convert WeChat voice (WAV) to OGG/Opus so Telegram shows it as a voice note. Needs ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k",
        "-f", "ogg", "pipe:1", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate(data)
    return out if proc.returncode == 0 and out else None


HELP = (
    "<b>微信 ↔ Telegram 桥接指南</b>\n\n"
    "每个微信会话对应一个 Telegram 群：在群里发消息即发送到微信，<b>回复</b>消息即微信引用回复。\n\n"
    "<b>私聊命令</b>\n"
    "• <code>/find</code> [关键词] — 搜索微信联系人/群（不填显示最近会话，点按钮直接打开）\n"
    "• <code>/open</code> [关键词] — 为微信联系人/群建群并主动发起聊天\n"
    "• <code>/chats</code> [关键词] — 查看已连接的会话列表（别名 <code>/list</code>）\n"
    "• <code>/link</code> [关键词] — 点选或搜索绑定现有 Telegram 群与微信会话（无需查 ID）\n"
    "• <code>/status</code> — 查看运行状态与微信登录态\n"
    "• <code>/wxlogin</code> — 重新登录微信（支持手机确认或扫码）\n"
    "• <code>/help</code> — 显示这份帮助\n\n"
    "<b>群内命令</b>\n"
    "• <code>/link</code> [关键词] — 在任意群内直接发，把当前群绑定到微信会话\n"
    "• <code>/wx</code> — 查看当前群对应的微信会话\n"
    "• <code>/members</code> [关键词] — 列出微信群成员，点按复制 <code>@名字</code>（别名 <code>/at</code>）\n"
    "• <code>/recall</code> — 回复自己发的消息撤回微信消息（2 分钟内）\n"
    "• <code>/avatar</code> — 同步微信头像为群头像\n"
    "• <code>/rename</code> — 同步微信备注/群名为群标题（别名 <code>/title</code>）\n"
    "• <code>/unlink</code> — 解除当前群与微信会话的绑定\n"
    "• <code>/help</code> — 显示群内帮助\n\n"
    "<b>常用技巧</b>\n"
    "• 群内发送 <code>@成员名</code> 或 <code>@所有人</code> 可触发微信 @ 提醒\n"
    "• 删除单条已发消息或回复 <code>/recall</code> 可自动撤回微信消息（2 分钟内有效）\n"
    "• 以 <code>//</code> 开头的内容仅留在 Telegram，不会发到微信\n"
    "• 图片默认原图收发；发文件选原图也会按原图送达微信；支持贴纸与来电一键拒接"
)

GROUP_HELP = (
    "<b>桥接群命令</b>\n"
    "• <code>/wx</code> — 查看当前群对应的微信会话\n"
    "• <code>/members</code> [关键词] — 查看群成员并点按复制 <code>@名字</code>（别名 <code>/at</code>）\n"
    "• <code>/recall</code> — 回复自己发的消息以撤回微信消息（2 分钟内）\n"
    "• <code>/avatar</code> — 同步微信头像为群头像\n"
    "• <code>/rename</code> — 同步最新微信备注/群名为群标题（别名 <code>/title</code>）\n"
    "• <code>/link</code> [关键词] — 重新绑定当前群到其他微信会话\n"
    "• <code>/unlink</code> — 解除当前群与微信会话的绑定\n"
    "• <code>/help</code> — 显示群内帮助\n\n"
    "💡 发 <code>@成员名</code> / <code>@所有人</code> 可微信提醒；删单条消息或回复 <code>/recall</code> 可撤回；<code>//</code> 开头仅留在 Telegram。"
)

PRIVATE_BOT_COMMANDS = [
    ("find", "搜索微信联系人或群（可一键建群）"),
    ("open", "为微信会话建群发起聊天"),
    ("chats", "查看已连接的会话列表"),
    ("link", "选择绑定微信会话与TG群"),
    ("status", "查看运行与微信在线状态"),
    ("wxlogin", "重新登录微信（手机确认或扫码）"),
    ("help", "查看使用指南"),
]

GROUP_BOT_COMMANDS = [
    ("wx", "查看对应的微信会话"),
    ("members", "查看群成员（点按复制 @名字）"),
    ("recall", "回复消息以撤回微信消息"),
    ("avatar", "同步微信头像到群头像"),
    ("rename", "同步最新微信名称到群标题"),
    ("link", "绑定或切换当前群对应的微信会话"),
    ("unlink", "解除当前群与微信的绑定"),
    ("help", "查看群内命令帮助"),
]


def _proxy(url: str):
    if not url:
        return None
    u = urlparse(url)
    return {"proxy_type": u.scheme, "addr": u.hostname, "port": u.port,
            "username": u.username, "password": u.password, "rdns": True}


def _esc(s: str | None) -> str:
    return html.escape(s or "")


WX_EMOJIS = {
    "微笑": "🙂", "撇嘴": "😒", "色": "😍", "发呆": "😳", "得意": "😎", "流泪": "😭", "害羞": "😊",
    "闭嘴": "🤐", "睡": "😴", "大哭": "😭", "尴尬": "😅", "发怒": "😡", "调皮": "😜", "呲牙": "😁",
    "惊讶": "😮", "难过": "😔", "囧": "😳", "抓狂": "😫", "吐": "🤮", "偷笑": "🤭", "愉快": "😊",
    "白眼": "🙄", "傲慢": "😤", "困": "😪", "惊恐": "😱", "憨笑": "😄", "悠闲": "😌", "咒骂": "🤬",
    "疑问": "❓", "嘘": "🤫", "晕": "😵", "衰": "😞", "骷髅": "💀", "敲打": "🔨", "再见": "👋",
    "擦汗": "😅", "抠鼻": "🤥", "鼓掌": "👏", "坏笑": "😏", "右哼哼": "😤", "鄙视": "😒", "委屈": "😢",
    "快哭了": "😢", "阴险": "😈", "亲亲": "😘", "可怜": "🥺", "菜刀": "🔪", "西瓜": "🍉", "啤酒": "🍺",
    "咖啡": "☕", "猪头": "🐷", "玫瑰": "🌹", "凋谢": "🥀", "嘴唇": "💋", "爱心": "❤️", "心碎": "💔",
    "蛋糕": "🎂", "炸弹": "💣", "便便": "💩", "月亮": "🌙", "太阳": "☀️", "拥抱": "🤗", "强": "👍",
    "弱": "👎", "握手": "🤝", "胜利": "✌️", "抱拳": "🙏", "勾引": "🤙", "拳头": "✊", "OK": "👌",
    "笑脸": "😄", "生病": "😷", "脸红": "😳", "恐惧": "😨", "失望": "😞", "无语": "😑", "嘿哈": "🤣",
    "捂脸": "🤦", "奸笑": "😏", "机智": "🧐", "皱眉": "😟", "耶": "✌️", "吃瓜": "🍉", "加油": "💪",
    "汗": "😓", "天啊": "😱", "旺柴": "🐶", "好的": "👌", "打脸": "🤦", "哇": "🤩", "翻白眼": "🙄",
    "破涕为笑": "😂", "笑哭": "😂", "叹气": "😮‍💨", "苦涩": "🥲", "裂开": "💔", "合十": "🙏", "赞": "👍",
    "庆祝": "🎉", "礼物": "🎁", "红包": "🧧", "烟花": "🎆", "爆竹": "🧨",
}
_WX_EMOJI_RE = re.compile(r"\[(" + "|".join(re.escape(k) for k in WX_EMOJIS) + r")\]")
_WX_TOPIC_RE = re.compile(r"(?<![A-Za-z0-9_/:.?&=])#([^\s#<>/?&=]{1,30})#")
_TOPIC_BOUNDARY = set(" \t\r\n,，。！？、；：.!?;:)）]】\"'”’")


def _normalize_wx_text(s: str | None) -> str:
    """Convert WeChat #topic# tags and [emoji] codes into plain Telegram-friendly text."""
    text = s or ""
    if not text:
        return ""
    if "[" in text:
        text = _WX_EMOJI_RE.sub(lambda m: WX_EMOJIS[m.group(1)], text)
    if "#" in text:
        def _topic_sub(m: re.Match) -> str:
            lead = " " if m.start() > 0 and ("\u4e00" <= text[m.start() - 1] <= "\u9fff") else ""
            nxt = text[m.end()] if m.end() < len(text) else ""
            tail = " " if nxt and nxt not in _TOPIC_BOUNDARY else ""
            return f"{lead}#{m.group(1)}{tail}"

        text = _WX_TOPIC_RE.sub(_topic_sub, text)
    return text


def _fmt_wx_text(s: str | None) -> str:
    """Convert WeChat #topic# tags and [emoji] codes into Telegram-friendly HTML-escaped text."""
    return _esc(_normalize_wx_text(s))


_CHANNELS_PLACEHOLDER_TITLE = "当前版本不支持展示该内容"
_CHANNELS_PLACEHOLDER_URL = "support.weixin.qq.com/security/readtemplate"


def _is_channels_placeholder(text: str) -> bool:
    return text.startswith(_CHANNELS_PLACEHOLDER_TITLE) or text.startswith(f"[Link] {_CHANNELS_PLACEHOLDER_TITLE}")


def _channels_body(link: dict | None, body: str = "") -> str:
    """Render WeChat Channels (type 49/51) without WeChat's legacy upgrade placeholder."""
    if link:
        source = (link.get("sourceName") or "").strip()
        header = f"🎬 <b>视频号 · {_esc(source)}</b>" if source else "🎬 <b>视频号</b>"
        title = (link.get("title") or "").strip()
        desc = (link.get("description") or "").strip()
        url = (link.get("url") or "").strip()
        if _is_channels_placeholder(title) or title == desc:
            title = ""
        if _CHANNELS_PLACEHOLDER_URL in url:
            url = ""
        return "\n".join(p for p in (header, _fmt_wx_text(title), _fmt_wx_text(desc), _esc(url)) if p)
    lines = [ln for ln in (body or "").splitlines()
             if ln.strip() and not _is_channels_placeholder(ln.strip()) and _CHANNELS_PLACEHOLDER_URL not in ln]
    return "\n".join(["🎬 <b>视频号</b>", *(_fmt_wx_text(ln) for ln in lines)])


def _is_link_card(msg: dict) -> bool:
    return msg.get("type") == APP and msg.get("appMessageType") != APP_CHANNELS and bool(msg.get("link"))


def _link_url(link: dict | None) -> str:
    url = ((link or {}).get("url") or "").strip()
    return url if url.startswith(("http://", "https://")) else ""


def _link_buttons(link: dict | None, app_type: int | None) -> list | None:
    if url := _link_url(link):
        src = cards.identify_link_source(link, app_type)
        return [[Button.url(src.button_label, url)]]
    return None


def _link_body(link: dict, app_type: int | None = None, *, inline_url: bool = False) -> str:
    src = cards.identify_link_source(link, app_type)
    icon = {"xhs": "📕", "wxmp": "📄", "bili": "📺", "zhihu": "💡", "music": "🎵", "video": "🎬"}.get(src.brand, "🔗")
    title = (link.get("title") or "").strip()
    desc = (link.get("description") or "").strip()
    url = _link_url(link)
    if title:
        head = (
            f'{icon} <a href="{_esc(url)}"><b>{_fmt_wx_text(title)}</b></a>'
            if (url and inline_url) else f"{icon} <b>{_fmt_wx_text(title)}</b>"
        )
    elif url and inline_url:
        head = f'{icon} <a href="{_esc(url)}"><b>{_esc(src.label)}</b></a>'
    else:
        head = f"{icon} <b>{_esc(src.label)}</b>"
    parts = [head]
    if desc:
        parts.append(_fmt_wx_text(desc))
    parts.append(f"<i>来自 {_esc(src.label)}</i>")
    return "\n".join(parts)


def _plain_sender(header: str) -> str:
    """Extract plain sender label from an HTML bridge header for non-HTML Telegram fields."""
    if not header:
        return ""
    s = re.sub(r"<[^>]+>", "", header).rstrip("\n")
    if s.endswith(("：", ":")):
        s = s[:-1]
    return html.unescape(s).strip()


def _location_venue(loc: dict, body: str = "", sender: str = "") -> tuple[str, str]:
    """Build (title, address) for Telegram InputMediaVenue from a WeChat location payload."""
    name = " ".join((loc.get("name") or "").split())
    address = " ".join((loc.get("address") or "").split())
    fallback_body = " ".join((body or "").split())
    coord = f"{loc['latitude']:.6f}, {loc['longitude']:.6f}"
    base_title = name or ("位置" if address else (fallback_body or "位置"))
    title = f"{sender}：{base_title}" if sender else base_title
    if address and address != base_title:
        venue_address = address
    elif fallback_body and fallback_body != base_title:
        venue_address = fallback_body
    else:
        venue_address = coord
    return title, venue_address


# agent-wechat reason codes for attachments it could not fetch
MEDIA_REASONS = {
    "unsupported_sticker_source": "这个表情没有可下载的地址（比如自定义/加密表情），agent-wechat 拿不到",
    "sticker_source_not_allowed": "表情地址不在 agent-wechat 允许的服务器上",
    "sticker_source_dns_not_public": "表情服务器解析失败",
    "sticker_dns_failed": "表情服务器解析失败",
    "media_expired": "已过期",
    "unsupported_media": "不支持",
    "unsupported_media_transport": "不支持",
}

WX_TYPE_NAMES = {
    37: "好友验证请求",
    42: "名片",
    50: "语音/视频通话",
    66: "企业微信名片",
    10002: "系统通知",
}

APP_TYPE_NAMES = {
    1: "文本分享",
    3: "音乐",
    4: "视频链接",
    5: "链接",
    6: "文件",
    17: "实时位置共享",
    19: "聊天记录",
    24: "收藏笔记",
    33: "小程序",
    36: "小程序",
    51: "视频号",
    57: "引用消息",
    63: "视频号直播",
    87: "群公告",
    2000: "转账",
    2001: "红包",
}


def _unsupported_label(t: int | None, sub: int | None) -> str:
    if t == APP and sub in APP_TYPE_NAMES:
        return f"[暂不支持的微信消息：{APP_TYPE_NAMES[sub]} ({t}/{sub})]"
    if t != APP and t in WX_TYPE_NAMES:
        return f"[暂不支持的微信消息：{WX_TYPE_NAMES[t]} ({t})]"
    return f"[暂不支持的微信消息类型 {t}/{sub}]"


def _avatar_photo(data: bytes, size: int = 512) -> bytes:
    """WeChat hands out small avatars (~132 px) that Telegram rejects as a chat photo
    (PhotoCropSizeSmallError), so centre-crop to a square and scale up to size x size JPEG."""
    from PIL import Image
    if data.startswith(b"wxgf"):  # WeChat's own HEVC-based format, which Pillow can't read
        if not (ffmpeg := stickers.find_ffmpeg()):
            raise ValueError("avatar is in WeChat's wxgf format and ffmpeg is not installed")
        im = stickers.wxgf_to_image(data, ffmpeg)
    else:
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except Exception as e:
            raise ValueError(f"avatar is not a readable image (starts with {data[:8]!r})") from e
    w, h = im.size
    side = min(w, h)
    im = im.crop(((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side))
    if side < size:
        im = im.resize((size, size), Image.LANCZOS)
    out = io.BytesIO()
    stickers.on_white(im).save(out, "JPEG", quality=92)
    return out.getvalue()


WX_IMAGE_MAX_BYTES = 26_214_383
WX_JPEG_MAX_EDGE = 8192
WX_JPEG_MAX_PIXELS = 64_000_000
WX_OTHER_IMAGE_MAX_EDGE = 4096
WX_OTHER_IMAGE_MAX_PIXELS = 16_777_216
_IMAGE_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".heic": "image/heic",
    ".heif": "image/heif", ".avif": "image/avif",
}


def _image_from_file(data: bytes, mime: str, name: str, ext: str) -> Media | None:
    """Validate/prepare an image sent as a Telegram file so WeChat receives it as an original image."""
    from PIL import Image, ImageOps
    norm_mime = mime.split(";")[0].strip().lower()
    if norm_mime == "image/jpg":
        norm_mime = "image/jpeg"
    file_ext = ext.lower() or (("." + name.rsplit(".", 1)[1].lower()) if "." in name else "")
    is_img = (norm_mime.startswith("image/") and norm_mime != "image/svg+xml") or (
        norm_mime in ("application/octet-stream", "") and file_ext in _IMAGE_EXT_MIME
    )
    if not is_img:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        fmt = (im.format or "").upper()
    except Exception:
        return None
    if fmt == "GIF" and getattr(im, "n_frames", 1) > 1:
        return None  # keep animated GIF as a file so WeChat doesn't flatten it to a static image
    if fmt == "JPEG":
        if len(data) <= WX_IMAGE_MAX_BYTES and max(w, h) <= WX_JPEG_MAX_EDGE and w * h <= WX_JPEG_MAX_PIXELS:
            out_name = name if file_ext in (".jpg", ".jpeg") else (
                (name.rsplit(".", 1)[0] if "." in name else name) + ".jpg")
            return Media(data, "image/jpeg", out_name)
        return None
    direct_mime = {"PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}.get(fmt)
    if direct_mime and len(data) <= WX_IMAGE_MAX_BYTES and max(w, h) <= WX_OTHER_IMAGE_MAX_EDGE and w * h <= WX_OTHER_IMAGE_MAX_PIXELS:
        return Media(data, direct_mime, name)
    if max(w, h) <= WX_JPEG_MAX_EDGE and w * h <= WX_JPEG_MAX_PIXELS:
        try:
            im.load()
            im = ImageOps.exif_transpose(im)
            out = io.BytesIO()
            stickers.on_white(im).save(out, "JPEG", quality=95)
            jpg = out.getvalue()
            if len(jpg) <= WX_IMAGE_MAX_BYTES:
                stem = name.rsplit(".", 1)[0] if "." in name else name
                return Media(jpg, "image/jpeg", f"{stem}.jpg")
        except Exception:
            return None
    return None


def _fingerprint(m) -> tuple:
    """What the bot's copy and your account's copy of one message have in common."""
    media = getattr(m, "media", None)
    if isinstance(media, types.MessageMediaWebPage):
        media = None
    return (getattr(m, "message", None) or "", type(media).__name__ if media else "")


def _tg_msg_fingerprint(m) -> tuple:
    """Fingerprint a user-sent message across user and bot sessions, including Telegram media IDs."""
    media = getattr(m, "media", None)
    if isinstance(media, types.MessageMediaWebPage):
        media = None
    media_id = None
    if media is not None:
        media_id = getattr(getattr(media, "document", None), "id", None) or getattr(
            getattr(media, "photo", None), "id", None)
    return (getattr(m, "message", None) or "", type(media).__name__ if media else "", media_id)


def _describe_tg_msg(msg) -> str:
    """Short human-readable label for a Telegram message shown in failure reports."""
    text = " ".join((getattr(msg, "message", None) or "").split())
    snippet = (text[:24] + "…") if len(text) > 25 else text
    if getattr(msg, "sticker", None):
        return "贴纸"
    if getattr(msg, "photo", None):
        return f"图片「{snippet}」" if snippet else "图片"
    if getattr(msg, "voice", None):
        return "语音"
    if getattr(msg, "video_note", None):
        return "视频消息"
    if getattr(msg, "gif", None):
        return f"GIF「{snippet}」" if snippet else "GIF"
    if getattr(msg, "video", None):
        return f"视频「{snippet}」" if snippet else "视频"
    media = getattr(msg, "media", None)
    if media and not isinstance(media, types.MessageMediaWebPage):
        f = getattr(msg, "file", None)
        name = (getattr(f, "name", None) or "").strip()
        return f"文件 {name}" if name else "文件"
    if snippet:
        return f"「{snippet}」"
    return "消息"


def _chunks(text: str, size: int = TG_TEXT_LIMIT) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _member_label(m: dict) -> str:
    return (m.get("mentionName") or m.get("roomDisplayName") or m.get("displayName") or m.get("id") or "").strip()


def _is_mention_boundary(name: str, after: str) -> bool:
    if not after:
        return True
    ch = after[0]
    if ch.isspace() or not ch.isalnum():
        return True
    return name[-1].isascii() and not ch.isascii()


def _match_mentions(text: str, members: list[dict]) -> dict | None:
    """Resolve @all/@所有人 and @memberName occurrences in `text` to an agent-wechat mentions payload."""
    candidates: list[tuple[str, str]] = []
    for m in members:
        uid = m.get("id")
        if not uid:
            continue
        for key in ("mentionName", "roomDisplayName", "displayName"):
            name = (m.get(key) or "").strip()
            if name:
                candidates.append((name, uid))
    candidates.sort(key=lambda item: len(item[0]), reverse=True)

    mention_all = False
    user_ids: list[str] = []
    i = 0
    while (pos := text.find("@", i)) != -1:
        if pos > 0 and text[pos - 1].isalnum():
            i = pos + 1
            continue
        rest = text[pos + 1:]
        if rest.startswith("所有人") and _is_mention_boundary("所有人", rest[3:]):
            mention_all = True
            i = pos + 4
            continue
        if rest[:3].lower() == "all" and _is_mention_boundary("all", rest[3:]):
            mention_all = True
            i = pos + 4
            continue
        matched_len = 0
        for name, uid in candidates:
            if len(name) < matched_len:
                break
            if rest[:len(name)].lower() == name.lower() and _is_mention_boundary(name, rest[len(name):]):
                matched_len = len(name)
                if uid not in user_ids:
                    user_ids.append(uid)
        i = pos + 1 + (matched_len or 0)
    if not mention_all and not user_ids:
        return None
    return {"all": mention_all, "userIds": user_ids}


class Bridge(LoginMixin):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._init_login()
        data = cfg.data_dir
        self.store = Store(data / "bridge.db")
        self.wx = WeChatClient(cfg.wechat.base_url, cfg.wechat.read_token(), cfg.wechat.account)
        tg = cfg.telegram
        proxy = _proxy(tg.proxy)
        self.user = TelegramClient(str(data / "user"), tg.api_id, tg.api_hash, proxy=proxy,
                                   flood_sleep_threshold=120)
        self.bot = TelegramClient(str(data / "bot"), tg.api_id, tg.api_hash, proxy=proxy,
                                  flood_sleep_threshold=60)
        self.max_media = cfg.bridge.max_media_mb * 1024 * 1024
        self.pending_sends: dict[str, int] = defaultdict(int)  # wx thread -> in-flight sends from Telegram
        self.create_lock = asyncio.Lock()
        self.last_create = 0.0
        self.names: dict[tuple[str, str], str] = {}
        self.started = time.time()
        self.forwarded = 0
        # Pairing bot-side and user-side ids of the same bridged message (see store.msgmap):
        # chat -> [(fingerprint, payload, time)] not matched yet.
        self.unpaired_bot: dict[int, list] = defaultdict(list)   # payload: (wx_thread, wx_msg, wx_server)
        self.unpaired_user: dict[int, list] = defaultdict(list)  # payload: user-side message id
        self._unpaired_tg_user: dict[int, list] = defaultdict(list)
        self._unpaired_tg_bot: dict[int, list] = defaultdict(list)
        self._user_to_bot_msg: dict[tuple[int, int], int] = {}
        self._bot_pair_events: dict[tuple[int, int], asyncio.Event] = {}
        self._bot_pair_wait: float = 0.5
        self.tg_recalls: set[tuple[str, str]] = set()            # (wx_thread, wx_server) recalled from Telegram
        self._calls_sock = None
        self._calls_audio_format: dict = {"sampleRate": 16000, "channels": 1, "frameMs": 20}
        self._active_calls: dict[str, dict] = {}
        self._call_tokens: dict[str, str] = {}
        self._conf_invites: dict[str, dict] = {}
        self._calls_retry_sleep: float = 300.0
        self._ui_tokens: dict[str, dict] = {}
        self._link_wait: dict | None = None
        self._recall_notice_ttl: float = 5.0
        self._notice_tasks: set[asyncio.Task] = set()

    # ======================================================================
    # startup
    # ======================================================================
    async def sync_bot_commands(self) -> None:
        """Register private and group `/` menus with Telegram."""
        scopes = [
            (types.BotCommandScopeDefault(), PRIVATE_BOT_COMMANDS),
            (types.BotCommandScopeUsers(), PRIVATE_BOT_COMMANDS),
            (types.BotCommandScopeChats(), GROUP_BOT_COMMANDS),
        ]
        for scope, items in scopes:
            cmds = [types.BotCommand(command=cmd, description=desc) for cmd, desc in items]
            try:
                await self.bot(functions.bots.SetBotCommandsRequest(scope=scope, lang_code="", commands=cmds))
            except Exception as e:
                log.warning("Could not set bot commands for %s: %s", type(scope).__name__, e)

    async def run(self) -> None:
        await self.bot.start(bot_token=self.cfg.telegram.bot_token)
        self.bot_me = await self.bot.get_me()
        if not self.bot_me.username:
            raise SystemExit("The bot has no username; check TG_BOT_TOKEN")
        await self.sync_bot_commands()
        await self.user.connect()
        self.bot.add_event_handler(self.on_bot_private, events.NewMessage(incoming=True, func=lambda e: e.is_private))
        self.bot.add_event_handler(self.on_bot_callback, events.CallbackQuery())
        # The user client's update loop may only start once that account is logged in
        # (before that Telegram answers AUTH_KEY_UNREGISTERED), so main() starts it after login.
        await asyncio.gather(self.main(), self.bot.run_until_disconnected())

    async def main(self) -> None:
        if await self.user.is_user_authorized():
            self.tg_ready.set()
        else:
            self.start_pairing()
            await self.tg_ready.wait()
        self.me = await self.user.get_me()
        self.owner_id = self.me.id
        log.info("Telegram user: %s, bot: @%s", utils.get_display_name(self.me), self.bot_me.username)
        self.bot_input = await self.user.get_input_entity(self.bot_me.username)
        if self.store.get("greeted") != str(self.me.id):
            # A bot can only message people who have talked to it first.
            if self.login_owner != self.me.id:
                await self.user.send_message(self.bot_me.username, "/start")
            self.store.set("greeted", str(self.me.id))

        self.user.add_event_handler(self.on_tg_message, events.NewMessage(
            outgoing=True, func=lambda e: self.store.thread_for_chat(e.chat_id) is not None))
        self.user.add_event_handler(self.on_tg_unlinked_command, events.NewMessage(
            outgoing=True, func=lambda e: not e.is_private and self.store.thread_for_chat(e.chat_id) is None
            and (e.raw_text or "").lstrip().startswith("/")))
        self.user.add_event_handler(self.on_tg_delete, events.MessageDeleted())
        self.user.add_event_handler(self.on_raw_update, events.Raw(types.UpdateNewMessage))
        self.user.add_event_handler(self.on_user_sees_bot_message, events.NewMessage(
            incoming=True, func=lambda e: e.sender_id == self.bot_me.id
            and self.store.thread_for_chat(e.chat_id) is not None))
        self.bot.add_event_handler(self.on_bot_sees_user_message, events.NewMessage(
            incoming=True, func=lambda e: not e.is_private and e.sender_id == self.owner_id
            and self.store.thread_for_chat(e.chat_id) is not None))
        await asyncio.gather(self.wechat_side(), self.user.run_until_disconnected())

    async def wechat_side(self) -> None:
        await self.ensure_wechat()
        log.info("WeChat is logged in; bridging messages")
        tasks = [self.event_loop(), self.wechat_monitor()]
        if self.cfg.bridge.call_notifications:
            tasks.append(self.calls_loop())
        await asyncio.gather(*tasks)

    async def on_bot_private(self, event) -> None:
        if not self.tg_ready.is_set() or self.owner_id is None:
            if not self.tg_ready.is_set():
                await self.on_login_message(event)
            return
        if event.sender_id != self.owner_id:
            return
        raw = event.raw_text or ""
        if raw.startswith("/"):
            self._link_wait = None
            await self.on_bot_command(event)
            return
        if self._link_wait and raw.strip():
            await self._on_link_search_input(event, raw.strip())

    async def on_bot_callback(self, event) -> None:
        if not self.tg_ready.is_set():
            await self.on_login_callback(event)
            return
        data = event.data or b""
        if data.startswith(b"callrej:"):
            await self._on_call_reject_callback(event, data[8:].decode("utf-8", errors="ignore"))
            return
        if data.startswith(b"ui:"):
            await self._on_ui_callback(event, data[3:].decode("utf-8", errors="ignore"))
            return
        if data.startswith(b"wxlogin:"):
            await self._on_wx_login_callback(event, data[8:].decode("utf-8", errors="ignore"))
            return
        await event.answer()

    async def close(self) -> None:
        for task in list(self._notice_tasks):
            task.cancel()
        if self._notice_tasks:
            await asyncio.gather(*list(self._notice_tasks), return_exceptions=True)
        if self._calls_sock is not None:
            await self._calls_sock.close()
            self._calls_sock = None
        await self.wx.close()

    # ======================================================================
    # WeChat -> Telegram
    # ======================================================================
    async def event_loop(self) -> None:
        cursor = self.store.get("cursor")
        if cursor is None:
            cursor = await self.wx.latest_cursor()
            self.store.set("cursor", cursor)
            log.info("First start: skipping old history, listening from event cursor %s", cursor)
        if self.store.get("since") is None:
            # Messages created before this moment are history and never forwarded, even when the
            # daemon syncs them in later ("catchup"), e.g. right after the first WeChat login.
            self.store.set("since", str(time.time()))
        self.since = float(self.store.get("since"))
        backoff = 1.0
        while True:
            try:
                async for event in self.wx.stream(cursor):
                    backoff = 1.0
                    await self.handle_event(event)
                    if event.get("cursor"):
                        cursor = str(event["cursor"])
                        self.store.set("cursor", cursor)
                        asyncio.create_task(self._ack(cursor))
                log.warning("Event stream ended; reconnecting")
            except CursorExpired:
                cursor = await self.wx.latest_cursor()
                self.store.set("cursor", cursor)
                log.warning("Event cursor expired on the daemon; some events were skipped. Resuming at %s", cursor)
            except (httpx.HTTPError, WeChatError) as e:
                log.warning("Event stream error: %s; retrying in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _ack(self, cursor: str) -> None:
        try:
            await self.wx.ack(self.cfg.wechat.consumer, cursor)
        except Exception as e:  # ack is advisory only
            log.debug("ack failed: %s", e)

    async def handle_event(self, event: dict) -> None:
        kind, data = event.get("kind"), event.get("data") or {}
        try:
            # origin: "live" (pushed now), "catchup" (synced in after login/reconnect), "local_send"
            # (sent through the daemon API, i.e. by us), "profile_import" (old data). Outgoing ones of
            # any origin are still checked so our Telegram sends get linked to their WeChat message.
            if kind == "message.committed":
                origin = data.get("origin")
                if origin in ("live", "catchup") or data.get("direction") == "outgoing":
                    await self.on_wx_message(data["messageId"], origin)
                else:
                    log.info("Skipped WeChat message %s in %s: origin %s", data.get("messageId"),
                             data.get("threadId"), origin)
            elif kind == "message.recalled":
                await self.on_wx_recall(data)
        except Exception:
            log.exception("Failed to handle %s event %s", kind, event.get("cursor"))

    def _allowed(self, thread: dict) -> bool:
        tid = thread["id"]
        b = self.cfg.bridge
        if tid in b.skip_threads:
            return False
        if thread.get("kind") == "group" and not b.bridge_groups:
            return False
        cls = thread.get("classification") or {}
        if cls.get("category") == "business" and not b.bridge_official_accounts:
            return False
        return True

    def _is_new(self, msg: dict, origin: str | None) -> bool:
        """Forward live messages, and caught-up ones created after the bridge started listening."""
        if origin == "live":
            return True
        if origin != "catchup":
            return False
        try:
            created = datetime.fromisoformat(msg["createdAt"]).timestamp()
        except (KeyError, TypeError, ValueError):
            return False
        return created >= getattr(self, "since", 0) - 60  # small allowance for clock differences

    async def on_wx_message(self, message_id: str, origin: str | None) -> None:
        if self.store.seen_wx_msg(message_id):
            return
        msg = await self.wx.message(message_id)
        thread_id = msg["threadId"]
        outgoing = msg.get("direction") == "outgoing"
        if outgoing:
            # Our own sends from Telegram come back as outgoing messages: drop the echo.
            if self.pending_sends.get(thread_id):
                asyncio.create_task(self._deferred_outgoing(msg, origin))
                return
            if msg.get("serverId") and self.store.claim_echo(thread_id, msg["serverId"], msg["id"]):
                return
        await self._maybe_forward(msg, origin)

    async def _maybe_forward(self, msg: dict, origin: str | None) -> None:
        thread_id = msg["threadId"]
        skip = None
        if msg.get("direction") == "outgoing" and not self.cfg.bridge.forward_outgoing:
            skip = "outgoing (forward_outgoing = false)"
        elif not self._is_new(msg, origin):
            skip = f"history (origin {origin}, created {msg.get('createdAt')})"
        elif msg.get("recall"):
            skip = "recalled"
        elif msg.get("type") == APP and msg.get("appMessageType") == APP_UPLOAD_NOTICE:
            skip = "file upload notice"
        thread = None
        if skip is None:
            thread = await self.wx.thread(thread_id)
            if not self._allowed(thread):
                skip = f"filtered (kind {thread.get('kind')}, {(thread.get('classification') or {}).get('category')})"
        if skip is not None:
            log.info("Skipped WeChat message %s in %s: %s", msg["id"], thread_id, skip)
            return
        chat = await self.ensure_chat(thread)
        if chat is None:
            return
        await self.forward_to_tg(chat, thread, msg)
        log.info("Forwarded WeChat message %s from %s to Telegram %s", msg["id"], thread_id, chat)

    async def _deferred_outgoing(self, msg: dict, origin: str | None) -> None:
        """Wait until in-flight Telegram sends for this chat settle, then decide whether msg is an echo."""
        tid = msg["threadId"]
        for _ in range(600):
            if not self.pending_sends.get(tid):
                break
            await asyncio.sleep(0.5)
        if msg.get("serverId") and self.store.claim_echo(tid, msg["serverId"], msg["id"]):
            return
        if not self.store.seen_wx_msg(msg["id"]):
            await self._maybe_forward(msg, origin)

    # --- group creation ---------------------------------------------------
    def _title(self, thread: dict) -> str:
        name = thread.get("displayName") or thread["id"]
        tpl = self.cfg.bridge.title_group if thread.get("kind") == "group" else self.cfg.bridge.title_direct
        return tpl.format(name=name)[:128]

    async def ensure_chat(self, thread: dict) -> int | None:
        tid = thread["id"]
        if (chat := self.store.chat_for_thread(tid)) is not None:
            return chat
        async with self.create_lock:
            if (chat := self.store.chat_for_thread(tid)) is not None:
                return chat
            wait = self.cfg.bridge.create_interval_seconds - (time.monotonic() - self.last_create)
            if wait > 0:
                await asyncio.sleep(wait)
            title = self._title(thread)
            while True:
                try:
                    result = await self.user(functions.messages.CreateChatRequest(users=[self.bot_input], title=title))
                    break
                except errors.FloodWaitError as e:
                    log.warning("Telegram asks to wait %ss before creating another group", e.seconds)
                    await asyncio.sleep(e.seconds + 1)
            self.last_create = time.monotonic()
            updates = getattr(result, "updates", result)
            tg_chat = next(c for c in updates.chats if isinstance(c, types.Chat))
            chat_id = utils.get_peer_id(types.PeerChat(tg_chat.id))
            self.store.link(tid, chat_id, title)
            log.info("Created Telegram group %r (%s) for WeChat chat %s", title, chat_id, tid)
        await self._init_chat(chat_id, tg_chat.id, thread)
        return chat_id

    async def sync_avatar(self, chat_id: int, thread_id: str) -> str:
        """Set the Telegram group photo to the WeChat avatar. "" when it was set, otherwise why not."""
        try:
            data, why = await self.wx.fetch_avatar(thread_id)
            if not data:
                log.info("No WeChat avatar for %s: %s", thread_id, why)
                return why
            data = await asyncio.to_thread(_avatar_photo, data)
            f = await self.user.upload_file(io.BytesIO(data), file_name="avatar.jpg")
            peer = utils.resolve_id(chat_id)[0]
            if chat_id < -1000000000000:  # upgraded to a supergroup
                await self.user(functions.channels.EditPhotoRequest(
                    channel=await self.user.get_input_entity(chat_id), photo=types.InputChatUploadedPhoto(file=f)))
            else:
                await self.user(functions.messages.EditChatPhotoRequest(
                    chat_id=peer, photo=types.InputChatUploadedPhoto(file=f)))
            return ""
        except Exception as e:
            log.info("Could not set group avatar for %s: %s", thread_id, e)
            return f"{type(e).__name__}: {e}"

    async def sync_title(self, chat_id: int, thread_id: str) -> tuple[str | None, str]:
        """Update Telegram group title from WeChat display name. Returns (new_title, error_or_status)."""
        try:
            try:
                thread = await self.wx.thread(thread_id)
            except WeChatError:
                if thread_id.endswith("@chatroom"):
                    raise
                contact = await self.wx.contact(thread_id)
                thread = {"id": thread_id, "kind": "direct", "displayName": contact.get("displayName") or thread_id}
            title = self._title(thread)
            try:
                if chat_id < -1000000000000:
                    await self.user(functions.channels.EditTitleRequest(
                        channel=await self.user.get_input_entity(chat_id), title=title))
                else:
                    await self.user(functions.messages.EditChatTitleRequest(
                        chat_id=utils.resolve_id(chat_id)[0], title=title))
            except errors.ChatNotModifiedError:
                self.store.update_title(thread_id, title)
                return title, "unchanged"
            self.store.update_title(thread_id, title)
            return title, ""
        except Exception as e:
            log.info("Could not sync group title for %s: %s", thread_id, e)
            return None, f"{type(e).__name__}: {e}"

    async def _init_chat(self, chat_id: int, raw_chat_id: int, thread: dict) -> None:
        if self.cfg.bridge.sync_avatar:
            # Fetching an avatar from WeChat can take a while; don't hold up the first message.
            asyncio.create_task(self.sync_avatar(chat_id, thread["id"]))
        kind = "群聊" if thread.get("kind") == "group" else "联系人"
        await self.bot.send_message(chat_id, f"已连接微信{kind}：<b>{_esc(thread.get('displayName'))}</b>\n"
                                             f"<code>{_esc(thread['id'])}</code>", parse_mode="html", silent=True)

    # --- rendering --------------------------------------------------------
    async def _sender_name(self, msg: dict) -> str:
        sid = msg.get("senderId")
        if not sid:
            return ""
        key = (msg["threadId"], sid)
        if key in self.names:
            return self.names[key]
        name = sid
        try:
            if msg["threadId"].endswith("@chatroom"):
                member = await self.wx.group_member(msg["threadId"], sid)
                if member and member.get("displayName"):
                    name = member["displayName"]
            if name == sid:
                name = (await self.wx.contact(sid)).get("displayName") or sid
        except WeChatError:
            pass
        self.names[key] = name
        return name

    def _body(self, msg: dict) -> str:
        t, body = msg.get("type"), msg.get("body") or ""
        if t == TEXT:
            return _fmt_wx_text(body)
        if t == SYSTEM:
            return f"<i>{_esc(body)}</i>"
        if t == LOCATION:
            loc = msg.get("location") or {}
            return "📍 " + _esc(" ".join(x for x in (loc.get("name"), loc.get("address")) if x) or body)
        if t == APP and msg.get("appMessageType") == APP_CHANNELS:
            return _channels_body(msg.get("link"), body)
        if t == APP and (link := msg.get("link")):
            return _link_body(link, msg.get("appMessageType"), inline_url=True)
        if t == APP and msg.get("appMessageType") == 19:
            return "📜 聊天记录\n" + _fmt_wx_text(body)
        if t == STICKER:
            return "[表情]"
        return _fmt_wx_text(body) or _unsupported_label(t, msg.get("appMessageType"))

    def _quote_prefix(self, msg: dict) -> str:
        q = msg.get("quote") or {}
        el = q.get("embeddedLink") or {}
        el_title = el.get("title") or ""
        if _is_channels_placeholder(el_title):
            el_title = el.get("description") or (f"视频号 · {el['sourceName']}" if el.get("sourceName") else "视频号")
        text = q.get("embeddedText") or ("[图片]" if q.get("embeddedImage") else "[表情]" if q.get("embeddedSticker")
                                         else el_title or "…")
        who = q.get("senderName") or ""
        return f"<blockquote>{_esc(who + '：' if who else '')}{_fmt_wx_text(text[:200])}</blockquote>"

    # --- sending to Telegram ----------------------------------------------
    async def forward_to_tg(self, chat: int, thread: dict, msg: dict) -> None:
        outgoing = msg.get("direction") == "outgoing"
        header = ""
        if outgoing:
            header = "<b>我</b>（手机端）：\n"
        elif thread.get("kind") == "group" and msg.get("senderId"):
            header = f"<b>{_esc(await self._sender_name(msg))}</b>：\n"

        reply_to = None
        prefix = ""
        if msg.get("type") == APP and msg.get("appMessageType") == APP_QUOTE and msg.get("quote"):
            hit = self.store.tg_for_wx(msg["threadId"], None, msg["quote"].get("serverId"))
            if hit and hit[0] == chat:
                reply_to = hit[1]
            else:
                prefix = self._quote_prefix(msg)

        media: Media | str | None = None
        t = msg.get("type")
        if t in (IMAGE, VIDEO, VOICE, STICKER) or (t == APP and msg.get("appMessageType") == APP_FILE):
            try:
                media = await self.wx.download_media(msg, self.max_media)
            except WeChatError as e:
                media = e.code
            if isinstance(media, str):
                log.info("Could not download the attachment of WeChat message %s (type %s): %s", msg["id"], t, media)
            if isinstance(media, Media) and t == VOICE and media.mime == "audio/wav":
                if ogg := await wav_to_ogg(media.data):
                    media = Media(ogg, "audio/ogg", "voice.ogg")
        elif _is_link_card(msg) and self.cfg.bridge.link_card_images:
            link = msg["link"]
            thumb_bytes = await self.wx.download_link_thumbnail(msg)
            card_png = await asyncio.to_thread(
                cards.render_link_card,
                title=_normalize_wx_text(link.get("title")),
                description=_normalize_wx_text(link.get("description")),
                url=link.get("url"),
                source_name=_normalize_wx_text(link.get("sourceName")),
                app_type=msg.get("appMessageType"),
                thumbnail_bytes=thumb_bytes,
            )
            if card_png:
                media = Media(card_png, "image/png", "link-card.png")

        silent = outgoing and self.cfg.bridge.outgoing_silent
        sent = await self._send_tg(chat, thread, header, prefix, msg, media, reply_to, silent)
        for m in sent:
            self.store.map_message(chat, "bot", m.id, msg["threadId"], msg["id"], msg.get("serverId"))
            self._pair(chat, _fingerprint(m), bot=(msg["threadId"], msg["id"], msg.get("serverId")))
        self.forwarded += 1

    # --- pairing the two views of a bridged message ----------------------
    async def on_user_sees_bot_message(self, event) -> None:
        """Your account received a message the bot posted; it has a different id there."""
        self._pair(event.chat_id, _fingerprint(event.message), user=event.message.id)

    def _pair(self, chat: int, fp: tuple, bot: tuple | None = None, user: int | None = None) -> None:
        now = time.monotonic()
        mine, other = (self.unpaired_bot, self.unpaired_user) if bot else (self.unpaired_user, self.unpaired_bot)
        for q in (mine[chat], other[chat]):
            q[:] = [e for e in q if now - e[2] < 300][-50:]
        for i, (ofp, payload, _) in enumerate(other[chat]):
            if ofp == fp:
                del other[chat][i]
                user_id, wx = (payload, bot) if bot else (user, payload)
                self.store.map_message(chat, "user", user_id, *wx)
                return
        mine[chat].append((fp, bot or user, now))

    async def on_bot_sees_user_message(self, event) -> None:
        """The bot received a message you posted in a group; record its bot-side id for replies."""
        self._pair_tg_user_msg(event.chat_id, _tg_msg_fingerprint(event.message), bot_id=event.message.id)

    def _record_user_bot_pair(self, chat_id: int, user_id: int, bot_id: int) -> None:
        key = (chat_id, user_id)
        if len(self._user_to_bot_msg) >= 2000:
            for k in list(self._user_to_bot_msg)[:1000]:
                self._user_to_bot_msg.pop(k, None)
        self._user_to_bot_msg[key] = bot_id
        if ev := self._bot_pair_events.get(key):
            ev.set()

    def _pair_tg_user_msg(self, chat_id: int, fp: tuple, *, user_id: int | None = None,
                          bot_id: int | None = None) -> None:
        if chat_id < -1000000000000:
            return
        now = time.monotonic()
        mine, other = (self._unpaired_tg_user, self._unpaired_tg_bot) if user_id is not None else (
            self._unpaired_tg_bot, self._unpaired_tg_user)
        for q in (mine[chat_id], other[chat_id]):
            q[:] = [e for e in q if now - e[2] < 300][-50:]
        for i, (ofp, payload, _) in enumerate(other[chat_id]):
            if ofp == fp:
                del other[chat_id][i]
                uid, bid = (user_id, payload) if user_id is not None else (payload, bot_id)
                self._record_user_bot_pair(chat_id, uid, bid)
                return
        mine[chat_id].append((fp, user_id if user_id is not None else bot_id, now))

    async def _bot_reply_id(self, chat_id: int, user_msg_id: int | None, *, wait: bool = True) -> int | None:
        if user_msg_id is None:
            return None
        if chat_id < -1000000000000:
            return user_msg_id
        key = (chat_id, user_msg_id)
        if (bid := self._user_to_bot_msg.get(key)) is not None:
            return bid
        if not wait or self._bot_pair_wait <= 0:
            return None
        ev = self._bot_pair_events.setdefault(key, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout=self._bot_pair_wait)
        except asyncio.TimeoutError:
            pass
        finally:
            self._bot_pair_events.pop(key, None)
        return self._user_to_bot_msg.get(key)

    async def _send_tg(self, chat, thread, header, prefix, msg, media, reply_to, silent) -> list:
        async def go(chat_id: int) -> list:
            out = []
            reply = reply_to
            t = msg.get("type")
            is_link = _is_link_card(msg)
            send_media = media
            if isinstance(send_media, Media) and is_link:
                f = io.BytesIO(send_media.data)
                f.name = send_media.filename
                caption = (header + prefix).rstrip("\n")
                buttons = _link_buttons(msg.get("link"), msg.get("appMessageType"))
                kw = dict(caption=caption[:TG_CAPTION_LIMIT] or None, parse_mode="html", reply_to=reply,
                          silent=silent, force_document=False, buttons=buttons)
                try:
                    out.append(await self.bot.send_file(chat_id, f, **kw))
                    return out
                except errors.RPCError as e:
                    if _dead_chat(e):
                        raise
                    if buttons:
                        url = _link_url(msg.get("link"))
                        src = cards.identify_link_source(msg.get("link"), msg.get("appMessageType"))
                        link_line = f'<a href="{_esc(url)}"><b>{_esc(src.button_label)}</b></a>'
                        fallback_cap = f"{caption}\n{link_line}" if caption else link_line
                        f.seek(0)
                        kw["buttons"] = None
                        kw["caption"] = fallback_cap[:TG_CAPTION_LIMIT]
                        try:
                            out.append(await self.bot.send_file(chat_id, f, **kw))
                            return out
                        except errors.RPCError as e2:
                            if _dead_chat(e2):
                                raise
                            log.info("Telegram refused link card photo (%s); falling back to text", e2)
                    else:
                        log.info("Telegram refused link card photo (%s); falling back to text", e)
                    send_media = None

            if isinstance(send_media, Media):
                caption = header + prefix + (self._body(msg) if t == APP and msg.get("appMessageType") != APP_FILE
                                             else "")
                if t == STICKER:
                    converted = await asyncio.to_thread(stickers.from_wechat_sticker, send_media.data, send_media.mime)
                    if converted is not None:
                        sk_kind, sk_bytes, sk_meta = converted
                        sf = io.BytesIO(sk_bytes)
                        if sk_kind == "static":
                            sf.name = "sticker.webp"
                            attrs = [
                                types.DocumentAttributeSticker(alt="😊", stickerset=types.InputStickerSetEmpty()),
                                types.DocumentAttributeImageSize(w=sk_meta["w"], h=sk_meta["h"]),
                            ]
                            sk_kwargs = dict(reply_to=reply, silent=silent, force_document=False,
                                             mime_type="image/webp", attributes=attrs)
                        else:
                            sf.name = "sticker.webm"
                            attrs = [
                                types.DocumentAttributeSticker(alt="😊", stickerset=types.InputStickerSetEmpty()),
                                types.DocumentAttributeVideo(duration=sk_meta["duration"],
                                                             w=sk_meta["w"], h=sk_meta["h"], nosound=True),
                            ]
                            sk_kwargs = dict(reply_to=reply, silent=silent, force_document=False,
                                             mime_type="video/webm", nosound_video=True, attributes=attrs)
                        if caption:
                            out.append(await self.bot.send_message(
                                chat_id, caption.rstrip("\n"), parse_mode="html",
                                reply_to=reply, silent=silent, link_preview=False))
                            sk_kwargs["reply_to"] = None
                        try:
                            out.append(await self.bot.send_file(chat_id, sf, **sk_kwargs))
                            return out
                        except errors.RPCError as e:
                            if _dead_chat(e):
                                raise
                            log.info("Telegram refused sticker as-is (%s); falling back to original media", e)
                            if out:
                                caption = ""
                                reply = None
                f = io.BytesIO(send_media.data)
                f.name = send_media.filename
                is_voice = t == VOICE and send_media.mime in ("audio/ogg", "audio/opus")
                kwargs = dict(caption=caption[:TG_CAPTION_LIMIT] or None, parse_mode="html", reply_to=reply,
                              silent=silent, voice_note=is_voice, supports_streaming=t == VIDEO,
                              force_document=t == APP or (t == VOICE and not send_media.mime.startswith("audio/")))
                try:
                    out.append(await self.bot.send_file(chat_id, f, **kwargs))
                except errors.RPCError as e:
                    if _dead_chat(e) or kwargs["force_document"]:
                        raise
                    # e.g. a photo too large/odd-sized for Telegram's photo type: send it as a file instead
                    log.info("Telegram refused media as-is (%s); sending as a file", e)
                    f.seek(0)
                    kwargs.update(force_document=True, voice_note=False, supports_streaming=False)
                    out.append(await self.bot.send_file(chat_id, f, **kwargs))
                return out
            if (
                t == LOCATION
                and not prefix
                and (loc := msg.get("location"))
                and loc.get("latitude") is not None
                and loc.get("longitude") is not None
            ):
                title, address = _location_venue(loc, msg.get("body") or "", _plain_sender(header))
                venue = types.InputMediaVenue(
                    geo_point=types.InputGeoPoint(lat=loc["latitude"], long=loc["longitude"]),
                    title=title,
                    address=address,
                    provider="",
                    venue_id="",
                    venue_type="",
                )
                try:
                    out.append(await self.bot.send_file(chat_id, venue, reply_to=reply, silent=silent))
                    return out
                except errors.RPCError as e:
                    if _dead_chat(e):
                        raise
                    log.info("Telegram refused venue (%s); falling back to text + geo point", e)
            if isinstance(send_media, str):
                label = {IMAGE: "图片", VIDEO: "视频", VOICE: "语音", STICKER: "表情"}.get(t, "文件")
                text = header + prefix + f"[{label}，无法获取：{_esc(MEDIA_REASONS.get(send_media, send_media))}]"
                buttons = None
            elif is_link:
                buttons = _link_buttons(msg.get("link"), msg.get("appMessageType"))
                text = header + prefix + _link_body(msg["link"], msg.get("appMessageType"), inline_url=not buttons)
            else:
                buttons = None
                text = header + prefix + self._body(msg)
            parts = _chunks(text)
            try:
                for i, part in enumerate(parts):
                    out.append(await self.bot.send_message(
                        chat_id, part, parse_mode="html", silent=silent,
                        reply_to=reply if i == 0 else None,
                        buttons=buttons if i == len(parts) - 1 else None,
                        link_preview=False,
                    ))
            except errors.RPCError as e:
                if _dead_chat(e) or not (is_link and buttons):
                    raise
                out.clear()
                fallback_parts = _chunks(header + prefix + _link_body(
                    msg["link"], msg.get("appMessageType"), inline_url=True))
                for i, part in enumerate(fallback_parts):
                    out.append(await self.bot.send_message(
                        chat_id, part, parse_mode="html", silent=silent,
                        reply_to=reply if i == 0 else None,
                        link_preview=False,
                    ))
            if (
                t == LOCATION
                and (loc := msg.get("location"))
                and loc.get("latitude") is not None
                and loc.get("longitude") is not None
            ):
                geo = types.InputMediaGeoPoint(types.InputGeoPoint(lat=loc["latitude"], long=loc["longitude"]))
                out.append(await self.bot.send_file(chat_id, geo, silent=silent))
            return out

        try:
            return await go(chat)
        except Exception as e:
            if not _dead_chat(e):
                raise
            log.warning("Telegram group %s for %s is gone (%s); creating a new one", chat, thread["id"], e)
            self.store.unlink(thread["id"])
            new_chat = await self.ensure_chat(thread)
            return await go(new_chat) if new_chat else []

    async def on_wx_recall(self, data: dict) -> None:
        tid = data.get("threadId") or ""
        sid = data.get("serverId")
        if sid and (tid, str(sid)) in self.tg_recalls:
            self.tg_recalls.discard((tid, str(sid)))
            return
        hit = self.store.tg_for_wx(tid, data.get("messageId"), sid)
        if not hit:
            return
        text = "↩️ 这条消息已在微信中撤回"
        if data.get("replacementText"):
            text += f"\n<i>{_esc(data['replacementText'])}</i>"
        try:
            await self.bot.send_message(hit[0], text, reply_to=hit[1], parse_mode="html", silent=True)
        except Exception as e:
            log.info("Could not mark recall: %s", e)

    # ======================================================================
    # WeChat calls -> Telegram notification + one-click reject
    # ======================================================================
    async def calls_loop(self) -> None:
        backoff = 1.0
        warned_disabled = False
        while True:
            sock = None
            try:
                sock, hello = await self.wx.calls_socket()
                self._calls_sock = sock
                backoff = 1.0
                warned_disabled = False
                await self.handle_call_event(hello)
                async for ev in sock.events():
                    try:
                        await self.handle_call_event(ev)
                    except Exception:
                        log.exception("Failed to handle WeChat call event %s", ev.get("type"))
                log.warning("Calls WebSocket ended; reconnecting")
            except WeChatError as e:
                if e.status in (403, 404, 501) or e.code in (
                    "forbidden", "unsupported", "unsupported_protocol_version", "invalid_protocol_version"
                ):
                    if not warned_disabled:
                        log.info("WeChat calls WebSocket unavailable (%s); incoming call notifications paused", e.code)
                        warned_disabled = True
                    await asyncio.sleep(self._calls_retry_sleep)
                    continue
                log.warning("Calls WebSocket error: %s; retrying in %.0fs", e, backoff)
            except (OSError, asyncio.TimeoutError, httpx.HTTPError) as e:
                log.warning("Calls WebSocket connection error: %s; retrying in %.0fs", e, backoff)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Calls WebSocket unexpected error: %s; retrying in %.0fs", e, backoff)
            finally:
                if sock is not None:
                    if self._calls_sock is sock:
                        self._calls_sock = None
                    await sock.close()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def handle_call_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "hello":
            if fmt := event.get("audioFormat"):
                self._calls_audio_format = fmt
            live_ids = {
                x.get("callId")
                for x in (event.get("currentOffer"), event.get("currentPendingOffer"), event.get("currentCall"))
                if isinstance(x, dict) and x.get("callId")
            }
            for cid, entry in list(self._active_calls.items()):
                if not entry["terminal"] and cid not in live_ids:
                    entry["terminal"] = True
                    entry["stage"] = "ended"
                    if not entry["rejected_by_tg"]:
                        entry["status_text"] = "⚪ 来电已结束或状态已重置"
                    entry["offered_event"].set()
                    await self._sync_call_msg(entry)
            if offer := event.get("currentOffer"):
                await self._on_call_offer(offer, stage="offered")
            if pending := event.get("currentPendingOffer"):
                await self._on_call_offer(pending, stage="pending")
            if call := event.get("currentCall"):
                await self._on_call_state(call)
            await self._on_conf_invitations(event.get("conferenceInvitations") or [])
        elif etype == "call.pending":
            if offer := event.get("offer"):
                await self._on_call_offer(offer, stage="pending")
            else:
                for entry in list(self._active_calls.values()):
                    if not entry["terminal"] and entry["stage"] == "pending":
                        entry["terminal"] = True
                        entry["stage"] = "ended"
                        if not entry["rejected_by_tg"]:
                            entry["status_text"] = "⚪ 来电已取消或已在其他设备处理"
                        entry["offered_event"].set()
                        await self._sync_call_msg(entry)
        elif etype == "call.offered":
            if offer := event.get("offer"):
                await self._on_call_offer(offer, stage="offered")
        elif etype == "call.state":
            if call := event.get("call"):
                await self._on_call_state(call)
        elif etype == "conference.invitations":
            await self._on_conf_invitations(event.get("invitations") or [])
        elif etype == "call.error":
            log.warning("WeChat call error event: %s", event.get("code"))

    async def _resolve_call_target(self, thread_id: str, caller_id: str, kind: str) -> tuple[dict | None, str]:
        try:
            thread = await self.wx.thread(thread_id)
        except WeChatError:
            name = thread_id
            if not thread_id.endswith("@chatroom"):
                try:
                    name = (await self.wx.contact(thread_id)).get("displayName") or thread_id
                except WeChatError:
                    pass
            thread = {"id": thread_id, "kind": "group" if kind == "conference" or thread_id.endswith("@chatroom") else "direct",
                      "displayName": name}
        if not self._allowed(thread):
            return None, ""
        if caller_id == thread_id and not thread_id.endswith("@chatroom"):
            caller_name = thread.get("displayName") or caller_id
        else:
            caller_name = await self._sender_name({"threadId": thread_id, "senderId": caller_id})
        return thread, caller_name or caller_id

    async def _send_call_tg_msg(self, thread: dict, text: str, buttons=None) -> tuple[int, int] | None:
        chat = await self.ensure_chat(thread)
        if chat is None:
            return None
        try:
            sent = await self.bot.send_message(chat, text, parse_mode="html", buttons=buttons, link_preview=False)
            return chat, sent.id
        except Exception as e:
            if not _dead_chat(e):
                raise
            log.warning("Telegram group %s for %s is gone (%s); creating a new one", chat, thread["id"], e)
            self.store.unlink(thread["id"])
            new_chat = await self.ensure_chat(thread)
            if not new_chat:
                return None
            sent = await self.bot.send_message(new_chat, text, parse_mode="html", buttons=buttons, link_preview=False)
            return new_chat, sent.id

    @staticmethod
    def _render_call(entry: dict) -> tuple[str, list | None]:
        title = "📹 <b>微信视频来电</b>" if entry["call_type"] == "video" else "📞 <b>微信语音来电</b>"
        text = (
            f"{title}\n"
            f"来电人：<b>{_esc(entry['caller_name'])}</b>\n"
            f"状态：{entry['status_text']}"
        )
        buttons = (
            [[Button.inline("📵 拒接", f"callrej:{entry['token']}".encode())]]
            if not entry["terminal"] and entry["stage"] in ("pending", "offered")
            else None
        )
        return text, buttons

    async def _sync_call_msg(self, entry: dict) -> None:
        if not entry.get("chat_id") or not entry.get("msg_id"):
            return
        text, buttons = self._render_call(entry)
        render_key = (text, bool(buttons))
        if entry.get("_last_render") == render_key:
            return
        try:
            await self.bot.edit_message(entry["chat_id"], entry["msg_id"], text, parse_mode="html",
                                        buttons=buttons, link_preview=False)
            entry["_last_render"] = render_key
        except errors.MessageNotModifiedError:
            entry["_last_render"] = render_key
        except Exception as e:
            log.info("Could not update call notification message for %s: %s", entry["call_id"], e)

    def _prune_calls(self) -> None:
        if len(self._active_calls) <= 200:
            return
        for cid, entry in list(self._active_calls.items()):
            if entry["terminal"]:
                self._active_calls.pop(cid, None)
                self._call_tokens.pop(entry["token"], None)
            if len(self._active_calls) <= 150:
                break

    async def _on_call_offer(self, offer: dict, *, stage: str) -> None:
        call_id = offer.get("callId")
        if not call_id:
            return
        if existing := self._active_calls.get(call_id):
            if not existing["terminal"]:
                if stage == "offered" or existing["stage"] == "pending":
                    existing["stage"] = stage
                existing["revision"] = offer.get("revision", existing["revision"])
                existing["phase"] = offer.get("phase")
                if stage == "offered":
                    existing["offered_event"].set()
                await self._sync_call_msg(existing)
            return

        caller_id = offer.get("callerId") or ""
        kind = offer.get("kind") or "direct"
        thread_id = offer.get("groupId") or caller_id
        if not thread_id:
            return
        thread, caller_name = await self._resolve_call_target(thread_id, caller_id or thread_id, kind)
        if thread is None:
            return

        self._prune_calls()
        token = secrets.token_hex(6)
        entry = {
            "call_id": call_id,
            "token": token,
            "kind": kind,
            "call_type": offer.get("callType") or "voice",
            "caller_id": caller_id,
            "caller_name": caller_name,
            "thread_id": thread["id"],
            "stage": stage,
            "phase": offer.get("phase"),
            "revision": offer.get("revision", 0),
            "chat_id": None,
            "msg_id": None,
            "terminal": False,
            "rejected_by_tg": False,
            "rejecting": False,
            "was_connected": False,
            "offered_event": asyncio.Event(),
            "status_text": "🔔 响铃中…",
            "_last_render": None,
        }
        if stage == "offered":
            entry["offered_event"].set()
        self._active_calls[call_id] = entry
        self._call_tokens[token] = call_id

        text, buttons = self._render_call(entry)
        res = await self._send_call_tg_msg(thread, text, buttons=buttons)
        if not res:
            return
        entry["chat_id"], entry["msg_id"] = res
        entry["_last_render"] = (text, bool(buttons))
        # In case call state updated while ensure_chat was creating the group:
        await self._sync_call_msg(entry)

    async def _on_call_state(self, call: dict) -> None:
        call_id = call.get("callId")
        if not call_id:
            return
        state = call.get("state") or ""
        entry = self._active_calls.get(call_id)
        if entry is None:
            if call.get("direction") != "incoming" or state in ("ending", "ended", "uncertain"):
                return
            caller_id = next(
                (c.get("participantId") for c in (call.get("controls") or []) if c.get("participantId")), ""
            )
            thread_id = call.get("groupId") or caller_id
            if not thread_id:
                return
            await self._on_call_offer({
                "callId": call_id,
                "revision": call.get("revision", 0),
                "kind": call.get("kind") or "direct",
                "callType": call.get("callType") or "voice",
                "callerId": caller_id,
                "groupId": call.get("groupId"),
            }, stage=state if state == "offered" else state)
            entry = self._active_calls.get(call_id)
            if entry is None:
                return

        entry["revision"] = call.get("revision", entry["revision"])
        if state == "offered":
            entry["stage"] = "offered"
            entry["status_text"] = "🔔 响铃中…"
            entry["offered_event"].set()
        elif state in ("dialing", "ringing"):
            entry["stage"] = state
            entry["status_text"] = "🔔 响铃中…"
        elif state == "connecting":
            entry["stage"] = "connecting"
            entry["status_text"] = "🟡 接通中…"
            entry["offered_event"].set()
        elif state == "connected":
            entry["stage"] = "connected"
            entry["was_connected"] = True
            entry["status_text"] = "🟢 通话中"
            entry["offered_event"].set()
        elif state in ("ending", "ended"):
            entry["stage"] = state
            entry["terminal"] = True
            if not entry["rejected_by_tg"]:
                entry["status_text"] = "⚪ 通话已结束" if entry["was_connected"] else "⚪ 来电已结束（未接听或已取消）"
            entry["offered_event"].set()
        elif state == "uncertain":
            entry["stage"] = "uncertain"
            entry["terminal"] = True
            if not entry["rejected_by_tg"]:
                entry["status_text"] = "⚠️ 来电状态未确认"
            entry["offered_event"].set()
        await self._sync_call_msg(entry)

    async def _on_conf_invitations(self, invitations: list[dict]) -> None:
        current_ids = {inv.get("invitationId") for inv in invitations if inv.get("invitationId")}
        for inv_id in list(self._conf_invites):
            if inv_id not in current_ids:
                old = self._conf_invites.pop(inv_id)
                old["status_text"] = "⚪ 邀请已结束或已过期"
                try:
                    await self.bot.edit_message(old["chat_id"], old["msg_id"],
                                                f"📞 <b>微信群语音通话邀请</b>\n"
                                                f"发起人：<b>{_esc(old['caller_name'])}</b>\n"
                                                f"状态：{old['status_text']}",
                                                parse_mode="html", link_preview=False)
                except errors.MessageNotModifiedError:
                    pass
                except Exception as e:
                    log.info("Could not update conference invitation %s: %s", inv_id, e)

        for inv in invitations:
            inv_id = inv.get("invitationId")
            group_id = inv.get("groupId") or ""
            caller_id = inv.get("callerId") or ""
            if not inv_id or not group_id or inv_id in self._conf_invites:
                continue
            thread, caller_name = await self._resolve_call_target(group_id, caller_id, "conference")
            if thread is None:
                continue
            text = (
                "📞 <b>微信群语音通话邀请</b>\n"
                f"发起人：<b>{_esc(caller_name)}</b>\n"
                "状态：🔔 邀请中…"
            )
            res = await self._send_call_tg_msg(thread, text)
            if res:
                self._conf_invites[inv_id] = {
                    "invitation_id": inv_id,
                    "group_id": group_id,
                    "caller_name": caller_name,
                    "chat_id": res[0],
                    "msg_id": res[1],
                    "status_text": "🔔 邀请中…",
                }

    async def _on_call_reject_callback(self, event, token: str) -> None:
        if event.sender_id != self.owner_id:
            await event.answer("只有桥接账号本人可以操作", alert=True)
            return
        call_id = self._call_tokens.get(token)
        entry = self._active_calls.get(call_id) if call_id else None
        if not entry or entry["terminal"] or entry["stage"] not in ("pending", "offered"):
            await event.answer("来电已结束或已处理")
            if entry:
                await self._sync_call_msg(entry)
            return
        if entry["rejecting"]:
            await event.answer("正在拒接中…")
            return
        sock = self._calls_sock
        if sock is None or sock.closed:
            await event.answer("通话信令未连接，无法拒接", alert=True)
            return

        entry["rejecting"] = True
        await event.answer("正在拒接…")
        readied = False
        try:
            if entry["stage"] == "pending":
                entry["offered_event"].clear()
                res = await sock.command({
                    "type": "call.ready",
                    "ready": True,
                    "format": self._calls_audio_format,
                    "scope": {
                        "kind": "offer",
                        "callId": call_id,
                        "expectedRevision": entry["revision"],
                    },
                })
                readied = True
                if res.get("status") == "error":
                    code = res.get("code") or "error"
                    if code == "offer_expired" and entry["stage"] == "pending" and not entry["terminal"]:
                        # Revision may have advanced to preparing; give the socket a moment to deliver call.offered
                        try:
                            await asyncio.wait_for(entry["offered_event"].wait(), timeout=2.0)
                        except asyncio.TimeoutError:
                            pass
                    if not (entry["stage"] == "offered" or entry["terminal"]):
                        await self._finish_reject_error(entry, code)
                        return
                if entry["stage"] == "pending" and not entry["terminal"]:
                    try:
                        await asyncio.wait_for(entry["offered_event"].wait(), timeout=10.0)
                    except asyncio.TimeoutError:
                        await self._report_call_reject_failure(entry, "等待来电就绪超时")
                        return

            if entry["terminal"] or entry["stage"] != "offered":
                await self._sync_call_msg(entry)
                return

            res = await sock.command({
                "type": "call.reject",
                "callId": call_id,
                "expectedRevision": entry["revision"],
            })
            if res.get("status") == "error" and res.get("code") == "stale" and not entry["terminal"] and entry["stage"] == "offered":
                res = await sock.command({
                    "type": "call.reject",
                    "callId": call_id,
                    "expectedRevision": entry["revision"],
                })
            if res.get("status") in ("accepted", "replayed"):
                entry["rejected_by_tg"] = True
                entry["terminal"] = True
                entry["stage"] = "ended"
                entry["status_text"] = "📵 已在 Telegram 拒接"
                await self._sync_call_msg(entry)
            else:
                await self._finish_reject_error(entry, res.get("code") or "error")
        except (OSError, WeChatError, asyncio.TimeoutError) as e:
            await self._report_call_reject_failure(entry, f"拒接请求失败：{e}")
        finally:
            entry["rejecting"] = False
            if readied and not sock.closed:
                try:
                    await sock.command({
                        "type": "call.ready",
                        "ready": False,
                        "format": self._calls_audio_format,
                    })
                except Exception:
                    pass

    async def _finish_reject_error(self, entry: dict, code: str) -> None:
        reasons = {
            "offer_expired": "来电已结束或已过期",
            "stale": "来电状态已变化，请重试",
            "already_claimed": "来电已被其他设备处理",
            "not_owner": "来电已被其他设备接管",
            "busy": "当前有其他未决通话",
            "forbidden": "API Token 缺少 calls 权限",
            "unsupported": "当前 agent-wechat 不支持拒接操作",
            "media_not_ready": "来电尚未准备就绪",
        }
        msg = reasons.get(code, f"拒接失败（{code}）")
        if code in ("offer_expired", "already_claimed", "not_owner"):
            entry["terminal"] = True
            entry["stage"] = "ended"
            entry["status_text"] = f"⚪ {msg}"
            await self._sync_call_msg(entry)
            return
        await self._report_call_reject_failure(entry, msg)

    async def _report_call_reject_failure(self, entry: dict, text: str) -> None:
        if entry.get("chat_id"):
            await self._report(entry["chat_id"], entry.get("msg_id"), text)

    # ======================================================================
    # Telegram -> WeChat
    # ======================================================================
    async def on_tg_message(self, event: events.NewMessage.Event) -> None:
        msg = event.message
        if msg.action or msg.via_bot_id:
            return
        chat_id = event.chat_id
        thread_id = self.store.thread_for_chat(chat_id)
        if not thread_id:
            return
        text = msg.message or ""
        if text.startswith("//"):
            return
        self._pair_tg_user_msg(chat_id, _tg_msg_fingerprint(msg), user_id=msg.id)
        cmd, _, arg = text.strip().partition(" ")
        cmd_name, _, at_bot = cmd.partition("@")
        if not at_bot or at_bot.lower() == self.bot_me.username.lower():
            cmd_name = cmd_name.lower()
            if cmd_name == "/wx":
                title = next((t for w, _, t, _ in self.store.list_threads() if w == thread_id), "")
                if self.wx_ready:
                    try:
                        thread = await self.wx.thread(thread_id)
                        title = thread.get("displayName") or title
                    except Exception:
                        pass
                head = f"对应微信会话：<b>{_esc(title)}</b>\n" if title else "对应微信会话："
                await self.bot.send_message(chat_id, f"{head}<code>{_esc(thread_id)}</code>",
                                            parse_mode="html", reply_to=await self._bot_reply_id(chat_id, msg.id))
                return
            if cmd_name == "/avatar":
                why = await self.sync_avatar(chat_id, thread_id)
                await self.bot.send_message(chat_id, f"没有拿到这个会话的微信头像（{why}）" if why else "已更新群头像",
                                            reply_to=await self._bot_reply_id(chat_id, msg.id))
                return
            if cmd_name in ("/rename", "/title"):
                title, status = await self.sync_title(chat_id, thread_id)
                if status == "unchanged":
                    reply = f"群标题已是最新：{_esc(title)}"
                elif status:
                    reply = f"无法同步群标题（{_esc(status)}）"
                else:
                    reply = f"已更新群标题：{_esc(title)}"
                await self.bot.send_message(chat_id, reply, parse_mode="html",
                                            reply_to=await self._bot_reply_id(chat_id, msg.id))
                return
            if cmd_name == "/unlink":
                self.store.unlink(thread_id)
                await self.bot.send_message(
                    chat_id,
                    f"已解除当前群与微信会话 <code>{_esc(thread_id)}</code> 的绑定。",
                    parse_mode="html", reply_to=await self._bot_reply_id(chat_id, msg.id))
                return
            if cmd_name == "/link":
                await self._handle_group_link(event, arg.strip())
                return
            if cmd_name == "/help":
                await self.bot.send_message(chat_id, GROUP_HELP, parse_mode="html",
                                            reply_to=await self._bot_reply_id(chat_id, msg.id))
                return
            if cmd_name in ("/members", "/at"):
                await self._send_members(chat_id, await self._bot_reply_id(chat_id, msg.id), thread_id, arg.strip())
                return
            if cmd_name == "/recall":
                reply_id = msg.reply_to.reply_to_msg_id if msg.reply_to and msg.reply_to.reply_to_msg_id else None
                if not reply_id:
                    await self.bot.send_message(chat_id, "请回复你要撤回的那条消息再发送 /recall",
                                                reply_to=await self._bot_reply_id(chat_id, msg.id))
                    return
                hit = self.store.wx_for_tg(chat_id, reply_id)
                if not hit:
                    await self._report(chat_id, await self._bot_reply_id(chat_id, msg.id),
                                       "找不到这条消息对应的微信记录")
                    return
                ok, info = await self._recall_tg_msg(chat_id, reply_id, thread_id, hit[0], hit[1], "recall-cmd")
                if ok:
                    target_reply = await self._bot_reply_id(chat_id, reply_id, wait=False)
                    if target_reply is None:
                        target_reply = await self._bot_reply_id(chat_id, msg.id)
                    await self.bot.send_message(chat_id, "↩️ " + info, reply_to=target_reply, silent=True)
                else:
                    await self._report(chat_id, await self._bot_reply_id(chat_id, msg.id), info)
                return
        if text.startswith("/") and not msg.media:
            return  # other commands are not forwarded

        quote_ref = None
        if msg.reply_to and msg.reply_to.reply_to_msg_id:
            hit = self.store.wx_for_tg(chat_id, msg.reply_to.reply_to_msg_id)
            if hit and hit[0] and hit[1]:
                quote_ref = {"messageId": hit[0], "serverId": hit[1]}

        mentions = await self._resolve_mentions(thread_id, text) if text else None

        label = _describe_tg_msg(msg)
        fail_prefix = f"发送{label}到微信失败：" if label.startswith("「") else f"发送[{label}]到微信失败："
        key = f"tg-{chat_id}-{msg.id}"
        self.pending_sends[thread_id] += 1
        try:
            result = None
            if msg.media and not isinstance(msg.media, (types.MessageMediaWebPage,)):
                native_stickers = await self.wx.supports_stickers() if msg.sticker else False
                kind, media = await self._tg_media(msg, native_stickers=native_stickers)
                if kind is None:
                    await self._report(chat_id, await self._bot_reply_id(chat_id, msg.id), f"{fail_prefix}{media}")
                else:
                    if kind == "sticker":
                        result = await self.wx.send_sticker(thread_id, media, key)
                    else:
                        result = await self.wx.send_media(thread_id, kind, media, key)
                    self._record_send(chat_id, msg.id, thread_id, result, media=True)
                    if text:  # caption goes as a separate text message
                        result = await self.wx.send_text(thread_id, text, key + "-caption", mentions=mentions)
                        self._record_send(chat_id, msg.id, thread_id, result)
            elif text:
                result = None
                if quote_ref:
                    try:
                        result = await self.wx.send_text(thread_id, text, key, quote_ref, mentions)
                    except WeChatError as e:
                        if e.status >= 500:
                            raise
                        log.info("WeChat refused the quote (%s); sending without it", e.code)
                    if result is not None and result.get("state") == "failed":
                        log.info("Quote send failed (%s); sending without it", result.get("errorCode"))
                        result = None
                    if result is None:
                        result = await self.wx.send_text(thread_id, text, key + "-plain", mentions=mentions)
                else:
                    result = await self.wx.send_text(thread_id, text, key, mentions=mentions)
                self._record_send(chat_id, msg.id, thread_id, result)
            if result is not None:
                state = result.get("state")
                if state not in SEND_OK:
                    reason = result.get("errorCode") or result.get("reason") or state
                    hint = "（结果未知，请在手机上确认，不要重复发送）" if state == "needs_review" else ""
                    await self._report(chat_id, await self._bot_reply_id(chat_id, msg.id),
                                       f"{fail_prefix}{reason}{hint}")
                elif self.cfg.bridge.mark_read_on_reply:
                    try:
                        await self.wx.mark_read(thread_id)
                    except Exception:
                        pass
        except WeChatError as e:
            await self._report(chat_id, await self._bot_reply_id(chat_id, msg.id), f"{fail_prefix}{e.code}")
        except Exception as e:
            log.exception("Telegram -> WeChat failed")
            err_detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            await self._report(chat_id, await self._bot_reply_id(chat_id, msg.id), f"{fail_prefix}{err_detail}")
        finally:
            self.pending_sends[thread_id] -= 1

    async def _resolve_mentions(self, thread_id: str, text: str) -> dict | None:
        if not thread_id.endswith("@chatroom") or "@" not in text:
            return None
        if (m := _match_mentions(text, [])) and not any(
            (pos := i) >= 0 and (pos == 0 or not text[pos - 1].isalnum())
            and not (text[pos + 1:].startswith("所有人") and _is_mention_boundary("所有人", text[pos + 4:]))
            and not (text[pos + 1:pos + 4].lower() == "all" and _is_mention_boundary("all", text[pos + 4:]))
            for i, ch in enumerate(text) if ch == "@"
        ):
            return m
        try:
            members = await self.wx.group_members(thread_id)
        except WeChatError as e:
            log.info("Could not load group members for %s (%s); sending without mentions", thread_id, e.code)
            return _match_mentions(text, [])
        return _match_mentions(text, members)

    async def _send_members(self, chat_id: int, reply_to: int, thread_id: str, query: str) -> None:
        if not thread_id.endswith("@chatroom"):
            await self.bot.send_message(chat_id, "只有在微信群聊里才能查看群成员", reply_to=reply_to)
            return
        try:
            members = await self.wx.group_members(thread_id)
        except WeChatError as e:
            await self.bot.send_message(chat_id, f"获取群成员失败：{e.code}", reply_to=reply_to)
            return
        if query:
            q = query.lower()
            members = [m for m in members if any(
                q in str(m.get(k) or "").lower()
                for k in ("mentionName", "roomDisplayName", "displayName", "id")
            )]
        if not members:
            await self.bot.send_message(chat_id, "没有匹配的群成员" if query else "没有拿到群成员", reply_to=reply_to)
            return
        lines = []
        for m in members:
            label = _member_label(m)
            disp = (m.get("displayName") or "").strip()
            extra = f" （{_esc(disp)}）" if disp and disp != label else ""
            lines.append(f"• <code>@{_esc(label)}</code>{extra}")
        header = f"<b>群成员</b>（{len(members)} 人，点按可复制）：\n也可直接发送 <code>@所有人</code>\n\n"
        pages: list[str] = []
        cur = header
        for line in lines:
            if len(cur) + len(line) + 1 > TG_TEXT_LIMIT:
                pages.append(cur.rstrip())
                cur = ""
            cur += line + "\n"
        if cur:
            pages.append(cur.rstrip())
        for i, part in enumerate(pages):
            await self.bot.send_message(chat_id, part, parse_mode="html", reply_to=reply_to if i == 0 else None)

    def _record_send(self, chat_id: int, tg_msg: int, thread_id: str, result: dict, media: bool = False) -> None:
        res = result.get("result") if isinstance(result.get("result"), dict) else {}
        server = (result.get("serverMessageId") or res.get("serverMessageId")) if media else result.get("serverId")
        local = result.get("localMessageId")
        if media and res:
            ids = res.get("messageIds") or []
            local = ids[0] if ids else res.get("messageId") or local
        if server:
            self.store.add_sent(thread_id, server)
        # One Telegram message may become two WeChat sends (media + caption); quotes point at the first.
        if (server or local) and self.store.wx_for_tg(chat_id, tg_msg) is None:
            self.store.map_message(chat_id, "user", tg_msg, thread_id, local, server)

    async def _tg_media(self, msg, *, native_stickers: bool = True) -> tuple[str | None, Media | str]:
        f = msg.file
        size = f.size if f else 0
        if size and size > self.max_media:
            return None, f"文件太大（{size // 1048576} MB），超过 max_media_mb 限制"
        data = await msg.download_media(file=bytes)
        if not data:
            return None, "无法下载这条 Telegram 媒体"
        mime = (f.mime_type if f and f.mime_type else "application/octet-stream").lower()
        ext = (getattr(f, "ext", None) or "").lower()
        name = (f.name if f and f.name else None) or f"telegram{ext}"
        if msg.photo:
            return "image", Media(data, "image/jpeg", "photo.jpg")
        if msg.sticker:
            if mime in ("application/octet-stream", "") or not mime.startswith(("image/", "video/", "application/x-tgsticker")):
                mime = {".webp": "image/webp", ".tgs": "application/x-tgsticker", ".webm": "video/webm"}.get(ext, mime)
            if native_stickers:
                converted = await stickers.to_wechat_sticker(data, mime)
                if isinstance(converted, str):
                    return None, converted
                _, out = converted
                return "sticker", Media(out, "image/gif", "sticker.gif")
            converted = await stickers.convert(data, mime)
            if isinstance(converted, str):
                return None, converted
            kind, out = converted
            if kind == "video":
                return "video", Media(out, "video/mp4", "sticker.mp4")
            return "image", Media(out, "image/jpeg", "sticker.jpg")
        if msg.voice:
            return "voice", Media(data, "audio/ogg", "voice.ogg")
        if msg.video or msg.video_note or msg.gif:
            return "video", Media(data, "video/mp4", name if name.endswith(".mp4") else "video.mp4")
        if img := await asyncio.to_thread(_image_from_file, data, mime, name, ext):
            return "image", img
        return "file", Media(data, mime, name)

    @staticmethod
    def _recall_expired_msg(key_prefix: str) -> str:
        if key_prefix == "recall-del":
            return "已超过微信 2 分钟撤回时限，微信端未撤回（仅从 Telegram 删除）"
        return "已超过微信 2 分钟撤回时限，无法从微信撤回"

    async def _recall_tg_msg(self, chat_id: int, tg_msg: int, thread_id: str, wx_msg: str | None,
                               wx_server: str | None, key_prefix: str) -> tuple[bool, str]:
        if not wx_msg and wx_server:
            try:
                for m in await self.wx.thread_messages(thread_id, limit=30):
                    if str(m.get("serverId") or "") == str(wx_server):
                        wx_msg = m.get("id")
                        if wx_msg:
                            self.store.map_message(chat_id, "user", tg_msg, thread_id, wx_msg, wx_server)
                        break
            except Exception as e:
                log.info("Could not resolve wx message id for recall (%s): %s", wx_server, e)
        if not wx_msg or not wx_server:
            return False, "这条消息还没有可用的微信消息 ID，暂时无法撤回"
        tag = (thread_id, str(wx_server))
        if len(self.tg_recalls) > 500:
            self.tg_recalls.clear()
        self.tg_recalls.add(tag)
        try:
            op = await self.wx.recall_message(wx_msg, str(wx_server), f"{key_prefix}-{chat_id}-{tg_msg}")
        except WeChatError as e:
            self.tg_recalls.discard(tag)
            if e.code == "recall_window_expired":
                return False, self._recall_expired_msg(key_prefix)
            return False, f"微信拒绝撤回（{e.code}）"
        state = op.get("state")
        if state in ("succeeded", "completed"):
            return True, "已从微信撤回"
        if state in ("failed", "expired", "rejected", "cancelled"):
            self.tg_recalls.discard(tag)
            err = op.get("error") or op.get("result") or {}
            code = err.get("code") if isinstance(err, dict) else None
            if code == "recall_window_expired":
                return False, self._recall_expired_msg(key_prefix)
            return False, f"微信撤回失败（{code or state}）"
        if state == "needs_review":
            return False, "撤回结果未确认（needs_review），请在手机微信确认"
        return False, f"撤回状态未确认（{state or 'unknown'}），请在手机微信确认"

    def _schedule_delete_notice(self, chat_id: int, msg_id: int, delay: float) -> None:
        async def _drop() -> None:
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                await self.bot.delete_messages(chat_id, [msg_id])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.info("Could not auto-delete recall notice %s in chat %s: %s", msg_id, chat_id, e)

        task = asyncio.create_task(_drop())
        self._notice_tasks.add(task)
        task.add_done_callback(self._notice_tasks.discard)

    async def on_tg_delete(self, event) -> None:
        deleted = getattr(event, "deleted_ids", None) or []
        # Bulk deletions (including Telegram "Clear History" in basic groups) emit all
        # deleted IDs together; only recall when the user explicitly deletes a single message.
        if len(deleted) != 1:
            return
        tg_msg = deleted[0]
        row = self.store.lookup_tg_delete(getattr(event, "chat_id", None), tg_msg)
        if not row:
            return
        chat_id, thread_id, wx_msg, wx_server = row
        if not (wx_server and self.store.is_sent(thread_id, wx_server)):
            return
        ok, info = await self._recall_tg_msg(chat_id, tg_msg, thread_id, wx_msg, wx_server, "recall-del")
        if ok:
            try:
                notice = await self.bot.send_message(chat_id, "↩️ " + info, silent=True)
            except Exception as e:
                log.info("Could not notify recall in chat %s: %s", chat_id, e)
                return
            if notice is not None and getattr(notice, "id", None) is not None:
                self._schedule_delete_notice(chat_id, notice.id, self._recall_notice_ttl)
        else:
            await self._report(chat_id, None, info)

    async def _report(self, chat_id: int, reply_to: int | None, text: str) -> None:
        try:
            await self.bot.send_message(chat_id, "⚠️ " + text, reply_to=reply_to)
        except Exception:
            if reply_to is not None:
                try:
                    await self.bot.send_message(chat_id, "⚠️ " + text)
                    return
                except Exception:
                    pass
            log.warning("%s (chat %s)", text, chat_id)

    async def on_raw_update(self, update: types.UpdateNewMessage) -> None:
        """Telegram may upgrade a basic group to a supergroup; follow the new chat id."""
        m = update.message
        if isinstance(m, types.MessageService) and isinstance(m.action, types.MessageActionChatMigrateTo):
            old = utils.get_peer_id(m.peer_id)
            new = utils.get_peer_id(types.PeerChannel(m.action.channel_id))
            if self.store.thread_for_chat(old):
                self.store.migrate_chat(old, new)
                log.info("Telegram group %s was upgraded to supergroup %s", old, new)

    # ======================================================================
    # interactive UI & bot commands (zero-ID /link, /find, /open)
    # ======================================================================
    def _make_ui_token(self, payload: dict) -> bytes:
        if len(self._ui_tokens) >= 400:
            for k in list(self._ui_tokens)[:200]:
                self._ui_tokens.pop(k, None)
        token = secrets.token_hex(6)
        self._ui_tokens[token] = payload
        return f"ui:{token}".encode()

    async def _wx_search_candidates(self, query: str, limit: int = 10) -> list[dict]:
        q = query.strip()
        items: list[dict] = []
        seen: set[str] = set()

        def add(raw: dict, default_kind: str | None = None) -> None:
            tid = str(raw.get("id") or "").strip()
            if not tid or tid in seen:
                return
            seen.add(tid)
            kind = raw.get("kind") or default_kind or ("group" if tid.endswith("@chatroom") else "direct")
            name = (raw.get("displayName") or raw.get("remark") or raw.get("nickname") or tid).strip()
            items.append({"id": tid, "name": name, "kind": kind})

        if not q:
            try:
                for t in await self.wx.list_threads(limit=limit):
                    add(t)
            except WeChatError as e:
                log.debug("list_threads failed: %s", e.code)
            return items[:limit]

        if " " not in q:
            try:
                add(await self.wx.thread(q))
            except WeChatError:
                pass
        try:
            for c in await self.wx.search_contacts(q, limit=limit):
                add(c, "direct")
        except WeChatError as e:
            log.debug("search_contacts failed: %s", e.code)
        try:
            for g in await self.wx.search_groups(q, limit=limit):
                add(g, "group")
        except WeChatError as e:
            log.debug("search_groups failed: %s", e.code)
        if len(items) < limit:
            ql = q.lower()
            try:
                for t in await self.wx.list_threads(limit=30):
                    name = str(t.get("displayName") or "")
                    tid = str(t.get("id") or "")
                    if ql in name.lower() or ql in tid.lower():
                        add(t)
            except WeChatError:
                pass
        return items[:limit]

    async def _list_tg_groups(self, limit: int = 12) -> list[tuple[int, str, str | None]]:
        iter_dialogs = getattr(self.user, "iter_dialogs", None)
        if not callable(iter_dialogs):
            return []
        unlinked: list[tuple[int, str, str | None]] = []
        linked: list[tuple[int, str, str | None]] = []
        try:
            async for d in iter_dialogs(limit=80):
                if not getattr(d, "is_group", False):
                    continue
                cid = d.id
                title = (getattr(d, "title", None) or getattr(d, "name", None) or str(cid)).strip()
                wx_id = self.store.thread_for_chat(cid)
                entry = (cid, title, wx_id)
                (linked if wx_id else unlinked).append(entry)
        except Exception as e:
            log.info("Could not list Telegram dialogs: %s", e)
            return []
        return (unlinked + linked)[:limit]

    def _wx_picker_buttons(self, cands: list[dict], *, action: str, extra: dict | None = None,
                           current_chat: int | None = None) -> list[list]:
        rows: list[list] = []
        for c in cands[:8]:
            icon = "👥" if c["kind"] == "group" else "👤"
            linked_chat = self.store.chat_for_thread(c["id"])
            if current_chat is not None and linked_chat == current_chat:
                suffix = "（当前）"
            elif linked_chat is not None:
                suffix = "（已绑）"
            else:
                suffix = ""
            payload = {"action": action, "wx_id": c["id"], "wx_name": c["name"], "kind": c["kind"], **(extra or {})}
            rows.append([Button.inline(f"{icon} {c['name'][:22]}{suffix}", self._make_ui_token(payload))])
        rows.append([Button.inline("❌ 取消", self._make_ui_token({"action": "cancel"}))])
        return rows

    def _tg_picker_buttons(self, groups: list[tuple[int, str, str | None]], *, action: str,
                           extra: dict | None = None) -> list[list]:
        rows: list[list] = []
        for cid, title, wx_id in groups[:10]:
            prefix = "🔗" if wx_id else "💬"
            suffix = "（已绑）" if wx_id else ""
            payload = {"action": action, "tg_chat": cid, "tg_title": title, **(extra or {})}
            rows.append([Button.inline(f"{prefix} {title[:22]}{suffix}", self._make_ui_token(payload))])
        rows.append([Button.inline("❌ 取消", self._make_ui_token({"action": "cancel"}))])
        return rows

    async def _send_group_bot_message(self, chat_id: int, text: str, *, reply_to: int | None = None,
                                      buttons=None, silent: bool = False):
        try:
            return await self.bot.send_message(
                chat_id, text, parse_mode="html", reply_to=reply_to, buttons=buttons, silent=silent)
        except Exception as e:
            if not _dead_chat(e):
                raise
        try:
            if chat_id < -1000000000000:
                await self.user(functions.channels.InviteToChannelRequest(
                    channel=await self.user.get_input_entity(chat_id), users=[self.bot_input]))
            else:
                await self.user(functions.messages.AddChatUserRequest(
                    chat_id=utils.resolve_id(chat_id)[0], user_id=self.bot_input, fwd_limit=0))
        except Exception as inv_err:
            log.info("Could not invite bot to group %s: %s", chat_id, inv_err)
            return None
        try:
            return await self.bot.send_message(
                chat_id, text, parse_mode="html", reply_to=None, buttons=buttons, silent=silent)
        except Exception as e2:
            log.info("Bot still could not send to group %s after invite: %s", chat_id, e2)
            return None

    async def _send_group_notice(self, chat_id: int, reply_to: int | None, text: str, buttons=None) -> None:
        sent = await self._send_group_bot_message(chat_id, text, reply_to=reply_to, buttons=buttons)
        if sent is None and hasattr(self.user, "send_message"):
            try:
                await self.user.send_message(
                    chat_id, f"⚠️ 请先把 @{self.bot_me.username} 拉进这个群，再发送 /link", reply_to=reply_to)
            except Exception as e:
                log.info("Fallback user notice in %s failed: %s", chat_id, e)

    async def on_tg_unlinked_command(self, event) -> None:
        msg = event.message
        if not msg or msg.action or msg.via_bot_id:
            return
        text = (msg.message or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd_name, _, at_bot = cmd.partition("@")
        if at_bot and at_bot.lower() != self.bot_me.username.lower():
            return
        if cmd_name.lower() == "/link":
            await self._handle_group_link(event, arg.strip())

    async def _handle_group_link(self, event, arg: str) -> None:
        chat_id = event.chat_id
        msg_id = event.message.id
        if not self.wx_ready:
            await self._send_group_notice(chat_id, msg_id, "微信还没有登录，请先在私聊中扫码或发 /wxlogin。")
            return
        chat_obj = getattr(event, "chat", None)
        if chat_obj is None and hasattr(event, "get_chat"):
            try:
                chat_obj = await event.get_chat()
            except Exception:
                chat_obj = None
        tg_title = (getattr(chat_obj, "title", None) or str(chat_id)).strip()
        cands = await self._wx_search_candidates(arg, limit=10)
        if arg and " " not in arg:
            exact = next((c for c in cands if c["id"] == arg), None)
            if exact:
                wx_title, _ = await self._complete_link(exact["id"], chat_id, exact["name"], notify_group=False)
                await self._send_group_notice(
                    chat_id, msg_id,
                    f"✅ 已将当前群绑定到微信会话：<b>{_esc(wx_title)}</b>\n<code>{_esc(exact['id'])}</code>")
                return
        if not cands:
            hint = (f"没有找到匹配「{_esc(arg)}」的微信联系人或群。" if arg
                    else "暂时没有获取到最近的微信会话，请发 <code>/link 关键词</code> 搜索。")
            await self._send_group_notice(chat_id, msg_id, hint)
            return
        buttons = self._wx_picker_buttons(
            cands, action="link_bind", extra={"tg_chat": chat_id, "tg_title": tg_title, "in_group": True},
            current_chat=chat_id)
        header = f"<b>搜索「{_esc(arg)}」的结果</b>" if arg else "<b>选择要绑定到此群的微信会话</b>"
        tip = "点下方按钮即可绑定（也可发 <code>/link 关键词</code> 缩小范围）："
        await self._send_group_notice(chat_id, msg_id, f"{header}\n{tip}", buttons=buttons)

    async def _complete_link(self, wx_id: str, tg_chat: int, wx_name: str = "",
                             *, notify_group: bool = False) -> tuple[str, bool]:
        title = wx_name.strip()
        if not title and self.wx_ready:
            try:
                title = (await self.wx.thread(wx_id)).get("displayName") or ""
            except WeChatError:
                if not wx_id.endswith("@chatroom"):
                    try:
                        title = (await self.wx.contact(wx_id)).get("displayName") or ""
                    except WeChatError:
                        pass
        title = title or wx_id
        self.store.link(wx_id, tg_chat, title)
        bot_ok = True
        if notify_group:
            sent = await self._send_group_bot_message(
                tg_chat,
                f"🔗 已将此群绑定到微信会话：<b>{_esc(title)}</b>\n<code>{_esc(wx_id)}</code>",
                silent=True)
            bot_ok = sent is not None
        return title, bot_ok

    async def _edit_ui_msg(self, event, text: str, buttons=None) -> None:
        if hasattr(event, "edit"):
            try:
                await event.edit(text, parse_mode="html", buttons=buttons)
                return
            except errors.MessageNotModifiedError:
                return
            except Exception:
                pass
        chat_id = getattr(event, "chat_id", None) or self.owner_id
        if chat_id is not None:
            await self.bot.send_message(chat_id, text, parse_mode="html", buttons=buttons)

    async def _on_link_search_input(self, event, query: str) -> None:
        wait = self._link_wait
        if not wait or wait.get("mode") != "search_wx":
            return
        if not self.wx_ready:
            self._link_wait = None
            await event.reply("微信还没有登录，请先扫描我发的二维码，或发 /wxlogin。")
            return
        cands = await self._wx_search_candidates(query, limit=10)
        if not cands:
            cancel_btn = [[Button.inline("❌ 取消", self._make_ui_token({"action": "cancel"}))]]
            await event.reply(
                f"没有找到匹配「{_esc(query)}」的微信联系人或群，请换个关键词重试：",
                parse_mode="html", buttons=cancel_btn)
            return
        tg_chat, tg_title = wait["tg_chat"], wait["tg_title"]
        buttons = self._wx_picker_buttons(
            cands, action="link_bind", extra={"tg_chat": tg_chat, "tg_title": tg_title, "in_group": False},
            current_chat=tg_chat)
        await event.reply(
            f"搜索「<b>{_esc(query)}</b>」的结果（将绑定到 <b>{_esc(tg_title)}</b>）：",
            parse_mode="html", buttons=buttons)

    async def _on_ui_callback(self, event, token: str) -> None:
        if event.sender_id != self.owner_id:
            await event.answer("只有桥接账号本人可以操作", alert=True)
            return
        payload = self._ui_tokens.get(token)
        if not payload:
            await event.answer("按钮已过期，请重新发送命令", alert=True)
            return
        action = payload.get("action")
        if action == "cancel":
            self._link_wait = None
            await self._edit_ui_msg(event, "已取消操作。")
            await event.answer("已取消")
            return
        if action == "open_wx":
            if not self.wx_ready:
                await event.answer("微信还没有登录，请先 /wxlogin", alert=True)
                return
            wx_id = payload["wx_id"]
            existed = self.store.chat_for_thread(wx_id) is not None
            try:
                thread = await self.wx.thread(wx_id)
            except WeChatError:
                thread = {"id": wx_id, "kind": payload.get("kind", "direct"),
                          "displayName": payload.get("name") or wx_id}
            await self.ensure_chat(thread)
            title = self._title(thread)
            msg = (f"ℹ️ <b>{_esc(title)}</b> 已经有对应的 Telegram 群了" if existed
                   else f"✅ 已建立 Telegram 群：<b>{_esc(title)}</b>")
            await self._edit_ui_msg(event, msg)
            await event.answer("已有群" if existed else "已建群")
            return
        if action == "link_pick_tg":
            if not self.wx_ready:
                await event.answer("微信还没有登录，请先 /wxlogin", alert=True)
                return
            tg_chat, tg_title = payload["tg_chat"], payload["tg_title"]
            self._link_wait = {"mode": "search_wx", "tg_chat": tg_chat, "tg_title": tg_title}
            cands = await self._wx_search_candidates("", limit=8)
            buttons = self._wx_picker_buttons(
                cands, action="link_bind", extra={"tg_chat": tg_chat, "tg_title": tg_title, "in_group": False},
                current_chat=tg_chat)
            prompt = (
                f"已选 Telegram 群：<b>{_esc(tg_title)}</b>\n\n"
                + ("请点选下方最近的微信会话，或<b>直接回复微信名称/关键词</b>搜索："
                   if cands else "请<b>直接回复微信名称/关键词</b>搜索要绑定的会话：")
            )
            await self._edit_ui_msg(event, prompt, buttons=buttons)
            await event.answer()
            return
        if action == "link_pick_wx":
            wx_id, wx_name = payload["wx_id"], payload["wx_name"]
            groups = await self._list_tg_groups(limit=12)
            if not groups:
                await self._edit_ui_msg(
                    event,
                    f"已选微信会话：<b>{_esc(wx_name)}</b>\n\n"
                    f"未检测到可选择的 Telegram 群。请直接到目标 Telegram 群里发送 <code>/link {_esc(wx_id)}</code>。")
                await event.answer()
                return
            buttons = self._tg_picker_buttons(
                groups, action="link_bind", extra={"wx_id": wx_id, "wx_name": wx_name, "in_group": False})
            await self._edit_ui_msg(
                event, f"已选微信会话：<b>{_esc(wx_name)}</b>\n\n请点选要绑定的 Telegram 群：", buttons=buttons)
            await event.answer()
            return
        if action == "link_bind":
            self._link_wait = None
            wx_id = payload["wx_id"]
            wx_name = payload.get("wx_name") or ""
            tg_chat = payload["tg_chat"]
            tg_title = payload.get("tg_title") or str(tg_chat)
            in_group = bool(payload.get("in_group"))
            wx_title, bot_ok = await self._complete_link(wx_id, tg_chat, wx_name, notify_group=not in_group)
            if in_group:
                text = f"✅ 已将当前群绑定到微信会话：<b>{_esc(wx_title)}</b>\n<code>{_esc(wx_id)}</code>"
            else:
                text = f"✅ 已绑定：<b>{_esc(tg_title)}</b> ↔ <b>{_esc(wx_title)}</b>\n<code>{_esc(wx_id)}</code>"
                if not bot_ok:
                    text += f"\n\n⚠️ 机器人暂未进入该群，请把 @{_esc(self.bot_me.username)} 拉进「{_esc(tg_title)}」后再使用。"
            await self._edit_ui_msg(event, text)
            await event.answer("绑定成功")
            return
        await event.answer()

    # ======================================================================
    # bot commands (private chat with the bot, from you only)
    # ======================================================================
    async def on_bot_command(self, event) -> None:
        cmd, _, arg = event.raw_text.partition(" ")
        cmd, arg = cmd.split("@")[0].lower(), arg.strip()
        try:
            if cmd in ("/start", "/help"):
                await event.reply(HELP, parse_mode="html")
            elif cmd == "/status":
                up = int(time.time() - self.started)
                state = {"online": "在线", "disconnected": "连接断开，重连中", "signed_out": "已退出登录（发 /wxlogin）",
                         "unknown": "未登录（发 /wxlogin）"}.get(self.wx_state, self.wx_state)
                await event.reply(f"运行 {up // 3600}h{up % 3600 // 60}m，已转发 {self.forwarded} 条，"
                                  f"已连接 {len(self.store.linked_chats())} 个会话。\n"
                                  f"微信：{state}，账号 <code>{_esc(self.wx.account or '-')}</code>", parse_mode="html")
            elif cmd in ("/chats", "/list"):
                rows = self.store.list_threads()
                kw = arg.lower()
                if kw:
                    rows = [r for r in rows if kw in r[0].lower() or kw in r[2].lower() or kw in str(r[1])]
                if not rows:
                    return await event.reply("没有找到匹配的已连接会话" if kw else "还没有任何已连接的会话")
                lines = [
                    f"• {_esc(title or wx_id)} — <code>{_esc(wx_id)}</code> ↔ <code>{tg_chat}</code>"
                    for wx_id, tg_chat, title, _ in rows[:40]
                ]
                suffix = f"\n\n共 {len(rows)} 个会话（仅显示前 40 个）" if len(rows) > 40 else f"\n\n共 {len(rows)} 个会话"
                await event.reply("\n".join(lines) + suffix, parse_mode="html")
            elif cmd == "/link":
                parts = arg.split()
                if len(parts) == 2:
                    try:
                        tg_chat = int(parts[1])
                    except ValueError:
                        tg_chat = None
                    if tg_chat is not None:
                        wx_id = parts[0]
                        wx_title, _ = await self._complete_link(wx_id, tg_chat, notify_group=False)
                        await event.reply(
                            f"已绑定：<b>{_esc(wx_title)}</b> (<code>{_esc(wx_id)}</code> ↔ <code>{tg_chat}</code>)",
                            parse_mode="html")
                        return
                if not self.wx_ready:
                    return await event.reply(
                        "🔐 微信尚未登录，请先在手机确认或扫码，也可点击下方按钮重新登录。",
                        buttons=self._wx_login_buttons())
                if arg:
                    cands = await self._wx_search_candidates(arg, limit=10)
                    if not cands:
                        return await event.reply(
                            f"没有找到匹配「{_esc(arg)}」的微信联系人或群。\n"
                            "💡 也可以直接发送 <code>/link</code> 选择 Telegram 群，或到目标群里直接发送 <code>/link</code>。",
                            parse_mode="html")
                    buttons = self._wx_picker_buttons(cands, action="link_pick_wx")
                    return await event.reply(
                        f"<b>搜索「{_esc(arg)}」的结果</b>\n请先点选要绑定的微信会话：",
                        parse_mode="html", buttons=buttons)
                groups = await self._list_tg_groups(limit=12)
                if not groups:
                    return await event.reply(
                        "没有检测到可选择的 Telegram 群。\n\n"
                        "💡 <b>最简单的绑定方式</b>：直接到你要绑定的 Telegram 群里发送 <code>/link</code>。\n"
                        "也可以先发送 <code>/link 微信名称</code> 搜索微信会话。",
                        parse_mode="html")
                buttons = self._tg_picker_buttons(groups, action="link_pick_tg")
                await event.reply(
                    "<b>绑定 Telegram 群与微信会话</b>\n\n"
                    "💡 <b>最省事的方式</b>：直接在目标 Telegram 群里发 <code>/link</code>（无需查 ID）。\n\n"
                    "也可以点选下方 Telegram 群（未绑定的群排在前面）：",
                    parse_mode="html", buttons=buttons)
            elif cmd == "/wxlogin":
                await self.request_wechat_login(force_qr=(arg.lower() in ("qr", "scan")))
            elif cmd in ("/find", "/open") and not self.wx_ready:
                await event.reply(
                    "🔐 微信尚未登录，请先在手机确认或扫码，也可点击下方按钮重新登录。",
                    buttons=self._wx_login_buttons())
            elif cmd == "/find":
                cands = await self._wx_search_candidates(arg, limit=15 if arg else 10)
                if not cands:
                    return await event.reply("没有找到匹配的微信联系人或群" if arg else "暂时没有获取到最近的微信会话")
                lines = []
                for c in cands:
                    icon = "👥" if c["kind"] == "group" else "👤"
                    tag = "（已建群）" if self.store.chat_for_thread(c["id"]) is not None else ""
                    lines.append(f"{icon} <b>{_esc(c['name'])}</b>{tag} — <code>{_esc(c['id'])}</code>")
                buttons = []
                for c in cands[:8]:
                    has_chat = self.store.chat_for_thread(c["id"]) is not None
                    lbl = f"{'💬 打开' if has_chat else '➕ 建群'}：{c['name'][:22]}"
                    tok = self._make_ui_token(
                        {"action": "open_wx", "wx_id": c["id"], "name": c["name"], "kind": c["kind"]})
                    buttons.append([Button.inline(lbl, tok)])
                header = (f"<b>搜索「{_esc(arg)}」的结果</b>（点按钮可直接建群）：\n\n" if arg
                          else "<b>最近的微信会话</b>（点按钮可直接建群）：\n\n")
                await event.reply(header + "\n".join(lines), parse_mode="html", buttons=buttons)
            elif cmd == "/open":
                if not arg:
                    cands = await self._wx_search_candidates("", limit=10)
                    if not cands:
                        return await event.reply("暂时没有获取到最近的微信会话，请直接发 <code>/open 微信名称</code> 搜索。",
                                                 parse_mode="html")
                    buttons = [
                        [Button.inline(
                            f"{'💬' if self.store.chat_for_thread(c['id']) is not None else '➕'} {c['name'][:24]}",
                            self._make_ui_token(
                                {"action": "open_wx", "wx_id": c["id"], "name": c["name"], "kind": c["kind"]})
                        )]
                        for c in cands[:8]
                    ]
                    buttons.append([Button.inline("❌ 取消", self._make_ui_token({"action": "cancel"}))])
                    return await event.reply("请点选要发起聊天的微信会话（也可直接发 <code>/open 关键词</code>）：",
                                             parse_mode="html", buttons=buttons)
                thread = None
                if " " not in arg:
                    try:
                        thread = await self.wx.thread(arg)
                    except WeChatError:
                        pass
                if thread is None:
                    cands = await self._wx_search_candidates(arg, limit=10)
                    if not cands and " " not in arg and arg.startswith(("wxid_", "gh_")):
                        try:
                            contact = await self.wx.contact(arg)
                            cands = [{"id": arg, "name": contact.get("displayName") or arg, "kind": "direct"}]
                        except WeChatError:
                            pass
                    if not cands:
                        return await event.reply(f"没有找到与「{_esc(arg)}」匹配的微信联系人或群")
                    exact = next((c for c in cands if c["id"] == arg), None)
                    if exact or len(cands) == 1:
                        chosen = exact or cands[0]
                        try:
                            thread = await self.wx.thread(chosen["id"])
                        except WeChatError:
                            thread = {"id": chosen["id"], "kind": chosen["kind"], "displayName": chosen["name"]}
                    else:
                        buttons = [
                            [Button.inline(
                                f"{'💬' if self.store.chat_for_thread(c['id']) is not None else '➕'} {c['name'][:24]}",
                                self._make_ui_token(
                                    {"action": "open_wx", "wx_id": c["id"], "name": c["name"], "kind": c["kind"]})
                            )]
                            for c in cands[:8]
                        ]
                        buttons.append([Button.inline("❌ 取消", self._make_ui_token({"action": "cancel"}))])
                        return await event.reply(
                            f"找到多个匹配「<b>{_esc(arg)}</b>」的会话，请点选：",
                            parse_mode="html", buttons=buttons)
                existed = self.store.chat_for_thread(thread["id"]) is not None
                await self.ensure_chat(thread)
                await event.reply(
                    f"这个会话已经有群了：<b>{_esc(self._title(thread))}</b>" if existed
                    else f"已建群：<b>{_esc(self._title(thread))}</b>",
                    parse_mode="html")
        except WeChatError as e:
            await event.reply(f"出错：{e.code}")
