"""WeChat (agent-wechat) <-> Telegram bridge: one Telegram group (or forum topic) per WeChat chat."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx
from telethon import TelegramClient, errors, events, functions, types, utils

from . import cards, stickers, tgtransfer, wechat
from .calls import CallsMixin, _callweb
from .commands import CommandsMixin
from .config import Config
from .contacts import ContactsMixin, friendly as contact_error
from .emojis import WX_EMOJIS, replace_wx_emojis  # noqa: F401
from .formatting import (
    APP_TYPE_NAMES,  # noqa: F401
    CALL_INVITEES_SHOWN,  # noqa: F401
    DEAD_CHAT_ERRORS,  # noqa: F401
    DIAL_PICK_PAGE,  # noqa: F401
    DIAL_PICK_SHOWN,  # noqa: F401
    GROUP_BOT_COMMANDS,
    GROUP_HELP,
    HELP,  # noqa: F401
    MEDIA_REASONS,
    OPENIM_MEDIA_NOTE,  # noqa: F401
    PRIVATE_BOT_COMMANDS,
    TG_CAPTION_LIMIT,
    TG_TEXT_LIMIT,  # noqa: F401
    TOPIC_GROUP_BOT_COMMANDS,
    TOPIC_GROUP_HELP,
    TOPIC_HELP,  # noqa: F401
    TOPIC_PRIVATE_BOT_COMMANDS,
    UNSUPPORTED_TRANSPORT_CODES,  # noqa: F401
    WX_TYPE_NAMES,  # noqa: F401
    _avatar_photo,
    _call_time_line,  # noqa: F401
    _card,  # noqa: F401
    _channels_body,  # noqa: F401
    _chunks,
    _clip_text,
    _dead_chat,
    _describe_tg_msg,
    _dice_text,
    _esc,
    _fingerprint,
    _fmt_wx_text,
    _format_duration,  # noqa: F401
    _guess_image_media,
    _image_from_file,
    _is_link_card,  # noqa: F401
    _is_mention_boundary,
    _is_official_account,
    _is_openim,  # noqa: F401
    _link_body,
    _link_buttons,
    _link_url,
    _location_venue,
    _match_mentions,  # noqa: F401
    _match_mentions_with_spans,
    _media_failure_text,
    _member_label,  # noqa: F401
    _member_name,  # noqa: F401
    _msg_link,
    _msg_topic,
    _normalize_wx_text,
    _plain_sender,
    _reply_target,
    _short,  # noqa: F401
    _tg_msg_fingerprint,
    _tg_text_to_wx,
    _topic_gone,
    _unsupported_label,  # noqa: F401
    format_wx_body,
    forum_setup_text,
    quote_inline_text,
    quote_msg_summary,
    wav_to_ogg,
)
from .groups import GROUP_COMMANDS, GroupsMixin
from .login import LoginMixin
from .mem import maybe_trim_memory
from .store import Store
from .wechat import (
    APP,
    APP_CHANNELS,
    APP_FILE,
    APP_QUOTE,
    APP_UPLOAD_NOTICE,
    IMAGE,
    LOCATION,
    SEND_OK,
    STICKER,
    VIDEO,
    VOICE,
    CursorExpired,
    Media,
    WeChatClient,
    WeChatError,
    is_group_id,
)

if TYPE_CHECKING:  # pragma: no cover
    from .callweb import CallWeb

log = logging.getLogger("bridge")

TG_ENTITY_CACHE_LIMIT = 500
NAMES_CACHE_MAX = 4000
CARD_CACHE_IDLE_SECONDS = 600.0
TRIM_MEDIA_BYTES = 1 * 1024 * 1024
EVENT_CONCURRENCY = 8
WX_MEDIA_DOWNLOAD_CONCURRENCY = 3
CONTACT_NAME_TTL = 600.0  # seconds a contact's remark/nickname is reused before reading it again
_UNSET = object()

# Telegram only accepts these topic icon colours; one for contacts, one for WeChat groups, one for official accounts.
TOPIC_COLOR_DIRECT = 0x6FB9F0
TOPIC_COLOR_GROUP = 0x8EEE98
TOPIC_COLOR_OFFICIAL = 0xFFD67E


def _proxy(url: str):
    if not url:
        return None
    u = urlparse(url)
    return {"proxy_type": u.scheme, "addr": u.hostname, "port": u.port,
            "username": u.username, "password": u.password, "rdns": True}


def _event_session_key(event: dict) -> str:
    data = event.get("data") or {}
    kind = str(event.get("kind") or "")
    key = data.get("threadId") or data.get("groupId") or data.get("contactId") or kind
    return str(key)


def _tune_tg_session(client: TelegramClient | None) -> None:
    """Bound Telethon's SQLiteSession page cache and keep temp B-trees off the heap."""
    conn = getattr(getattr(client, "session", None), "_conn", None)
    if conn is not None:
        try:
            conn.execute("PRAGMA cache_size=-512")
            conn.execute("PRAGMA temp_store=FILE")
        except Exception:
            pass


class Bridge(ContactsMixin, GroupsMixin, CallsMixin, CommandsMixin, LoginMixin):
    def __init__(self, cfg: Config, *, user_client: object = _UNSET, bot_client: object = _UNSET):
        self.cfg = cfg
        self._init_login()
        data = cfg.data_dir
        self.store = Store(data / "bridge.db")
        self.sticker_cache = stickers.StickerDiskCache(data / "sticker_cache")
        self.wx = WeChatClient(cfg.wechat.base_url, cfg.wechat.read_token(), cfg.wechat.account)
        # Topics mode: everything happens in one forum supergroup and only the bot is used.
        self.topics = cfg.bridge.mode == "topics"
        tg = cfg.telegram
        proxy = _proxy(tg.proxy)
        if user_client is _UNSET:
            self.user = None if self.topics else TelegramClient(
                str(data / "user"), tg.api_id, tg.api_hash, proxy=proxy,
                flood_sleep_threshold=120, entity_cache_limit=TG_ENTITY_CACHE_LIMIT,
            )
            _tune_tg_session(self.user)
        else:
            self.user = user_client
        if bot_client is _UNSET:
            self.bot = TelegramClient(
                str(data / "bot"), tg.api_id, tg.api_hash, proxy=proxy,
                flood_sleep_threshold=60, entity_cache_limit=TG_ENTITY_CACHE_LIMIT,
            )
            _tune_tg_session(self.bot)
        else:
            self.bot = bot_client
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
        self._calls_protocol_version: int = 2
        self._calls_caps: dict = {}
        self._calls_video_caps: dict = {}
        self._orphan_call_states: dict[str, dict] = {}
        b = cfg.bridge
        self.call_web: CallWeb | None = (
            _callweb().CallWeb(self, b.call_web_url, b.call_web_listen, b.call_web_app)
            if b.call_web_url and b.call_notifications else None)
        self._ui_tokens: dict[str, dict] = {}
        self._link_wait: dict | None = None
        self._recall_notice_ttl: float = 5.0
        self._notice_tasks: set[asyncio.Task] = set()
        self._bg_tasks: set[asyncio.Task] = set()
        self._event_concurrency: int = EVENT_CONCURRENCY
        self._media_dl_sem = asyncio.Semaphore(WX_MEDIA_DOWNLOAD_CONCURRENCY)
        self._card_cache_handle: asyncio.TimerHandle | None = None
        self._card_cache_idle_seconds: float = CARD_CACHE_IDLE_SECONDS
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
        """t.me Mini App link that opens a link card in the Mini App (None = plain URL)."""
        if self.call_web is None or not (url := _link_url(link)):
            return None
        try:
            return self.call_web.article_link(url)
        except Exception:
            log.exception("Could not create a reader link for %s", url)
            return None

    def _card_buttons(self, link: dict | None, app_type: int | None) -> list | None:
        open_url = self._reader_link(link)
        in_place = bool(open_url) and self.call_web.reader_kind(_link_url(link)) == "link"
        return _link_buttons(link, app_type, open_url, original=in_place)

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

    def _spawn_bg(self, coro) -> asyncio.Task:
        """Create and track a background task so it is cancelled and awaited in close()."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._bg_tasks.discard(t)
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    log.debug("Background task failed: %s", exc)

        task.add_done_callback(_done)
        return task

    async def close(self) -> None:
        self._cancel_auto_relogin()
        if self._card_cache_handle is not None:
            self._card_cache_handle.cancel()
            self._card_cache_handle = None
        seen_tasks: set[asyncio.Task] = set()
        tasks: list[asyncio.Task] = []
        current = asyncio.current_task()

        def _collect(t: asyncio.Task | None) -> None:
            if t is not None and t is not current and not t.done() and t not in seen_tasks:
                seen_tasks.add(t)
                tasks.append(t)

        for t in list(self._notice_tasks):
            _collect(t)
        for t in list(self._bg_tasks):
            _collect(t)
        _collect(self._wx_auto_relogin_task)
        _collect(self.qr_task)
        _collect(self._wx_login_task)
        for req in list(self._dial_requests.values()):
            _collect(req.get("expire_task"))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.call_web is not None:
            await self.call_web.stop()
        if self._calls_sock is not None:
            await self._calls_sock.close()
            self._calls_sock = None
        await self.wx.close()
        self.store.close()
        for client in (self.user, self.bot):
            if client is None:
                continue
            session = getattr(client, "session", None)
            if session is not None and hasattr(session, "close"):
                is_conn = getattr(client, "is_connected", None)
                if not (callable(is_conn) and is_conn()):
                    try:
                        session.close()
                    except Exception:
                        pass

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
            concurrency = max(1, int(self._event_concurrency))
            global_sem = asyncio.Semaphore(concurrency)
            in_flight_sem = asyncio.Semaphore(concurrency * 4)
            key_tail: dict[str, asyncio.Task] = {}
            in_flight: set[asyncio.Task] = set()
            cursors_by_seq: dict[int, str | None] = {}
            done_seqs: set[int] = set()
            next_seq = 0
            next_commit_seq = 1

            def _commit_completed(seq: int) -> None:
                nonlocal cursor, next_commit_seq
                done_seqs.add(seq)
                advanced: str | None = None
                while next_commit_seq in done_seqs:
                    done_seqs.remove(next_commit_seq)
                    ev_cur = cursors_by_seq.pop(next_commit_seq, None)
                    if ev_cur:
                        advanced = ev_cur
                    next_commit_seq += 1
                if advanced:
                    cursor = advanced
                    self.store.set("cursor", cursor)
                    self._spawn_bg(self._ack(cursor))

            async def _run_event(seq: int, key: str, ev: dict, prev: asyncio.Task | None) -> None:
                completed = False
                try:
                    if prev is not None:
                        await prev
                    async with global_sem:
                        await self.handle_event(ev)
                    completed = True
                finally:
                    if key_tail.get(key) is asyncio.current_task():
                        key_tail.pop(key, None)
                    in_flight_sem.release()
                    if completed:
                        _commit_completed(seq)

            async def _drain_in_flight() -> None:
                if in_flight:
                    await asyncio.gather(*list(in_flight), return_exceptions=True)

            async def _cancel_in_flight() -> None:
                pending = [t for t in list(in_flight) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

            try:
                if cursor is None:
                    cursor = await self.wx.latest_cursor()
                    self.store.set("cursor", cursor)
                    log.info("First start: skipping old history, listening from event cursor %s", cursor)
                async for event in self.wx.stream(cursor):
                    backoff = 1.0
                    await in_flight_sem.acquire()
                    next_seq += 1
                    seq = next_seq
                    ev_cursor = str(event["cursor"]) if event.get("cursor") else None
                    cursors_by_seq[seq] = ev_cursor
                    key = _event_session_key(event)
                    prev = key_tail.get(key)
                    task = asyncio.create_task(_run_event(seq, key, event, prev))
                    key_tail[key] = task
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)
                await _drain_in_flight()
                log.warning("Event stream ended; reconnecting")
            except asyncio.CancelledError:
                await _cancel_in_flight()
                raise
            except CursorExpired:
                await _drain_in_flight()
                try:
                    cursor = await self.wx.latest_cursor()
                    self.store.set("cursor", cursor)
                    log.warning("Event cursor expired on the daemon; some events were skipped. Resuming at %s", cursor)
                except (httpx.HTTPError, WeChatError) as e:
                    cursor = None
                    log.warning("Event cursor expired and newest cursor lookup failed (%s); retrying in %.0fs",
                                e, backoff)
            except (httpx.HTTPError, WeChatError) as e:
                await _drain_in_flight()
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
            elif kind == "message.updated":
                await self.on_wx_message_updated(data)
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
        if not sender or sender == thread_id or is_group_id(sender) or sender == self._self_wxid:
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
                self._spawn_bg(self._deferred_outgoing(msg, origin))
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
                if is_group_id(thread_id):
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
            self._spawn_bg(self.sync_avatar(chat_id, thread["id"]))
        kind = "群聊" if thread.get("kind") == "group" else ("公众号/服务号" if _is_official_account(thread) else "联系人")
        kw = {"reply_to": topic} if (topic := self._topic_for(chat_id, thread["id"])) else {}
        await self.bot.send_message(chat_id, f"已连接微信{kind}：<b>{_esc(thread.get('displayName'))}</b>\n"
                                             f"<code>{_esc(thread['id'])}</code>", parse_mode="html", silent=True,
                                    **kw)

    # --- names ------------------------------------------------------------
    async def _contact_names(self, wx_id: str, *, fresh: bool = False) -> tuple[str, str]:
        """(remark, nickname) from the contact record itself, "" when unknown; cached for a while."""
        if not wx_id or is_group_id(wx_id):
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
        if thread.get("kind") not in (None, "direct") or is_group_id(tid):
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
            elif is_group_id(msg["threadId"]):
                member = await self.wx.group_member(msg["threadId"], sid)
                if member and (m_name := member.get("displayName") or member.get("nickname")):
                    name = m_name
        except WeChatError:
            pass
        if name == sid:
            name = nick or sid
        self.names[key] = name
        while len(self.names) > NAMES_CACHE_MAX:
            self.names.pop(next(iter(self.names)))
        return name


    def _body(self, msg: dict) -> str:
        return format_wx_body(msg)

    @staticmethod
    def _quote_inline_text(q: dict) -> str:
        return quote_inline_text(q)

    @staticmethod
    def _quote_msg_summary(orig: dict) -> str:
        return quote_msg_summary(orig)

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

    async def _msg_header(self, thread: dict, msg: dict) -> str:
        if msg.get("direction") == "outgoing":
            return "<b>我</b>（手机端）：\n"
        if thread.get("kind") == "group" and msg.get("senderId"):
            mentions = msg.get("mentions") if isinstance(msg.get("mentions"), dict) else {}
            tag = " 🔔<b>@我</b>" if mentions.get("self") else " 📢<b>@所有人</b>" if mentions.get("all") else ""
            return f"<b>{_esc(await self._sender_name(msg))}</b>{tag}：\n"
        return ""

    async def _resolve_quote(self, chat: int, msg: dict) -> tuple[int | None, str]:
        """Resolve a WeChat APP_QUOTE message to either a Telegram reply_to id or an HTML quote prefix."""
        if not (msg.get("type") == APP and msg.get("appMessageType") == APP_QUOTE and msg.get("quote")):
            return None, ""
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
            return hit[1], ""
        return None, self._quote_prefix(msg, orig)

    # --- sending to Telegram ----------------------------------------------
    async def forward_to_tg(self, chat: int, thread: dict, msg: dict) -> None:
        outgoing = msg.get("direction") == "outgoing"
        header = await self._msg_header(thread, msg)
        reply_to, prefix = await self._resolve_quote(chat, msg)

        media: Media | str | None = None
        channels_video_err = ""
        wx_dl_s = 0.0
        t = msg.get("type")
        is_channels = t == APP and msg.get("appMessageType") == APP_CHANNELS
        if t in (IMAGE, VIDEO, VOICE, STICKER) or (t == APP and msg.get("appMessageType") == APP_FILE):
            t_dl0 = time.monotonic()
            try:
                # Animated wxgf stickers keep their frames only in the original bytes (decoding needs ffmpeg).
                raw_wxgf = t == STICKER and stickers.find_ffmpeg() is not None
                async with self._media_dl_sem:
                    media = await self.wx.download_media(msg, self.max_media, raw_wxgf=raw_wxgf)
            except WeChatError as e:
                media = e.code
            wx_dl_s = time.monotonic() - t_dl0
            if isinstance(media, str):
                log.info("Could not download the attachment of WeChat message %s (type %s): %s", msg["id"], t, media)
            if isinstance(media, Media) and t == VOICE and media.mime == "audio/wav":
                if ogg := await (wav_to_ogg(path=media.path) if media.path is not None else wav_to_ogg(media.data)):
                    media.close()
                    media = Media(ogg, "audio/ogg", "voice.ogg")
        elif is_channels:
            t_dl0 = time.monotonic()
            try:
                async with self._media_dl_sem:
                    media = await self.wx.download_channels_video(msg, self.max_media)
            except WeChatError as e:
                media = e.code
            wx_dl_s = time.monotonic() - t_dl0
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
            self._schedule_card_cache_clear()
            if card_png:
                media = Media(card_png, "image/png", "link-card.png")

        silent = outgoing and self.cfg.bridge.outgoing_silent
        media_size = media.size if isinstance(media, Media) else 0
        media_name = media.filename if isinstance(media, Media) else ""
        should_trim = isinstance(media, Media) and (
            media.path is not None or media_size >= TRIM_MEDIA_BYTES or t == STICKER
        )
        t_tg0 = time.monotonic()
        try:
            sent = await self._send_tg(
                chat, thread, header, prefix, msg, media, reply_to, silent,
                channels_video_err=channels_video_err,
            )
        finally:
            if isinstance(media, Media):
                media.close()  # large downloads were streamed to a temp file
            media = None
            if should_trim:
                maybe_trim_memory()
        tg_send_s = time.monotonic() - t_tg0
        if media_size >= tgtransfer.LOG_MEDIA_MIN_BYTES:
            log.info(
                "WeChat -> Telegram media %s (%s): total=%.2fs (wx_dl=%.2fs %s, tg_send=%.2fs)",
                media_name, tgtransfer.format_mib(media_size), wx_dl_s + tg_send_s,
                wx_dl_s, tgtransfer.format_rate(media_size, wx_dl_s), tg_send_s,
            )
        for m in sent:
            self.store.map_message(chat, "bot", m.id, msg["threadId"], msg["id"], msg.get("serverId"))
            if chat < -1000000000000:
                self.store.map_message(chat, "user", m.id, msg["threadId"], msg["id"], msg.get("serverId"))
            if not self.topics:
                self._pair(chat, _fingerprint(m), bot=(msg["threadId"], msg["id"], msg.get("serverId")))
        self.forwarded += 1

    def _schedule_card_cache_clear(self) -> None:
        if self._card_cache_handle is not None:
            self._card_cache_handle.cancel()
            self._card_cache_handle = None
        if self._card_cache_idle_seconds <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        def _clear() -> None:
            self._card_cache_handle = None
            cards.clear_caches()
            maybe_trim_memory(min_interval=0.0)

        self._card_cache_handle = loop.call_later(self._card_cache_idle_seconds, _clear)

    # --- pairing the two views of a bridged message ----------------------
    async def on_user_sees_bot_message(self, event) -> None:
        """Your account received a message the bot posted; it has a different id there."""
        self._pair(event.chat_id, _fingerprint(event.message), user=event.message.id)

    def _pair(self, chat: int, fp: tuple, bot: tuple | None = None, user: int | None = None) -> None:
        now = time.monotonic()
        mine, other = (self.unpaired_bot, self.unpaired_user) if bot else (self.unpaired_user, self.unpaired_bot)
        for d in (mine, other):
            if q := d.get(chat):
                q[:] = [e for e in q if now - e[2] < 300][-50:]
                if not q:
                    d.pop(chat, None)
        oq = other.get(chat)
        if oq:
            for i, (ofp, payload, _) in enumerate(oq):
                if ofp == fp:
                    del oq[i]
                    if not oq:
                        other.pop(chat, None)
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
        for d in (mine, other):
            if q := d.get(chat_id):
                q[:] = [e for e in q if now - e[2] < 300][-50:]
                if not q:
                    d.pop(chat_id, None)
        oq = other.get(chat_id)
        if oq:
            for i, (ofp, payload, _) in enumerate(oq):
                if ofp == fp:
                    del oq[i]
                    if not oq:
                        other.pop(chat_id, None)
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
            extra_media: list[Media] = []
            try:
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
                    cap_parts = _chunks(caption, TG_CAPTION_LIMIT, rest_size=TG_TEXT_LIMIT) if caption else [""]
                    buttons = self._card_buttons(link, link_app_type)
                    kw = dict(caption=cap_parts[0] or None, parse_mode="html", reply_to=reply,
                              silent=silent, force_document=False, buttons=buttons)
                    first_card = None
                    cont_parts: list[str] = []
                    try:
                        first_card = await self.bot.send_file(chat_id, f, **kw)
                        cont_parts = cap_parts[1:]
                    except errors.RPCError as e:
                        if _dead_chat(e) or _topic_gone(e):
                            raise
                        if buttons:
                            url = _link_url(link)
                            src = cards.identify_link_source(link, link_app_type)
                            link_line = f'<a href="{_esc(url)}"><b>{_esc(src.button_label)}</b></a>'
                            fallback_cap = f"{caption}\n{link_line}" if caption else link_line
                            fb_parts = _chunks(fallback_cap, TG_CAPTION_LIMIT, rest_size=TG_TEXT_LIMIT)
                            f = send_media.tg_file()
                            kw["buttons"] = None
                            kw["caption"] = fb_parts[0] or None
                            try:
                                first_card = await self.bot.send_file(chat_id, f, **kw)
                                cont_parts = fb_parts[1:]
                            except errors.RPCError as e2:
                                if _dead_chat(e2) or _topic_gone(e2):
                                    raise
                                log.info("Telegram refused link card photo (%s); falling back to text", e2)
                        else:
                            log.info("Telegram refused link card photo (%s); falling back to text", e)
                    if first_card is not None:
                        out.append(first_card)
                        cont_reply = getattr(first_card, "id", None) or top
                        for extra in cont_parts:
                            out.append(await self.bot.send_message(
                                chat_id, extra, parse_mode="html", reply_to=cont_reply,
                                silent=silent, link_preview=False,
                            ))
                        return out
                    send_media = None

                sticker = None
                if t == STICKER and isinstance(send_media, Media):
                    sticker = await stickers.run_in_media_executor(
                        stickers.from_wechat_sticker, send_media.read(), send_media.mime
                    )
                    if sticker is None and send_media.mime == "image/x-wxgf":
                        # The original wxgf could not be decoded: use agent-wechat's still preview instead.
                        send_media = await self._wxgf_still(msg)
                        if isinstance(send_media, Media):
                            extra_media.append(send_media)
                            sticker = await stickers.run_in_media_executor(
                                stickers.from_wechat_sticker, send_media.read(), send_media.mime
                            )
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
                                cap_clean = caption.rstrip("\n")
                                for ci, cpart in enumerate(_chunks(cap_clean)):
                                    out.append(await self.bot.send_message(
                                        chat_id, cpart, parse_mode="html",
                                        reply_to=reply if ci == 0 else top, silent=silent, link_preview=False))
                                sk_kwargs["reply_to"] = top
                            try:
                                out.append(await self.bot.send_file(chat_id, sf, **sk_kwargs))
                                return out
                            except errors.RPCError as e:
                                if _dead_chat(e) or _topic_gone(e):
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
                            extra_media.append(still)
                            send_media = still
                    f = send_media.tg_file()
                    send_size = send_media.size
                    is_voice = t == VOICE and send_media.mime in ("audio/ogg", "audio/opus")
                    is_video = t == VIDEO or (is_channels and send_media.mime.startswith("video/"))
                    cap_parts = _chunks(caption, TG_CAPTION_LIMIT, rest_size=TG_TEXT_LIMIT) if caption else [""]
                    kwargs = dict(
                        caption=cap_parts[0] or None, parse_mode="html", reply_to=reply,
                        silent=silent, voice_note=is_voice, supports_streaming=is_video,
                        force_document=(t == APP and msg.get("appMessageType") == APP_FILE) or (
                            t == VOICE and not send_media.mime.startswith("audio/")),
                    )
                    if is_video:
                        if send_media.mime.startswith("video/"):
                            kwargs["mime_type"] = send_media.mime
                        kwargs["nosound_video"] = True
                        vmeta = await stickers.run_in_media_executor(
                            lambda: stickers.extract_video_meta_and_thumb(
                                send_media.data, send_media.filename, path=send_media.path,
                            )
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
                    uploaded_handle = None
                    upload_workers = 0
                    t_up0 = time.monotonic()
                    as_photo = not kwargs["force_document"] and utils.is_image(f)
                    if send_media.path is not None and not as_photo:
                        uploaded_handle, upload_workers = await tgtransfer.upload_file_parallel(
                            self.bot,
                            send_media.path,
                            send_media.filename,
                            send_size,
                            max_workers=self.cfg.bridge.tg_transfer_workers,
                        )
                        if uploaded_handle is not None:
                            f = uploaded_handle
                            if send_media.mime and send_media.mime != "application/octet-stream":
                                kwargs.setdefault("mime_type", send_media.mime)
                    try:
                        first_media = await self.bot.send_file(chat_id, f, **kwargs)
                    except errors.RPCError as e:
                        if _dead_chat(e) or _topic_gone(e) or kwargs["force_document"]:
                            raise
                        # e.g. a photo too large/odd-sized for Telegram's photo type: send it as a file instead
                        log.info("Telegram refused media as-is (%s); sending as a file", e)
                        f = uploaded_handle if uploaded_handle is not None else send_media.tg_file()
                        kwargs.update(force_document=True, voice_note=False, supports_streaming=False, nosound_video=None)
                        kwargs.pop("attributes", None)
                        kwargs.pop("thumb", None)
                        first_media = await self.bot.send_file(chat_id, f, **kwargs)
                    out.append(first_media)
                    cont_reply = getattr(first_media, "id", None) or top
                    for extra in cap_parts[1:]:
                        out.append(await self.bot.send_message(
                            chat_id, extra, parse_mode="html", reply_to=cont_reply,
                            silent=silent, link_preview=False,
                        ))
                    if send_size >= tgtransfer.LOG_MEDIA_MIN_BYTES:
                        t_up = time.monotonic() - t_up0
                        mode = f"{upload_workers}x512KiB" if upload_workers else "stock"
                        log.info(
                            "Telegram media upload %s (%s, %s): %.2fs (%s)",
                            send_media.filename, tgtransfer.format_mib(send_size), mode,
                            t_up, tgtransfer.format_rate(send_size, t_up),
                        )
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
                        if _dead_chat(e) or _topic_gone(e):
                            raise
                        log.info("Telegram refused venue (%s); falling back to text + geo point", e)
                if isinstance(send_media, str):
                    text = header + prefix + _media_failure_text(msg, send_media)
                    buttons = None
                elif is_link:
                    buttons = self._card_buttons(link, link_app_type)
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
            finally:
                for m in extra_media:
                    m.close()

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

    async def on_wx_message_updated(self, data: dict) -> None:
        """Refresh a previously forwarded message in place when agent-wechat fills in its body (e.g. 49:40 -> 49:19)."""
        message_id = data.get("messageId")
        if not message_id:
            return
        try:
            msg = await self.wx.message(message_id)
        except WeChatError as e:
            log.debug("Could not fetch updated WeChat message %s: %s", message_id, e)
            return
        if msg.get("recall"):
            return
        t = msg.get("type")
        if t in (IMAGE, VIDEO, VOICE, STICKER) or (t == APP and msg.get("appMessageType") == APP_FILE):
            return
        thread_id = msg.get("threadId") or data.get("threadId") or ""
        if not thread_id:
            return
        dest = self._dest_for_thread(thread_id)
        hit = self.store.tg_msgs_for_wx(thread_id, message_id, msg.get("serverId"), tg_chat=dest[0] if dest else None)
        if not hit:
            return
        chat_id, msg_ids = hit
        try:
            thread = await self.wx.thread(thread_id)
        except WeChatError:
            thread = {"id": thread_id, "kind": "group" if is_group_id(thread_id) else "direct"}
        header = await self._msg_header(thread, msg)
        _, prefix = await self._resolve_quote(chat_id, msg)
        parts = _chunks(header + prefix + self._body(msg))
        for msg_id, part in zip(msg_ids, parts):
            try:
                await self.bot.edit_message(chat_id, msg_id, part, parse_mode="html", link_preview=False)
            except errors.MessageNotModifiedError:
                continue
            except Exception as e:
                log.info("Could not update Telegram message %s/%s for WeChat %s: %s", chat_id, msg_id, message_id, e)
                return
        for extra in parts[len(msg_ids):]:
            try:
                sent = await self.bot.send_message(
                    chat_id, extra, parse_mode="html", reply_to=msg_ids[0], silent=True, link_preview=False,
                )
            except Exception as e:
                log.info("Could not send overflow chunk for updated WeChat %s: %s", message_id, e)
                break
            self.store.map_message(chat_id, "bot", sent.id, thread_id, msg["id"], msg.get("serverId"))
            if chat_id < -1000000000000:
                self.store.map_message(chat_id, "user", sent.id, thread_id, msg["id"], msg.get("serverId"))

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
    # Telegram -> WeChat
    # ======================================================================
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
            if cmd_name in ("/call", "/vcall"):
                await self._start_dial(
                    chat_id, await self._bot_reply_id(chat_id, msg.id), thread_id, arg.strip(),
                    video=cmd_name == "/vcall",
                )
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
                t_m0 = time.monotonic()
                kind, media = await self._tg_media(msg)
                t_dl = time.monotonic() - t_m0
                if kind is None:
                    await self._report(chat_id, await self._bot_reply_id(chat_id, msg.id), f"{fail_prefix}{media}")
                else:
                    media_size = media.size if isinstance(media, Media) else 0
                    media_name = media.filename if isinstance(media, Media) else ""
                    should_trim = isinstance(media, Media) and (
                        media.path is not None or media_size >= TRIM_MEDIA_BYTES or kind == "sticker"
                    )
                    t_wx0 = time.monotonic()
                    try:
                        if kind == "sticker":
                            result = await self.wx.send_sticker(thread_id, media, key)
                        else:
                            result = await self.wx.send_media(thread_id, kind, media, key)
                    finally:
                        if isinstance(media, Media):
                            media.close()
                        media = None
                        if should_trim:
                            maybe_trim_memory()
                    t_wx = time.monotonic() - t_wx0
                    if media_size >= tgtransfer.LOG_MEDIA_MIN_BYTES:
                        log.info(
                            "Telegram -> WeChat media %s (%s): total=%.2fs (tg_dl=%.2fs, wx=%.2fs, state=%s)",
                            media_name, tgtransfer.format_mib(media_size), t_dl + t_wx,
                            t_dl, t_wx, result.get("state") if isinstance(result, dict) else None,
                        )
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
        if not is_group_id(thread_id) or "@" not in text:
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

    async def _tg_media(self, msg) -> tuple[str | None, Media | str]:
        f = msg.file
        size = f.size if f else 0
        if size and size > self.max_media:
            return None, f"文件太大（{size // 1048576} MB），超过 max_media_mb 限制"
        mime = (f.mime_type if f and f.mime_type else "application/octet-stream").lower()
        ext = (getattr(f, "ext", None) or "").lower()
        name = (f.name if f and f.name else None) or f"telegram{ext}"
        needs_bytes = bool(msg.sticker or (msg.gif and self.cfg.bridge.gif_as_sticker))
        spill_to_disk = bool(size and size > wechat.MEDIA_SPILL_BYTES and not needs_bytes)
        media_path: str | None = None
        dl_workers = 0
        t_dl0 = time.monotonic()
        if spill_to_disk:
            tmp_dir = tempfile.mkdtemp(prefix="tg-media-")
            dst = os.path.join(tmp_dir, wechat._safe_filename(name))
            try:
                saved = None
                msg_client = getattr(msg, "client", None)
                if msg_client is not None:
                    used_parallel, dl_workers = await tgtransfer.download_document_parallel(
                        msg_client, msg, dst, max_workers=self.cfg.bridge.tg_transfer_workers,
                    )
                    if used_parallel:
                        saved = dst
                if not saved:
                    saved = await msg.download_media(file=dst)
            except BaseException:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise
            if not saved:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return None, "无法下载这条 Telegram 媒体"
            if isinstance(saved, (bytes, bytearray)):
                shutil.rmtree(tmp_dir, ignore_errors=True)
                data = bytes(saved)
            else:
                saved_path = os.fspath(saved)
                if not os.path.isfile(saved_path):
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    return None, "无法下载这条 Telegram 媒体"
                data = b""
                media_path = saved_path
        else:
            raw = await msg.download_media(file=bytes)
            if not raw:
                return None, "无法下载这条 Telegram 媒体"
            data = bytes(raw)
        actual_size = os.path.getsize(media_path) if media_path is not None else len(data)
        if actual_size >= tgtransfer.LOG_MEDIA_MIN_BYTES:
            t_dl = time.monotonic() - t_dl0
            mode = f"{dl_workers}x512KiB" if dl_workers else "stock"
            log.info(
                "Telegram media download %s (%s, %s): %.2fs (%s)",
                name, tgtransfer.format_mib(actual_size), mode,
                t_dl, tgtransfer.format_rate(actual_size, t_dl),
            )
        if msg.photo:
            return "image", Media(data, "image/jpeg", "photo.jpg", path=media_path)
        cache = getattr(self, "sticker_cache", None)
        if msg.sticker:
            if mime in ("application/octet-stream", "") or not mime.startswith(("image/", "video/", "application/x-tgsticker")):
                mime = {".webp": "image/webp", ".tgs": "application/x-tgsticker", ".webm": "video/webm"}.get(ext, mime)
            converted = await stickers.to_wechat_sticker(data, mime, cache=cache)
            if isinstance(converted, str):
                return None, converted
            _, out = converted
            return "sticker", Media(out, "image/gif", "sticker.gif")
        if msg.voice:
            return "voice", Media(data, "audio/ogg", "voice.ogg", path=media_path)
        if msg.gif and self.cfg.bridge.gif_as_sticker:
            if mime in ("application/octet-stream", "") or not mime.startswith(("image/", "video/")):
                mime = {".gif": "image/gif", ".mp4": "video/mp4", ".webm": "video/webm"}.get(ext, mime)
            converted = await stickers.gif_to_wechat_sticker(data, mime, cache=cache)
            if not isinstance(converted, str):
                _, out = converted
                return "sticker", Media(out, "image/gif", "sticker.gif")
            log.info("Telegram GIF -> WeChat sticker failed (%s); falling back to %s",
                     converted, "file" if mime == "image/gif" else "video")
            if mime == "image/gif":
                return "file", Media(data, mime, name if name.endswith(".gif") else "animation.gif")
        if msg.video or msg.video_note or msg.gif:
            return "video", Media(data, "video/mp4", name if name.endswith(".mp4") else "video.mp4", path=media_path)
        try:
            if img := await asyncio.to_thread(_image_from_file, data, mime, name, ext, path=media_path):
                if media_path is not None and img.path != media_path:
                    shutil.rmtree(os.path.dirname(media_path), ignore_errors=True)
                return "image", img
        except BaseException:
            if media_path is not None:
                shutil.rmtree(os.path.dirname(media_path), ignore_errors=True)
            raise
        return "file", Media(data, mime, name, path=media_path)

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
