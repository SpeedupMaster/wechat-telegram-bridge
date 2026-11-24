"""Message formatting, help texts, bot command menus, media helpers, and @mention matching."""
from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import re
import time

from telethon import Button, errors, types

from . import cards, stickers, tghtml
from .emojis import replace_wx_emojis, unicode_to_wx_emojis
from .wechat import (
    APP,
    APP_BIG_XML,
    APP_CHANNELS,
    APP_FILE,
    APP_FORWARDED,
    APP_QUOTE,
    IMAGE,
    LOCATION,
    STICKER,
    SYSTEM,
    TEXT,
    VIDEO,
    VOICE,
    Media,
    is_wecom_group_id,
)

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


async def wav_to_ogg(data: bytes = b"", *, path: str | None = None, timeout: float = 60.0) -> bytes | None:
    """Convert WeChat voice (WAV) to OGG/Opus so Telegram shows it as a voice note. Needs ffmpeg."""
    ffmpeg = stickers.find_ffmpeg()
    if not ffmpeg or (path is None and not data):
        return None
    inp = path if path is not None else "pipe:0"
    stdin_arg = asyncio.subprocess.DEVNULL if path is not None else asyncio.subprocess.PIPE
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", inp, "-c:a", "libopus", "-b:a", "32k",
        "-f", "ogg", "pipe:1", stdin=stdin_arg, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(None if path is not None else data),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.communicate()
        except Exception:
            pass
        log.warning("wav_to_ogg timed out after %.1fs", timeout)
        return None
    except BaseException:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
        raise
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
    "• <code>/call</code> [成员] — 给好友打微信语音/视频电话；在微信群里勾选成员发起群通话（需配置通话页面）\n"
    "• <code>/vcall</code> [成员] — 直接发起微信视频通话或群视频通话\n"
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
    "• <code>/call</code> [成员] — 给好友打微信语音/视频电话；在微信群里勾选成员发起群通话\n"
    "• <code>/vcall</code> [成员] — 直接发起微信视频通话或群视频通话\n"
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
    "• <code>/call</code> [成员] — 给好友打微信语音/视频电话；在微信群里勾选成员发起群通话（需配置通话页面）\n"
    "• <code>/vcall</code> [成员] — 直接发起微信视频通话或群视频通话\n"
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
    "• <code>/call</code> [成员] — 给好友打微信语音/视频电话；在微信群里勾选成员发起群通话\n"
    "• <code>/vcall</code> [成员] — 直接发起微信视频通话或群视频通话\n"
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
    ("call", "给好友打微信电话或发起群通话"),
    ("vcall", "给好友打微信视频电话或发起群视频通话"),
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
    ("link", "绑定微信会话与独立TG群"),
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
    ("call", "给好友打微信电话或发起群通话"),
    ("vcall", "给好友打微信视频电话或发起群视频通话"),
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
    ("avatar", "同步微信头像到独立群头像"),
    ("rename", "同步最新微信名称到群/话题标题"),
    ("link", "绑定或切换当前群/话题对应的微信会话"),
    ("unlink", "解除当前群/话题与微信的绑定"),
    ("forum", "把当前群设为桥接话题群"),
    ("oaforum", "把当前群设为公众号/服务号专用话题群"),
    ("help", "查看群内命令帮助"),
]


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


def _link_buttons(link: dict | None, app_type: int | None, open_url: str | None = None,
                  *, original: bool = False) -> list | None:
    """CTA button for a link card; open_url (the reader Mini App link) replaces the raw URL.

    original=True adds the raw URL as a second button, for pages the Mini App merely opens in place (it has no
    address bar or "open in browser"), so a page that needs a real browser is still one tap away."""
    if url := _link_url(link):
        src = cards.identify_link_source(link, app_type)
        row = [Button.url(src.button_label, open_url or url)]
        if open_url and original:
            row.append(Button.url("🔗 原链接", url))
        return [row]
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
    "unsupported_media_variant": "暂不支持该媒体格式",
    "tp_media_source_not_allowed": "企业微信媒体地址不在允许的服务器上",
    "tp_media_dns_failed": "企业微信媒体服务器解析失败",
    "tp_media_dns_not_public": "企业微信媒体服务器解析失败",
    "tp_media_size_limit": "企业微信媒体文件超过大小限制",
    "tp_media_encoding_refused": "企业微信媒体响应编码不被支持",
    "tp_media_fetch_failed": "企业微信媒体下载失败",
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
    40: "聊天记录",
    51: "视频号",
    57: "引用消息",
    63: "视频号直播",
    87: "群公告",
    2000: "转账",
    2001: "红包",
}


# agent-wechat < 3.0 only knows ordinary WeChat CDN fields; images and files from WeCom (`@openim` / `@im.chatroom`)
# use encrypted HTTP (`tp*`) downloads added in agent-wechat 3.0.0-beta.1 (thisnick/agent-wechat#660).
UNSUPPORTED_TRANSPORT_CODES = {"unsupported_media", "unsupported_media_transport", "unsupported_attachment_transport"}
OPENIM_MEDIA_NOTE = "企业微信发来的图片/文件需 agent-wechat 3.0 及以上版本才能下载，请升级或先在手机微信查看"


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
    if reason in UNSUPPORTED_TRANSPORT_CODES and (
        _is_openim(msg.get("senderId")) or _is_openim(msg.get("threadId")) or is_wecom_group_id(msg.get("threadId"))
    ):
        why = OPENIM_MEDIA_NOTE
    else:
        why = MEDIA_REASONS.get(reason, reason)
    return f"[{_esc(label)}，无法获取：{_esc(why)}]"


_CHAT_HISTORY_UNAVAILABLE = "[Chat History unavailable]"
_CHAT_HISTORY_UNAVAILABLE_ZH = "<i>（合并转发聊天记录暂不可用，加载成功后将自动更新，也可在手机微信查看）</i>"


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


def _image_from_file(data: bytes, mime: str, name: str, ext: str, *, path: str | None = None) -> Media | None:
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
        byte_len = os.path.getsize(path) if path is not None else len(data)
        src = path if path is not None else io.BytesIO(data)
        with Image.open(src) as im:
            w, h = im.size
            fmt = (im.format or "").upper()
            if fmt == "GIF" and getattr(im, "n_frames", 1) > 1:
                return None  # keep animated GIF as a file so WeChat doesn't flatten it to a static image
            if fmt == "JPEG":
                if byte_len <= WX_IMAGE_MAX_BYTES and max(w, h) <= WX_JPEG_MAX_EDGE and w * h <= WX_JPEG_MAX_PIXELS:
                    out_name = name if file_ext in (".jpg", ".jpeg") else (
                        (name.rsplit(".", 1)[0] if "." in name else name) + ".jpg")
                    return Media(data, "image/jpeg", out_name, path=path)
                return None
            direct_mime = {"PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}.get(fmt)
            if direct_mime and byte_len <= WX_IMAGE_MAX_BYTES and max(w, h) <= WX_OTHER_IMAGE_MAX_EDGE and w * h <= WX_OTHER_IMAGE_MAX_PIXELS:
                return Media(data, direct_mime, name, path=path)
            if max(w, h) <= WX_JPEG_MAX_EDGE and w * h <= WX_JPEG_MAX_PIXELS:
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


def _chunks(text: str, size: int = TG_TEXT_LIMIT, *, rest_size: int | None = None) -> list[str]:
    return tghtml.split_html(text, size, rest_limit=rest_size)


# Group-call member picker: members per page (two per row) and names listed in the message text.
DIAL_PICK_PAGE = 10
DIAL_PICK_SHOWN = 12
CALL_INVITEES_SHOWN = 40  # names listed on a group call card (the list folds away in Telegram)


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


def format_wx_body(msg: dict) -> str:
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
    if t == APP and msg.get("appMessageType") == APP_FORWARDED:
        return "📜 聊天记录\n" + _fmt_wx_text(body)
    if t == APP and msg.get("appMessageType") == APP_BIG_XML:
        raw = body.strip()
        if not raw or raw == _CHAT_HISTORY_UNAVAILABLE:
            return f"📜 聊天记录\n{_CHAT_HISTORY_UNAVAILABLE_ZH}"
        if raw.endswith(_CHAT_HISTORY_UNAVAILABLE):
            head = raw[:-len(_CHAT_HISTORY_UNAVAILABLE)].rstrip()
            if head:
                return f"📜 聊天记录\n{_fmt_wx_text(head)}\n{_CHAT_HISTORY_UNAVAILABLE_ZH}"
            return f"📜 聊天记录\n{_CHAT_HISTORY_UNAVAILABLE_ZH}"
        return "📜 聊天记录\n" + _fmt_wx_text(raw)
    if t == STICKER:
        return "[表情]"
    return _fmt_wx_text(body) or _unsupported_label(t, msg.get("appMessageType"))


def quote_inline_text(q: dict) -> str:
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
        return {
            APP_FILE: "[文件]",
            APP_FORWARDED: "[聊天记录]",
            APP_BIG_XML: "[聊天记录]",
            APP_CHANNELS: "[视频号]",
            APP_QUOTE: "[引用消息]",
        }.get(qsub, "")
    return {IMAGE: "[图片]", VOICE: "[语音]", VIDEO: "[视频]", STICKER: "[表情]", LOCATION: "[位置]"}.get(qt, "")


def quote_msg_summary(orig: dict) -> str:
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
        if sub in (APP_FORWARDED, APP_BIG_XML):
            clean = body.replace(_CHAT_HISTORY_UNAVAILABLE, "").strip()
            return f"📜 聊天记录 {clean}".strip() if clean else "[聊天记录]"
        if link:
            return (link.get("title") or link.get("description") or link.get("url") or "").strip()
    return body
