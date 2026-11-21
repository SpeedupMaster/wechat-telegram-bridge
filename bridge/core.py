"""WeChat (agent-wechat) <-> Telegram bridge: one Telegram group (or forum topic) per WeChat chat."""
from __future__ import annotations

import asyncio
import html
import io
import logging
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

import httpx
from telethon import Button, TelegramClient, errors, events, functions, types, utils

from .config import Config
from . import cards, stickers
from . import callweb, tghtml
from .callweb import CallActionError, CallWeb
from .contacts import ContactsMixin, friendly as contact_error
from .emojis import WX_EMOJIS, replace_wx_emojis, unicode_to_wx_emojis
from .groups import GROUP_COMMANDS, GroupsMixin
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


def _topic_gone(e: Exception) -> bool:
    """The forum topic we posted into was deleted (the forum itself is still there)."""
    # Telethon raises known errors as their own class (whose .message is generic), unknown ones as
    # a base class carrying Telegram's error string.
    return isinstance(e, getattr(errors, "TopicDeletedError", ())) or (
        isinstance(e, errors.RPCError) and getattr(e, "message", "") in ("TOPIC_DELETED", "TOPIC_ID_INVALID"))


def _msg_topic(msg) -> int | None:
    """Forum topic a message was posted in (id of the topic's first message); None = General / not a forum."""
    r = getattr(msg, "reply_to", None)
    if r is None or not getattr(r, "forum_topic", False):
        return None
    return getattr(r, "reply_to_top_id", None) or getattr(r, "reply_to_msg_id", None)


def _reply_target(msg) -> int | None:
    """The message this one really replies to. In forums every message "replies" to its topic's
    first message; that is only a topic marker, not a reply."""
    r = getattr(msg, "reply_to", None)
    rid = getattr(r, "reply_to_msg_id", None) if r is not None else None
    if not rid:
        return None
    if getattr(r, "forum_topic", False) and not getattr(r, "reply_to_top_id", None):
        return None
    return rid


# Telegram only accepts these topic icon colours; one for contacts, one for WeChat groups, one for official accounts.
TOPIC_COLOR_DIRECT = 0x6FB9F0
TOPIC_COLOR_GROUP = 0x8EEE98
TOPIC_COLOR_OFFICIAL = 0xFFD67E


def _is_official_account(thread: dict | str | None) -> bool:
    """True for WeChat official/service accounts (classification.category == 'business' or gh_* ID)."""
    if isinstance(thread, str):
        return thread.startswith("gh_")
    if not isinstance(thread, dict):
        return False
    cls = thread.get("classification") or {}
    if cls.get("category") == "business":
        return True
    return str(thread.get("id") or "").startswith("gh_")


async def wav_to_ogg(data: bytes) -> bytes | None:
    """Convert WeChat voice (WAV) to OGG/Opus so Telegram shows it as a voice note. Needs ffmpeg."""
    ffmpeg = stickers.find_ffmpeg()
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
    "• <code>/requests</code> — 查看待处理的微信好友请求，一键通过\n"
    "• <code>/add</code> 微信号 [验证消息] — 按微信号添加微信好友\n"
    "• <code>/newgroup</code> 关键词 关键词 [| 群名] — 勾选好友新建微信群\n"
    "• <code>/syncnames</code> — 从微信重新同步全部好友资料与备注（不改标题）\n"
    "• <code>/status</code> — 查看运行状态与微信登录态\n"
    "• <code>/wxlogin</code> — 重新登录微信（支持手机确认或扫码）\n"
    "• <code>/help</code> — 显示这份帮助\n\n"
    "<b>群内命令</b>\n"
    "• <code>/link</code> [关键词] — 在任意群内直接发，把当前群绑定到微信会话\n"
    "• <code>/wx</code> — 查看当前群对应的微信会话\n"
    "• <code>/contact</code> — 查看好友资料、设置朋友圈权限（别名 <code>/info</code>）\n"
    "• <code>/remark</code> 新备注 — 修改微信好友备注（<code>/remark -</code> 清除）\n"
    "• <code>/call</code> [成员] — 给好友打微信语音电话；在微信群里勾选成员发起群通话（需配置通话页面）\n"
    "• <code>/members</code> [关键词] — 列出微信群成员，点按复制 <code>@名字</code>（别名 <code>/at</code>）\n"
    "• <code>/group</code> — 微信群资料与管理：免打扰、邀请/移出成员、清空公告、退群\n"
    "• <code>/invite</code> [关键词] / <code>/kick</code> [关键词] — 邀请好友进群 / 移出群成员\n"
    "• <code>/groupname</code> 新群名 · <code>/myname</code> 昵称 — 改微信群名 / 我的群昵称\n"
    "• <code>/mute</code> · <code>/unmute</code> — 开启/关闭微信消息免打扰；<code>/leave</code> — 退出微信群\n"
    "• <code>/recall</code> — 回复自己发的消息撤回微信消息（2 分钟内）\n"
    "• <code>/avatar</code> — 同步微信头像为群头像\n"
    "• <code>/rename</code> — 同步微信备注/群名为群标题（别名 <code>/title</code>）\n"
    "• <code>/unlink</code> — 解除当前群与微信会话的绑定\n"
    "• <code>/oaforum</code> — 把开启了话题的群设为公众号/服务号专用话题群（<code>/oaforum off</code> 取消）\n"
    "• <code>/help</code> — 显示群内帮助\n\n"
    "<b>常用技巧</b>\n"
    "• 群内发送 <code>@成员名</code> 或 <code>@所有人</code> 可触发微信 @ 提醒\n"
    "• 删除单条已发消息或回复 <code>/recall</code> 可自动撤回微信消息（2 分钟内有效）\n"
    "• 以 <code>//</code> 开头的内容仅留在 Telegram，不会发到微信\n"
    "• 收到微信好友请求会推送到这里，可直接通过\n"
    "• 图片默认原图收发；发文件选原图也会按原图送达微信；支持贴纸与来电一键拒接"
)

GROUP_HELP = (
    "<b>桥接群命令</b>\n"
    "• <code>/wx</code> — 查看当前群对应的微信会话\n"
    "• <code>/contact</code> — 查看好友资料、设置朋友圈权限（别名 <code>/info</code>）\n"
    "• <code>/remark</code> 新备注 — 修改微信好友备注（<code>/remark -</code> 清除）\n"
    "• <code>/call</code> [成员] — 给好友打微信语音电话；在微信群里勾选成员发起群通话\n"
    "• <code>/members</code> [关键词] — 查看群成员并点按复制 <code>@名字</code>（别名 <code>/at</code>）\n"
    "• <code>/group</code> — 微信群资料与管理（免打扰、邀请/移出成员、清空公告、退群）\n"
    "• <code>/invite</code> · <code>/kick</code> [关键词] — 邀请好友进群 / 移出群成员\n"
    "• <code>/groupname</code> 新群名 · <code>/myname</code> 昵称 — 改微信群名 / 我的群昵称\n"
    "• <code>/mute</code> · <code>/unmute</code> — 微信免打扰；<code>/leave</code> — 退出微信群\n"
    "• <code>/recall</code> — 回复自己发的消息以撤回微信消息（2 分钟内）\n"
    "• <code>/avatar</code> — 同步微信头像为群头像\n"
    "• <code>/rename</code> — 同步最新微信备注/群名为群标题（别名 <code>/title</code>）\n"
    "• <code>/link</code> [关键词] — 重新绑定当前群到其他微信会话\n"
    "• <code>/unlink</code> — 解除当前群与微信会话的绑定\n"
    "• <code>/help</code> — 显示群内帮助\n\n"
    "💡 发 <code>@成员名</code> / <code>@所有人</code> 可微信提醒；删单条消息或回复 <code>/recall</code> 可撤回；<code>//</code> 开头仅留在 Telegram。"
)

TOPIC_HELP = (
    "<b>微信 ↔ Telegram 桥接指南（话题模式）</b>\n\n"
    "默认所有微信会话都在一个话题群里，每个会话对应一个话题；也可以按需把某个会话单独绑定到新建的普通 Telegram 群："
    "在话题或独立群里发消息即发送到微信，<b>回复</b>消息即微信引用回复。\n\n"
    "<b>私聊命令</b>\n"
    "• <code>/find</code> [关键词] — 搜索微信联系人/群（不填显示最近会话，点按钮直接打开）\n"
    "• <code>/open</code> [关键词] — 为微信联系人/群新建话题并主动发起聊天\n"
    "• <code>/chats</code> [关键词] — 查看已连接的会话列表（别名 <code>/list</code>）\n"
    "• <code>/link</code> 微信ID TG群ID — 把指定微信会话直接绑定到独立群（也可直接在群/话题里发 <code>/link</code>）\n"
    "• <code>/requests</code> — 查看待处理的微信好友请求，一键通过\n"
    "• <code>/add</code> 微信号 [验证消息] — 按微信号添加微信好友\n"
    "• <code>/newgroup</code> 关键词 关键词 [| 群名] — 勾选好友新建微信群\n"
    "• <code>/syncnames</code> — 从微信重新同步全部好友资料与备注（不改标题）\n"
    "• <code>/status</code> — 查看运行状态与微信登录态\n"
    "• <code>/wxlogin</code> — 重新登录微信（支持手机确认或扫码）\n"
    "• <code>/help</code> — 显示这份帮助\n\n"
    "<b>话题群 / 独立群内命令</b>\n"
    "• <code>/forum</code> — 把当前群设为桥接话题群（需先开启话题，并把我设为管理员）\n"
    "• <code>/oaforum</code> — 把当前话题群设为公众号/服务号专用话题群（<code>/oaforum off</code> 取消）\n"
    "• <code>/link</code> [关键词] — 在话题里发可绑定该话题；在新建的普通群里发可按需绑定为独立群（原话题自动解绑并保留历史）\n"
    "• <code>/wx</code> — 查看当前话题/群对应的微信会话\n"
    "• <code>/contact</code> — 查看好友资料、设置朋友圈权限（别名 <code>/info</code>）\n"
    "• <code>/remark</code> 新备注 — 修改微信好友备注（<code>/remark -</code> 清除）\n"
    "• <code>/call</code> [成员] — 给好友打微信语音电话；在微信群里勾选成员发起群通话（需配置通话页面）\n"
    "• <code>/members</code> [关键词] — 列出微信群成员，点按复制 <code>@名字</code>（别名 <code>/at</code>）\n"
    "• <code>/group</code> — 微信群资料与管理：免打扰、邀请/移出成员、清空公告、退群\n"
    "• <code>/invite</code> [关键词] / <code>/kick</code> [关键词] — 邀请好友进群 / 移出群成员\n"
    "• <code>/groupname</code> 新群名 · <code>/myname</code> 昵称 — 改微信群名 / 我的群昵称\n"
    "• <code>/mute</code> · <code>/unmute</code> — 开启/关闭微信消息免打扰；<code>/leave</code> — 退出微信群\n"
    "• <code>/recall</code> — 回复自己发的消息撤回微信消息（2 分钟内）\n"
    "• <code>/avatar</code> — 在独立群里同步微信头像为群头像（需管理员改群信息权限）\n"
    "• <code>/rename</code> — 同步微信备注/群名为话题或群标题（别名 <code>/title</code>）\n"
    "• <code>/unlink</code> — 解除当前话题或独立群与微信会话的绑定（独立群解绑后自动回到话题群）\n"
    "• <code>/help</code> — 显示群内帮助\n\n"
    "<b>常用技巧</b>\n"
    "• 话题或群内发送 <code>@成员名</code> 或 <code>@所有人</code> 可触发微信 @ 提醒\n"
    "• 回复 <code>/recall</code> 可撤回微信消息（2 分钟内有效）\n"
    "• 以 <code>//</code> 开头的内容仅留在 Telegram，不会发到微信\n"
    "• 想把重要聊天单独拎出成独立群：新建未开启话题的普通群，拉我进群并设为管理员，在群里发 <code>/link</code> 即可\n"
    "• 收到微信好友请求会推送到私聊，可直接通过"
)

TOPIC_GROUP_HELP = (
    "<b>话题群命令</b>\n"
    "• <code>/wx</code> — 查看当前话题对应的微信会话\n"
    "• <code>/contact</code> — 查看好友资料、设置朋友圈权限（别名 <code>/info</code>）\n"
    "• <code>/remark</code> 新备注 — 修改微信好友备注（<code>/remark -</code> 清除）\n"
    "• <code>/call</code> [成员] — 给好友打微信语音电话；在微信群里勾选成员发起群通话\n"
    "• <code>/members</code> [关键词] — 查看群成员并点按复制 <code>@名字</code>（别名 <code>/at</code>）\n"
    "• <code>/group</code> — 微信群资料与管理（免打扰、邀请/移出成员、清空公告、退群）\n"
    "• <code>/invite</code> · <code>/kick</code> [关键词] — 邀请好友进群 / 移出群成员\n"
    "• <code>/groupname</code> 新群名 · <code>/myname</code> 昵称 — 改微信群名 / 我的群昵称\n"
    "• <code>/mute</code> · <code>/unmute</code> — 微信免打扰；<code>/leave</code> — 退出微信群\n"
    "• <code>/recall</code> — 回复自己发的消息以撤回微信消息（2 分钟内）\n"
    "• <code>/rename</code> — 同步最新微信备注/群名为话题标题（别名 <code>/title</code>）\n"
    "• <code>/link</code> [关键词] — 把当前话题绑定（或改绑）到微信会话；在普通群里发可绑定为独立群\n"
    "• <code>/unlink</code> — 解除当前话题与微信会话的绑定\n"
    "• <code>/forum</code> — 把当前群设为桥接话题群\n"
    "• <code>/oaforum</code> — 把当前群设为公众号/服务号专用话题群（<code>/oaforum off</code> 取消）\n"
    "• <code>/help</code> — 显示群内帮助\n\n"
    "💡 发 <code>@成员名</code> / <code>@所有人</code> 可微信提醒；回复 <code>/recall</code> 可撤回；<code>//</code> 开头仅留在 Telegram。"
)


def forum_setup_text(bot_username: str) -> str:
    return (
        "<b>设置话题群</b>（只需一次）\n"
        "1. 新建一个 Telegram 群，在群设置里开启「话题」(Topics)\n"
        f"2. 把 @{_esc(bot_username)} 拉进群并设为管理员，勾选「管理话题」权限\n"
        "3. 在群里发送 <code>/forum</code>\n\n"
        "之后每个微信会话会自动在群里生成一个话题。"
    )


PRIVATE_BOT_COMMANDS = [
    ("find", "搜索微信联系人或群（可一键建群）"),
    ("open", "为微信会话建群发起聊天"),
    ("chats", "查看已连接的会话列表"),
    ("link", "选择绑定微信会话与TG群"),
    ("requests", "查看待处理的微信好友请求"),
    ("add", "按微信号添加微信好友"),
    ("newgroup", "勾选好友新建微信群"),
    ("syncnames", "从微信重新同步好友备注"),
    ("status", "查看运行与微信在线状态"),
    ("wxlogin", "重新登录微信（手机确认或扫码）"),
    ("help", "查看使用指南"),
]

GROUP_BOT_COMMANDS = [
    ("wx", "查看对应的微信会话"),
    ("contact", "查看好友资料与朋友圈权限"),
    ("remark", "修改微信好友备注"),
    ("call", "给好友打微信电话或发起群语音通话"),
    ("members", "查看群成员（点按复制 @名字）"),
    ("group", "微信群资料与管理"),
    ("invite", "邀请好友进微信群"),
    ("kick", "把成员移出微信群"),
    ("groupname", "修改微信群名"),
    ("myname", "修改我在本群的昵称"),
    ("mute", "开启微信消息免打扰"),
    ("unmute", "关闭微信消息免打扰"),
    ("leave", "退出微信群"),
    ("recall", "回复消息以撤回微信消息"),
    ("avatar", "同步微信头像到群头像"),
    ("rename", "同步最新微信名称到群标题"),
    ("link", "绑定或切换当前群对应的微信会话"),
    ("unlink", "解除当前群与微信的绑定"),
    ("oaforum", "把当前话题群设为公众号/服务号专用话题群"),
    ("help", "查看群内命令帮助"),
]


TOPIC_PRIVATE_BOT_COMMANDS = [
    ("find", "搜索微信联系人或群（可一键建话题）"),
    ("open", "为微信会话新建话题发起聊天"),
    ("chats", "查看已连接的会话列表"),
    ("requests", "查看待处理的微信好友请求"),
    ("add", "按微信号添加微信好友"),
    ("newgroup", "勾选好友新建微信群"),
    ("syncnames", "从微信重新同步好友备注"),
    ("status", "查看运行与微信在线状态"),
    ("wxlogin", "重新登录微信（手机确认或扫码）"),
    ("help", "查看使用指南"),
]

TOPIC_GROUP_BOT_COMMANDS = [
    ("wx", "查看对应的微信会话"),
    ("contact", "查看好友资料与朋友圈权限"),
    ("remark", "修改微信好友备注"),
    ("call", "给好友打微信电话或发起群语音通话"),
    ("members", "查看群成员（点按复制 @名字）"),
    ("group", "微信群资料与管理"),
    ("invite", "邀请好友进微信群"),
    ("kick", "把成员移出微信群"),
    ("groupname", "修改微信群名"),
    ("myname", "修改我在本群的昵称"),
    ("mute", "开启微信消息免打扰"),
    ("unmute", "关闭微信消息免打扰"),
    ("leave", "退出微信群"),
    ("recall", "回复消息以撤回微信消息"),
    ("rename", "同步最新微信名称到群/话题标题"),
    ("link", "绑定或切换当前群/话题对应的微信会话"),
    ("unlink", "解除当前群/话题与微信的绑定"),
    ("forum", "把当前群设为桥接话题群"),
    ("oaforum", "把当前群设为公众号/服务号专用话题群"),
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


_WX_TOPIC_RE = re.compile(r"(?<![A-Za-z0-9_/:.?&=])#([^\s#<>/?&=]{1,30})#")
_TOPIC_BOUNDARY = set(" \t\r\n,，。！？、；：.!?;:)）]】\"'”’")


def _clip_text(text: str, limit: int) -> str:
    """Truncate text to at most `limit` codepoints without splitting a ZWJ/variation-selector emoji cluster."""
    if len(text) <= limit:
        return text
    cut = limit
    while cut > 0 and (
        text[cut] in ("\ufe0f", "\u200d")
        or text[cut - 1] == "\u200d"
        or 0x1F3FB <= ord(text[cut]) <= 0x1F3FF
    ):
        cut -= 1
    return text[:cut]


def _normalize_wx_text(s: str | None) -> str:
    """Convert WeChat #topic# tags and built-in emoji codes into plain Telegram-friendly text."""
    text = replace_wx_emojis(s)
    if not text:
        return ""
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


def _format_duration(seconds: int | float | None) -> str:
    if seconds is None or isinstance(seconds, bool):
        return ""
    sec = int(seconds)
    if sec <= 0:
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _channels_body(link: dict | None, body: str = "") -> str:
    """Render WeChat Channels (type 49/51) without WeChat's legacy upgrade placeholder."""
    if link:
        source = (link.get("sourceName") or "").strip()
        dur = _format_duration(link.get("durationSeconds"))
        header = f"🎬 <b>视频号 · {_esc(source)}</b>" if source else "🎬 <b>视频号</b>"
        if dur:
            header += f" ({dur})"
        title = (link.get("title") or "").strip()
        desc = (link.get("description") or "").strip()
        url = (link.get("url") or "").strip()
        if _is_channels_placeholder(title) or title == desc:
            title = ""
        if _CHANNELS_PLACEHOLDER_URL in url:
            url = ""
        return "\n".join(p for p in (header, _fmt_wx_text(title), _fmt_wx_text(desc), _esc(url)) if p)
    lines = []
    for raw_ln in (body or "").splitlines():
        ln = raw_ln.strip()
        if not ln or _is_channels_placeholder(ln) or _CHANNELS_PLACEHOLDER_URL in ln:
            continue
        for prefix in ("[Channels video] ", "[Channels] "):
            if ln.startswith(prefix):
                ln = ln[len(prefix):].strip()
                break
        if ln:
            lines.append(ln)
    return "\n".join(["🎬 <b>视频号</b>", *(_fmt_wx_text(ln) for ln in lines)])


def _guess_image_media(data: bytes, stem: str) -> Media:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return Media(data, "image/png", f"{stem}.png")
    if data.startswith(b"GIF8"):
        return Media(data, "image/gif", f"{stem}.gif")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return Media(data, "image/webp", f"{stem}.webp")
    return Media(data, "image/jpeg", f"{stem}.jpg")


def _msg_link(msg: dict) -> tuple[dict | None, int | None, bool]:
    """Return (link_payload, app_type, is_native_app_link) for WeChat link cards and app share texts."""
    t = msg.get("type")
    sub = msg.get("appMessageType")
    if t == APP and sub not in (APP_CHANNELS, APP_FILE) and (link := msg.get("link")):
        return link, sub, True
    if t == TEXT or (t == APP and sub == APP_QUOTE and not msg.get("link")):
        if link := cards.extract_text_link(msg.get("body")):
            return link, None, False
    return None, None, False


def _is_link_card(msg: dict) -> bool:
    return _msg_link(msg)[0] is not None


def _link_url(link: dict | None) -> str:
    url = ((link or {}).get("url") or "").strip()
    return url if url.startswith(("http://", "https://")) else ""


def _link_buttons(link: dict | None, app_type: int | None, open_url: str | None = None) -> list | None:
    """CTA button for a link card; open_url (e.g. the article reader Mini App link) replaces the raw URL."""
    if url := _link_url(link):
        src = cards.identify_link_source(link, app_type)
        return [[Button.url(src.button_label, open_url or url)]]
    return None


def _link_body(link: dict, app_type: int | None = None, *, inline_url: bool = False) -> str:
    src = cards.identify_link_source(link, app_type)
    icon = {
        "xhs": "📕", "wxmp": "📄", "bili": "📺", "zhihu": "💡",
        "douyin": "🎬", "kuaishou": "🎬", "music": "🎵", "video": "🎬",
    }.get(src.brand, "🔗")
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
    "unsupported_attachment_transport": "上游暂不支持该附件类型",
    "media_integrity_failed": "文件校验失败",
    "video_representation_mismatch": "视频版本校验不一致",
    "cdn_download_failed": "微信 CDN 下载失败",
    "media_source_changed": "源文件已变更",
    "media_identity_unavailable": "媒体标识不可用",
    "auth_required": "微信登录鉴权已失效，请重新登录",
    "transport_unavailable": "传输服务暂不可用",
    "media_service_rejected": "微信媒体服务拒绝请求",
    "channels_post_unavailable": "该视频号内容已删除、过期或设为私密",
    "channels_unsupported_media": "暂不支持该类型的视频号内容",
    "channels_route_unsupported": "当前通道不支持获取该视频号视频",
    "channels_rejected": "微信拒绝获取该视频号内容",
    "channels_media_too_large": "视频号视频超过大小限制",
    "channels_download_failed": "视频号视频下载失败",
    "channels_integrity_failed": "视频号视频校验失败",
    "channels_ticket_failed": "获取视频号凭证失败",
    "channels_page_failed": "获取视频号页面失败",
    "channels_detail_failed": "获取视频号详情失败",
    "channels_ticket_unreadable": "解析视频号凭证失败",
    "channels_page_unreadable": "解析视频号页面失败",
    "channels_detail_unreadable": "解析视频号详情失败",
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


# agent-wechat only knows the ordinary WeChat CDN fields; images and files from WeCom (企业微信, `@openim`)
# senders carry their reference in another shape, so it rejects them with one of these codes
# (thisnick/agent-wechat#467).
UNSUPPORTED_TRANSPORT_CODES = {"unsupported_media", "unsupported_media_transport", "unsupported_attachment_transport"}
OPENIM_MEDIA_NOTE = "企业微信发来的图片/文件，agent-wechat 暂不支持下载，请在手机微信查看"


def _is_openim(wx_id: str | None) -> bool:
    """True for WeCom contacts (`…@openim`) and WeCom customer service (`…@kefu.openim`)."""
    return bool(wx_id) and wx_id.rsplit("@", 1)[-1].endswith("openim")


def _media_failure_text(msg: dict, reason: str) -> str:
    """The `[图片，无法获取：…]` notice for an attachment that could not be fetched (escaped HTML)."""
    t = msg.get("type")
    label = {IMAGE: "图片", VIDEO: "视频", VOICE: "语音", STICKER: "表情"}.get(t, "文件")
    if t == APP and msg.get("appMessageType") == APP_FILE:
        name = ((msg.get("link") or {}).get("title") or msg.get("body") or "").strip()
        if name:
            label += f"「{name}」"
    if reason in UNSUPPORTED_TRANSPORT_CODES and (_is_openim(msg.get("senderId")) or _is_openim(msg.get("threadId"))):
        why = OPENIM_MEDIA_NOTE
    else:
        why = MEDIA_REASONS.get(reason, reason)
    return f"[{_esc(label)}，无法获取：{_esc(why)}]"


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


def _dice_text(media) -> str:
    """Turn a Telegram interactive emoji (`MessageMediaDice`: 🎲/🎯/🏀/⚽/🎳/🎰) into plain text for WeChat."""
    if not isinstance(media, types.MessageMediaDice):
        return ""
    emo = (getattr(media, "emoticon", None) or "").strip()
    if not emo:
        return ""
    val = getattr(media, "value", None)
    if emo == "🎲" and isinstance(val, int) and 1 <= val <= 6:
        return f"🎲 {val}"
    return emo


def _tg_msg_fingerprint(m) -> tuple:
    """Fingerprint a user-sent message across user and bot sessions, including Telegram media IDs."""
    media = getattr(m, "media", None)
    if isinstance(media, (types.MessageMediaWebPage, types.MessageMediaEmpty)):
        media = None
    media_id = None
    if isinstance(media, types.MessageMediaDice):
        media_id = (getattr(media, "emoticon", None), getattr(media, "value", None))
    elif media is not None:
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
    if isinstance(media, types.MessageMediaDice):
        emo = (getattr(media, "emoticon", None) or "").strip()
        return f"「{emo}」" if emo else "表情"
    if media and not isinstance(media, (types.MessageMediaWebPage, types.MessageMediaEmpty)):
        f = getattr(msg, "file", None)
        name = (getattr(f, "name", None) or "").strip()
        return f"文件 {name}" if name else "文件"
    if snippet:
        return f"「{snippet}」"
    return "消息"


def _chunks(text: str, size: int = TG_TEXT_LIMIT) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


# Group-call member picker: members per page (two per row) and names listed in the message text.
DIAL_PICK_PAGE = 10
DIAL_PICK_SHOWN = 12
CALL_INVITEES_SHOWN = 40  # names listed on a group call card (the list folds away in Telegram)
CONTACT_NAME_TTL = 600.0  # seconds a contact's remark/nickname is reused before reading it again


def _short(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _member_label(m: dict) -> str:
    """How a member is @mentioned: their own name in the group, never my private remark."""
    return (
        m.get("mentionName") or m.get("roomDisplayName") or m.get("displayName") or m.get("nickname") or m.get("id") or ""
    ).strip()


def _member_name(m: dict) -> str:
    """How a member is shown to me: agent-wechat's `displayName` is my remark first, then their group name."""
    return str(
        m.get("displayName") or m.get("roomDisplayName") or m.get("nickname") or m.get("mentionName") or m.get("id") or ""
    ).strip()


def _card(title: str, lines: list[str]) -> str:
    """A call card: bold title, then the details grouped in a quote block."""
    return f"{title}\n<blockquote>" + "\n".join(lines) + "</blockquote>"


def _call_time_line(entry: dict) -> str:
    """When the call connected (in each reader's own timezone) and, once over, how long it lasted."""
    start = entry.get("connected_at")
    if not start:
        return ""
    at = tghtml.tg_time(start, time.strftime("%H:%M", time.localtime(start)))
    end = entry.get("ended_at")
    if entry.get("terminal") and end:
        return f"时长：<b>{_format_duration(end - start) or '0:00'}</b>（{at} 接通）"
    return f"接通：{at}"


def _is_mention_boundary(name: str, after: str) -> bool:
    if not after:
        return True
    ch = after[0]
    if ch.isspace() or not ch.isalnum():
        return True
    return name[-1].isascii() and not ch.isascii()


def _match_mentions_with_spans(text: str, members: list[dict]) -> tuple[dict | None, list[tuple[int, int]]]:
    """Resolve @all/@所有人 and @memberName occurrences in `text` and return matched character spans."""
    candidates: list[tuple[str, str]] = []
    for m in members:
        uid = m.get("id")
        if not uid:
            continue
        for key in ("mentionName", "roomDisplayName", "displayName", "nickname", "alias"):
            name = (m.get(key) or "").strip()
            if name:
                candidates.append((name, uid))
    candidates.sort(key=lambda item: len(item[0]), reverse=True)

    mention_all = False
    user_ids: list[str] = []
    spans: list[tuple[int, int]] = []
    i = 0
    while (pos := text.find("@", i)) != -1:
        if pos > 0 and text[pos - 1].isalnum():
            i = pos + 1
            continue
        rest = text[pos + 1:]
        if rest.startswith("所有人") and _is_mention_boundary("所有人", rest[3:]):
            mention_all = True
            spans.append((pos, pos + 4))
            i = pos + 4
            continue
        if rest[:3].lower() == "all" and _is_mention_boundary("all", rest[3:]):
            mention_all = True
            spans.append((pos, pos + 4))
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
        if matched_len:
            spans.append((pos, pos + 1 + matched_len))
        i = pos + 1 + (matched_len or 0)
    if not mention_all and not user_ids:
        return None, []
    return {"all": mention_all, "userIds": user_ids}, spans


def _match_mentions(text: str, members: list[dict]) -> dict | None:
    """Resolve @all/@所有人 and @memberName occurrences in `text` to an agent-wechat mentions payload."""
    return _match_mentions_with_spans(text, members)[0]


def _tg_text_to_wx(text: str, mention_spans: list[tuple[int, int]]) -> str:
    """Convert Telegram Unicode emojis to WeChat [emoji] codes while preserving matched @mention names."""
    if not mention_spans:
        return unicode_to_wx_emojis(text)
    out: list[str] = []
    pos = 0
    for start, end in mention_spans:
        if start > pos:
            out.append(unicode_to_wx_emojis(text[pos:start]))
        out.append(text[start:end])
        pos = end
    if pos < len(text):
        out.append(unicode_to_wx_emojis(text[pos:]))
    return "".join(out)


class Bridge(ContactsMixin, GroupsMixin, LoginMixin):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._init_login()
        data = cfg.data_dir
        self.store = Store(data / "bridge.db")
        self.sticker_cache = stickers.StickerDiskCache(data / "sticker_cache")
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
        self._names_syncing = False  # a /syncnames is running
        self._remark_poll = 1.5  # seconds between checks that a /remark change was saved
        self._names_quiet_until = 0.0  # until then (monotonic) contact updates don't rename: after /syncnames
        self._tg_time_ok = True  # Telegram accepted <tg-time> date entities (False after a refusal)
        self._contact_name_cache: dict[str, tuple[str, str, float]] = {}  # wx id -> (remark, nickname, at)
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
        self._dial_requests: dict[str, dict] = {}  # dial_id -> a /call card (kept after cancel/expiry for its page)
        self._orphan_invite_results: dict[str, list[dict]] = {}  # call_id -> invitation results seen before the call
        self._self_wxid: str = self.store.get("self_wxid") or ""
        self._group_picks: dict[str, dict] = {}  # pick id -> /invite, /kick or /newgroup member picker
        self._calls_retry_sleep: float = 300.0
        self._calls_client_id: str = ""
        self._calls_caps: dict = {}
        self._orphan_call_states: dict[str, dict] = {}
        b = cfg.bridge
        self.call_web: CallWeb | None = (
            CallWeb(self, b.call_web_url, b.call_web_listen, b.call_web_app)
            if b.call_web_url and b.call_notifications else None)
        self._ui_tokens: dict[str, dict] = {}
        self._link_wait: dict | None = None
        self._recall_notice_ttl: float = 5.0
        self._notice_tasks: set[asyncio.Task] = set()
        # Topics mode: everything happens in one forum supergroup and only the bot is used.
        self.topics = cfg.bridge.mode == "topics"
        self.forum_chat: int | None = (
            cfg.bridge.forum_chat or int(self.store.get("forum_chat") or 0) or None) if self.topics else None
        # Optional dedicated forum supergroup for official/service accounts (works in both modes).
        self.oa_forum_chat: int | None = (
            cfg.bridge.official_accounts_forum_chat or int(self.store.get("oa_forum_chat") or 0) or None)
        self._forum_warned_at: float | None = None

    # ======================================================================
    # startup
    # ======================================================================
    async def sync_bot_commands(self) -> None:
        """Register private and group `/` menus with Telegram."""
        private, group = ((TOPIC_PRIVATE_BOT_COMMANDS, TOPIC_GROUP_BOT_COMMANDS) if self.topics
                          else (PRIVATE_BOT_COMMANDS, GROUP_BOT_COMMANDS))
        scopes = [
            (types.BotCommandScopeDefault(), private),
            (types.BotCommandScopeUsers(), private),
            (types.BotCommandScopeChats(), group),
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
        if not self.topics:  # topics mode never uses your Telegram account
            await self.user.connect()
        self.bot.add_event_handler(self.on_bot_private, events.NewMessage(incoming=True, func=lambda e: e.is_private))
        self.bot.add_event_handler(self.on_bot_callback, events.CallbackQuery())
        # The user client's update loop may only start once that account is logged in
        # (before that Telegram answers AUTH_KEY_UNREGISTERED), so main() starts it after login.
        await asyncio.gather(self.main(), self.bot.run_until_disconnected())

    async def main(self) -> None:
        if self.topics:
            await self._main_topics()
            return
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
            outgoing=True, func=lambda e: not e.is_private and not self._is_forum_chat(e.chat_id)
            and self.store.thread_for_chat(e.chat_id) is None
            and (e.raw_text or "").lstrip().startswith("/")))
        self.user.add_event_handler(self.on_tg_delete, events.MessageDeleted())
        self.user.add_event_handler(self.on_raw_update, events.Raw(types.UpdateNewMessage))
        self.user.add_event_handler(self.on_user_sees_bot_message, events.NewMessage(
            incoming=True, func=lambda e: e.sender_id == self.bot_me.id
            and self.store.thread_for_chat(e.chat_id) is not None))
        self.bot.add_event_handler(self.on_bot_sees_user_message, events.NewMessage(
            incoming=True, func=lambda e: not e.is_private and e.sender_id == self.owner_id
            and self.store.thread_for_chat(e.chat_id) is not None))
        self.bot.add_event_handler(self.on_forum_message, events.NewMessage(
            incoming=True, func=lambda e: not e.is_private and e.sender_id == self.owner_id
            and (self._is_forum_chat(e.chat_id)
                 or (e.raw_text or "").lstrip().lower().startswith("/oaforum"))))
        await asyncio.gather(self.wechat_side(), self.user.run_until_disconnected())

    async def _main_topics(self) -> None:
        """Topics mode: the bot alone. Whoever enters the pairing code once becomes the owner."""
        # "greeted" holds the account id from groups mode, so switching modes needs no new pairing.
        owner = self.store.get("owner") or self.store.get("greeted")
        if owner:
            self.owner_id = int(owner)
            self.tg_ready.set()
        else:
            self.start_pairing()
            await self.tg_ready.wait()
        self.store.set("owner", str(self.owner_id))
        log.info("Topics mode: bot @%s, owner %s, forum group %s", self.bot_me.username, self.owner_id,
                 self.forum_chat or "not set yet (send /forum in the group)")
        self.bot.add_event_handler(self.on_forum_message, events.NewMessage(
            incoming=True, func=lambda e: not e.is_private and e.sender_id == self.owner_id))
        self.bot.add_event_handler(self.on_tg_delete, events.MessageDeleted())
        self.bot.add_event_handler(self.on_raw_update, events.Raw(types.UpdateNewMessage))
        if not self.forum_chat:
            await self.notify(forum_setup_text(self.bot_me.username), parse_mode="html")
        await self.wechat_side()

    async def _pair_topics_owner(self, event) -> None:
        """Topics mode: a correct pairing code is all the setup the Telegram side needs."""
        self.owner_id = event.sender_id
        self.store.set("owner", str(self.owner_id))
        self.pairing_code = None
        self.login_step = None
        text = "✅ <b>配对成功</b>（话题模式：只使用机器人，无需登录 Telegram 账号）。"
        if not self.forum_chat:
            text += "\n\n" + forum_setup_text(self.bot_me.username)
        text += "\n\n接下来会自动发起微信登录（支持手机确认或扫码）。"
        await event.reply(text, parse_mode="html")
        self.tg_ready.set()

    def _reader_link(self, link: dict | None) -> str | None:
        """t.me Mini App link that opens an article / Xiaohongshu note / Douyin video in the reader (None = plain URL)."""
        if self.call_web is None or not (url := _link_url(link)):
            return None
        try:
            return self.call_web.article_link(url)
        except Exception:
            log.exception("Could not create a reader link for %s", url)
            return None

    async def wechat_side(self) -> None:
        await self.ensure_wechat()
        log.info("WeChat is logged in; bridging messages")
        tasks = [self.event_loop(), self.wechat_monitor()]
        if self.cfg.bridge.call_notifications:
            tasks.append(self.calls_loop())
            if self.call_web is not None:
                try:
                    await self.call_web.start()
                except OSError as e:
                    log.error("Could not start the call answering page on %s: %s", self.cfg.bridge.call_web_listen, e)
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
        if data.startswith(b"callhup:"):
            await self._on_call_hangup_callback(event, data[8:].decode("utf-8", errors="ignore"))
            return
        if data.startswith(b"callinv:"):
            await self._on_call_invite_callback(event, data[8:].decode("utf-8", errors="ignore"))
            return
        if data.startswith(b"dialx:"):
            await self._on_dial_cancel_callback(event, data[6:].decode("utf-8", errors="ignore"))
            return
        if data.startswith(b"dialp:"):
            await self._on_dial_pick_callback(event, data[6:].decode("utf-8", errors="ignore"))
            return
        if data.startswith(b"ui:"):
            await self._on_ui_callback(event, data[3:].decode("utf-8", errors="ignore"))
            return
        if data.startswith(b"wxlogin:"):
            await self._on_wx_login_callback(event, data[8:].decode("utf-8", errors="ignore"))
            return
        await event.answer()

    async def close(self) -> None:
        self._cancel_auto_relogin()
        tasks = list(self._notice_tasks)
        for task in tasks:
            task.cancel()
        if self._wx_auto_relogin_task and not self._wx_auto_relogin_task.done():
            self._wx_auto_relogin_task.cancel()
            tasks.append(self._wx_auto_relogin_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.call_web is not None:
            await self.call_web.stop()
        if self._calls_sock is not None:
            await self._calls_sock.close()
            self._calls_sock = None
        await self.wx.close()

    # ======================================================================
    # WeChat -> Telegram
    # ======================================================================
    async def event_loop(self) -> None:
        cursor = self.store.get("cursor")
        if self.store.get("since") is None:
            # Messages created before this moment are history and never forwarded, even when the
            # daemon syncs them in later ("catchup"), e.g. right after the first WeChat login.
            self.store.set("since", str(time.time()))
        self.since = float(self.store.get("since"))
        backoff = 1.0
        while True:
            try:
                if cursor is None:
                    cursor = await self.wx.latest_cursor()
                    self.store.set("cursor", cursor)
                    log.info("First start: skipping old history, listening from event cursor %s", cursor)
                async for event in self.wx.stream(cursor):
                    backoff = 1.0
                    await self.handle_event(event)
                    if event.get("cursor"):
                        cursor = str(event["cursor"])
                        self.store.set("cursor", cursor)
                        asyncio.create_task(self._ack(cursor))
                log.warning("Event stream ended; reconnecting")
            except CursorExpired:
                try:
                    cursor = await self.wx.latest_cursor()
                    self.store.set("cursor", cursor)
                    log.warning("Event cursor expired on the daemon; some events were skipped. Resuming at %s", cursor)
                except (httpx.HTTPError, WeChatError) as e:
                    cursor = None
                    log.warning("Event cursor expired and newest cursor lookup failed (%s); retrying in %.0fs",
                                e, backoff)
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
            elif kind == "friend_request.updated":
                await self.on_friend_request_event(data)
            elif kind == "contacts.updated":
                await self.on_wx_profile_changed(data.get("contactId"))
            elif kind == "group.updated":
                await self.on_wx_profile_changed(data.get("groupId"))
        except Exception:
            log.exception("Failed to handle %s event %s", kind, event.get("cursor"))

    def _allowed(self, thread: dict) -> bool:
        tid = thread["id"]
        b = self.cfg.bridge
        if tid in b.skip_threads:
            return False
        if thread.get("kind") == "group" and not b.bridge_groups:
            return False
        if _is_official_account(thread) and not b.bridge_official_accounts:
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

    def _note_self_id(self, sender: str | None, thread_id: str) -> None:
        """agent-wechat has no "who am I" call: learn our own WeChat ID from the sender of our own messages,
        so the group-call member picker can leave us out."""
        sender = (sender or "").strip()
        if not sender or sender == thread_id or sender.endswith("@chatroom") or sender == self._self_wxid:
            return
        self._self_wxid = sender
        self.store.set("self_wxid", sender)

    async def on_wx_message(self, message_id: str, origin: str | None) -> None:
        if self.store.seen_wx_msg(message_id):
            return
        msg = await self.wx.message(message_id)
        thread_id = msg["threadId"]
        outgoing = msg.get("direction") == "outgoing"
        if outgoing:
            self._note_self_id(msg.get("senderId"), thread_id)
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

    # --- bindings, whichever the mode ---------------------------------------
    def _active_forums(self) -> tuple[int, ...]:
        """Forum supergroup IDs active in this run (main forum in topics mode, plus OA forum if set)."""
        forums: list[int] = []
        if self.topics and self.forum_chat:
            forums.append(self.forum_chat)
        if self.oa_forum_chat and self.oa_forum_chat not in forums:
            forums.append(self.oa_forum_chat)
        return tuple(forums)

    def _is_forum_chat(self, chat_id: int | None) -> bool:
        return chat_id is not None and chat_id in self._active_forums()

    def _target_forum(self, thread: dict | str | None) -> int | None:
        """Forum supergroup ID this WeChat thread should live in, or None for a standalone Telegram group."""
        if _is_official_account(thread) and self.oa_forum_chat:
            return self.oa_forum_chat
        if self.topics:
            return self.forum_chat
        return None

    def _uses_topic_for_thread(self, thread: dict | str | None) -> bool:
        """True when this WeChat thread should be created/resolved as a forum topic."""
        return self.topics or (_is_official_account(thread) and bool(self.oa_forum_chat))

    def _thread_noun(self, tid: str, thread: dict | str | None = None) -> str:
        """'话题' or '群' for this WeChat thread (follows its active binding if linked, else default)."""
        if (dest := self._dest_for_thread(tid)) is not None:
            return "话题" if dest[1] is not None else "群"
        return "话题" if self._uses_topic_for_thread(thread if thread is not None else tid) else "群"

    def _dest_for_thread(self, tid: str) -> tuple[int, int | None] | None:
        """(chat, topic) a WeChat thread is bridged to; topic is None for standalone Telegram groups."""
        dest = self.store.topic_for_thread(tid)
        if self.topics:
            if not dest:
                return None
            if dest[1] == 0:
                return None if self._is_forum_chat(dest[0]) else (dest[0], None)
            if _is_official_account(tid) and self.oa_forum_chat:
                return dest if dest[0] == self.oa_forum_chat else None
            if self.oa_forum_chat and dest[0] == self.oa_forum_chat:
                return dest
            return dest if self.forum_chat and dest[0] == self.forum_chat else None
        if self.oa_forum_chat and dest and dest[0] == self.oa_forum_chat and dest[1] > 0:
            return dest
        if self.oa_forum_chat and _is_official_account(tid):
            return None
        chat = self.store.chat_for_thread(tid)
        return None if chat is None else (chat, None)

    def _topic_for(self, chat: int, tid: str) -> int | None:
        """Topic to post into for a thread in `chat` (None: standalone group, or not an active forum)."""
        if not self._is_forum_chat(chat):
            return None
        dest = self.store.topic_for_thread(tid)
        return dest[1] if dest and dest[0] == chat and dest[1] > 0 else None

    def _thread_for_event(self, event) -> str | None:
        if self._is_forum_chat(event.chat_id):
            topic = _msg_topic(event.message)
            if topic is None:
                return None
            return self.store.thread_for_topic(event.chat_id, topic)
        if self.topics:
            return self.store.thread_for_topic_group(event.chat_id)
        return self.store.thread_for_chat(event.chat_id)

    def _unlink_thread(self, tid: str, chat_id: int | None = None) -> None:
        if chat_id is not None:
            if self.topics or self._is_forum_chat(chat_id):
                self.store.unlink_topic(tid)
            else:
                self.store.unlink(tid)
            return
        dest = self._dest_for_thread(tid)
        if self.topics or (dest is not None and dest[1] is not None):
            self.store.unlink_topic(tid)
        else:
            self.store.unlink(tid)

    def _link_rows(self) -> list[tuple[str, str, str]]:
        """(wx_thread, title, where) for every active binding of the current mode."""
        rows: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for forum in self._active_forums():
            for wx, topic, title, _ in self.store.list_topics(forum):
                if wx not in seen:
                    seen.add(wx)
                    rows.append((wx, title, f"{forum}#{topic}"))
        if self.topics:
            for wx, chat, title, _ in self.store.list_topic_groups():
                if wx not in seen and not self._is_forum_chat(chat):
                    seen.add(wx)
                    rows.append((wx, title, str(chat)))
        else:
            for wx, chat, title, _ in self.store.list_threads():
                if wx not in seen and not (self.oa_forum_chat and _is_official_account(wx)):
                    seen.add(wx)
                    rows.append((wx, title, str(chat)))
        return rows

    def _link_title(self, tid: str) -> str:
        return next((title for wx, title, _ in self._link_rows() if wx == tid), "")

    async def _drop_dead_dest(self, chat: int, thread: dict, e: Exception) -> bool:
        """After a failed send: forget a destination that is gone. True = create a new one and retry."""
        if self._is_forum_chat(chat):
            if _topic_gone(e):
                log.warning("Topic for %s in %s is gone (%s); creating a new one", thread["id"], chat, e)
                self.store.unlink_topic(thread["id"])
                self.store.forget_messages(chat, thread["id"])
                return True
            if _dead_chat(e):
                cmd_hint = "/oaforum" if (chat == self.oa_forum_chat and not self.topics) else "/forum"
                await self._forum_problem(
                    f"无法在话题群 {chat} 里发消息（{type(e).__name__}: {e}）。"
                    f"请确认我还在群里、是管理员，或在新的话题群里发送 {cmd_hint}。")
            return False
        if not _dead_chat(e):
            return False
        if self.topics:
            log.warning("Standalone Telegram group %s for %s is gone (%s); falling back to topic",
                        chat, thread["id"], e)
            self.store.unlink_topic(thread["id"])
            self.store.forget_messages(chat, thread["id"])
            name = thread.get("displayName") or thread["id"]
            await self.notify(
                f"⚠️ 独立群 <code>{chat}</code>（{_esc(name)}）无法发送消息（{_esc(type(e).__name__)}），"
                "已解除独立群绑定并退回话题群。如需继续使用独立群，请确认我在群里后再发 <code>/link</code>。",
                parse_mode="html",
            )
            return True
        log.warning("Telegram group %s for %s is gone (%s); creating a new one", chat, thread["id"], e)
        self.store.unlink(thread["id"])
        return True

    async def _forum_problem(self, text: str) -> None:
        """Tell the owner the forum group can't be used; at most every 10 minutes."""
        log.warning("%s", text)
        now = time.monotonic()
        if self._forum_warned_at is not None and now - self._forum_warned_at < 600:
            return
        self._forum_warned_at = now
        await self.notify("⚠️ " + text + "\n（期间收到的微信消息不会转发到 Telegram）")

    # --- forum topics -----------------------------------------------------
    async def _forum_call(self, name: str, chat: int, **kw):
        """Run a forum-topic request. Newer Telegram layers have it under messages., older under channels."""
        peer = await self.bot.get_input_entity(chat)
        if cls := getattr(functions.messages, name, None):
            return await self.bot(cls(peer=peer, **kw))
        return await self.bot(getattr(functions.channels, name)(channel=peer, **kw))

    async def _create_topic(self, chat: int, title: str, *, group: bool, official: bool = False) -> int:
        color = TOPIC_COLOR_OFFICIAL if official else (TOPIC_COLOR_GROUP if group else TOPIC_COLOR_DIRECT)
        while True:
            try:
                result = await self._forum_call("CreateForumTopicRequest", chat, title=title, icon_color=color)
                break
            except errors.FloodWaitError as e:
                log.warning("Telegram asks to wait %ss before creating another topic", e.seconds)
                await asyncio.sleep(e.seconds + 1)
        updates = getattr(result, "updates", None) or []
        for u in updates:
            m = getattr(u, "message", None)
            if isinstance(m, types.MessageService) and isinstance(m.action, types.MessageActionTopicCreate):
                return m.id
        for u in updates:
            if isinstance(u, types.UpdateMessageID):
                return u.id
        raise RuntimeError("Telegram did not return the id of the new topic")

    async def _ensure_topic(self, thread: dict) -> int | None:
        tid = thread["id"]
        if (dest := self._dest_for_thread(tid)) is not None:
            return dest[0]
        async with self.create_lock:
            if (dest := self._dest_for_thread(tid)) is not None:
                return dest[0]
            forum = self._target_forum(thread)
            if not forum:
                await self._forum_problem("还没有设置话题群：请在开启了「话题」的群里把我设为管理员，然后在群里发送 /forum。")
                return None
            title = self._title(await self._named_thread(thread))
            try:
                topic = await self._create_topic(
                    forum, title, group=thread.get("kind") == "group", official=_is_official_account(thread))
            except Exception as e:
                await self._forum_problem(
                    f"无法在话题群 {forum} 里创建话题「{title}」（{type(e).__name__}: {e}）。"
                    "请确认群已开启「话题」，我是管理员且有「管理话题」权限。")
                return None
            self.store.link_topic(tid, forum, topic, title)
            log.info("Created topic %r (%s) in %s for WeChat chat %s", title, topic, forum, tid)
        await self._init_chat(forum, None, thread)
        return forum

    async def ensure_chat(self, thread: dict) -> int | None:
        tid = thread["id"]
        if self.topics:
            if (dest := self._dest_for_thread(tid)) is not None:
                return dest[0]
            return await self._ensure_topic(thread)
        if self._uses_topic_for_thread(thread):
            return await self._ensure_topic(thread)
        if (chat := self.store.chat_for_thread(tid)) is not None:
            return chat
        async with self.create_lock:
            if (chat := self.store.chat_for_thread(tid)) is not None:
                return chat
            wait = self.cfg.bridge.create_interval_seconds - (time.monotonic() - self.last_create)
            if wait > 0:
                await asyncio.sleep(wait)
            title = self._title(await self._named_thread(thread))
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
            client = self.bot if self.topics else self.user
            f = await client.upload_file(io.BytesIO(data), file_name="avatar.jpg")
            peer = utils.resolve_id(chat_id)[0]
            if chat_id < -1000000000000:  # upgraded to a supergroup
                await client(functions.channels.EditPhotoRequest(
                    channel=await client.get_input_entity(chat_id), photo=types.InputChatUploadedPhoto(file=f)))
            else:
                await client(functions.messages.EditChatPhotoRequest(
                    chat_id=peer, photo=types.InputChatUploadedPhoto(file=f)))
            return ""
        except Exception as e:
            log.info("Could not set group avatar for %s: %s", thread_id, e)
            return f"{type(e).__name__}: {e}"

    async def sync_title(self, chat_id: int, thread_id: str) -> tuple[str | None, str]:
        """Update Telegram group (or topic) title from WeChat display name. Returns (new_title, error_or_status)."""
        try:
            try:
                thread = await self.wx.thread(thread_id)
            except WeChatError:
                if thread_id.endswith("@chatroom"):
                    raise
                contact = await self.wx.contact(thread_id)
                thread = {"id": thread_id, "kind": "direct", "displayName": contact.get("displayName") or thread_id}
            title = self._title(await self._named_thread(thread, fresh=True))
            if self._is_forum_chat(chat_id):
                topic = self._topic_for(chat_id, thread_id)
                if topic is None:
                    return None, "这个会话没有对应的话题"
                try:
                    await self._forum_call("EditForumTopicRequest", chat_id, topic_id=topic, title=title)
                except errors.RPCError as e:
                    if getattr(e, "message", "") != "TOPIC_NOT_MODIFIED":
                        raise
                    self.store.update_topic_title(thread_id, title)
                    return title, "unchanged"
                self.store.update_topic_title(thread_id, title)
                return title, ""
            client = self.bot if self.topics else self.user
            save_title = self.store.update_topic_title if self.topics else self.store.update_title
            try:
                if chat_id < -1000000000000:
                    await client(functions.channels.EditTitleRequest(
                        channel=await client.get_input_entity(chat_id), title=title))
                else:
                    await client(functions.messages.EditChatTitleRequest(
                        chat_id=utils.resolve_id(chat_id)[0], title=title))
            except errors.ChatNotModifiedError:
                save_title(thread_id, title)
                return title, "unchanged"
            save_title(thread_id, title)
            return title, ""
        except Exception as e:
            log.info("Could not sync group title for %s: %s", thread_id, e)
            return None, f"{type(e).__name__}: {e}"

    async def _init_chat(self, chat_id: int, raw_chat_id: int | None, thread: dict) -> None:
        if self.cfg.bridge.sync_avatar and not self._is_forum_chat(chat_id):  # topics have no photo
            # Fetching an avatar from WeChat can take a while; don't hold up the first message.
            asyncio.create_task(self.sync_avatar(chat_id, thread["id"]))
        kind = "群聊" if thread.get("kind") == "group" else ("公众号/服务号" if _is_official_account(thread) else "联系人")
        kw = {"reply_to": topic} if (topic := self._topic_for(chat_id, thread["id"])) else {}
        await self.bot.send_message(chat_id, f"已连接微信{kind}：<b>{_esc(thread.get('displayName'))}</b>\n"
                                             f"<code>{_esc(thread['id'])}</code>", parse_mode="html", silent=True,
                                    **kw)

    # --- names ------------------------------------------------------------
    async def _contact_names(self, wx_id: str, *, fresh: bool = False) -> tuple[str, str]:
        """(remark, nickname) from the contact record itself, "" when unknown; cached for a while.

        Threads and group members are meant to report the remark as `displayName` already, but
        profiles saved before agent-wechat 2.6 can miss it there, so the remark is read directly.
        """
        if not wx_id or wx_id.endswith("@chatroom"):
            return "", ""
        now = time.monotonic()
        hit = self._contact_name_cache.get(wx_id)
        if hit and not fresh and now - hit[2] < CONTACT_NAME_TTL:
            return hit[0], hit[1]
        try:
            c = await self.wx.contact(wx_id)
            remark, nick = str(c.get("remark") or "").strip(), str(c.get("nickname") or "").strip()
        except WeChatError:
            remark, nick = (hit[0], hit[1]) if hit else ("", "")
        self._contact_name_cache[wx_id] = (remark, nick, now)
        while len(self._contact_name_cache) > 2000:
            self._contact_name_cache.pop(next(iter(self._contact_name_cache)))
        return remark, nick

    def _known_remark(self, wx_id: str) -> str:
        """A remark already read for this contact, without asking agent-wechat (for long member lists)."""
        hit = self._contact_name_cache.get(wx_id)
        return hit[0] if hit else ""

    def _forget_names(self, wx_id: str) -> None:
        """A contact's names changed: drop what is cached about them."""
        self._contact_name_cache.pop(wx_id, None)
        for key in [k for k in self.names if k[1] == wx_id]:
            self.names.pop(key, None)

    async def _named_thread(self, thread: dict, *, fresh: bool = False) -> dict:
        """The thread with my remark as its name when it is a direct chat with one; the contact's
        own nickname is kept as `nickname`."""
        tid = thread.get("id") or ""
        if thread.get("kind") not in (None, "direct") or tid.endswith("@chatroom"):
            return thread
        remark, nick = await self._contact_names(tid, fresh=fresh)
        out = dict(thread)
        if remark:
            out["displayName"] = remark
        if nick:
            out["nickname"] = nick
        return out

    async def _thread_name(self, thread_id: str) -> str:
        """Name to show for a chat: my remark first, then its own name."""
        try:
            thread = await self.wx.thread(thread_id)
        except WeChatError:
            thread = {"id": thread_id, "displayName": self._link_title(thread_id) or thread_id}
        return (await self._named_thread(thread)).get("displayName") or thread_id

    # --- rendering --------------------------------------------------------
    async def _sender_name(self, msg: dict) -> str:
        sid = msg.get("senderId")
        if not sid:
            return ""
        key = (msg["threadId"], sid)
        if key in self.names:
            return self.names[key]
        name = sid
        remark, nick = await self._contact_names(sid)
        try:
            if remark:
                name = remark
            elif msg["threadId"].endswith("@chatroom"):
                member = await self.wx.group_member(msg["threadId"], sid)
                if member and (m_name := member.get("displayName") or member.get("nickname")):
                    name = m_name
        except WeChatError:
            pass
        if name == sid:
            name = nick or sid
        self.names[key] = name
        return name

    def _body(self, msg: dict) -> str:
        t, body = msg.get("type"), msg.get("body") or ""
        if t == TEXT:
            if link := cards.extract_text_link(body):
                note = f"{_fmt_wx_text(link['note'])}\n" if link.get("note") else ""
                return note + _link_body(link, inline_url=True)
            return _fmt_wx_text(body)
        if t == SYSTEM:
            return f"<i>{_esc(replace_wx_emojis(body))}</i>"
        if t == LOCATION:
            loc = msg.get("location") or {}
            return "📍 " + _esc(" ".join(x for x in (loc.get("name"), loc.get("address")) if x) or body)
        if t == APP and msg.get("appMessageType") == APP_CHANNELS:
            return _channels_body(msg.get("link"), body)
        if t == APP and (link := msg.get("link")):
            return _link_body(link, msg.get("appMessageType"), inline_url=True)
        if t == APP and msg.get("appMessageType") == APP_QUOTE and (link := cards.extract_text_link(body)):
            note = f"{_fmt_wx_text(link['note'])}\n" if link.get("note") else ""
            return note + _link_body(link, inline_url=True)
        if t == APP and msg.get("appMessageType") == 19:
            return "📜 聊天记录\n" + _fmt_wx_text(body)
        if t == STICKER:
            return "[表情]"
        return _fmt_wx_text(body) or _unsupported_label(t, msg.get("appMessageType"))

    @staticmethod
    def _quote_inline_text(q: dict) -> str:
        el = q.get("embeddedLink") or {}
        el_title = el.get("title") or ""
        if _is_channels_placeholder(el_title) or (
            not el_title and (el.get("description") or el.get("sourceName") or el.get("durationSeconds") is not None)
        ):
            el_title = el.get("description") or (f"视频号 · {el['sourceName']}" if el.get("sourceName") else "视频号")
        emb_text = q.get("embeddedText") or ""
        for pfx in ("[Channels video] ", "[Channels] "):
            if emb_text.startswith(pfx):
                emb_text = "🎬 " + emb_text[len(pfx):]
                break
        if emb_text:
            return emb_text
        if q.get("embeddedImage"):
            return "[图片]"
        if q.get("embeddedSticker"):
            return "[表情]"
        if q.get("embeddedVideo"):
            return "[视频]"
        if q.get("embeddedVoice"):
            return "[语音]"
        if ef := q.get("embeddedFile"):
            name = (ef.get("filename") or ef.get("title") or "") if isinstance(ef, dict) else ""
            return f"[文件] {name}" if name else "[文件]"
        if eloc := q.get("embeddedLocation"):
            label = " ".join(x for x in (eloc.get("name"), eloc.get("address")) if x) if isinstance(eloc, dict) else ""
            return f"📍 {label}" if label else "[位置]"
        if el_title:
            return el_title
        for key in ("text", "body", "content", "title"):
            val = q.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        qt, qsub = q.get("type"), q.get("appMessageType")
        if qt == APP:
            return {APP_FILE: "[文件]", 19: "[聊天记录]", APP_CHANNELS: "[视频号]", APP_QUOTE: "[引用消息]"}.get(qsub, "")
        return {IMAGE: "[图片]", VOICE: "[语音]", VIDEO: "[视频]", STICKER: "[表情]", LOCATION: "[位置]"}.get(qt, "")

    @staticmethod
    def _quote_msg_summary(orig: dict) -> str:
        t, sub, body = orig.get("type"), orig.get("appMessageType"), (orig.get("body") or "").strip()
        if t == TEXT:
            if link := cards.extract_text_link(body):
                return (link.get("title") or link.get("description") or link.get("url") or body).strip()
            return body
        if t in (IMAGE, VOICE, VIDEO, STICKER):
            return {IMAGE: "[图片]", VOICE: "[语音]", VIDEO: "[视频]", STICKER: "[表情]"}[t]
        if t == LOCATION:
            loc = orig.get("location") or {}
            label = " ".join(x for x in (loc.get("name"), loc.get("address")) if x) or body
            return f"📍 {label}" if label else "[位置]"
        if t == APP:
            link = orig.get("link") or {}
            if sub == APP_FILE:
                name = (link.get("title") or "").strip()
                return f"[文件] {name}" if name else "[文件]"
            if sub == APP_CHANNELS:
                title = (link.get("description") or link.get("title") or "").strip()
                if title and not _is_channels_placeholder(title):
                    return f"🎬 {title}"
                return f"🎬 视频号 · {link['sourceName']}" if link.get("sourceName") else "[视频号]"
            if sub == 19:
                return f"📜 聊天记录 {body}".strip() if body else "[聊天记录]"
            if link:
                return (link.get("title") or link.get("description") or link.get("url") or "").strip()
        return body

    async def _lookup_wx_message(self, thread_id: str, wx_msg: str | None, wx_server: str | None) -> dict | None:
        wx_msg = Store._norm_id(wx_msg)
        wx_server = Store._norm_id(wx_server)
        if wx_msg:
            try:
                if m := await self.wx.message(wx_msg):
                    return m
            except Exception as e:
                log.debug("Could not fetch quoted WeChat message %s: %s", wx_msg, e)
        if wx_msg or wx_server:
            try:
                for m in await self.wx.thread_messages(thread_id, limit=30):
                    if (wx_msg and Store._norm_id(m.get("id")) == wx_msg) or (
                        wx_server and Store._norm_id(m.get("serverId")) == wx_server
                    ):
                        return m
            except Exception as e:
                log.debug("Could not scan thread %s for message (%s/%s): %s", thread_id, wx_msg, wx_server, e)
        return None

    def _quote_prefix(self, msg: dict, orig: dict | None = None) -> str:
        q = msg.get("quote") or {}
        text = self._quote_inline_text(q) or (self._quote_msg_summary(orig) if orig else "") or "…"
        who = q.get("senderName") or ""
        return f"<blockquote>{_esc(who + '：' if who else '')}{_esc(_clip_text(_normalize_wx_text(text), 200))}</blockquote>"

    # --- sending to Telegram ----------------------------------------------
    async def forward_to_tg(self, chat: int, thread: dict, msg: dict) -> None:
        outgoing = msg.get("direction") == "outgoing"
        header = ""
        if outgoing:
            header = "<b>我</b>（手机端）：\n"
        elif thread.get("kind") == "group" and msg.get("senderId"):
            mentions = msg.get("mentions") if isinstance(msg.get("mentions"), dict) else {}
            tag = " 🔔<b>@我</b>" if mentions.get("self") else " 📢<b>@所有人</b>" if mentions.get("all") else ""
            header = f"<b>{_esc(await self._sender_name(msg))}</b>{tag}：\n"

        reply_to = None
        prefix = ""
        if msg.get("type") == APP and msg.get("appMessageType") == APP_QUOTE and msg.get("quote"):
            q = msg["quote"]
            q_msg_id = Store._norm_id(q.get("messageId") or q.get("referencedMessageId") or q.get("targetMessageId"))
            q_server = Store._norm_id(q.get("serverId") or q.get("serverMessageId"))
            hit = self.store.tg_for_wx(msg["threadId"], q_msg_id, q_server, tg_chat=chat)
            orig = None
            if not (hit and hit[0] == chat) and (q_msg_id or q_server):
                if not self._quote_inline_text(q):
                    orig = await self._lookup_wx_message(msg["threadId"], q_msg_id, q_server)
                    if orig:
                        hit2 = self.store.tg_for_wx(msg["threadId"], orig.get("id"), orig.get("serverId"),
                                                    tg_chat=chat)
                        if hit2 and hit2[0] == chat:
                            hit = hit2
            if hit and hit[0] == chat:
                reply_to = hit[1]
            else:
                prefix = self._quote_prefix(msg, orig)

        media: Media | str | None = None
        channels_video_err = ""
        t = msg.get("type")
        is_channels = t == APP and msg.get("appMessageType") == APP_CHANNELS
        if t in (IMAGE, VIDEO, VOICE, STICKER) or (t == APP and msg.get("appMessageType") == APP_FILE):
            try:
                # Animated wxgf stickers keep their frames only in the original bytes (decoding needs ffmpeg).
                raw_wxgf = t == STICKER and stickers.find_ffmpeg() is not None
                media = await self.wx.download_media(msg, self.max_media, raw_wxgf=raw_wxgf)
            except WeChatError as e:
                media = e.code
            if isinstance(media, str):
                log.info("Could not download the attachment of WeChat message %s (type %s): %s", msg["id"], t, media)
            if isinstance(media, Media) and t == VOICE and media.mime == "audio/wav":
                if ogg := await wav_to_ogg(media.read()):
                    media.close()
                    media = Media(ogg, "audio/ogg", "voice.ogg")
        elif is_channels:
            try:
                media = await self.wx.download_channels_video(msg, self.max_media)
            except WeChatError as e:
                media = e.code
            if isinstance(media, str):
                channels_video_err = media
                log.info("Could not download Channels video of WeChat message %s: %s", msg["id"], media)
                media = None
            if media is None and (cover := await self.wx.download_link_thumbnail(msg)):
                media = _guess_image_media(cover, "channels-cover")
        elif (link_info := _msg_link(msg))[0] and self.cfg.bridge.link_card_images:
            link, link_app_type, is_native_link = link_info
            thumb_bytes = await self.wx.download_link_thumbnail(msg) if is_native_link else None
            card_png = await asyncio.to_thread(
                cards.render_link_card,
                title=_normalize_wx_text(link.get("title")),
                description=_normalize_wx_text(link.get("description")),
                url=link.get("url"),
                source_name=_normalize_wx_text(link.get("sourceName")),
                app_type=link_app_type,
                thumbnail_bytes=thumb_bytes,
            )
            if card_png:
                media = Media(card_png, "image/png", "link-card.png")

        silent = outgoing and self.cfg.bridge.outgoing_silent
        try:
            sent = await self._send_tg(
                chat, thread, header, prefix, msg, media, reply_to, silent,
                channels_video_err=channels_video_err,
            )
        finally:
            if isinstance(media, Media):
                media.close()  # large downloads were streamed to a temp file
        for m in sent:
            self.store.map_message(chat, "bot", m.id, msg["threadId"], msg["id"], msg.get("serverId"))
            if chat < -1000000000000:
                self.store.map_message(chat, "user", m.id, msg["threadId"], msg["id"], msg.get("serverId"))
            if not self.topics:
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
        # If this user message was recorded before the bot update arrived, persist its bot-side id too.
        if self.store.wx_for_tg(chat_id, bot_id, side="bot") is None:
            if entry := self.store.wx_entry_for_tg(chat_id, user_id, side="user"):
                self.store.map_message(chat_id, "bot", bot_id, *entry)
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
        if self.topics or chat_id < -1000000000000:
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

    async def _send_tg(self, chat, thread, header, prefix, msg, media, reply_to, silent,
                       *, channels_video_err: str = "") -> list:
        async def go(chat_id: int) -> list:
            out = []
            # Topics mode: anything that is not a reply goes to the thread's topic.
            top = self._topic_for(chat_id, thread["id"])
            reply = reply_to if reply_to is not None else top
            t = msg.get("type")
            is_channels = t == APP and msg.get("appMessageType") == APP_CHANNELS
            link, link_app_type, _ = _msg_link(msg)
            is_link = link is not None
            link_note = f"{_fmt_wx_text(link['note'])}\n" if (link and link.get("note")) else ""
            send_media = media
            channels_err_note = (
                f"\n<i>（视频无法获取：{_esc(MEDIA_REASONS.get(channels_video_err, channels_video_err))}）</i>"
                if is_channels and channels_video_err else ""
            )
            if isinstance(send_media, Media) and is_link:
                f = send_media.tg_file()
                caption = (header + prefix + link_note).rstrip("\n")
                buttons = _link_buttons(link, link_app_type, self._reader_link(link))
                kw = dict(caption=caption[:TG_CAPTION_LIMIT] or None, parse_mode="html", reply_to=reply,
                          silent=silent, force_document=False, buttons=buttons)
                try:
                    out.append(await self.bot.send_file(chat_id, f, **kw))
                    return out
                except errors.RPCError as e:
                    if _dead_chat(e) or _topic_gone(e):
                        raise
                    if buttons:
                        url = _link_url(link)
                        src = cards.identify_link_source(link, link_app_type)
                        link_line = f'<a href="{_esc(url)}"><b>{_esc(src.button_label)}</b></a>'
                        fallback_cap = f"{caption}\n{link_line}" if caption else link_line
                        f = send_media.tg_file()
                        kw["buttons"] = None
                        kw["caption"] = fallback_cap[:TG_CAPTION_LIMIT]
                        try:
                            out.append(await self.bot.send_file(chat_id, f, **kw))
                            return out
                        except errors.RPCError as e2:
                            if _dead_chat(e2) or _topic_gone(e2):
                                raise
                            log.info("Telegram refused link card photo (%s); falling back to text", e2)
                    else:
                        log.info("Telegram refused link card photo (%s); falling back to text", e)
                    send_media = None

            sticker = None
            if t == STICKER and isinstance(send_media, Media):
                sticker = await asyncio.to_thread(stickers.from_wechat_sticker, send_media.read(), send_media.mime)
                if sticker is None and send_media.mime == "image/x-wxgf":
                    # The original wxgf could not be decoded: use agent-wechat's still preview instead.
                    send_media = await self._wxgf_still(msg)
                    if isinstance(send_media, Media):
                        sticker = await asyncio.to_thread(
                            stickers.from_wechat_sticker, send_media.read(), send_media.mime)
            if isinstance(send_media, Media):
                caption = (
                    header + prefix
                    + (self._body(msg) if t == APP and msg.get("appMessageType") != APP_FILE else "")
                    + channels_err_note
                )
                if t == STICKER:
                    if sticker is not None:
                        sk_kind, sk_bytes, sk_meta = sticker
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
                            sk_kwargs["reply_to"] = top
                        try:
                            out.append(await self.bot.send_file(chat_id, sf, **sk_kwargs))
                            return out
                        except errors.RPCError as e:
                            if _dead_chat(e):
                                raise
                            log.info("Telegram refused sticker as-is (%s); falling back to original media", e)
                            if out:
                                caption = ""
                                reply = top
                    if send_media.mime == "image/x-wxgf":  # Telegram can't show wxgf itself
                        still = await self._wxgf_still(msg)
                        if not isinstance(still, Media):
                            text = ("" if out else header + prefix) + _media_failure_text(msg, still)
                            out.append(await self.bot.send_message(chat_id, text, parse_mode="html", reply_to=reply,
                                                                   silent=silent, link_preview=False))
                            return out
                        send_media = still
                f = send_media.tg_file()
                is_voice = t == VOICE and send_media.mime in ("audio/ogg", "audio/opus")
                is_video = t == VIDEO or (is_channels and send_media.mime.startswith("video/"))
                kwargs = dict(
                    caption=caption[:TG_CAPTION_LIMIT] or None, parse_mode="html", reply_to=reply,
                    silent=silent, voice_note=is_voice, supports_streaming=is_video,
                    force_document=(t == APP and msg.get("appMessageType") == APP_FILE) or (
                        t == VOICE and not send_media.mime.startswith("audio/")),
                )
                if is_video:
                    if send_media.mime.startswith("video/"):
                        kwargs["mime_type"] = send_media.mime
                    kwargs["nosound_video"] = True
                    vmeta = await asyncio.to_thread(
                        stickers.extract_video_meta_and_thumb, send_media.data, send_media.filename,
                        path=send_media.path,
                    )
                    if vmeta is not None:
                        vw, vh, vdur, vthumb = vmeta
                        kwargs["attributes"] = [
                            types.DocumentAttributeVideo(
                                duration=round(vdur, 2), w=vw, h=vh, supports_streaming=True
                            )
                        ]
                        tf = io.BytesIO(vthumb)
                        tf.name = "thumb.jpg"
                        kwargs["thumb"] = tf
                try:
                    out.append(await self.bot.send_file(chat_id, f, **kwargs))
                except errors.RPCError as e:
                    if _dead_chat(e) or _topic_gone(e) or kwargs["force_document"]:
                        raise
                    # e.g. a photo too large/odd-sized for Telegram's photo type: send it as a file instead
                    log.info("Telegram refused media as-is (%s); sending as a file", e)
                    f = send_media.tg_file()
                    kwargs.update(force_document=True, voice_note=False, supports_streaming=False, nosound_video=None)
                    kwargs.pop("attributes", None)
                    kwargs.pop("thumb", None)
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
                text = header + prefix + _media_failure_text(msg, send_media)
                buttons = None
            elif is_link:
                buttons = _link_buttons(link, link_app_type, self._reader_link(link))
                text = header + prefix + link_note + _link_body(link, link_app_type, inline_url=not buttons)
            else:
                buttons = None
                text = header + prefix + self._body(msg) + channels_err_note
            parts = _chunks(text)
            try:
                for i, part in enumerate(parts):
                    out.append(await self.bot.send_message(
                        chat_id, part, parse_mode="html", silent=silent,
                        reply_to=reply if i == 0 else top,
                        buttons=buttons if i == len(parts) - 1 else None,
                        link_preview=False,
                    ))
            except errors.RPCError as e:
                if _dead_chat(e) or _topic_gone(e) or not (is_link and buttons):
                    raise
                out.clear()
                fallback_parts = _chunks(header + prefix + link_note + _link_body(
                    link, link_app_type, inline_url=True))
                for i, part in enumerate(fallback_parts):
                    out.append(await self.bot.send_message(
                        chat_id, part, parse_mode="html", silent=silent,
                        reply_to=reply if i == 0 else top,
                        link_preview=False,
                    ))
            if (
                t == LOCATION
                and (loc := msg.get("location"))
                and loc.get("latitude") is not None
                and loc.get("longitude") is not None
            ):
                geo = types.InputMediaGeoPoint(types.InputGeoPoint(lat=loc["latitude"], long=loc["longitude"]))
                out.append(await self.bot.send_file(chat_id, geo, silent=silent,
                                                    **({"reply_to": top} if top else {})))
            return out

        try:
            return await go(chat)
        except Exception as e:
            if not await self._drop_dead_dest(chat, thread, e):
                raise
            new_chat = await self.ensure_chat(thread)
            if not new_chat:
                return []
            reply_to = None  # the message it replied to went away with the old group/topic
            return await go(new_chat)

    async def on_wx_recall(self, data: dict) -> None:
        tid = data.get("threadId") or ""
        sid = data.get("serverId")
        if sid and (tid, str(sid)) in self.tg_recalls:
            self.tg_recalls.discard((tid, str(sid)))
            return
        dest = self._dest_for_thread(tid)
        hit = self.store.tg_for_wx(tid, data.get("messageId"), sid, tg_chat=dest[0] if dest else None)
        if not hit:
            return
        text = "↩️ 这条消息已在微信中撤回"
        if data.get("replacementText"):
            text += f"\n<i>{_esc(replace_wx_emojis(data['replacementText']))}</i>"
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
            self._calls_client_id = event.get("clientId") or ""
            self._calls_caps = event.get("capabilities") or {}
            live_ids = {
                x.get("callId")
                for x in (event.get("currentOffer"), event.get("currentPendingOffer"), event.get("currentCall"))
                if isinstance(x, dict) and x.get("callId")
            }
            for cid, entry in list(self._active_calls.items()):
                if not entry["terminal"] and cid not in live_ids:
                    entry["terminal"] = True
                    entry["stage"] = "ended"
                    if not self._call_status_final(entry):
                        entry["status_text"] = (
                            "⚪ 通话已结束" if entry["was_connected"]
                            else "⚪ 呼叫已结束或状态已重置" if entry.get("direction") == "outgoing"
                            else "⚪ 来电已结束或状态已重置")
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
                        if not self._call_status_final(entry):
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
        elif etype == "call.invitation_result":
            await self._on_invitation_result(event)
        elif etype == "track.updated":
            if self.call_web is not None:
                await self.call_web.on_track_updated(event)
        elif etype == "call.error":
            log.warning("WeChat call error event: %s", event.get("code"))

    async def _resolve_call_target(self, thread_id: str, caller_id: str,
                                   kind: str) -> tuple[dict | None, str]:
        """(thread, caller's name, my remark first); thread None when the chat is not bridged."""
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
            caller_name = (await self._named_thread(thread)).get("displayName") or caller_id
        else:
            caller_name = await self._sender_name({"threadId": thread_id, "senderId": caller_id})
        return thread, caller_name or caller_id

    async def _send_call_tg_msg(self, thread: dict, text: str, buttons=None) -> tuple[int, int] | None:
        chat = await self.ensure_chat(thread)
        if chat is None:
            return None
        for retry in (False, True):
            topic = self._topic_for(chat, thread["id"])
            try:
                sent = await self._rich_call_msg(self.bot.send_message, chat, text, buttons=buttons,
                                                 link_preview=False, **({"reply_to": topic} if topic else {}))
                return chat, sent.id
            except Exception as e:
                if retry or not await self._drop_dead_dest(chat, thread, e):
                    raise
                chat = await self.ensure_chat(thread)
                if not chat:
                    return None
        return None

    async def _rich_call_msg(self, send, *args, **kw):
        """Send or edit a call card with Telegram's date entity; if Telegram refuses it, drop it for good."""
        text = args[-1]
        if self._tg_time_ok or "<tg-time" not in text:
            try:
                return await send(*args, parse_mode=tghtml.HTML, **kw)
            except errors.MessageNotModifiedError:
                raise
            except errors.BadRequestError as e:
                if "<tg-time" not in text or "ENTIT" not in str(getattr(e, "message", "") or e).upper():
                    raise
                log.warning("Telegram refused a date entity (%s); call cards show plain times from now on", e)
                self._tg_time_ok = False
        return await send(*args[:-1], tghtml.strip(text), parse_mode="html", **kw)

    @staticmethod
    def _call_status_final(entry: dict) -> bool:
        """The call ended by our own action: keep that status instead of a generic "ended"."""
        return bool(entry.get("rejected_by_tg") or entry.get("hungup_by_tg"))

    def _render_call(self, entry: dict) -> tuple[str, list | None]:
        outgoing = entry.get("direction") == "outgoing"
        if entry["kind"] == "conference" and outgoing:
            title, who = "📞 <b>微信群语音通话</b>", "群"
        elif entry["kind"] == "conference":
            title, who = "📞 <b>微信群语音通话</b>", "发起人"
        elif outgoing:
            title, who = "📞 <b>微信语音通话</b>", "呼叫"
        elif entry["call_type"] == "video":
            title, who = "📹 <b>微信视频来电</b>", "来电人"
        else:
            title, who = "📞 <b>微信语音来电</b>", "来电人"
        lines = [f"{who}：<b>{_esc(entry['caller_name'])}</b>",
                 f"状态：{entry['status_text']}"]
        if when := _call_time_line(entry):
            lines.append(when)
        text = _card(title, lines) + self._render_group_progress(entry)
        rows: list[list] = []
        web = self.call_web
        if not entry["terminal"]:
            # One button per row: full-width buttons are much easier to hit on a phone.
            if entry["stage"] in ("pending", "offered"):
                if web is not None and web.can_answer(entry):
                    rows.append([Button.url("📞 接听", web.link(web.token_for_call(entry)))])
                rows.append([Button.inline("📵 拒接", f"callrej:{entry['token']}".encode())])
            elif web is not None and entry.get("answered_by_tg") and web.has_session(entry["call_id"]):
                rows.append([Button.url("🎧 通话界面", web.link(web.token_for_call(entry)))])
                if web.can_invite(entry):
                    rows.append([Button.inline("➕ 邀请更多人", f"callinv:{entry['token']}".encode())])
                label = "📴 取消呼叫" if outgoing and entry["stage"] in ("dialing", "ringing") else "📴 挂断"
                rows.append([Button.inline(label, f"callhup:{entry['token']}".encode())])
        return text, rows or None

    @staticmethod
    def _render_group_progress(entry: dict) -> str:
        """Extra lines for a group call we started: who was invited, who joined, whose invitation failed."""
        invitees: dict = entry.get("invitees") or {}
        if not invitees:
            return ""
        names = list(invitees.values())
        shown = "、".join(_esc(n) for n in names[:CALL_INVITEES_SHOWN])
        more = f" 等 {len(names)} 人" if len(names) > CALL_INVITEES_SHOWN else ""
        out = [f"邀请（{len(names)} 人）：{shown}{more}"]
        joined = [p for p in entry.get("participants") or [] if p.get("membership") == "joined"
                  and p.get("contactId") in invitees]
        if joined:
            out.append(f"已加入：{len(joined)} 人")
        failed = entry.get("invite_failed") or {}
        if failed:
            out.append("邀请异常：" + "、".join(f"{_esc(n)}（{why}）" for n, why in failed.values()))
        # A long list folds away: Telegram shows the first lines and a tap expands the rest.
        return "\n<blockquote expandable>" + "\n".join(out) + "</blockquote>"

    async def _on_invitation_result(self, event: dict) -> None:
        call_id = event.get("callId") or ""
        entry = self._active_calls.get(call_id)
        if entry is None:
            # The results of a group call we are starting can beat the dial command's own result.
            if call_id:
                self._orphan_invite_results.setdefault(call_id, []).append(event)
                while len(self._orphan_invite_results) > 20:
                    self._orphan_invite_results.pop(next(iter(self._orphan_invite_results)))
            return
        invitees = entry.get("invitees") or {}
        failed = entry.setdefault("invite_failed", {})
        for r in event.get("results") or []:
            cid = r.get("contactId") or ""
            status = r.get("status")
            if r.get("verificationRequired"):
                why = "需要验证"
            elif status == "rejected":
                why = "被拒绝"
            elif status in ("unknown", "uncertain"):
                why = "结果未知"
            else:
                failed.pop(cid, None)
                continue
            failed[cid] = (invitees.get(cid) or cid, why)
        await self._sync_call_msg(entry)

    @staticmethod
    def _render_key(text: str, buttons: list | None) -> tuple:
        return text, tuple(getattr(b, "text", "") for row in (buttons or []) for b in row)

    async def _sync_call_msg(self, entry: dict) -> None:
        if self.call_web is not None:
            try:
                await self.call_web.on_entry_changed(entry)
            except Exception:
                log.exception("Could not update the call page for %s", entry.get("call_id"))
        if entry["terminal"] and entry.get("direction") == "outgoing" and entry["kind"] == "conference":
            await self._close_call_invites(entry)
        if entry["terminal"] and entry.get("connected_at") and not entry.get("ended_at"):
            entry["ended_at"] = time.time()
        if not entry.get("chat_id") or not entry.get("msg_id"):
            return
        text, buttons = self._render_call(entry)
        render_key = self._render_key(text, buttons)
        if entry.get("_last_render") == render_key:
            return
        try:
            await self._rich_call_msg(self.bot.edit_message, entry["chat_id"], entry["msg_id"], text,
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
            if entry["terminal"] and not (self.call_web and self.call_web.has_session(cid)):
                self._active_calls.pop(cid, None)
                self._call_tokens.pop(entry["token"], None)
            if len(self._active_calls) <= 150:
                break

    def _new_call_entry(self, call_id: str, *, kind: str, call_type: str, caller_id: str, caller_name: str,
                        thread_id: str, stage: str, revision: int, phase: str | None = None) -> dict:
        self._prune_calls()
        token = secrets.token_hex(6)
        entry = {
            "call_id": call_id,
            "token": token,
            "kind": kind,
            "call_type": call_type,
            "caller_id": caller_id,
            "caller_name": caller_name,
            "thread_id": thread_id,
            "stage": stage,
            "phase": phase,
            "revision": revision,
            "chat_id": None,
            "msg_id": None,
            "terminal": False,
            "rejected_by_tg": False,
            "rejecting": False,
            "answering": False,
            "answered_by_tg": False,
            "hungup_by_tg": False,
            "was_connected": False,
            "connected_at": None,
            "offered_event": asyncio.Event(),
            "status_text": "🔔 响铃中…",
            "_last_render": None,
        }
        if stage == "offered":
            entry["offered_event"].set()
        self._active_calls[call_id] = entry
        self._call_tokens[token] = call_id
        return entry

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

        entry = self._new_call_entry(
            call_id, kind=kind, call_type=offer.get("callType") or "voice", caller_id=caller_id,
            caller_name=caller_name, thread_id=thread["id"], stage=stage,
            revision=offer.get("revision", 0),
            phase=offer.get("phase"),
        )
        text, buttons = self._render_call(entry)
        res = await self._send_call_tg_msg(thread, text, buttons=buttons)
        if not res:
            return
        entry["chat_id"], entry["msg_id"] = res
        entry["_last_render"] = self._render_key(text, buttons)
        # In case call state updated while ensure_chat was creating the group:
        await self._sync_call_msg(entry)

    async def _replay_orphan_call_state(self, call_id: str) -> None:
        """A call we just claimed may have reported its state before we knew about it."""
        if call := self._orphan_call_states.pop(call_id, None):
            await self._on_call_state(call)
        for ev in self._orphan_invite_results.pop(call_id, []):
            await self._on_invitation_result(ev)

    async def _on_call_state(self, call: dict) -> None:
        call_id = call.get("callId")
        if not call_id:
            return
        state = call.get("state") or ""
        entry = self._active_calls.get(call_id)
        if entry is None:
            if call.get("direction") != "incoming" or state in ("ending", "ended", "uncertain"):
                # Maybe a group call we are joining right now: keep it for _replay_orphan_call_state.
                self._orphan_call_states[call_id] = call
                while len(self._orphan_call_states) > 20:
                    self._orphan_call_states.pop(next(iter(self._orphan_call_states)))
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
        if entry["terminal"]:
            await self._sync_call_msg(entry)
            return
        conf = entry["kind"] == "conference"
        outgoing = entry.get("direction") == "outgoing"
        group_out = conf and outgoing
        if group_out and isinstance(call.get("participants"), list):
            entry["participants"] = call["participants"]
        if state == "offered":
            entry["stage"] = "offered"
            entry["status_text"] = "🔔 响铃中…"
            entry["offered_event"].set()
        elif state in ("dialing", "ringing") and outgoing:
            entry["stage"] = state
            entry["status_text"] = ("📞 正在发起群通话…" if group_out else
                                    "📞 正在呼叫…" if state == "dialing" else "🔔 对方响铃中…")
        elif state in ("dialing", "ringing"):
            entry["stage"] = state
            entry["status_text"] = "🟡 正在加入…" if entry.get("answered_by_tg") else "🔔 响铃中…"
        elif state == "connecting":
            entry["stage"] = "connecting"
            entry["status_text"] = ("🟡 正在发起群通话…" if group_out else
                                    "🟡 正在加入…" if conf and entry.get("answered_by_tg") else "🟡 接通中…")
            entry["offered_event"].set()
        elif state == "connected":
            entry["stage"] = "connected"
            entry["was_connected"] = True
            entry["connected_at"] = entry.get("connected_at") or time.time()
            entry["status_text"] = ("🟢 群通话中（Telegram 发起）" if group_out else
                                    "🟢 通话中（Telegram 发起）" if outgoing else
                                    "🟢 通话中（Telegram 接听）" if entry.get("answered_by_tg") else "🟢 通话中")
            entry["offered_event"].set()
        elif state in ("ending", "ended"):
            entry["stage"] = state
            entry["terminal"] = True
            if not self._call_status_final(entry):
                entry["status_text"] = ("⚪ 通话已结束" if entry["was_connected"] else
                                        "⚪ 群通话未能发起" if group_out else
                                        "⚪ 呼叫未接通（对方未接听、已拒绝或忙线）" if outgoing else
                                        "⚪ 来电已结束（未接听或已取消）")
            entry["offered_event"].set()
        elif state == "uncertain":
            entry["stage"] = "uncertain"
            entry["terminal"] = True
            if not self._call_status_final(entry):
                entry["status_text"] = "⚠️ 通话状态未确认"
            entry["offered_event"].set()
        await self._sync_call_msg(entry)

    def _render_invite(self, inv: dict) -> tuple[str, list | None]:
        text = _card("📞 <b>微信群语音通话邀请</b>",
                     [f"发起人：<b>{_esc(inv['caller_name'])}</b>",
                      f"状态：{inv['status_text']}"])
        web = self.call_web
        if web is not None and inv.get("live") and web.can_join():
            return text, [[Button.url("📞 加入", web.link(web.token_for_invite(inv)))]]
        return text, None

    async def _on_conf_invitations(self, invitations: list[dict]) -> None:
        current = {inv.get("invitationId"): inv for inv in invitations if inv.get("invitationId")}
        for inv_id in list(self._conf_invites):
            if inv_id in current:
                continue
            old = self._conf_invites.pop(inv_id)
            old["live"] = False
            if old.get("joined_call"):
                continue  # the invitation message now shows the call we joined
            old["status_text"] = "⚪ 邀请已结束或已过期"
            if self.call_web is not None:
                await self.call_web.on_invite_changed(old)
            text, _ = self._render_invite(old)
            try:
                await self.bot.edit_message(old["chat_id"], old["msg_id"], text, buttons=None,
                                            parse_mode="html", link_preview=False)
            except errors.MessageNotModifiedError:
                pass
            except Exception as e:
                log.info("Could not update conference invitation %s: %s", inv_id, e)

        for inv_id, inv in current.items():
            group_id = inv.get("groupId") or ""
            caller_id = inv.get("callerId") or ""
            if known := self._conf_invites.get(inv_id):
                known["revision"] = inv.get("revision", known.get("revision", 0))
                continue
            if not group_id:
                continue
            thread, caller_name = await self._resolve_call_target(group_id, caller_id, "conference")
            if thread is None:
                continue
            entry = {
                "invitation_id": inv_id,
                "revision": inv.get("revision", 0),
                "group_id": group_id,
                "group_name": thread.get("displayName") or group_id,
                "caller_id": caller_id,
                "caller_name": caller_name,
                "status_text": "🔔 邀请中…",
                "live": True,
                "chat_id": None,
                "msg_id": None,
            }
            text, buttons = self._render_invite(entry)
            res = await self._send_call_tg_msg(thread, text, buttons=buttons)
            if res:
                entry["chat_id"], entry["msg_id"] = res
                self._conf_invites[inv_id] = entry

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
        if entry["rejecting"] or entry.get("answering"):
            await event.answer("正在处理中…")
            return
        sock = self._calls_sock
        if sock is None or sock.closed:
            await event.answer("通话信令未连接，无法拒接", alert=True)
            return
        entry["rejecting"] = True  # before the first await: a double tap must not reject twice
        await event.answer("正在拒接…")
        await self._reject_call(entry)

    async def _on_call_hangup_callback(self, event, token: str) -> None:
        if event.sender_id != self.owner_id:
            await event.answer("只有桥接账号本人可以操作", alert=True)
            return
        call_id = self._call_tokens.get(token)
        entry = self._active_calls.get(call_id) if call_id else None
        if not entry or entry["terminal"] or self.call_web is None:
            await event.answer("通话已结束")
            if entry:
                await self._sync_call_msg(entry)
            return
        await event.answer("正在挂断…")
        try:
            await self.call_web.hangup(entry)
        except CallActionError as e:
            await self._report_call_reject_failure(entry, str(e))

    async def _reject_call(self, entry: dict) -> None:
        """Reject a pending/offered incoming call; failures are reported in the call's Telegram chat."""
        sock = self._calls_sock
        if sock is None or sock.closed:
            entry["rejecting"] = False
            await self._report_call_reject_failure(entry, "通话信令未连接，无法拒接")
            return
        call_id = entry["call_id"]
        entry["rejecting"] = True
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
                if self.call_web is not None:
                    await self.call_web.withdraw_readiness()
                else:
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

    # ---------------------------------------------------------------- outgoing calls (/call)
    def _dial_unavailable(self, thread_id: str) -> str:
        conf = thread_id.endswith("@chatroom")
        if thread_id == "filehelper" or thread_id.startswith("gh_"):
            return "文件传输助手和公众号不能接听微信电话。"
        if self.call_web is None:
            return ("没有配置通话页面（<code>CALL_WEB_URL</code>），无法在 Telegram 打微信电话。"
                    "配置方法见 README「在 Telegram 接听与拨打微信语音通话」。")
        if not self.wx_ready:
            return "🔐 微信尚未登录，登录后再打电话。"
        if self._calls_sock is None or self._calls_sock.closed:
            return "通话信令未连接（agent-wechat 通话功能不可用或正在重连），请稍后重试。"
        if not self.call_web.can_dial(conf):
            what = "群语音通话" if conf else "语音通话"
            return f"当前 agent-wechat 不支持发起微信{what}，请升级到 2.7 或更新版本。"
        return ""

    def _render_dial(self, req: dict) -> tuple[str, list | None]:
        if req.get("kind") == "conference":
            return self._render_group_dial(req)
        text = _card("📞 <b>微信语音通话</b>",
                     [f"呼叫：<b>{_esc(req['name'])}</b>", f"状态：{req['status_text']}"])
        web = self.call_web
        if not req["live"] or web is None:
            return text, None
        return text, [[Button.url("📞 开始呼叫", web.link(web.token_for_dial(req)))],
                      [Button.inline("✖️ 取消", f"dialx:{req['dial_id']}".encode())]]

    def _render_group_dial(self, req: dict) -> tuple[str, list | None]:
        selected = req["selected"]
        names = req["names"]
        inviting = bool(req.get("invite_call"))
        title = "➕ <b>邀请加入群语音通话</b>" if inviting else "📞 <b>微信群语音通话</b>"
        lines = [f"群：<b>{_esc(req['name'])}</b>"]
        if selected:
            shown = "、".join(_esc(names.get(cid) or cid) for cid in selected[:DIAL_PICK_SHOWN])
            more = f" 等 {len(selected)} 人" if len(selected) > DIAL_PICK_SHOWN else ""
            lines.append(f"邀请（{len(selected)} 人）：{shown}{more}")
        else:
            lines.append("邀请：尚未选择")
        lines.append(f"状态：{req['status_text']}")
        text = _card(title, lines)
        web = self.call_web
        if not req["live"] or web is None:
            return text, None
        did = req["dial_id"]
        members = req["members"]
        pages = max(1, -(-len(members) // DIAL_PICK_PAGE))
        page = min(req["page"], pages - 1)
        rows: list[list] = []
        chunk = list(enumerate(members))[page * DIAL_PICK_PAGE:(page + 1) * DIAL_PICK_PAGE]
        chosen = set(selected)
        for i in range(0, len(chunk), 2):
            rows.append([
                Button.inline(f"{'✅' if m['id'] in chosen else '▫️'} {_short(m['label'], 12)}",
                              f"dialp:{did}:t{idx}".encode())
                for idx, m in chunk[i:i + 2]
            ])
        if pages > 1:
            rows.append([Button.inline("◀️", f"dialp:{did}:p{(page - 1) % pages}".encode()),
                         Button.inline(f"{page + 1}/{pages}", f"dialp:{did}:n".encode()),
                         Button.inline("▶️", f"dialp:{did}:p{(page + 1) % pages}".encode())])
        rows.append([Button.inline("☑️ 全选", f"dialp:{did}:a".encode()),
                     Button.inline("🔄 清空", f"dialp:{did}:c".encode())])
        if selected and inviting:
            # The call page already has the microphone: inviting is a plain command, no page needed.
            rows.append([Button.inline(f"➕ 发送邀请（{len(selected)} 人）", f"dialp:{did}:go".encode())])
        elif selected:
            rows.append([Button.url(f"📞 开始呼叫（{len(selected)} 人）", web.link(web.token_for_dial(req)))])
        rows.append([Button.inline("✖️ 取消", f"dialx:{did}".encode())])
        return text, rows

    async def _group_dial_members(self, group_id: str) -> list[dict]:
        members = await self.wx.group_members(group_id)
        out = []
        for m in members:
            cid = m.get("id") or m.get("username")
            if not cid or cid == self._self_wxid or cid.endswith("@chatroom"):
                continue
            out.append({"id": cid, "label": self._known_remark(cid) or _member_name(m) or cid,
                        "search": " ".join(str(m.get(k) or "") for k in
                                           ("mentionName", "roomDisplayName", "displayName", "nickname",
                                            "alias", "username", "id")).lower()})
        return out

    async def _start_dial(self, chat_id: int, reply_to: int | None, thread_id: str, query: str = "") -> None:
        if why := self._dial_unavailable(thread_id):
            await self.bot.send_message(chat_id, why, parse_mode="html", reply_to=reply_to)
            return
        conf = thread_id.endswith("@chatroom")
        name = self._link_title(thread_id) or thread_id
        try:
            name = await self._thread_name(thread_id)
        except Exception:
            pass
        req = {
            "dial_id": secrets.token_hex(6),
            "kind": "conference" if conf else "direct",
            "contact_id": thread_id,
            "thread_id": thread_id,
            "name": name,
            "chat_id": chat_id,
            "msg_id": None,
            "status_text": "点击「开始呼叫」，在通话页面授权麦克风后拨出",
            "live": True,
            "dialing": False,
            "call_id": None,
            "expire_task": None,
        }
        if conf:
            try:
                members = await self._group_dial_members(thread_id)
            except WeChatError as e:
                await self.bot.send_message(chat_id, f"获取群成员失败：{_esc(e.code)}", parse_mode="html",
                                            reply_to=reply_to)
                return
            if not members:
                await self.bot.send_message(chat_id, "没有拿到群成员，无法发起群语音通话", reply_to=reply_to)
                return
            terms = query.lower().split()
            picked = [m for m in members if any(t in m["search"] for t in terms)] if terms else []
            if terms and not picked:
                await self.bot.send_message(chat_id, f"没有匹配「{_esc(query)}」的群成员", parse_mode="html",
                                            reply_to=reply_to)
                return
            picked = picked[:callweb.MAX_INVITEES]
            ids = {m["id"] for m in picked}
            req.update(
                members=picked + [m for m in members if m["id"] not in ids],  # matches first
                names={m["id"]: m["label"] for m in members},
                selected=[m["id"] for m in picked],
                page=0,
                status_text="勾选要邀请的成员，再点「开始呼叫」，在通话页面授权麦克风后发起",
            )
        for old in list(self._dial_requests.values()):
            if (old["live"] and old["thread_id"] == thread_id and not old.get("dialing")
                    and not old.get("invite_call")):
                await self._close_dial_request(old, "⚪ 已发起新的呼叫，此链接已失效")
        await self._post_dial_request(req, reply_to)

    async def _post_dial_request(self, req: dict, reply_to: int | None) -> None:
        while len(self._dial_requests) > 50:
            self._dial_requests.pop(next(iter(self._dial_requests)))
        text, buttons = self._render_dial(req)
        sent = await self.bot.send_message(req["chat_id"], text, parse_mode="html", buttons=buttons,
                                           link_preview=False, reply_to=reply_to)
        req["msg_id"] = sent.id
        req["_last_render"] = self._render_key(text, buttons)
        self._dial_requests[req["dial_id"]] = req
        req["expire_task"] = task = asyncio.create_task(self._expire_dial(req))
        self._notice_tasks.add(task)
        task.add_done_callback(self._notice_tasks.discard)

    async def _sync_dial_msg(self, req: dict) -> None:
        if self.call_web is not None:
            await self.call_web.on_dial_changed(req)
        text, buttons = self._render_dial(req)
        key = self._render_key(text, buttons)
        if req.get("_last_render") == key:
            return
        try:
            await self.bot.edit_message(req["chat_id"], req["msg_id"], text, parse_mode="html", buttons=buttons,
                                        link_preview=False)
            req["_last_render"] = key
        except errors.MessageNotModifiedError:
            req["_last_render"] = key
        except Exception as e:
            log.info("Could not update call request %s: %s", req["dial_id"], e)

    async def _on_dial_pick_callback(self, event, payload: str) -> None:
        if event.sender_id != self.owner_id:
            await event.answer("只有桥接账号本人可以操作", alert=True)
            return
        dial_id, _, op = payload.partition(":")
        req = self._dial_requests.get(dial_id)
        if req is None or not req["live"] or req.get("kind") != "conference":
            await event.answer("呼叫已开始或已结束")
            return
        if req.get("dialing"):
            await event.answer("正在发起群通话…" if not req.get("invite_call") else "正在发送邀请…")
            return
        if req.get("invite_call"):
            entry = self._active_calls.get(req["invite_call"])
            if entry is None or self.call_web is None or not self.call_web.can_invite(entry):
                await event.answer("群通话已结束")
                await self._close_dial_request(req, "⚪ 群通话已结束，无法再邀请")
                return
            if op == "go":
                await self._send_call_invite(event, req, entry)
                return
        selected: list[str] = req["selected"]
        members: list[dict] = req["members"]
        limit = callweb.MAX_INVITEES
        note = None
        if op.startswith("t") and op[1:].isdigit() and int(op[1:]) < len(members):
            cid = members[int(op[1:])]["id"]
            if cid in selected:
                selected.remove(cid)
            elif len(selected) >= limit:
                await event.answer(f"一次最多邀请 {limit} 人", alert=True)
                return
            else:
                selected.append(cid)
        elif op.startswith("p") and op[1:].isdigit():
            req["page"] = int(op[1:])
        elif op == "a":
            chosen = set(selected)
            for m in members:
                if len(selected) >= limit:
                    note = f"一次最多邀请 {limit} 人，已选前 {limit} 人"
                    break
                if m["id"] not in chosen:
                    selected.append(m["id"])
        elif op == "c":
            selected.clear()
        if note:
            await event.answer(note, alert=True)
        else:
            await event.answer()
        await self._sync_dial_msg(req)

    async def _expire_dial(self, req: dict) -> None:
        await asyncio.sleep(callweb.DIAL_REQUEST_TTL)
        while req.get("dialing"):
            await asyncio.sleep(1.0)
        if req["live"] and not req.get("call_id"):
            await self._close_dial_request(req, "⚪ 邀请列表已过期，可在通话消息上重新点「邀请更多人」"
                                           if req.get("invite_call") else "⚪ 呼叫链接已过期，请重新发送 /call")

    async def _close_dial_request(self, req: dict, status: str) -> None:
        req["live"] = False
        req["status_text"] = status
        task = req.get("expire_task")
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        if self.call_web is not None:
            await self.call_web.on_dial_changed(req)
        text, _ = self._render_dial(req)
        try:
            await self.bot.edit_message(req["chat_id"], req["msg_id"], text, parse_mode="html", buttons=None,
                                        link_preview=False)
        except errors.MessageNotModifiedError:
            pass
        except Exception as e:
            log.info("Could not update call request %s: %s", req["dial_id"], e)

    def _finish_dial_request(self, req: dict) -> None:
        """The call was placed: its message and page now follow the call entry."""
        self._dial_requests.pop(req["dial_id"], None)
        task = req.get("expire_task")
        if task is not None and not task.done():
            task.cancel()

    async def _on_dial_cancel_callback(self, event, dial_id: str) -> None:
        if event.sender_id != self.owner_id:
            await event.answer("只有桥接账号本人可以操作", alert=True)
            return
        req = self._dial_requests.get(dial_id)
        if req is None or not req["live"]:
            await event.answer("呼叫已开始或已结束")
            return
        if req.get("dialing"):
            await event.answer("正在呼叫，请在通话界面取消")
            return
        await event.answer("已取消")
        await self._close_dial_request(req, "⚪ 已取消")

    # ---------------------------------------------------------------- inviting more people (call.invite)
    async def _on_call_invite_callback(self, event, token: str) -> None:
        if event.sender_id != self.owner_id:
            await event.answer("只有桥接账号本人可以操作", alert=True)
            return
        call_id = self._call_tokens.get(token)
        entry = self._active_calls.get(call_id) if call_id else None
        web = self.call_web
        if not entry or web is None or not web.can_invite(entry):
            await event.answer("群通话已结束，或当前 agent-wechat 不支持追加邀请", alert=True)
            if entry:
                await self._sync_call_msg(entry)
            return
        try:
            members = await self._group_dial_members(entry["thread_id"])
        except WeChatError as e:
            await event.answer(f"获取群成员失败：{e.code}", alert=True)
            return
        # Leave out whoever is in the call or still has a pending invitation; failed or left ones can be retried.
        state = {p.get("contactId"): p.get("membership") for p in entry.get("participants") or []}
        failed = entry.get("invite_failed") or {}
        busy = {cid for cid, m in state.items() if m in ("joined", "invited")}
        busy |= {cid for cid in entry.get("invitees") or {} if cid not in failed and state.get(cid) != "left"}
        members = [m for m in members if m["id"] not in busy]
        if not members:
            await event.answer("群里没有可以再邀请的成员了", alert=True)
            return
        for old in list(self._dial_requests.values()):
            if old["live"] and old.get("invite_call") == call_id and not old.get("dialing"):
                await self._close_dial_request(old, "⚪ 已打开新的邀请列表")
        await event.answer()
        req = {
            "dial_id": secrets.token_hex(6),
            "kind": "conference",
            "invite_call": call_id,
            "contact_id": entry["thread_id"],
            "thread_id": entry["thread_id"],
            "name": entry["caller_name"],
            "chat_id": entry["chat_id"],
            "msg_id": None,
            "status_text": "勾选要邀请的成员，再点「发送邀请」",
            "live": True,
            "dialing": False,
            "call_id": None,
            "expire_task": None,
            "members": members,
            "names": {m["id"]: m["label"] for m in members},
            "selected": [],
            "page": 0,
        }
        await self._post_dial_request(req, entry.get("msg_id"))

    async def _send_call_invite(self, event, req: dict, entry: dict) -> None:
        selected = list(req["selected"])
        if not selected:
            await event.answer("还没有选择要邀请的成员")
            return
        req["dialing"] = True  # before the first await: a double tap must not invite twice
        await event.answer("正在发送邀请…")
        try:
            await self.call_web.invite(entry, selected, req["names"])
        except CallActionError as e:
            req["dialing"] = False
            await self._report(req["chat_id"], req["msg_id"], f"邀请失败：{e}")
            return
        req["dialing"] = False
        await self._close_dial_request(req, f"✅ 已邀请 {len(selected)} 人，结果见通话消息")

    async def _close_call_invites(self, entry: dict) -> None:
        for req in list(self._dial_requests.values()):
            if req["live"] and req.get("invite_call") == entry["call_id"] and not req.get("dialing"):
                await self._close_dial_request(req, "⚪ 群通话已结束，无法再邀请")

    # ======================================================================
    # Telegram -> WeChat
    # ======================================================================
    async def on_forum_message(self, event) -> None:
        """Group messages received by the bot from owner (topics mode: forums and standalone groups;
        groups mode: dedicated official-accounts forum and /oaforum)."""
        msg = event.message
        if msg.action or msg.via_bot_id:
            return
        text = (msg.message or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd_name, _, at_bot = cmd.partition("@")
        cmd_name = cmd_name.lower() if cmd_name.startswith("/") else ""
        if at_bot and at_bot.lower() != self.bot_me.username.lower():
            cmd_name = ""
        if cmd_name == "/forum" and self.topics:
            await self._set_forum(event)
            return
        if cmd_name == "/oaforum":
            await self._set_oa_forum(event, arg.strip())
            return
        is_forum = self._is_forum_chat(event.chat_id)
        if not is_forum and not self.topics:
            return
        if self._thread_for_event(event) is not None:
            await self.on_tg_message(event)
            return
        if not cmd_name:
            return
        if not is_forum:
            # Unlinked non-forum group in topics mode: allow on-demand /link into a standalone group.
            if cmd_name == "/link":
                try:
                    chat = await event.get_chat() if hasattr(event, "get_chat") else getattr(event, "chat", None)
                except Exception:
                    chat = None
                if getattr(chat, "forum", False):
                    await self._send_group_notice(
                        event.chat_id, msg.id,
                        "这个群开启了「话题」，但还不是当前话题群。\n"
                        "• 要把它设为话题群，请先发送 <code>/forum</code>（公众号专用群发 <code>/oaforum</code>）；\n"
                        "• 要把单个微信会话绑定到独立群，请在<b>未开启话题</b>的普通群里发送 <code>/link</code>。")
                    return
                await self._handle_group_link(event, arg.strip())
            return
        # The General topic, or a topic not bound to a WeChat chat: only a few commands do anything.
        topic = _msg_topic(msg)
        if cmd_name == "/link":
            if topic is None:
                await self._send_group_notice(
                    event.chat_id, msg.id,
                    "请打开要绑定的话题，在话题里发送 <code>/link</code>；"
                    "想为微信会话新建话题，私聊我发 <code>/open</code>。")
            else:
                await self._handle_group_link(event, arg.strip())
        elif cmd_name == "/help":
            await self._send_group_notice(event.chat_id, msg.id, TOPIC_GROUP_HELP)
        elif cmd_name in ("/wx", "/unlink", "/members", "/at", "/recall", "/rename", "/title", "/avatar",
                          "/contact", "/info", "/call", "/remark", *GROUP_COMMANDS):
            await self._send_group_notice(event.chat_id, msg.id,
                                          "这个话题还没有绑定微信会话，发送 <code>/link</code> 绑定。")

    async def _set_oa_forum(self, event, arg: str = "") -> None:
        chat_id, msg_id = event.chat_id, event.message.id
        fixed = self.cfg.bridge.official_accounts_forum_chat
        if arg.lower() in ("off", "clear", "disable"):
            if fixed:
                await self._send_group_notice(
                    chat_id, msg_id,
                    f"公众号话题群已由 TG_OA_FORUM_CHAT / official_accounts_forum_chat 固定为 <code>{fixed}</code>。"
                    "要取消，请清空该配置后重启。")
                return
            self.oa_forum_chat = None
            self.store.set("oa_forum_chat", "")
            self._forum_warned_at = None
            log.info("Official-accounts forum group cleared")
            await self._send_group_notice(
                chat_id, msg_id,
                "✅ 已取消公众号/服务号专用话题群设置（原有话题绑定已保留）。")
            return
        try:
            chat = await event.get_chat()
        except Exception:
            chat = None
        if not getattr(chat, "forum", False):
            await self._send_group_notice(
                chat_id, msg_id,
                "这个群还没有开启「话题」：请在群设置中开启话题（Topics），"
                "并把我设为管理员、勾选「管理话题」权限，然后再发 <code>/oaforum</code>。")
            return
        bound = self.store.thread_for_topic_group(chat_id) if self.topics else self.store.thread_for_chat(chat_id)
        if bound is not None:
            await self._send_group_notice(
                chat_id, msg_id,
                "这个群当前已绑定到一个普通微信会话。请新建一个专用的话题群（或先发送 <code>/unlink</code> 解绑），"
                "再发送 <code>/oaforum</code>。")
            return
        if fixed and fixed != chat_id:
            await self._send_group_notice(
                chat_id, msg_id,
                f"公众号话题群已由 TG_OA_FORUM_CHAT / official_accounts_forum_chat 固定为 <code>{fixed}</code>。"
                f"要改用这个群，请改为 <code>{chat_id}</code>（或删掉它）后重启。")
            return
        old = self.oa_forum_chat
        self.oa_forum_chat = chat_id
        self.store.set("oa_forum_chat", str(chat_id))
        self._forum_warned_at = None
        log.info("Official-accounts forum group set to %s (was %s)", chat_id, old)
        text = "✅ 已把当前群设为<b>公众号/服务号专用话题群</b>。每个公众号/服务号会自动在这里生成独立话题。"
        if old and old != chat_id:
            text += f"\n\n（原公众号话题群 <code>{old}</code> 的绑定已保留，改回去即可恢复。）"
        if not self.cfg.bridge.bridge_official_accounts:
            text += ("\n\n⚠️ 当前尚未开启公众号/服务号转发：请在 <code>.env</code> 设置 "
                     "<code>BRIDGE_OFFICIAL_ACCOUNTS=true</code>（或在 <code>config/config.toml</code> 中设 "
                     "<code>bridge_official_accounts = true</code>）后重启生效。")
        await self._send_group_notice(chat_id, msg_id, text)

    async def _set_forum(self, event) -> None:
        chat_id, msg_id = event.chat_id, event.message.id
        try:
            chat = await event.get_chat()
        except Exception:
            chat = None
        if not getattr(chat, "forum", False):
            await self._send_group_notice(
                chat_id, msg_id,
                "这个群还没有开启「话题」：请在群设置中开启话题（Topics），"
                "并把我设为管理员、勾选「管理话题」权限，然后再发 <code>/forum</code>。")
            return
        if self.store.thread_for_topic_group(chat_id) is not None:
            await self._send_group_notice(
                chat_id, msg_id,
                "这个群当前已绑定到一个普通微信会话。请新建一个专用的话题群（或先发送 <code>/unlink</code> 解绑），"
                "再发送 <code>/forum</code>。")
            return
        fixed = self.cfg.bridge.forum_chat
        if fixed and fixed != chat_id:
            await self._send_group_notice(
                chat_id, msg_id,
                f"话题群已由 TG_FORUM_CHAT 固定为 <code>{fixed}</code>。要改用这个群，"
                f"请把 TG_FORUM_CHAT 改为 <code>{chat_id}</code>（或删掉它）后重启。")
            return
        old = self.forum_chat
        self.forum_chat = chat_id
        self.store.set("forum_chat", str(chat_id))
        self._forum_warned_at = None
        log.info("Forum group set to %s (was %s)", chat_id, old)
        text = "✅ 已把当前群设为桥接话题群。新的微信消息会自动在这里创建话题；私聊我发 <code>/open</code> 可主动新建话题。"
        if old and old != chat_id:
            text += f"\n\n（原话题群 <code>{old}</code> 的绑定已保留，改回去即可恢复。）"
        await self._send_group_notice(chat_id, msg_id, text)

    async def on_tg_message(self, event: events.NewMessage.Event) -> None:
        msg = event.message
        if msg.action or msg.via_bot_id:
            return
        chat_id = event.chat_id
        thread_id = self._thread_for_event(event)
        if not thread_id:
            return
        is_topic = self._is_forum_chat(chat_id)
        noun = "话题" if is_topic else "群"
        tg_side = "bot" if self.topics else "user"
        text = msg.message or ""
        if text.startswith("//"):
            return
        if not self.topics:
            self._pair_tg_user_msg(chat_id, _tg_msg_fingerprint(msg), user_id=msg.id)
        cmd, _, arg = text.strip().partition(" ")
        cmd_name, _, at_bot = cmd.partition("@")
        if not at_bot or at_bot.lower() == self.bot_me.username.lower():
            cmd_name = cmd_name.lower()
            if cmd_name == "/wx":
                title = self._link_title(thread_id)
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
            if cmd_name in ("/contact", "/info"):
                buttons = None
                if not self.wx_ready:
                    card = "🔐 微信尚未登录，登录后再查看联系人资料。"
                else:
                    try:
                        card, buttons = await self.contact_card(thread_id)
                    except WeChatError as e:
                        card = f"⚠️ 读取联系人资料失败：{_esc(contact_error(e.code))}"
                await self.bot.send_message(chat_id, card, parse_mode="html", buttons=buttons, link_preview=False,
                                            reply_to=await self._bot_reply_id(chat_id, msg.id))
                return
            if cmd_name == "/call":
                await self._start_dial(chat_id, await self._bot_reply_id(chat_id, msg.id), thread_id, arg.strip())
                return
            if cmd_name == "/remark":
                await self.cmd_remark(chat_id, await self._bot_reply_id(chat_id, msg.id), thread_id, arg)
                return
            if cmd_name == "/avatar" and is_topic:
                await self.bot.send_message(chat_id, "Telegram 话题不支持设置头像",
                                            reply_to=await self._bot_reply_id(chat_id, msg.id))
                return
            if cmd_name == "/avatar":
                why = await self.sync_avatar(chat_id, thread_id)
                await self.bot.send_message(chat_id, f"没有拿到这个会话的微信头像（{why}）" if why else "已更新群头像",
                                            reply_to=await self._bot_reply_id(chat_id, msg.id))
                return
            if cmd_name in ("/rename", "/title"):
                title, status = await self.sync_title(chat_id, thread_id)
                if status == "unchanged":
                    reply = f"{noun}标题已是最新：{_esc(title)}"
                elif status:
                    reply = f"无法同步{noun}标题（{_esc(status)}）"
                else:
                    reply = f"已更新{noun}标题：{_esc(title)}"
                await self.bot.send_message(chat_id, reply, parse_mode="html",
                                            reply_to=await self._bot_reply_id(chat_id, msg.id))
                return
            if cmd_name == "/unlink":
                self._unlink_thread(thread_id, chat_id)
                extra = "之后新消息将自动回到话题群。" if (self.topics and not is_topic) else ""
                await self.bot.send_message(
                    chat_id,
                    f"已解除当前{noun}与微信会话 <code>{_esc(thread_id)}</code> 的绑定。{extra}",
                    parse_mode="html", reply_to=await self._bot_reply_id(chat_id, msg.id))
                return
            if cmd_name == "/link":
                await self._handle_group_link(event, arg.strip())
                return
            if cmd_name == "/help":
                await self.bot.send_message(chat_id, TOPIC_GROUP_HELP if is_topic else GROUP_HELP, parse_mode="html",
                                            reply_to=await self._bot_reply_id(chat_id, msg.id))
                return
            if cmd_name in GROUP_COMMANDS:
                await self.on_group_command(chat_id, await self._bot_reply_id(chat_id, msg.id), thread_id,
                                            cmd_name, arg.strip())
                return
            if cmd_name in ("/members", "/at"):
                await self._send_members(chat_id, await self._bot_reply_id(chat_id, msg.id), thread_id, arg.strip())
                return
            if cmd_name == "/recall":
                reply_id = _reply_target(msg)
                if not reply_id:
                    await self.bot.send_message(chat_id, "请回复你要撤回的那条消息再发送 /recall",
                                                reply_to=await self._bot_reply_id(chat_id, msg.id))
                    return
                hit = self.store.wx_for_tg(chat_id, reply_id, side=tg_side)
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
        if reply_id := _reply_target(msg):
            if entry := self.store.wx_entry_for_tg(chat_id, reply_id, side=tg_side):
                r_thread, r_wx_msg, r_server = entry
                if r_thread == thread_id and r_server:
                    if not r_wx_msg:
                        r_wx_msg = await self._resolve_wx_msg_id(chat_id, reply_id, r_thread, r_wx_msg, r_server)
                    if r_wx_msg:
                        quote_ref = {"messageId": r_wx_msg, "serverId": r_server}

        dice_text = _dice_text(msg.media)
        if dice_text and not text:
            text = dice_text

        mentions, mention_spans = await self._resolve_mentions_with_spans(thread_id, text) if text else (None, [])
        wx_text = _tg_text_to_wx(text, mention_spans) if (text and self.cfg.bridge.emoji_to_wechat) else text

        label = _describe_tg_msg(msg)
        fail_prefix = f"发送{label}到微信失败：" if label.startswith("「") else f"发送[{label}]到微信失败："
        key = f"tg-{chat_id}-{msg.id}"
        self.pending_sends[thread_id] += 1
        try:
            result = None
            if msg.media and not isinstance(msg.media, (types.MessageMediaWebPage, types.MessageMediaDice,
                                                        types.MessageMediaEmpty)):
                want_sticker = bool(msg.sticker or (msg.gif and self.cfg.bridge.gif_as_sticker))
                native_stickers = await self.wx.supports_stickers() if want_sticker else False
                kind, media = await self._tg_media(msg, native_stickers=native_stickers)
                if kind is None:
                    await self._report(chat_id, await self._bot_reply_id(chat_id, msg.id), f"{fail_prefix}{media}")
                else:
                    if kind == "sticker":
                        result = await self.wx.send_sticker(thread_id, media, key)
                    else:
                        result = await self.wx.send_media(thread_id, kind, media, key)
                    self._record_send(chat_id, msg.id, thread_id, result, media=True)
                    if wx_text:  # caption goes as a separate text message
                        result = await self.wx.send_text(thread_id, wx_text, key + "-caption", mentions=mentions)
                        self._record_send(chat_id, msg.id, thread_id, result)
            elif wx_text:
                result = None
                if quote_ref:
                    try:
                        result = await self.wx.send_text(thread_id, wx_text, key, quote_ref, mentions)
                    except WeChatError as e:
                        if e.status >= 500:
                            raise
                        log.info("WeChat refused the quote (%s); sending without it", e.code)
                    if result is not None and result.get("state") == "failed":
                        log.info("Quote send failed (%s); sending without it", result.get("errorCode"))
                        result = None
                    if result is None:
                        result = await self.wx.send_text(thread_id, wx_text, key + "-plain", mentions=mentions)
                else:
                    result = await self.wx.send_text(thread_id, wx_text, key, mentions=mentions)
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

    async def _resolve_mentions_with_spans(self, thread_id: str, text: str) -> tuple[dict | None, list[tuple[int, int]]]:
        if not thread_id.endswith("@chatroom") or "@" not in text:
            return None, []
        m, spans = _match_mentions_with_spans(text, [])
        if m and not any(
            (pos := i) >= 0 and (pos == 0 or not text[pos - 1].isalnum())
            and not (text[pos + 1:].startswith("所有人") and _is_mention_boundary("所有人", text[pos + 4:]))
            and not (text[pos + 1:pos + 4].lower() == "all" and _is_mention_boundary("all", text[pos + 4:]))
            for i, ch in enumerate(text) if ch == "@"
        ):
            return m, spans
        try:
            members = await self.wx.group_members(thread_id)
        except WeChatError as e:
            log.info("Could not load group members for %s (%s); sending without mentions", thread_id, e.code)
            return m, spans
        return _match_mentions_with_spans(text, members)

    async def _resolve_mentions(self, thread_id: str, text: str) -> dict | None:
        return (await self._resolve_mentions_with_spans(thread_id, text))[0]

    async def _send_members(self, chat_id: int, reply_to: int, thread_id: str, query: str) -> None:
        if not thread_id.endswith("@chatroom"):
            await self.bot.send_message(chat_id, "只有在微信群聊里才能查看群成员", reply_to=reply_to)
            return
        try:
            members = await self.wx.group_members(thread_id, search=query or None)
        except WeChatError as e:
            await self.bot.send_message(chat_id, f"获取群成员失败：{e.code}", reply_to=reply_to)
            return
        terms = query.lower().split() if query else []
        if terms:
            # Keep client-side AND filtering across fields for older daemons that ignore `?search=`
            members = [m for m in members if all(
                any(
                    term in str(m.get(k) or "").lower()
                    for k in ("mentionName", "roomDisplayName", "displayName", "nickname", "alias", "username", "id")
                )
                for term in terms
            )]
        if not members:
            await self.bot.send_message(chat_id, "没有匹配的群成员" if query else "没有拿到群成员", reply_to=reply_to)
            return
        lines = []
        for m in members:
            label = _member_label(m)
            disp = (self._known_remark(str(m.get("id") or "")) or m.get("displayName") or "").strip()
            nick = (m.get("nickname") or "").strip()
            alias = (m.get("alias") or "").strip()
            extras: list[str] = []
            if disp and disp != label:
                extras.append(disp)
            if nick and nick not in (label, disp):
                extras.append(nick)
            if alias and alias not in (label, disp, nick) and any(t in alias.lower() for t in terms):
                extras.append(f"微信号 {alias}")
            extra = f" （{_esc(' · '.join(extras))}）" if extras else ""
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
        top = self._topic_for(chat_id, thread_id)
        for i, part in enumerate(pages):
            await self.bot.send_message(chat_id, part, parse_mode="html", reply_to=reply_to if i == 0 else top)

    def _map_tg_wx(self, chat_id: int, tg_msg: int, thread_id: str, wx_msg: str | None,
                   wx_server: str | None) -> None:
        if self.topics:
            self.store.map_message(chat_id, "bot", tg_msg, thread_id, wx_msg, wx_server)
            if chat_id < -1000000000000:
                self.store.map_message(chat_id, "user", tg_msg, thread_id, wx_msg, wx_server)
            return
        self.store.map_message(chat_id, "user", tg_msg, thread_id, wx_msg, wx_server)
        bot_msg = tg_msg if chat_id < -1000000000000 else self._user_to_bot_msg.get((chat_id, tg_msg))
        if bot_msg is not None:
            self.store.map_message(chat_id, "bot", bot_msg, thread_id, wx_msg, wx_server)

    def _record_send(self, chat_id: int, tg_msg: int, thread_id: str, result: dict, media: bool = False) -> None:
        res = result.get("result") if isinstance(result.get("result"), dict) else {}
        server = Store._norm_id(
            result.get("serverMessageId") or res.get("serverMessageId")
            or result.get("serverId") or res.get("serverId")
        )
        local = result.get("localMessageId")
        if media and res:
            ids = res.get("messageIds") or []
            local = ids[0] if ids else res.get("messageId") or local
        local = Store._norm_id(local)
        if server:
            self.store.add_sent(thread_id, server)
        # One Telegram message may become two WeChat sends (media + caption); quotes point at the first.
        if server or local:
            if self.topics:
                if self.store.wx_for_tg(chat_id, tg_msg, side="bot") is None:
                    self.store.map_message(chat_id, "bot", tg_msg, thread_id, local, server)
                if chat_id < -1000000000000 and self.store.wx_for_tg(chat_id, tg_msg, side="user") is None:
                    self.store.map_message(chat_id, "user", tg_msg, thread_id, local, server)
                return
            if self.store.wx_for_tg(chat_id, tg_msg) is None:
                self.store.map_message(chat_id, "user", tg_msg, thread_id, local, server)
            bot_msg = tg_msg if chat_id < -1000000000000 else self._user_to_bot_msg.get((chat_id, tg_msg))
            if bot_msg is not None and self.store.wx_for_tg(chat_id, bot_msg, side="bot") is None:
                self.store.map_message(chat_id, "bot", bot_msg, thread_id, local, server)

    async def _wxgf_still(self, msg: dict) -> Media | str:
        """agent-wechat's still JPEG/PNG preview of a wxgf attachment, or why it could not be fetched."""
        try:
            still = await self.wx.download_media(msg, self.max_media)
        except WeChatError as e:
            still = e.code
        if isinstance(still, str):
            log.info("Could not fetch the preview of wxgf sticker %s: %s", msg["id"], still)
        return still

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
        cache = getattr(self, "sticker_cache", None)
        wx = getattr(self, "wx", None)
        if native_stickers and (msg.sticker or (msg.gif and self.cfg.bridge.gif_as_sticker)):
            max_edge, static_max_edge = await wx.sticker_edge_limits() if wx else (
                stickers.WX_STICKER_SIZE, stickers.WX_STICKER_STATIC_SIZE,
            )
        else:
            max_edge, static_max_edge = stickers.WX_STICKER_SIZE, stickers.WX_STICKER_STATIC_SIZE
        if msg.sticker:
            if mime in ("application/octet-stream", "") or not mime.startswith(("image/", "video/", "application/x-tgsticker")):
                mime = {".webp": "image/webp", ".tgs": "application/x-tgsticker", ".webm": "video/webm"}.get(ext, mime)
            if native_stickers:
                converted = await stickers.to_wechat_sticker(
                    data, mime, cache=cache, max_edge=max_edge, static_max_edge=static_max_edge,
                )
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
        if msg.gif and native_stickers and self.cfg.bridge.gif_as_sticker:
            if mime in ("application/octet-stream", "") or not mime.startswith(("image/", "video/")):
                mime = {".gif": "image/gif", ".mp4": "video/mp4", ".webm": "video/webm"}.get(ext, mime)
            converted = await stickers.gif_to_wechat_sticker(
                data, mime, cache=cache, max_edge=max_edge, static_max_edge=static_max_edge,
            )
            if not isinstance(converted, str):
                _, out = converted
                return "sticker", Media(out, "image/gif", "sticker.gif")
            log.info("Telegram GIF -> WeChat sticker failed (%s); falling back to %s",
                     converted, "file" if mime == "image/gif" else "video")
            if mime == "image/gif":
                return "file", Media(data, mime, name if name.endswith(".gif") else "animation.gif")
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

    async def _resolve_wx_msg_id(self, chat_id: int, tg_msg: int, thread_id: str, wx_msg: str | None,
                                   wx_server: str | None) -> str | None:
        if wx_msg or not wx_server:
            return wx_msg
        if m := await self._lookup_wx_message(thread_id, None, wx_server):
            resolved = Store._norm_id(m.get("id"))
            if resolved:
                self._map_tg_wx(chat_id, tg_msg, thread_id, resolved, wx_server)
                return resolved
        return None

    async def _recall_tg_msg(self, chat_id: int, tg_msg: int, thread_id: str, wx_msg: str | None,
                               wx_server: str | None, key_prefix: str) -> tuple[bool, str]:
        wx_msg = await self._resolve_wx_msg_id(chat_id, tg_msg, thread_id, wx_msg, wx_server)
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
        row = self.store.lookup_tg_delete(getattr(event, "chat_id", None), tg_msg,
                                          side="bot" if self.topics else "user")
        if not row:
            return
        chat_id, thread_id, wx_msg, wx_server = row
        if not (wx_server and self.store.is_sent(thread_id, wx_server)):
            return
        ok, info = await self._recall_tg_msg(chat_id, tg_msg, thread_id, wx_msg, wx_server, "recall-del")
        topic = self._topic_for(chat_id, thread_id)
        if ok:
            try:
                notice = await self.bot.send_message(chat_id, "↩️ " + info, silent=True,
                                                     **({"reply_to": topic} if topic else {}))
            except Exception as e:
                log.info("Could not notify recall in chat %s: %s", chat_id, e)
                return
            if notice is not None and getattr(notice, "id", None) is not None:
                self._schedule_delete_notice(chat_id, notice.id, self._recall_notice_ttl)
        else:
            await self._report(chat_id, None, info, topic=topic)

    async def _report(self, chat_id: int, reply_to: int | None, text: str, *, topic: int | None = None) -> None:
        try:
            await self.bot.send_message(chat_id, "⚠️ " + text, reply_to=reply_to if reply_to is not None else topic)
        except Exception:
            if reply_to is not None:
                try:
                    await self.bot.send_message(chat_id, "⚠️ " + text, **({"reply_to": topic} if topic else {}))
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
            if self.store.thread_for_chat(old) or self.store.thread_for_topic_group(old):
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
            items.append({
                "id": tid, "name": name, "kind": kind, "relation": raw.get("relation"),
                "classification": raw.get("classification"),
            })

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
            # agent-wechat >= 2.7 lists only friends by default; also show people you share a group with
            # (or who removed you), friends first.
            found = await self.wx.search_contacts(q, limit=limit, relation="all")
            for c in sorted(found, key=lambda c: c.get("relation") not in (None, "friend")):
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
                if self._is_forum_chat(cid):
                    continue
                title = (getattr(d, "title", None) or getattr(d, "name", None) or str(cid)).strip()
                wx_id = self.store.thread_for_chat(cid)
                entry = (cid, title, wx_id)
                (linked if wx_id else unlinked).append(entry)
        except Exception as e:
            log.info("Could not list Telegram dialogs: %s", e)
            return []
        return (unlinked + linked)[:limit]

    def _wx_picker_buttons(self, cands: list[dict], *, action: str, extra: dict | None = None,
                           current: tuple[int, int | None] | None = None) -> list[list]:
        rows: list[list] = []
        for c in cands[:8]:
            icon = "👥" if c["kind"] == "group" else ("📰" if _is_official_account(c) else "👤")
            linked = self._dest_for_thread(c["id"])
            if current is not None and linked == current:
                suffix = "（当前）"
            elif linked is not None:
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
            if self.topics or self._is_forum_chat(chat_id):  # bot must be admin in forums; do not invite via user
                log.info("Bot cannot send to group %s: %s", chat_id, e)
                return None
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
        if sent is None and not self.topics and not self._is_forum_chat(chat_id) and hasattr(self.user, "send_message"):
            try:
                await self.user.send_message(
                    chat_id, f"⚠️ 请先把 @{self.bot_me.username} 拉进这个群，再发送 /link", reply_to=reply_to)
            except Exception as e:
                log.info("Fallback user notice in %s failed: %s", chat_id, e)

    async def on_tg_unlinked_command(self, event) -> None:
        msg = event.message
        if not msg or msg.action or msg.via_bot_id or self._is_forum_chat(event.chat_id):
            return
        text = (msg.message or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd_name, _, at_bot = cmd.partition("@")
        if at_bot and at_bot.lower() != self.bot_me.username.lower():
            return
        if cmd_name.lower() == "/link":
            await self._handle_group_link(event, arg.strip())

    async def _bot_group_read_warning(self, chat_id: int) -> str:
        """In topics mode standalone groups, warn if the bot cannot read regular messages (privacy mode on & not admin)."""
        if not self.topics or self._is_forum_chat(chat_id):
            return ""
        if getattr(self.bot_me, "bot_chat_history", False):
            return ""
        get_perms = getattr(self.bot, "get_permissions", None)
        if not callable(get_perms):
            return ""
        try:
            perm = await get_perms(chat_id, self.bot_me)
        except Exception:
            return ""
        if getattr(perm, "is_admin", False):
            return ""
        return (f"\n\n⚠️ 请把 @{_esc(self.bot_me.username)} 设为本群<b>管理员</b>"
                "（或在 @BotFather 关闭群隐私模式），否则群里发送的普通消息不会转发到微信。")

    async def _standalone_link_notes(self, chat_id: int, moved_from_topic: bool, *, check_perm: bool = True) -> str:
        notes = ""
        if moved_from_topic:
            notes += "\n（已解除原话题绑定并保留历史；之后在本群发 <code>/unlink</code> 可回到话题群。）"
        if check_perm:
            notes += await self._bot_group_read_warning(chat_id)
        return notes

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
        is_forum = self._is_forum_chat(chat_id)
        topic = _msg_topic(event.message) if is_forum else None
        noun = "话题" if is_forum else "群"
        cands = await self._wx_search_candidates(arg, limit=10)
        if arg and " " not in arg:
            exact = next((c for c in cands if c["id"] == arg), None)
            if exact:
                prev = self._dest_for_thread(exact["id"])
                moved_from_topic = self.topics and not is_forum and prev is not None and prev[1] is not None
                wx_title, _ = await self._complete_link(exact["id"], chat_id, exact["name"], notify_group=False,
                                                        tg_topic=topic)
                notes = await self._standalone_link_notes(chat_id, moved_from_topic) if (self.topics and not is_forum) else ""
                await self._send_group_notice(
                    chat_id, msg_id,
                    f"✅ 已将当前{noun}绑定到微信会话：<b>{_esc(wx_title)}</b>\n<code>{_esc(exact['id'])}</code>{notes}")
                return
        if not cands:
            hint = (f"没有找到匹配「{_esc(arg)}」的微信联系人或群。" if arg
                    else "暂时没有获取到最近的微信会话，请发 <code>/link 关键词</code> 搜索。")
            await self._send_group_notice(chat_id, msg_id, hint)
            return
        buttons = self._wx_picker_buttons(
            cands, action="link_bind",
            extra={"tg_chat": chat_id, "tg_title": tg_title, "in_group": True, "tg_topic": topic},
            current=(chat_id, topic))
        header = f"<b>搜索「{_esc(arg)}」的结果</b>" if arg else f"<b>选择要绑定到此{noun}的微信会话</b>"
        tip = "点下方按钮即可绑定（也可发 <code>/link 关键词</code> 缩小范围）："
        await self._send_group_notice(chat_id, msg_id, f"{header}\n{tip}", buttons=buttons)

    async def _complete_link(self, wx_id: str, tg_chat: int, wx_name: str = "",
                             *, notify_group: bool = False, tg_topic: int | None = None) -> tuple[str, bool]:
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
        if tg_topic and self._is_forum_chat(tg_chat):
            self.store.link_topic(wx_id, tg_chat, tg_topic, title)
        elif self.topics:
            self.store.link_topic_group(wx_id, tg_chat, title)
        else:
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
            current=(tg_chat, None))
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
        if str(action).startswith(("fr_", "add_", "ct_")):
            await self.on_contact_ui(event, payload)
            return
        if str(action).startswith("gp_"):
            await self.on_group_ui(event, payload)
            return
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
            existed = self._dest_for_thread(wx_id) is not None
            try:
                thread = await self.wx.thread(wx_id)
            except WeChatError:
                thread = {"id": wx_id, "kind": payload.get("kind", "direct"),
                          "displayName": payload.get("name") or wx_id,
                          "classification": payload.get("classification")}
            noun = self._thread_noun(wx_id, thread)
            if await self.ensure_chat(thread) is None:
                await self._edit_ui_msg(event, f"⚠️ 无法建立 Telegram {noun}，请查看我刚发的提示。")
                await event.answer()
                return
            title = self._link_title(wx_id) or self._title(thread)
            msg = (f"ℹ️ <b>{_esc(title)}</b> 已经有对应的 Telegram {noun}了" if existed
                   else f"✅ 已建立 Telegram {noun}：<b>{_esc(title)}</b>")
            await self._edit_ui_msg(event, msg)
            await event.answer(f"已有{noun}" if existed else f"已建{noun}")
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
                current=(tg_chat, None))
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
            tg_topic = payload.get("tg_topic")
            if tg_topic is None and self._is_forum_chat(tg_chat):
                await self._edit_ui_msg(event, "⚠️ 这是当前话题群，请在其中的某个话题里发送 <code>/link</code>。")
                await event.answer()
                return
            prev = self._dest_for_thread(wx_id)
            moved_from_topic = self.topics and not tg_topic and prev is not None and prev[1] is not None
            wx_title, bot_ok = await self._complete_link(wx_id, tg_chat, wx_name, notify_group=not in_group,
                                                         tg_topic=tg_topic)
            notes = await self._standalone_link_notes(
                tg_chat, moved_from_topic, check_perm=bot_ok) if (self.topics and not tg_topic) else ""
            if in_group:
                text = (f"✅ 已将当前{'话题' if tg_topic else '群'}绑定到微信会话：<b>{_esc(wx_title)}</b>\n"
                        f"<code>{_esc(wx_id)}</code>{notes}")
            else:
                text = f"✅ 已绑定：<b>{_esc(tg_title)}</b> ↔ <b>{_esc(wx_title)}</b>\n<code>{_esc(wx_id)}</code>{notes}"
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
        noun = "话题" if self.topics else "群"
        try:
            if cmd in ("/start", "/help"):
                await event.reply(TOPIC_HELP if self.topics else HELP, parse_mode="html")
            elif cmd == "/status":
                up = int(time.time() - self.started)
                state = self._wx_state_text()
                mode = ""
                if self.topics:
                    forum = f"<code>{self.forum_chat}</code>" if self.forum_chat else "未设置（在群里发 /forum）"
                    mode = f"\n模式：话题群 {forum}"
                    standalone = sum(1 for _, c, _, _ in self.store.list_topic_groups() if not self._is_forum_chat(c))
                    if standalone:
                        mode += f"（另含 {standalone} 个独立群）"
                if self.oa_forum_chat:
                    oa_note = "" if self.cfg.bridge.bridge_official_accounts else "（未开启 bridge_official_accounts）"
                    mode += f"\n公众号话题群：<code>{self.oa_forum_chat}</code>{oa_note}"
                mode += self._wx_auto_relogin_status_line()
                if self.wx_ready:
                    try:
                        page = await self.wx.contact_requests(direction="incoming", actionable=True, limit=1)
                        if pending := int(page.get("pendingIncomingCount") or 0):
                            mode += f"\n好友请求：{pending} 条待处理（发 /requests 查看）"
                    except (WeChatError, ValueError, TypeError):
                        pass  # older daemon or not readable right now
                await event.reply(f"运行 {up // 3600}h{up % 3600 // 60}m，已转发 {self.forwarded} 条，"
                                  f"已连接 {len(self._link_rows())} 个会话。\n"
                                  f"微信：{state}，账号 <code>{_esc(self.wx.account or '-')}</code>{mode}",
                                  parse_mode="html")
            elif cmd in ("/chats", "/list"):
                rows = self._link_rows()
                kw = arg.lower()
                if kw:
                    rows = [r for r in rows if kw in r[0].lower() or kw in r[1].lower() or kw in r[2]]
                if not rows:
                    return await event.reply("没有找到匹配的已连接会话" if kw else "还没有任何已连接的会话")
                lines = [
                    f"• {_esc(title or wx_id)} — <code>{_esc(wx_id)}</code> ↔ <code>{_esc(where)}</code>"
                    for wx_id, title, where in rows[:40]
                ]
                suffix = f"\n\n共 {len(rows)} 个会话（仅显示前 40 个）" if len(rows) > 40 else f"\n\n共 {len(rows)} 个会话"
                await event.reply("\n".join(lines) + suffix, parse_mode="html")
            elif cmd == "/link" and self.topics:
                parts = arg.split()
                if len(parts) == 2:
                    try:
                        tg_chat = int(parts[1])
                    except ValueError:
                        tg_chat = None
                    if tg_chat is not None:
                        wx_id = parts[0]
                        if tg_chat >= 0:
                            return await event.reply(
                                "TG 群 ID 应为负数（普通群如 <code>-123456</code>，超级群如 <code>-1001234567890</code>）。",
                                parse_mode="html")
                        if self._is_forum_chat(tg_chat):
                            return await event.reply(
                                f"<code>{tg_chat}</code> 是当前话题群，不能直接绑定整个群；"
                                f"请打开其中的某个话题，在话题里发送 <code>/link {_esc(wx_id)}</code>。",
                                parse_mode="html")
                        get_entity = getattr(self.bot, "get_entity", None)
                        if callable(get_entity):
                            try:
                                ent = await get_entity(tg_chat)
                            except Exception:
                                ent = None
                            if getattr(ent, "forum", False):
                                return await event.reply(
                                    f"<code>{tg_chat}</code> 开启了「话题」，不能绑定为独立普通群；"
                                    "请改用未开启话题的普通群，或先在该群发送 <code>/forum</code>。",
                                    parse_mode="html")
                        prev = self._dest_for_thread(wx_id)
                        moved_from_topic = prev is not None and prev[1] is not None
                        wx_title, bot_ok = await self._complete_link(wx_id, tg_chat, notify_group=True)
                        notes = await self._standalone_link_notes(tg_chat, moved_from_topic, check_perm=bot_ok)
                        text = f"✅ 已绑定：<b>{_esc(wx_title)}</b> (<code>{_esc(wx_id)}</code> ↔ <code>{tg_chat}</code>){notes}"
                        if not bot_ok:
                            text += (f"\n\n⚠️ 机器人暂时无法在该群发消息，请先把 @{_esc(self.bot_me.username)} "
                                     "拉进群并设为管理员。")
                        await event.reply(text, parse_mode="html")
                        return
                await event.reply(
                    "<b>话题模式下的绑定方式</b>\n\n"
                    f"• <b>绑定到独立新群</b>：新建一个<b>未开启话题</b>的 Telegram 群，把 @{_esc(self.bot_me.username)} "
                    "拉进群并设为管理员，在群里发送 <code>/link</code> [关键词]（也可在这里直接发 <code>/link 微信ID TG群ID</code>）。\n"
                    "• <b>绑定到话题群的话题</b>：打开要绑定的话题，在话题里发送 <code>/link</code> [关键词]。\n"
                    "• <b>新建话题</b>：直接发 <code>/open</code> [关键词]。",
                    parse_mode="html")
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
                        "🔐 微信尚未登录，请先在手机微信上确认登录或扫码登录，也可以点击下方按钮重新发起登录。",
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
            elif cmd in ("/find", "/open", "/requests", "/add", "/newgroup", "/syncnames") and not self.wx_ready:
                await event.reply(
                    "🔐 微信尚未登录，请先在手机微信上确认登录或扫码登录，也可以点击下方按钮重新发起登录。",
                    buttons=self._wx_login_buttons())
            elif cmd == "/requests":
                await self.cmd_requests(event)
            elif cmd == "/add":
                await self.cmd_add(event, arg)
            elif cmd == "/newgroup":
                await self.cmd_new_group(event, arg)
            elif cmd == "/syncnames":
                await self.cmd_sync_names(event)
            elif cmd == "/find":
                cands = await self._wx_search_candidates(arg, limit=15 if arg else 10)
                if not cands:
                    return await event.reply("没有找到匹配的微信联系人或群" if arg else "暂时没有获取到最近的微信会话")
                lines = []
                for c in cands:
                    icon = "👥" if c["kind"] == "group" else ("📰" if _is_official_account(c) else "👤")
                    item_noun = self._thread_noun(c["id"], c)
                    has_chat = self._dest_for_thread(c["id"]) is not None
                    tag = (f"（已绑{item_noun}）" if (has_chat and self.topics and item_noun == "群")
                           else f"（已建{item_noun}）" if has_chat else "")
                    if c.get("relation") == "non_contact":
                        tag += "（非好友）"
                    lines.append(f"{icon} <b>{_esc(c['name'])}</b>{tag} — <code>{_esc(c['id'])}</code>")
                buttons = []
                for c in cands[:8]:
                    item_noun = self._thread_noun(c["id"], c)
                    has_chat = self._dest_for_thread(c["id"]) is not None
                    lbl = f"{'💬 打开' if has_chat else f'➕ 建{item_noun}'}：{c['name'][:22]}"
                    tok = self._make_ui_token({
                        "action": "open_wx", "wx_id": c["id"], "name": c["name"], "kind": c["kind"],
                        "classification": c.get("classification"),
                    })
                    buttons.append([Button.inline(lbl, tok)])
                header = (f"<b>搜索「{_esc(arg)}」的结果</b>（点按钮可直接建{noun}）：\n\n" if arg
                          else f"<b>最近的微信会话</b>（点按钮可直接建{noun}）：\n\n")
                await event.reply(header + "\n".join(lines), parse_mode="html", buttons=buttons)
            elif cmd == "/open":
                if not arg:
                    cands = await self._wx_search_candidates("", limit=10)
                    if not cands:
                        return await event.reply("暂时没有获取到最近的微信会话，请直接发 <code>/open 微信名称</code> 搜索。",
                                                 parse_mode="html")
                    buttons = [
                        [Button.inline(
                            f"{'💬' if self._dest_for_thread(c['id']) is not None else '➕'} {c['name'][:24]}",
                            self._make_ui_token({
                                "action": "open_wx", "wx_id": c["id"], "name": c["name"], "kind": c["kind"],
                                "classification": c.get("classification"),
                            })
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
                            thread = {
                                "id": chosen["id"], "kind": chosen["kind"], "displayName": chosen["name"],
                                "classification": chosen.get("classification"),
                            }
                    else:
                        buttons = [
                            [Button.inline(
                                f"{'💬' if self._dest_for_thread(c['id']) is not None else '➕'} {c['name'][:24]}",
                                self._make_ui_token({
                                    "action": "open_wx", "wx_id": c["id"], "name": c["name"], "kind": c["kind"],
                                    "classification": c.get("classification"),
                                })
                            )]
                            for c in cands[:8]
                        ]
                        buttons.append([Button.inline("❌ 取消", self._make_ui_token({"action": "cancel"}))])
                        return await event.reply(
                            f"找到多个匹配「<b>{_esc(arg)}</b>」的会话，请点选：",
                            parse_mode="html", buttons=buttons)
                target_noun = self._thread_noun(thread["id"], thread)
                existed = self._dest_for_thread(thread["id"]) is not None
                if await self.ensure_chat(thread) is None:
                    return await event.reply(f"⚠️ 无法建立 Telegram {target_noun}，请查看我刚发的提示。")
                title = self._link_title(thread["id"]) or self._title(thread)
                await event.reply(
                    f"这个会话已经有{target_noun}了：<b>{_esc(title)}</b>" if existed
                    else f"已建{target_noun}：<b>{_esc(title)}</b>",
                    parse_mode="html")
        except WeChatError as e:
            await event.reply(f"出错：{e.code}")
