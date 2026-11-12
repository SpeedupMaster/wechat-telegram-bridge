"""WeChat (agent-wechat) <-> Telegram bridge: one Telegram group per WeChat chat."""
from __future__ import annotations

import asyncio
import html
import io
import logging
import shutil
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

import httpx
from telethon import TelegramClient, errors, events, functions, types, utils

from .config import Config
from . import stickers
from .login import LoginMixin
from .store import Store
from .wechat import (APP, APP_FILE, APP_QUOTE, IMAGE, LOCATION, SEND_OK, STICKER, SYSTEM, TEXT, VIDEO, VOICE,
                     CursorExpired, Media, WeChatClient, WeChatError)

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
    "<b>微信 ↔ Telegram 桥</b>\n\n"
    "每个微信会话会自动对应一个 Telegram 群。在群里直接发消息就会发到对应的微信会话；"
    "回复某条消息会变成微信里的引用回复。\n\n"
    "私聊我可用的命令：\n"
    "/find 关键词 — 搜索微信联系人和群\n"
    "/open 微信ID — 为这个联系人/群建立 Telegram 群（主动发起聊天）\n"
    "/status — 查看桥的运行状态\n"
    "/wxlogin — 重新扫码登录微信（掉线或被踢下线时用）\n\n"
    "在桥接群里可用：\n"
    "/wx — 查看这个群对应的微信会话\n"
    "/avatar — 重新获取微信头像作为群头像\n"
    "以 // 开头的消息只留在 Telegram，不会发到微信。"
)


def _proxy(url: str):
    if not url:
        return None
    u = urlparse(url)
    return {"proxy_type": u.scheme, "addr": u.hostname, "port": u.port,
            "username": u.username, "password": u.password, "rdns": True}


def _esc(s: str | None) -> str:
    return html.escape(s or "")


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


def _avatar_photo(data: bytes, size: int = 512) -> bytes:
    """WeChat hands out small avatars (~132 px) that Telegram rejects as a chat photo
    (PhotoCropSizeSmallError), so centre-crop to a square and scale up to size x size JPEG."""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return data
    w, h = im.size
    side = min(w, h)
    im = im.crop(((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side))
    if side < size:
        im = im.resize((size, size), Image.LANCZOS)
    out = io.BytesIO()
    stickers.on_white(im).save(out, "JPEG", quality=92)
    return out.getvalue()


def _fingerprint(m) -> tuple:
    """What the bot's copy and your account's copy of one message have in common."""
    media = getattr(m, "media", None)
    if isinstance(media, types.MessageMediaWebPage):
        media = None
    return (getattr(m, "message", None) or "", type(media).__name__ if media else "")


def _chunks(text: str, size: int = TG_TEXT_LIMIT) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


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

    # ======================================================================
    # startup
    # ======================================================================
    async def run(self) -> None:
        await self.bot.start(bot_token=self.cfg.telegram.bot_token)
        self.bot_me = await self.bot.get_me()
        if not self.bot_me.username:
            raise SystemExit("The bot has no username; check TG_BOT_TOKEN")
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
        self.user.add_event_handler(self.on_raw_update, events.Raw(types.UpdateNewMessage))
        self.user.add_event_handler(self.on_user_sees_bot_message, events.NewMessage(
            incoming=True, func=lambda e: e.sender_id == self.bot_me.id
            and self.store.thread_for_chat(e.chat_id) is not None))
        await asyncio.gather(self.wechat_side(), self.user.run_until_disconnected())

    async def wechat_side(self) -> None:
        await self.ensure_wechat()
        log.info("WeChat is logged in; bridging messages")
        await asyncio.gather(self.event_loop(), self.wechat_monitor())

    async def on_bot_private(self, event) -> None:
        if not self.tg_ready.is_set() or self.owner_id is None:
            if not self.tg_ready.is_set():
                await self.on_login_message(event)
            return
        if event.sender_id == self.owner_id and (event.raw_text or "").startswith("/"):
            await self.on_bot_command(event)

    async def on_bot_callback(self, event) -> None:
        await self.on_login_callback(event)

    async def close(self) -> None:
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
            return _esc(body)
        if t == SYSTEM:
            return f"<i>{_esc(body)}</i>"
        if t == LOCATION:
            loc = msg.get("location") or {}
            return "📍 " + _esc(" ".join(x for x in (loc.get("name"), loc.get("address")) if x) or body)
        if t == APP and (link := msg.get("link")):
            parts = [f"🔗 <b>{_esc(link.get('title'))}</b>" if link.get("title") else "🔗",
                     _esc(link.get("description")), _esc(link.get("url"))]
            if link.get("sourceName"):
                parts.append(f"<i>来自 {_esc(link['sourceName'])}</i>")
            return "\n".join(p for p in parts if p)
        if t == APP and msg.get("appMessageType") == 19:
            return "📜 聊天记录\n" + _esc(body)
        if t == STICKER:
            return "[表情]"
        return _esc(body) or f"[暂不支持的微信消息类型 {t}/{msg.get('appMessageType')}]"

    def _quote_prefix(self, msg: dict) -> str:
        q = msg.get("quote") or {}
        text = q.get("embeddedText") or ("[图片]" if q.get("embeddedImage") else "[表情]" if q.get("embeddedSticker")
                                         else (q.get("embeddedLink") or {}).get("title") or "…")
        who = q.get("senderName") or ""
        return f"<blockquote>{_esc(who + '：' if who else '')}{_esc(text[:200])}</blockquote>"

    # --- sending to Telegram ----------------------------------------------
    async def forward_to_tg(self, chat: int, thread: dict, msg: dict) -> None:
        outgoing = msg.get("direction") == "outgoing"
        header = ""
        if outgoing:
            header = "🙋 <b>我</b>（手机端）：\n"
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

    async def _send_tg(self, chat, thread, header, prefix, msg, media, reply_to, silent) -> list:
        async def go(chat_id: int) -> list:
            out = []
            t = msg.get("type")
            if isinstance(media, Media):
                f = io.BytesIO(media.data)
                f.name = media.filename
                caption = header + prefix + (self._body(msg) if t == APP and msg.get("appMessageType") != APP_FILE
                                             else "")
                is_voice = t == VOICE and media.mime in ("audio/ogg", "audio/opus")
                kwargs = dict(caption=caption[:TG_CAPTION_LIMIT] or None, parse_mode="html", reply_to=reply_to,
                              silent=silent, voice_note=is_voice, supports_streaming=t == VIDEO,
                              force_document=t == APP or (t == VOICE and not media.mime.startswith("audio/")))
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
            if isinstance(media, str):
                label = {IMAGE: "图片", VIDEO: "视频", VOICE: "语音", STICKER: "表情"}.get(t, "文件")
                text = header + prefix + f"[{label}，无法获取：{_esc(MEDIA_REASONS.get(media, media))}]"
            else:
                text = header + prefix + self._body(msg)
            for i, part in enumerate(_chunks(text)):
                out.append(await self.bot.send_message(chat_id, part, parse_mode="html", silent=silent,
                                                       reply_to=reply_to if i == 0 else None,
                                                       link_preview=False))
            if t == LOCATION and (loc := msg.get("location")):
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
        hit = self.store.tg_for_wx(tid, data.get("messageId"), data.get("serverId"))
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
        if text.strip() in ("/wx", f"/wx@{self.bot_me.username}"):
            await self.bot.send_message(chat_id, f"对应微信会话：<code>{_esc(thread_id)}</code>",
                                        parse_mode="html", reply_to=msg.id)
            return
        if text.strip() in ("/avatar", f"/avatar@{self.bot_me.username}"):
            why = await self.sync_avatar(chat_id, thread_id)
            await self.bot.send_message(chat_id, f"没有拿到这个会话的微信头像（{why}）" if why else "已更新群头像",
                                        reply_to=msg.id)
            return
        if text.startswith("/") and not msg.media:
            return  # other commands are not forwarded

        quote_ref = None
        if msg.reply_to and msg.reply_to.reply_to_msg_id:
            hit = self.store.wx_for_tg(chat_id, msg.reply_to.reply_to_msg_id)
            if hit and hit[0] and hit[1]:
                quote_ref = {"messageId": hit[0], "serverId": hit[1]}

        key = f"tg-{chat_id}-{msg.id}"
        self.pending_sends[thread_id] += 1
        try:
            result = None
            if msg.media and not isinstance(msg.media, (types.MessageMediaWebPage,)):
                kind, media = await self._tg_media(msg)
                if kind is None:
                    await self._report(chat_id, msg.id, media)
                else:
                    result = await self.wx.send_media(thread_id, kind, media, key)
                    self._record_send(chat_id, msg.id, thread_id, result, media=True)
                    if text:  # caption goes as a separate text message
                        result = await self.wx.send_text(thread_id, text, key + "-caption")
                        self._record_send(chat_id, msg.id, thread_id, result)
            elif text:
                result = None
                if quote_ref:
                    try:
                        result = await self.wx.send_text(thread_id, text, key, quote_ref)
                    except WeChatError as e:
                        if e.status >= 500:
                            raise
                        log.info("WeChat refused the quote (%s); sending without it", e.code)
                    if result is not None and result.get("state") == "failed":
                        log.info("Quote send failed (%s); sending without it", result.get("errorCode"))
                        result = None
                    if result is None:
                        result = await self.wx.send_text(thread_id, text, key + "-plain")
                else:
                    result = await self.wx.send_text(thread_id, text, key)
                self._record_send(chat_id, msg.id, thread_id, result)
            if result is not None:
                state = result.get("state")
                if state not in SEND_OK:
                    reason = result.get("errorCode") or result.get("reason") or state
                    hint = "（结果未知，请在手机上确认，不要重复发送）" if state == "needs_review" else ""
                    await self._report(chat_id, msg.id, f"发送到微信失败：{reason}{hint}")
                elif self.cfg.bridge.mark_read_on_reply:
                    try:
                        await self.wx.mark_read(thread_id)
                    except Exception:
                        pass
        except WeChatError as e:
            await self._report(chat_id, msg.id, f"发送到微信失败：{e.code}")
        except Exception as e:
            log.exception("Telegram -> WeChat failed")
            await self._report(chat_id, msg.id, f"发送到微信失败：{type(e).__name__}: {e}")
        finally:
            self.pending_sends[thread_id] -= 1

    def _record_send(self, chat_id: int, tg_msg: int, thread_id: str, result: dict, media: bool = False) -> None:
        server = result.get("serverMessageId") if media else result.get("serverId")
        local = result.get("localMessageId")
        if media and isinstance(result.get("result"), dict):
            ids = result["result"].get("messageIds") or []
            local = ids[0] if ids else None
        if server:
            self.store.add_sent(thread_id, server)
        # One Telegram message may become two WeChat sends (media + caption); quotes point at the first.
        if (server or local) and self.store.wx_for_tg(chat_id, tg_msg) is None:
            self.store.map_message(chat_id, "user", tg_msg, thread_id, local, server)

    async def _tg_media(self, msg) -> tuple[str | None, Media | str]:
        f = msg.file
        size = f.size if f else 0
        if size and size > self.max_media:
            return None, f"文件太大（{size // 1048576} MB），超过 max_media_mb 限制"
        data = await msg.download_media(file=bytes)
        if not data:
            return None, "无法下载这条 Telegram 媒体"
        mime = (f.mime_type if f and f.mime_type else "application/octet-stream").lower()
        name = (f.name if f and f.name else None) or f"telegram{f.ext if f and f.ext else ''}"
        if msg.photo:
            return "image", Media(data, "image/jpeg", "photo.jpg")
        if msg.sticker:
            # WeChat stickers cannot be sent through agent-wechat; send a picture or a short video instead
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
        # Anything sent as a file in Telegram (including pictures) goes to WeChat as a file: agent-wechat
        # squeezes every *image* it sends to at most 1280 px / ~64 KB, while files keep their exact bytes.
        return "file", Media(data, mime, name)

    async def _report(self, chat_id: int, reply_to: int, text: str) -> None:
        try:
            await self.bot.send_message(chat_id, "⚠️ " + text, reply_to=reply_to)
        except Exception:
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
            elif cmd == "/wxlogin":
                await self.request_wechat_login()
            elif cmd in ("/find", "/open") and not self.wx_ready:
                await event.reply("微信还没有登录。请先扫描我发的二维码，或发 /wxlogin。")
            elif cmd == "/find":
                if not arg:
                    return await event.reply("用法：/find 关键词")
                contacts = await self.wx.search_contacts(arg)
                groups = await self.wx.search_groups(arg)
                lines = [f"👤 {_esc(c.get('displayName'))} — <code>{_esc(c['id'])}</code>" for c in contacts]
                lines += [f"👥 {_esc(g.get('displayName'))} — <code>{_esc(g['id'])}</code>" for g in groups]
                await event.reply("\n".join(lines[:40]) + "\n\n用 /open 微信ID 建群" if lines else "没有找到",
                                  parse_mode="html")
            elif cmd == "/open":
                if not arg:
                    return await event.reply("用法：/open 微信ID（先用 /find 查）")
                try:
                    thread = await self.wx.thread(arg)
                except WeChatError:
                    kind = "group" if arg.endswith("@chatroom") else "direct"
                    name = (await self.wx.contact(arg)).get("displayName") if kind == "direct" else arg
                    thread = {"id": arg, "kind": kind, "displayName": name or arg}
                existed = self.store.chat_for_thread(arg) is not None
                chat = await self.ensure_chat(thread)
                await event.reply("这个会话已经有群了" if existed else f"已建群：{_esc(self._title(thread))}",
                                  parse_mode="html")
        except WeChatError as e:
            await event.reply(f"出错：{e.code}")
