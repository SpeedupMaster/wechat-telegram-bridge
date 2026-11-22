"""Answer WeChat voice calls from a Telegram Mini App (or any browser) and relay the call audio.

    browser page  <--WebSocket: JSON + binary PCM16-->  CallWeb (aiohttp)  <--calls media socket-->  agent-wechat

The page is served by a small aiohttp server that listens on loopback (`call_web_listen`); put an HTTPS
reverse proxy in front of it (`call_web_url`), because browsers only grant microphone access to secure pages.

Every call / group-call invitation gets an unguessable web token. The token in the link authorises the page;
when the page runs as a Telegram Mini App its signed initData must additionally belong to the bridge owner.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import struct
import time
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, quote, urlsplit

from aiohttp import WSMsgType, web

from . import article as articles
from . import bilibili, douyin, xhs
from .wechat import WeChatError

if TYPE_CHECKING:  # pragma: no cover
    from .core import Bridge

log = logging.getLogger(__name__)

# How long a connected call survives without any page holding its audio before it is hung up.
DETACH_GRACE_SECONDS = 20.0
# How long to wait for an answered call to reach "connected".
CONNECT_TIMEOUT_SECONDS = 45.0
# How long an outgoing call may ring before we give up (WeChat itself stops ringing after about a minute).
OUTGOING_CONNECT_TIMEOUT_SECONDS = 90.0
# How long a /call link stays usable before the call is placed.
DIAL_REQUEST_TTL = 600.0
# agent-wechat accepts 1-100 invitees when starting a group call.
MAX_INVITEES = 100
INIT_DATA_MAX_AGE = 24 * 3600
MAX_PCM_CHUNK = 65536
# How long after the media socket attaches to check that audio is flowing both ways (logged, not enforced).
AUDIO_CHECK_SECONDS = 8
# Mini App start parameters that open the article reader instead of a call: "r_" + Store.article_id().
READER_PREFIX = "r_"
_ARTICLE_ID_RE = re.compile(r"[A-Za-z0-9]{8,32}")
# Telegram's own links already open natively; sending them through the Mini App would only get in the way.
_NATIVE_HOSTS = ("t.me", "telegram.me", "telegram.dog", "telegram.org")
MAX_LINK_LEN = 4096


def _is_plain_link(url: str | None) -> bool:
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host or len(url) > MAX_LINK_LEN or any(c.isspace() for c in url):
        return False
    return not any(host == d or host.endswith("." + d) for d in _NATIVE_HOSTS)

CALL_ERRORS = {
    "offer_expired": "来电已结束或已过期",
    "stale": "来电状态已变化，请重试",
    "already_claimed": "来电已被其他设备接听",
    "not_owner": "通话已被其他设备接管",
    "busy": "当前有其他未结束的通话",
    "forbidden": "API Token 缺少 calls 权限",
    "unsupported": "当前 agent-wechat 不支持该操作",
    "media_not_ready": "通话音频尚未就绪",
    "capacity": "agent-wechat 通话容量已满",
    "outcome_unknown": "操作结果未确认，请在手机上查看",
    "invalid_request": "请求无效",
    "closed": "通话连接已关闭",
}


class CallActionError(Exception):
    """A user-facing reason why answering / joining / hanging up failed."""


class CallPending(CallActionError):
    """The same action is already in flight (e.g. a double tap); the page just gets the current state back."""


def verify_init_data(init_data: str, bot_token: str, *, max_age: float = INIT_DATA_MAX_AGE,
                     now: float | None = None) -> int | None:
    """Check Telegram Mini App initData (HMAC-SHA256 keyed by the bot token); return the user id or None."""
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    data = dict(pairs)
    received = data.pop("hash", "")
    if not received or len(data) + 1 != len(pairs):
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(data.get("auth_date", "0"))
        user = json.loads(data.get("user") or "{}")
        uid = int(user["id"])
    except (ValueError, KeyError, TypeError):
        return None
    if max_age and abs((time.time() if now is None else now) - auth_date) > max_age:
        return None
    return uid


def _err_text(code: str | None, default: str) -> str:
    return CALL_ERRORS.get(code or "", f"{default}（{code or 'error'}）")


class CallSession:
    """One call answered/joined from Telegram: owns the media socket and the page that plays its audio."""

    def __init__(self, web_: CallWeb, entry: dict, owner_token: str, readied: bool):
        self.web = web_
        self.entry = entry
        self.call_id: str = entry["call_id"]
        self.owner_token = owner_token
        self.readied = readied
        self.media = None
        self.send_track: int | None = None
        self.send_gen: int | None = None
        self.rate = 16000
        self.frame = 320
        self.active_ws: web.WebSocketResponse | None = None
        self.muted = False
        self.changed = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.grace_task: asyncio.Task | None = None
        self.closed = False
        # audio flow, for the "no sound" diagnosis: chunks WeChat sent / reached a page, mic chunks a page sent
        self.rx_chunks = 0
        self.rx_to_page = 0
        self.tx_chunks = 0
        self.watch_task: asyncio.Task | None = None

    @property
    def attached(self) -> bool:
        return self.media is not None and not self.closed

    async def run(self) -> None:
        bridge = self.web.bridge
        try:
            loop = asyncio.get_running_loop()
            outgoing = self.entry.get("direction") == "outgoing"
            deadline = loop.time() + (OUTGOING_CONNECT_TIMEOUT_SECONDS if outgoing else CONNECT_TIMEOUT_SECONDS)
            while self.entry["stage"] != "connected":
                if self.entry["terminal"] or self.closed:
                    return
                remaining = deadline - loop.time()
                if remaining <= 0:
                    if outgoing:
                        await self.web.hangup(self.entry, quiet=True, status="⚪ 对方长时间未接听，已取消呼叫")
                    else:
                        await self.web.report(self.entry, "通话长时间未接通，已挂断")
                        await self.web.hangup(self.entry, quiet=True)
                    return
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    pass
            if self.entry["terminal"] or self.closed:
                return
            sock, ready = await bridge.wx.call_media(self.call_id, bridge._calls_client_id, self.owner_token)
            self.media = sock
            send = ready.get("sendTrack") or {}
            fmt = send.get("format") or bridge._calls_audio_format
            if int(fmt.get("channels") or 1) != 1:
                raise CallActionError(f"不支持的通话音频格式：{fmt}")
            self.rate = int(fmt.get("sampleRate") or 16000)
            self.frame = max(1, self.rate * int(fmt.get("frameMs") or 20) // 1000)
            self.send_track = send.get("id")
            self.send_gen = send.get("generation")
            if self.closed:
                return
            await self._start_send()
            await self.web.push_state(self.entry)
            log.info("Call %s media attached: %d Hz, frame %d, send track %s", self.call_id, self.rate, self.frame,
                     self.send_track)
            self.watch_task = asyncio.create_task(self._watch_audio())
            async for ev in sock.events():
                etype = ev.get("type")
                if etype == "audio.chunk":
                    await self._to_page(ev)
                elif etype == "track.removed":
                    await self._send_page_json({"type": "drop", "trackId": ev.get("trackId")})
            if not self.closed and not self.entry["terminal"]:
                log.warning("Call %s media socket closed while the call was still live", self.call_id)
                await self.web.report(self.entry, "通话音频连接已断开")
                await self.web.hangup(self.entry, quiet=True)
        except asyncio.CancelledError:
            raise
        except CallActionError as e:
            await self.web.report(self.entry, str(e))
            await self.web.hangup(self.entry, quiet=True)
        except (OSError, WeChatError, asyncio.TimeoutError, ValueError) as e:
            log.warning("Call %s media failed: %s", self.call_id, e)
            if not self.entry["terminal"]:
                await self.web.report(self.entry, f"通话音频连接失败：{e}")
                await self.web.hangup(self.entry, quiet=True)
        except Exception:
            log.exception("Call %s media relay crashed", self.call_id)
        finally:
            await self.close()

    async def _watch_audio(self) -> None:
        """Log once if, shortly after the media socket attached, audio is not flowing in some direction."""
        await asyncio.sleep(AUDIO_CHECK_SECONDS)
        if self.closed or self.entry["terminal"]:
            return
        if not self.rx_chunks:
            log.warning("Call %s: no audio from WeChat %ds after the media socket attached", self.call_id,
                        AUDIO_CHECK_SECONDS)
        elif not self.rx_to_page:
            log.warning("Call %s: WeChat audio is arriving but no call page is attached to play it", self.call_id)
        if not self.tx_chunks:
            log.warning("Call %s: no microphone audio from the call page %ds after the media socket attached "
                        "(page attached: %s, muted: %s)", self.call_id, AUDIO_CHECK_SECONDS,
                        self.active_ws is not None, self.muted)

    async def _start_send(self) -> None:
        if self.media is not None and self.send_track is not None and self.send_gen is not None:
            await self.media.send_json({"type": "audio.start", "trackId": self.send_track, "generation": self.send_gen})

    async def on_track_updated(self, track: dict) -> None:
        if track.get("direction") != "send" or track.get("id") != self.send_track:
            return
        self.send_gen = track.get("generation")
        try:
            await self._start_send()
        except (OSError, WeChatError) as e:
            log.info("Could not restart send track for %s: %s", self.call_id, e)

    async def _to_page(self, ev: dict) -> None:
        self.rx_chunks += 1
        ws = self.active_ws
        if ws is None or ws.closed:
            return
        try:
            pcm = base64.b64decode(ev.get("pcmBase64") or "")
        except ValueError:
            return
        if not pcm:
            return
        try:
            await ws.send_bytes(struct.pack("<I", int(ev.get("trackId") or 0)) + pcm)
            self.rx_to_page += 1
        except (ConnectionError, RuntimeError):
            pass

    async def _send_page_json(self, obj: dict) -> None:
        ws = self.active_ws
        if ws is not None and not ws.closed:
            try:
                await ws.send_json(obj)
            except (ConnectionError, RuntimeError):
                pass

    async def from_page(self, ws: web.WebSocketResponse, data: bytes) -> None:
        if ws is not self.active_ws or self.muted or self.media is None or self.send_gen is None:
            return
        if len(data) & 1:
            data = data[:-1]
        if not data or len(data) > MAX_PCM_CHUNK:
            return
        self.tx_chunks += 1
        try:
            await self.media.send_json({
                "type": "audio.chunk",
                "trackId": self.send_track,
                "generation": self.send_gen,
                "pcmBase64": base64.b64encode(data).decode("ascii"),
            })
        except (OSError, WeChatError):
            pass

    def attach(self, ws: web.WebSocketResponse) -> None:
        self.active_ws = ws
        if self.grace_task and not self.grace_task.done():
            self.grace_task.cancel()
        self.grace_task = None

    def detach(self, ws: web.WebSocketResponse) -> None:
        if self.active_ws is not ws:
            return
        self.active_ws = None
        if self.closed or self.entry["terminal"]:
            return
        self.grace_task = asyncio.create_task(self._grace())

    async def _grace(self) -> None:
        await asyncio.sleep(DETACH_GRACE_SECONDS)
        if self.active_ws is None and not self.closed and not self.entry["terminal"]:
            await self.web.hangup(self.entry, quiet=True, status="📴 通话界面已关闭，已自动挂断")

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.changed.set()
        if self.watch_task and not self.watch_task.done():
            self.watch_task.cancel()
        if self.media is not None:
            log.info("Call %s audio: %d chunks from WeChat (%d relayed to the page), %d mic chunks from the page",
                     self.call_id, self.rx_chunks, self.rx_to_page, self.tx_chunks)
        if self.grace_task and not self.grace_task.done() and self.grace_task is not asyncio.current_task():
            self.grace_task.cancel()
        if self.media is not None:
            await self.media.close()
        if self.task and not self.task.done() and self.task is not asyncio.current_task():
            self.task.cancel()
        if self.readied:
            await self.web.withdraw_readiness()
        if self.web.sessions.get(self.call_id) is self:
            self.web.sessions.pop(self.call_id, None)
        await self.web.push_state(self.entry)
        await self.web.bridge._sync_call_msg(self.entry)


class CallWeb:
    def __init__(self, bridge: Bridge, base_url: str, listen: str, app_name: str = ""):
        self.bridge = bridge
        self.base_url = base_url.rstrip("/")
        self.prefix = urlsplit(self.base_url).path.rstrip("/")
        host, _, port = listen.rpartition(":")
        self.host = host.strip("[]")
        self.port = int(port)
        self.app_name = app_name
        # web token -> ("call", call_id) | ("invite", invitation_id) | ("dial", dial_id)
        self.tokens: dict[str, tuple[str, str]] = {}
        self.clients: dict[str, set[web.WebSocketResponse]] = {}  # web token -> open pages
        self.sessions: dict[str, CallSession] = {}  # call_id -> session
        self._tasks: set[asyncio.Task] = set()
        self._runner: web.AppRunner | None = None
        self.bound_port = self.port
        self.article_cache = articles.ArticleCache()
        self.article_fetcher = articles.ArticleFetcher(proxy=bridge.cfg.bridge.article_proxy)
        self._article_fetches: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ links / tokens
    def link(self, token: str) -> str:
        if self.app_name and getattr(self.bridge, "bot_me", None) and self.bridge.bot_me.username:
            return f"https://t.me/{self.bridge.bot_me.username}/{quote(self.app_name)}?startapp={token}"
        return f"{self.base_url}/call/{token}"

    def reader_kind(self, url: str | None) -> str | None:
        """How the Mini App shows a link: "fetch" (note / embedded video, resolved ahead of time), "link" (any
        other web page, Official Account articles included, opened in place), or None (not through the Mini App)."""
        cfg = self.bridge.cfg.bridge
        if (cfg.xhs_reader and xhs.is_note_url(url) or cfg.douyin_reader and douyin.is_video_url(url)
                or cfg.bilibili_reader and bilibili.is_video_url(url)):
            return "fetch"
        if cfg.links_in_app and _is_plain_link(url):
            return "link"
        return None

    def article_link(self, url: str | None) -> str | None:
        """t.me Mini App link for a link card's button (None = keep the plain URL)."""
        kind = self.reader_kind(url) if self.app_name else None
        me = getattr(self.bridge, "bot_me", None)
        if kind is None or not (me and me.username):
            return None
        # other pages keep their exact URL (scheme, fragment); readers use the canonical https form
        key = url.strip() if kind == "link" else articles.normalize_url(url)
        aid = self.bridge.store.article_id(key)
        if kind == "fetch":  # share tokens expire; resolve while the card is fresh
            self._prefetch(key)
        return f"https://t.me/{me.username}/{quote(self.app_name)}?startapp={READER_PREFIX}{aid}"

    def _prefetch(self, url: str) -> None:
        """Load a note while its share link's xsec_token is still fresh; the result lands in the cache."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if self.article_cache.get(url) is None and url not in self._article_fetches:
            self._spawn(self._load_article(url))

    def _new_token(self, kind: str, ref: str) -> str:
        self._prune_tokens()
        token = secrets.token_urlsafe(18)
        while token.startswith(READER_PREFIX):  # reserved for reader start parameters
            token = secrets.token_urlsafe(18)
        self.tokens[token] = (kind, ref)
        return token

    def _prune_tokens(self) -> None:
        if len(self.tokens) < 300:
            return
        for token, (kind, ref) in list(self.tokens.items()):
            live = (self.bridge._active_calls.get(ref) if kind == "call" else
                    self.bridge._dial_requests.get(ref) if kind == "dial" else self.bridge._conf_invites.get(ref))
            if live is None and token not in self.clients:
                self.tokens.pop(token, None)

    def token_for_call(self, entry: dict) -> str:
        if not entry.get("web_token"):
            entry["web_token"] = self._new_token("call", entry["call_id"])
        return entry["web_token"]

    def token_for_invite(self, inv: dict) -> str:
        if not inv.get("web_token"):
            inv["web_token"] = self._new_token("invite", inv["invitation_id"])
        return inv["web_token"]

    def token_for_dial(self, req: dict) -> str:
        if not req.get("web_token"):
            req["web_token"] = self._new_token("dial", req["dial_id"])
        return req["web_token"]

    def can_dial(self, conference: bool = False) -> bool:
        caps = self.bridge._calls_caps
        if conference:
            return bool(caps.get("conferenceCreate") and caps.get("conferenceAudio"))
        return bool(caps.get("directAudio"))

    def can_invite(self, entry: dict) -> bool:
        """More people can be invited into a connected group call this bridge started and still owns."""
        return (entry["kind"] == "conference" and entry.get("direction") == "outgoing"
                and entry["stage"] == "connected" and not entry["terminal"]
                and self.has_session(entry["call_id"])
                and bool(self.bridge._calls_caps.get("conferenceInvite")))

    async def invite(self, entry: dict, contact_ids: list[str], names: dict[str, str]) -> None:
        if not self.can_invite(entry):
            raise CallActionError("群通话已结束，或当前 agent-wechat 不支持追加邀请")
        if not contact_ids:
            raise CallActionError("还没有选择要邀请的群成员")
        if len(contact_ids) > MAX_INVITEES:
            raise CallActionError(f"一次最多邀请 {MAX_INVITEES} 人")
        res = await self._command({"type": "call.invite", "callId": entry["call_id"], "contactIds": contact_ids})
        if res.get("status") not in ("accepted", "replayed"):
            raise CallActionError(_err_text(res.get("code"), "agent-wechat 拒绝了邀请"))
        invitees = entry.setdefault("invitees", {})
        failed = entry.setdefault("invite_failed", {})
        for cid in contact_ids:
            invitees[cid] = names.get(cid) or invitees.get(cid) or cid
            failed.pop(cid, None)  # a new attempt: wait for its own result
        await self.bridge._sync_call_msg(entry)

    def can_answer(self, entry: dict) -> bool:
        caps = self.bridge._calls_caps
        return (entry["kind"] == "direct" and entry.get("call_type", "voice") == "voice"
                and bool(caps.get("directAudio")))

    def can_join(self) -> bool:
        caps = self.bridge._calls_caps
        return bool(caps.get("conferenceJoin") and caps.get("conferenceAudio"))

    def has_session(self, call_id: str) -> bool:
        sess = self.sessions.get(call_id)
        return sess is not None and not sess.closed

    # ------------------------------------------------------------------ bridge hooks
    async def on_entry_changed(self, entry: dict) -> None:
        sess = self.sessions.get(entry["call_id"])
        if sess is not None:
            sess.changed.set()
            if entry["terminal"] and not sess.closed:
                await sess.close()
                return  # close() pushes the final state
        await self.push_state(entry)

    async def on_track_updated(self, event: dict) -> None:
        sess = self.sessions.get(event.get("callId") or "")
        if sess is not None and not sess.closed:
            await sess.on_track_updated(event.get("track") or {})

    async def on_invite_changed(self, inv: dict) -> None:
        if inv.get("web_token"):
            await self._push_token(inv["web_token"])

    async def on_dial_changed(self, req: dict) -> None:
        if req.get("web_token") and not req.get("call_id"):
            await self._push_token(req["web_token"])

    async def report(self, entry: dict, text: str) -> None:
        token = entry.get("web_token")
        for ws in list(self.clients.get(token, ())) if token else ():
            await _safe_send(ws, {"type": "error", "message": text})
        if entry.get("chat_id"):
            await self.bridge._report(entry["chat_id"], entry.get("msg_id"), text)

    # ------------------------------------------------------------------ call control
    async def _command(self, payload: dict) -> dict:
        sock = self.bridge._calls_sock
        if sock is None or sock.closed:
            raise CallActionError("通话信令未连接，请稍后重试")
        try:
            return await sock.command(payload)
        except (OSError, WeChatError, asyncio.TimeoutError) as e:
            raise CallActionError(f"通话指令发送失败：{e}") from None

    async def _ready(self, scope: dict) -> dict:
        return await self._command({"type": "call.ready", "ready": True,
                                    "format": self.bridge._calls_audio_format, "scope": scope})

    async def withdraw_readiness(self) -> None:
        if any(s.readied and not s.closed for s in self.sessions.values()):
            return  # another live call still needs it
        sock = self.bridge._calls_sock
        if sock is None or sock.closed:
            return
        try:
            await sock.command({"type": "call.ready", "ready": False, "format": self.bridge._calls_audio_format})
        except Exception:
            pass

    async def answer(self, entry: dict, ws: web.WebSocketResponse | None) -> None:
        if entry.get("answering") or entry.get("rejecting") or self.has_session(entry["call_id"]):
            raise CallPending("正在处理中…")
        if entry["terminal"] or entry["stage"] not in ("pending", "offered"):
            raise CallActionError("来电已结束或已在其他设备处理")
        if not self.can_answer(entry):
            raise CallActionError("当前 agent-wechat 不支持接听该通话（仅支持单聊语音来电）")
        entry["answering"] = True
        readied = False
        claimed = False
        try:
            call_id = entry["call_id"]
            if entry["stage"] == "pending":
                entry["offered_event"].clear()
                res = await self._ready({"kind": "offer", "callId": call_id, "expectedRevision": entry["revision"]})
                readied = True
                if res.get("status") == "error":
                    code = res.get("code")
                    if code == "offer_expired" and entry["stage"] == "pending" and not entry["terminal"]:
                        try:
                            await asyncio.wait_for(entry["offered_event"].wait(), timeout=2.0)
                        except asyncio.TimeoutError:
                            pass
                    if not (entry["stage"] == "offered" or entry["terminal"]):
                        raise CallActionError(_err_text(code, "接听失败"))
                if entry["stage"] == "pending" and not entry["terminal"]:
                    try:
                        await asyncio.wait_for(entry["offered_event"].wait(), timeout=10.0)
                    except asyncio.TimeoutError:
                        raise CallActionError("等待来电就绪超时") from None
            if entry["terminal"] or entry["stage"] != "offered":
                raise CallActionError("来电已结束或已在其他设备处理")

            res = await self._command({"type": "call.answer", "callId": call_id, "expectedRevision": entry["revision"]})
            if res.get("status") == "error" and res.get("code") == "media_not_ready" and not readied:
                await self._ready({"kind": "all"})
                readied = True
                res = await self._command({"type": "call.answer", "callId": call_id,
                                           "expectedRevision": entry["revision"]})
            if res.get("status") == "error" and res.get("code") == "stale" and entry["stage"] == "offered":
                res = await self._command({"type": "call.answer", "callId": call_id,
                                           "expectedRevision": entry["revision"]})
            if res.get("status") not in ("accepted", "replayed"):
                code = res.get("code")
                if code in ("offer_expired", "already_claimed", "not_owner") and not entry["terminal"]:
                    entry["terminal"] = True
                    entry["stage"] = "ended"
                    entry["status_text"] = f"⚪ {_err_text(code, '接听失败')}"
                    await self.bridge._sync_call_msg(entry)
                raise CallActionError(_err_text(code, "接听失败"))
            owner_token = res.get("ownerToken")
            if not owner_token:
                raise CallActionError("agent-wechat 未返回通话凭证，无法接管音频")
            claimed = True
            entry["answered_by_tg"] = True
            entry["status_text"] = "🟡 正在接通…"
            if entry["stage"] == "offered":
                entry["stage"] = "connecting"
            self._start_session(entry, owner_token, readied, ws)
        finally:
            entry["answering"] = False
            if readied and not claimed:
                await self.withdraw_readiness()
        await self.bridge._sync_call_msg(entry)

    async def join(self, inv: dict, ws: web.WebSocketResponse | None) -> None:
        if inv.get("joining") or inv.get("joined_call"):
            raise CallPending("正在加入中…")
        if not self.can_join():
            raise CallActionError("当前 agent-wechat 不支持加入群语音通话")
        inv["joining"] = True
        readied = False
        claimed = False
        try:
            res = await self._ready({"kind": "outgoing"})
            if res.get("status") == "error":
                raise CallActionError(_err_text(res.get("code"), "加入失败"))
            readied = True
            res = await self._command({"type": "call.join", "invitationId": inv["invitation_id"],
                                       "invitationRevision": inv.get("revision", 0)})
            if res.get("status") not in ("accepted", "replayed") or not res.get("callId"):
                raise CallActionError(_err_text(res.get("code"), "加入失败"))
            owner_token = res.get("ownerToken")
            if not owner_token:
                raise CallActionError("agent-wechat 未返回通话凭证，无法接管音频")
            claimed = True
            call_id = res["callId"]
            entry = self.bridge._new_call_entry(
                call_id, kind="conference", call_type="voice", caller_id=inv.get("caller_id") or "",
                caller_name=inv.get("caller_name") or "", thread_id=inv.get("group_id") or "",
                stage="connecting", revision=0,
            )
            entry.update(chat_id=inv.get("chat_id"), msg_id=inv.get("msg_id"), answered_by_tg=True,
                         status_text="🟡 正在加入…")
            token = self.token_for_invite(inv)
            entry["web_token"] = token
            self.tokens[token] = ("call", call_id)
            inv["joined_call"] = call_id
            self._start_session(entry, owner_token, readied, ws)
            await self.bridge._replay_orphan_call_state(call_id)
        finally:
            inv["joining"] = False
            if readied and not claimed:
                await self.withdraw_readiness()
        await self.bridge._sync_call_msg(entry)

    async def dial(self, req: dict, ws: web.WebSocketResponse | None) -> None:
        """Place the call a /call request asked for (one contact, or a group call); this page then owns its audio."""
        if req.get("dialing") or req.get("call_id"):
            raise CallPending("正在呼叫…")
        if not req.get("live"):
            raise CallActionError("呼叫已取消或已过期，请重新发送 /call")
        conf = req.get("kind") == "conference"
        if not self.can_dial(conf):
            raise CallActionError("当前 agent-wechat 不支持发起微信群语音通话" if conf
                                  else "当前 agent-wechat 不支持发起微信语音通话")
        invitees = list(req.get("selected") or ()) if conf else []
        if conf and not invitees:
            raise CallActionError("还没有选择要邀请的群成员")
        if conf and len(invitees) > MAX_INVITEES:
            raise CallActionError(f"一次最多邀请 {MAX_INVITEES} 人")
        if conf:
            target = {"kind": "conference", "groupId": req["thread_id"], "invitees": invitees}
        else:
            target = {"kind": "direct", "contactId": req["contact_id"]}
        req["dialing"] = True
        readied = False
        claimed = False
        entry = None
        try:
            res = await self._ready({"kind": "outgoing"})
            if res.get("status") == "error":
                raise CallActionError(_err_text(res.get("code"), "呼叫失败"))
            readied = True
            res = await self._command({"type": "call.dial", "callType": "voice", "target": target})
            if res.get("status") not in ("accepted", "replayed") or not res.get("callId"):
                raise CallActionError(_err_text(res.get("code"), "呼叫失败"))
            owner_token = res.get("ownerToken")
            if not owner_token:
                raise CallActionError("agent-wechat 未返回通话凭证，无法接管音频")
            claimed = True
            call_id = res["callId"]
            entry = self.bridge._new_call_entry(
                call_id, kind="conference" if conf else "direct", call_type="voice",
                caller_id=req["contact_id"], caller_name=req.get("name") or req["contact_id"],
                thread_id=req["thread_id"], stage="dialing", revision=0,
            )
            entry.update(direction="outgoing", chat_id=req.get("chat_id"), msg_id=req.get("msg_id"),
                         answered_by_tg=True, status_text="📞 正在发起群通话…" if conf else "📞 正在呼叫…")
            if conf:
                names = req.get("names") or {}
                entry["invitees"] = {cid: names.get(cid) or cid for cid in invitees}
                entry["dial_request_id"] = res.get("requestId") or ""
            token = self.token_for_dial(req)
            entry["web_token"] = token
            self.tokens[token] = ("call", call_id)
            req["call_id"] = call_id
            self.bridge._finish_dial_request(req)
            self._start_session(entry, owner_token, readied, ws)
            await self.bridge._replay_orphan_call_state(call_id)
        finally:
            req["dialing"] = False
            if readied and not claimed:
                await self.withdraw_readiness()
        await self.bridge._sync_call_msg(entry)

    def _start_session(self, entry: dict, owner_token: str, readied: bool, ws) -> CallSession:
        sess = CallSession(self, entry, owner_token, readied)
        if ws is not None:
            sess.attach(ws)
        self.sessions[entry["call_id"]] = sess
        sess.task = asyncio.create_task(sess.run())
        return sess

    @staticmethod
    def _ringing_out(entry: dict) -> bool:
        """An outgoing call the other side has not picked up yet: it is cancelled, not hung up."""
        return entry.get("direction") == "outgoing" and entry["stage"] in ("dialing", "ringing")

    async def _end_call(self, entry: dict) -> dict:
        cmd = "call.cancel" if self._ringing_out(entry) else "call.hangup"
        return await self._command({"type": cmd, "callId": entry["call_id"], "expectedRevision": entry["revision"]})

    async def hangup(self, entry: dict, *, quiet: bool = False, status: str | None = None) -> None:
        if entry["terminal"]:
            return
        if status is None:
            status = "📴 已在 Telegram 取消呼叫" if self._ringing_out(entry) else "📴 已在 Telegram 挂断"
        try:
            res = await self._end_call(entry)
            if res.get("status") == "error" and res.get("code") == "stale" and not entry["terminal"]:
                res = await self._end_call(entry)
        except CallActionError:
            if quiet:
                return
            raise
        if res.get("status") in ("accepted", "replayed"):
            if not entry["terminal"]:
                entry["hungup_by_tg"] = True
                entry["status_text"] = status
            await self.bridge._sync_call_msg(entry)
            return
        if quiet:
            log.info("Hangup of %s failed: %s", entry["call_id"], res.get("code"))
            return
        raise CallActionError(_err_text(res.get("code"), "挂断失败"))

    async def set_mute(self, sess: CallSession, muted: bool) -> None:
        res = await self._command({"type": "call.mute", "callId": sess.call_id, "muted": bool(muted)})
        if res.get("status") not in ("accepted", "replayed"):
            raise CallActionError(_err_text(res.get("code"), "静音切换失败"))
        sess.muted = bool(muted)
        await self.push_state(sess.entry)

    # ------------------------------------------------------------------ page state
    def _state(self, token: str, ws: web.WebSocketResponse | None) -> dict:
        kind, ref = self.tokens.get(token, ("", ""))
        st: dict = {"type": "state", "phase": "ended", "title": "微信通话", "name": "", "status": "通话已结束或链接已失效",
                    "mine": False, "muted": False, "elapsed": None, "rate": 16000, "frame": 320,
                    "answerable": False, "joinable": False, "dialable": False, "group": False}
        if kind == "dial":
            req = self.bridge._dial_requests.get(ref)
            if req is not None:
                conf = req.get("kind") == "conference"
                st.update(title="微信群语音通话" if conf else "微信语音通话", name=req.get("name") or "",
                          status=req.get("status_text") or "", group=conf)
                if req.get("live"):
                    # The request's own status is the Telegram card's instruction; the page says it its own way.
                    st["phase"] = "dial"
                    if conf:
                        n = len(req.get("selected") or ())
                        st["status"] = f"将邀请 {n} 人" if n else "尚未选择成员"
                        st["dialable"] = self.can_dial(True) and 0 < n <= MAX_INVITEES
                        st["note"] = ("当前 agent-wechat 不支持发起群语音通话" if not self.can_dial(True)
                                      else "点击「呼叫」并允许使用麦克风后发起群通话" if st["dialable"]
                                      else "请回到 Telegram 消息里勾选要邀请的成员")
                    else:
                        st["status"] = "准备就绪"
                        st["dialable"] = self.can_dial()
                        st["note"] = ("点击「呼叫」并允许使用麦克风后才会拨出" if st["dialable"]
                                      else "当前 agent-wechat 不支持发起微信语音通话")
            return st
        if kind == "invite":
            inv = self.bridge._conf_invites.get(ref)
            if inv is not None:
                st.update(phase="invite", title="微信群语音通话邀请", name=inv.get("group_name") or "",
                          caller=inv.get("caller_name") or "", status=inv.get("status_text") or "🔔 邀请中…",
                          joinable=self.can_join())
            return st
        entry = self.bridge._active_calls.get(ref) if kind == "call" else None
        if entry is None:
            return st
        conf = entry["kind"] == "conference"
        if conf:
            title = "微信群语音通话"
        elif entry.get("direction") == "outgoing":
            title = "微信语音通话"
        else:
            title = "微信视频来电" if entry.get("call_type") == "video" else "微信语音来电"
        st.update(title=title, name=entry.get("caller_name") or "", status=entry.get("status_text") or "",
                  group=conf and entry.get("direction") == "outgoing")
        if entry.get("connected_at"):
            st["elapsed"] = max(0, int(time.time() - entry["connected_at"]))
        sess = self.sessions.get(entry["call_id"])
        if entry["terminal"]:
            st["phase"] = "ended"
        elif sess is not None and not sess.closed:
            phase = "calling" if self._ringing_out(entry) else ("active" if sess.attached else "connecting")
            st.update(phase=phase, mine=ws is not None and sess.active_ws is ws,
                      muted=sess.muted, rate=sess.rate, frame=sess.frame)
        elif entry["stage"] in ("pending", "offered"):
            st.update(phase="ringing", answerable=self.can_answer(entry))
        else:
            st["phase"] = "elsewhere"
        return st

    async def push_state(self, entry: dict) -> None:
        if token := entry.get("web_token"):
            await self._push_token(token)

    async def _push_token(self, token: str) -> None:
        for ws in list(self.clients.get(token, ())):
            await _safe_send(ws, self._state(token, ws))

    # ------------------------------------------------------------------ HTTP / WebSocket
    def make_app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024)
        p = self.prefix
        app.router.add_get(p + "/", self._page)
        if p:
            app.router.add_get(p, self._page)
        app.router.add_get(p + "/call/{token}", self._page)
        app.router.add_get(p + "/read/{aid}", self._read)
        app.router.add_get(p + "/ws", self._ws)
        app.router.add_get(p + "/healthz", self._health)
        return app

    async def start(self) -> None:
        if self._runner is not None:
            return
        runner = web.AppRunner(self.make_app(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        self._runner = runner
        self.bound_port = next((addr[1] for addr in runner.addresses if isinstance(addr, tuple)), self.port)
        log.info("Call answering page listening on %s:%s (public URL %s)", self.host, self.port, self.base_url)

    async def stop(self) -> None:
        for sess in list(self.sessions.values()):
            await sess.close()
        for task in [*self._tasks, *self._article_fetches.values()]:
            task.cancel()
        await self.article_fetcher.close()
        for conns in list(self.clients.values()):
            for ws in list(conns):
                await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _health(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def _page(self, request: web.Request) -> web.Response:
        start = request.query.get("tgWebAppStartParam", "")
        if start.startswith(READER_PREFIX):  # reader links share the Mini App's single URL
            return await self._reader_response(start[len(READER_PREFIX):])
        cfg = json.dumps({"base": self.prefix}, ensure_ascii=False)
        return web.Response(
            text=PAGE_HTML.replace("__CFG__", cfg).replace("__WORKLET__", json.dumps(WORKLET_JS)),
            content_type="text/html", charset="utf-8",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                     "X-Content-Type-Options": "nosniff"},
        )

    async def _read(self, request: web.Request) -> web.Response:
        retry = request.query.get("retry", "") in ("1", "true", "yes")
        return await self._reader_response(request.match_info["aid"], force_refresh=retry)

    async def _reader_response(self, aid: str, *, force_refresh: bool = False) -> web.Response:
        url = self.bridge.store.article_url(aid) if _ARTICLE_ID_RE.fullmatch(aid) else None
        nonce = secrets.token_urlsafe(12)
        status = 200
        if url is None:
            page = articles.render_page(None, error="文章链接已失效", nonce=nonce)
            status = 404
        elif self.reader_kind(url) in ("link", None):  # nothing to render: open the page itself, in place
            page = articles.render_redirect_page(url, reason="正在打开链接…", nonce=nonce, open_label="立即打开")
        else:
            retry_url = f"{self.prefix}/read/{aid}?retry=1"
            result = await self._load_article(url, force_refresh=force_refresh)
            if isinstance(result, articles.Article):
                # embedded videos hand over to the official player page; everything else is rendered here
                page = douyin.render_player(result, nonce=nonce) or bilibili.render_player(result, nonce=nonce)
                if page is None:
                    page = articles.render_page(result, nonce=nonce, retry_url=retry_url)
            elif result.fallback_original:
                page = articles.render_redirect_page(url, reason=str(result), retry_url=retry_url, nonce=nonce)
                status = 200
            else:
                page = articles.render_page(None, error=str(result), source_url=url,
                                            nonce=nonce, retry_url=retry_url)
                status = 502
        return web.Response(
            text=page, status=status, content_type="text/html", charset="utf-8",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                     "Content-Security-Policy": (
                         "default-src 'none'; img-src https: data:; media-src https:; font-src https: data:; "
                         f"frame-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; "
                         "base-uri 'none'; form-action 'none'")},
        )

    async def _load_article(self, url: str, *, force_refresh: bool = False) -> articles.Article | articles.ArticleError:
        if force_refresh:
            self.article_cache.delete(url)
            self.bridge.store.delete_cached_article(url)
        else:
            cached = self.article_cache.get(url)
            if cached is not None:
                return cached
            stored = self.bridge.store.get_cached_article(url)
            if stored is not None:
                art = articles.Article(
                    url=stored["url"], title=stored["title"], account=stored["account"],
                    author=stored["author"], published=stored["published"], cover=stored["cover"],
                    description=stored["description"], content=stored["content"],
                )
                self.article_cache.put(url, art)
                return art
        task = self._article_fetches.get(url)
        if task is None:  # one download per article even if the button is tapped repeatedly
            task = asyncio.create_task(self._fetch_article(url))
            self._article_fetches[url] = task
            task.add_done_callback(lambda _t: self._article_fetches.pop(url, None))
        return await asyncio.shield(task)  # a closed page must not cancel the shared download

    async def _fetch_article(self, url: str) -> articles.Article | articles.ArticleError:
        try:
            with articles.use_fetcher(self.article_fetcher):
                result: articles.Article | articles.ArticleError = await articles.fetch_article(url)
        except articles.ArticleError as e:
            log.info("Reader could not load %s: [%s] %s", url, e.code, e)
            result = e
        except Exception as e:
            log.exception("Reader fetch of %s failed", url)
            result = articles.ArticleError(f"加载失败（{type(e).__name__}）")
        self.article_cache.put(url, result)
        if isinstance(result, articles.Article):
            self.bridge.store.save_cached_article(url, {
                "url": result.url, "title": result.title, "account": result.account,
                "author": result.author, "published": result.published, "cover": result.cover,
                "description": result.description, "content": result.content,
            })
        return result

    def _authorize(self, msg: dict) -> tuple[str | None, str]:
        token = str(msg.get("token") or "")
        if not token or token not in self.tokens:
            return None, "链接已失效或通话已结束"
        init_data = str(msg.get("initData") or "")
        if init_data:
            uid = verify_init_data(init_data, self.bridge.cfg.telegram.bot_token)
            if uid is None:
                return None, "Telegram 身份校验失败，请从机器人消息重新打开"
            if uid != self.bridge.owner_id:
                return None, "只有桥接账号本人可以接听"
        return token, ""

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=MAX_PCM_CHUNK + 1024)
        await ws.prepare(request)
        token = None
        try:
            try:
                first = await ws.receive(timeout=10.0)
            except asyncio.TimeoutError:
                await ws.close(code=4408, message=b"auth timeout")
                return ws
            if first.type != WSMsgType.TEXT:
                await ws.close(code=4400, message=b"auth required")
                return ws
            try:
                hello = json.loads(first.data)
            except ValueError:
                hello = {}
            token, why = self._authorize(hello if isinstance(hello, dict) else {})
            if token is None:
                await _safe_send(ws, {"type": "fatal", "message": why})
                await ws.close(code=4403, message=b"forbidden")
                return ws
            self.clients.setdefault(token, set()).add(ws)
            await _safe_send(ws, self._state(token, ws))
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    sess = self._session_for(token)
                    if sess is not None:
                        await sess.from_page(ws, msg.data)
                elif msg.type == WSMsgType.TEXT:
                    try:
                        cmd = json.loads(msg.data)
                    except ValueError:
                        continue
                    if isinstance(cmd, dict):
                        self._spawn(self._action(token, ws, cmd))
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            if token is not None:
                conns = self.clients.get(token)
                if conns is not None:
                    conns.discard(ws)
                    if not conns:
                        self.clients.pop(token, None)
                sess = self._session_for(token)
                if sess is not None:
                    sess.detach(ws)
        return ws

    def _session_for(self, token: str) -> CallSession | None:
        kind, ref = self.tokens.get(token, ("", ""))
        sess = self.sessions.get(ref) if kind == "call" else None
        return sess if sess is not None and not sess.closed else None

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _action(self, token: str, ws: web.WebSocketResponse, cmd: dict) -> None:
        action = cmd.get("type")
        kind, ref = self.tokens.get(token, ("", ""))
        try:
            if kind == "invite":
                inv = self.bridge._conf_invites.get(ref)
                if inv is None:
                    raise CallActionError("邀请已结束或已过期")
                if action == "join":
                    await self.join(inv, ws)
                return
            if kind == "dial":
                req = self.bridge._dial_requests.get(ref)
                if req is None:
                    raise CallActionError("呼叫已取消或已过期，请重新发送 /call")
                if action == "dial":
                    await self.dial(req, ws)
                return
            entry = self.bridge._active_calls.get(ref) if kind == "call" else None
            if entry is None:
                raise CallActionError("通话已结束")
            sess = self._session_for(token)
            if action == "answer":
                await self.answer(entry, ws)
            elif action == "reject":
                if entry.get("rejecting") or entry.get("answering"):
                    raise CallPending("正在处理中…")
                if entry["terminal"] or entry["stage"] not in ("pending", "offered"):
                    raise CallActionError("来电已结束或已处理")
                await self.bridge._reject_call(entry)
            elif action == "attach":
                if sess is None:
                    raise CallActionError("通话未由 Telegram 接管或已结束")
                prev = sess.active_ws
                sess.attach(ws)
                if prev is not None and prev is not ws:
                    await _safe_send(prev, self._state(token, prev))
                await self.push_state(entry)
            elif action == "hangup":
                if entry.get("hanging_up"):
                    raise CallPending("正在挂断…")
                entry["hanging_up"] = True
                try:
                    await self.hangup(entry)
                finally:
                    entry["hanging_up"] = False
            elif action == "mute":
                if sess is None:
                    raise CallActionError("通话未由 Telegram 接管或已结束")
                await self.set_mute(sess, bool(cmd.get("muted")))
        except CallPending:
            await _safe_send(ws, self._state(token, ws))
        except CallActionError as e:
            await _safe_send(ws, {"type": "error", "message": str(e)})
            await _safe_send(ws, self._state(token, ws))
        except Exception:
            log.exception("Call page action %s failed", action)
            await _safe_send(ws, {"type": "error", "message": "操作失败，请查看桥接日志"})


async def _safe_send(ws: web.WebSocketResponse, obj: dict) -> None:
    if ws.closed:
        return
    try:
        await ws.send_json(obj)
    except (ConnectionError, RuntimeError):
        pass


# ---------------------------------------------------------------------- browser side
# AudioWorklet: captures the microphone, downsamples it to the call rate in 20 ms PCM16 frames, and plays every
# receive track from its own jitter buffer (resampled to the device rate and mixed).
WORKLET_JS = r"""
class BridgeIO extends AudioWorkletProcessor {
  constructor(o) {
    super();
    const p = (o && o.processorOptions) || {};
    this.setRate(p.net || 16000, p.frame || 320);
    this.port.onmessage = (e) => {
      const d = e.data;
      if (d.t === 'pcm') this.push(d.id, d.pcm);
      else if (d.t === 'drop') this.tracks.delete(d.id);
      else if (d.t === 'rate') this.setRate(d.net, d.frame);
    };
  }
  setRate(net, frame) {
    this.net = net; this.frame = frame; this.step = net / sampleRate;
    this.acc = 0; this.sum = 0; this.cnt = 0; this.buf = new Int16Array(frame); this.n = 0;
    this.tracks = new Map();
  }
  push(id, pcm) {
    let t = this.tracks.get(id);
    if (!t) { const size = this.net * 2; t = {b: new Float32Array(size), size, w: 0, r: 0, on: false}; this.tracks.set(id, t); }
    for (let i = 0; i < pcm.length; i++) { t.b[t.w % t.size] = pcm[i] / 32768; t.w++; }
    if (t.w - t.r > this.net * 0.4) t.r = t.w - this.net * 0.12;  // cap latency
  }
  process(inputs, outputs) {
    const inp = inputs[0] && inputs[0][0];
    if (inp) {
      for (let i = 0; i < inp.length; i++) {
        this.sum += inp[i]; this.cnt++; this.acc += this.step;
        if (this.acc >= 1) {
          this.acc -= 1;
          let v = this.sum / this.cnt; this.sum = 0; this.cnt = 0;
          v = v < -1 ? -1 : v > 1 ? 1 : v;
          this.buf[this.n++] = v < 0 ? v * 32768 : v * 32767;
          if (this.n === this.frame) {
            const out = this.buf; this.buf = new Int16Array(this.frame); this.n = 0;
            this.port.postMessage(out.buffer, [out.buffer]);
          }
        }
      }
    }
    const out = outputs[0], ch = out && out[0];
    if (!ch) return true;
    ch.fill(0);
    for (const t of this.tracks.values()) {
      if (!t.on) { if (t.w - t.r >= this.net * 0.06) t.on = true; else continue; }
      for (let i = 0; i < ch.length; i++) {
        if (t.w - t.r < 2) { t.on = false; break; }
        const i0 = Math.floor(t.r), f = t.r - i0;
        const a = t.b[i0 % t.size], b = t.b[(i0 + 1) % t.size];
        ch[i] += a + (b - a) * f; t.r += this.step;
      }
    }
    for (let i = 0; i < ch.length; i++) { const v = ch[i]; ch[i] = v > 1 ? 1 : v < -1 ? -1 : v; }
    for (let c = 1; c < out.length; c++) out[c].set(ch);
    return true;
  }
}
registerProcessor('bridge-io', BridgeIO);
"""

PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex">
<meta name="theme-color" content="#14161b">
<title>微信通话</title>
<script>
// Article reader links share this Mini App URL. When the start parameter only arrives in the URL fragment (so the
// server could not route it), switch to the reader before the call UI is ever painted, on the reader's background.
(function () {
  try {
    if (/\/(call|read)\//.test(location.pathname)) return;
    var hp = new URLSearchParams(location.hash.slice(1));
    var sp = new URLSearchParams(hp.get('tgWebAppData') || '').get('start_param') || hp.get('tgWebAppStartParam') ||
             new URLSearchParams(location.search).get('tgWebAppStartParam') || '';
    if (sp.indexOf('r_') !== 0) return;
    window.__wxReader = true;
    var bg = '';
    try { bg = JSON.parse(hp.get('tgWebAppThemeParams') || '{}').bg_color || ''; } catch (e) {}
    if (!/^#[0-9a-fA-F]{3,8}$/.test(bg)) {
      bg = window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? '#17181c' : '#ffffff';
    }
    document.documentElement.style.cssText = 'background:' + bg + ';color-scheme:light dark;visibility:hidden';
    var base = (__CFG__).base;
    location.replace(location.origin + base + '/read/' + encodeURIComponent(sp.slice(2)) + location.hash);
  } catch (e) {}
})();
</script>
<style>
:root{color-scheme:dark;--bg:#14161b;--fg:#f4f5f7;--hint:#8d929c;--glass:rgba(255,255,255,.12);
--green:#07c160;--red:#f2484b;--amber:#f5a524;--gray:#7b808a}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);overflow:hidden;
font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans CJK SC","Microsoft YaHei",sans-serif;
-webkit-user-select:none;user-select:none}
body{background:radial-gradient(130% 70% at 50% -10%,#1f3a2b 0%,rgba(20,22,27,0) 60%),var(--bg)}
main{height:100%;display:flex;flex-direction:column;align-items:center;text-align:center;
padding:max(48px,env(safe-area-inset-top)) 24px calc(40px + env(safe-area-inset-bottom))}
.info{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;width:100%;
padding-bottom:6vh}
.avatar{position:relative;width:112px;height:112px;border-radius:50%;display:flex;align-items:center;
justify-content:center;font-size:46px;font-weight:600;color:#fff;margin-bottom:14px;
background:linear-gradient(145deg,#10d673,#06a352);box-shadow:0 10px 30px rgba(7,193,96,.28)}
.avatar svg{width:52px;height:52px}
.avatar::before,.avatar::after{content:"";position:absolute;inset:0;border-radius:50%;border:2px solid var(--green);
opacity:0;pointer-events:none}
.ringing .avatar::before{animation:ring 2s ease-out infinite}
.ringing .avatar::after{animation:ring 2s ease-out 1s infinite}
@keyframes ring{0%{transform:scale(1);opacity:.55}100%{transform:scale(1.55);opacity:0}}
.ended .avatar{filter:grayscale(.85);opacity:.7;box-shadow:none}
.title{font-size:14px;color:var(--hint);letter-spacing:.5px}
.name{font-size:28px;font-weight:600;line-height:1.25;max-width:100%;overflow-wrap:anywhere}
.status{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:16px;
background:rgba(255,255,255,.07);font-size:14px;line-height:20px;color:#c9ccd2;min-height:32px;max-width:100%;
text-align:left}
.status:empty{visibility:hidden}
.dot{flex:none;width:8px;height:8px;border-radius:50%;background:var(--gray)}
.tone-ring .dot{background:var(--amber);animation:blink 1s ease-in-out infinite}
.tone-wait .dot{background:var(--amber);animation:blink 1.4s ease-in-out infinite}
.tone-live .dot{background:var(--green)}
.tone-err .dot{background:var(--red)}
@keyframes blink{50%{opacity:.25}}
.timer{font-size:20px;font-variant-numeric:tabular-nums;color:var(--fg);min-height:26px;letter-spacing:1px}
.hint{font-size:13px;color:var(--hint);max-width:320px;line-height:1.55;min-height:20px}
.hint.err{color:#ff7b7d}
.hint a{color:#5eb5f7;text-decoration:none;font-weight:500}
.actions{display:flex;justify-content:center;gap:clamp(40px,16vw,72px);min-height:104px;width:100%}
.btn{display:flex;flex-direction:column;align-items:center;gap:10px;font-size:13px;color:#c9ccd2;
background:none;border:0;padding:0;font-family:inherit;cursor:pointer;touch-action:manipulation}
.btn .c{position:relative;width:72px;height:72px;border-radius:50%;display:flex;align-items:center;
justify-content:center;color:#fff;transition:transform .12s ease,opacity .2s,background .2s}
.btn svg{width:32px;height:32px}
.btn:active:not(:disabled) .c{transform:scale(.9)}
.btn:disabled{cursor:default}
.btn:disabled:not(.busy) .c{opacity:.45}
.green .c{background:var(--green);box-shadow:0 8px 22px rgba(7,193,96,.35)}
.red .c{background:var(--red);box-shadow:0 8px 22px rgba(242,72,75,.3)}
.glass .c{background:var(--glass);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px)}
.glass.on .c{background:#f4f5f7;color:#14161b}
.busy .c svg{opacity:.35}
.busy .c::after{content:"";position:absolute;inset:-6px;border-radius:50%;border:3px solid rgba(255,255,255,.25);
border-top-color:#fff;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<main id="main">
  <section class="info">
    <div class="avatar" id="avatar"></div>
    <div class="title" id="title">微信通话</div>
    <div class="name" id="name">正在连接…</div>
    <div class="status" id="status"></div>
    <div class="timer" id="timer"></div>
    <div class="hint" id="hint"></div>
  </section>
  <section class="actions" id="actions"></section>
</main>
<script>
(function () {
  if (window.__wxReader) return;  // already on the way to the article reader (see <head>)
  const CFG = __CFG__;
  const WORKLET = __WORKLET__;
  const PENDING_MS = 30000;
  const $ = (id) => document.getElementById(id);

  // Minimal Telegram Mini App bridge: reads the signed initData from the URL fragment and talks to the client
  // directly, so the page does not wait for telegram-web-app.js from telegram.org before it can render.
  const tg = (function () {
    const hp = {};
    try {
      location.hash.slice(1).split('&').forEach((kv) => {
        const i = kv.indexOf('=');
        if (i > 0) hp[decodeURIComponent(kv.slice(0, i))] = decodeURIComponent(kv.slice(i + 1));
      });
    } catch (e) {}
    let initData = hp.tgWebAppData || '';
    try {
      if (initData) sessionStorage.setItem('tgWebAppData', initData);
      else initData = sessionStorage.getItem('tgWebAppData') || '';
    } catch (e) {}
    if (!initData) return null;
    function post(type, data) {
      try {
        if (window.TelegramWebviewProxy && window.TelegramWebviewProxy.postEvent) {
          window.TelegramWebviewProxy.postEvent(type, JSON.stringify(data || ''));
        } else if (window.external && 'notify' in window.external) {
          window.external.notify(JSON.stringify({eventType: type, eventData: data || ''}));
        } else if (window.parent !== window) {
          window.parent.postMessage(JSON.stringify({eventType: type, eventData: data || ''}), '*');
        }
      } catch (e) {}
    }
    return {initData: initData, startParam: new URLSearchParams(initData).get('start_param') || hp.tgWebAppStartParam || '',
            post: post};
  })();
  if (tg) {
    tg.post('web_app_set_header_color', {color: '#14161b'});
    tg.post('web_app_set_background_color', {color: '#14161b'});
    tg.post('web_app_setup_swipe_behavior', {allow_vertical_swipe: false});
    tg.post('web_app_expand');
    tg.post('web_app_ready');
  }
  const haptic = (style) => tg && tg.post('web_app_trigger_haptic_feedback', {type: 'impact', impact_style: style || 'medium'});

  const ICON = {
    phone: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M6.62 10.79a15.1 15.1 0 0 0 6.59 6.59l2.2-2.2a1 1 0 0 1 1.02-.24c1.12.37 2.33.57 3.57.57a1 1 0 0 1 1 1V20a1 1 0 0 1-1 1A17 17 0 0 1 3 4a1 1 0 0 1 1-1h3.5a1 1 0 0 1 1 1c0 1.25.2 2.45.57 3.57a1 1 0 0 1-.25 1.02l-2.2 2.2z"/></svg>',
    hangup: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 9c-1.6 0-3.15.25-4.6.72v3.1c0 .39-.23.74-.56.9-.98.49-1.87 1.12-2.66 1.85-.18.18-.43.28-.7.28-.28 0-.53-.11-.71-.29L.29 13.08a.96.96 0 0 1-.29-.7c0-.28.11-.53.29-.71C3.34 8.78 7.46 7 12 7s8.66 1.78 11.71 4.67c.18.18.29.43.29.71s-.11.53-.29.71l-2.48 2.48c-.18.18-.43.29-.71.29-.27 0-.52-.11-.7-.28a11.3 11.3 0 0 0-2.67-1.85 1 1 0 0 1-.56-.9v-3.1C15.15 9.25 13.6 9 12 9z"/></svg>',
    mic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2.5" width="6" height="12" rx="3"/><path d="M5 10.5v.5a7 7 0 0 0 14 0v-.5M12 18v3.5"/></svg>',
    micOff: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 9.3V5.5a3 3 0 0 0-5.7-1.3M9 9v2.5a3 3 0 0 0 4.9 2.3M5 10.5v.5a7 7 0 0 0 11.9 5M19 10.5v.5a7 7 0 0 1-.5 2.6M12 18v3.5M3 3l18 18"/></svg>',
    headset: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 18v-5.5a8.5 8.5 0 0 1 17 0V18"/><path d="M20.5 18.5a2 2 0 0 1-2 2H17v-6.5h3.5zM3.5 18.5a2 2 0 0 0 2 2H7v-6.5H3.5z"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    group: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18 14.2a6.5 6.5 0 0 1 3.5 5.8"/></svg>',
  };
  const PENDING_TEXT = {answer: '正在接听…', join: '正在加入…', reject: '正在拒接…', hangup: '正在挂断…',
                        attach: '正在切换到此设备…', dial: '正在呼叫…'};
  // Phases in which this browser may own the call's audio.
  const LIVE = {calling: true, connecting: true, active: true};

  function findToken() {
    const m = location.pathname.match(/\/call\/([A-Za-z0-9_-]{8,})\/?$/);
    if (m) return m[1];
    if (tg && tg.startParam) return tg.startParam;
    const q = new URLSearchParams(location.search);
    return q.get('tgWebAppStartParam') || q.get('t') || '';
  }
  const token = findToken();
  if (token.indexOf('r_') === 0) {  // article reader link opened through the shared Mini App URL
    location.replace(location.origin + CFG.base + '/read/' + encodeURIComponent(token.slice(2)) + location.hash);
    return;
  }
  const pageUrl = location.origin + CFG.base + '/call/' + token;
  const workletUrl = URL.createObjectURL(new Blob([WORKLET], {type: 'application/javascript'}));

  let ws = null, st = null, audio = null, audioStarting = null, ended = false, fatal = false, retry = 0, reattach = false;
  let timerBase = null, timerId = null, wakeLock = null, pending = null, lastPhase = null, micGranted = false;
  let closingConfirm = null;

  try {
    navigator.permissions.query({name: 'microphone'}).then((p) => {
      micGranted = p.state === 'granted';
      p.onchange = () => { micGranted = p.state === 'granted'; };
    }).catch(() => {});
  } catch (e) {}

  function hint(text, isErr) {
    const h = $('hint');
    h.className = 'hint' + (isErr ? ' err' : '');
    h.textContent = text || '';
  }

  function browserFallback() {
    if (!tg) return;
    const a = document.createElement('a');
    a.href = pageUrl; a.textContent = '在浏览器中打开';
    a.onclick = (e) => { e.preventDefault(); tg.post('web_app_open_link', {url: pageUrl}); };
    const h = $('hint');
    h.appendChild(document.createTextNode(' '));
    h.appendChild(a);
  }

  // Creates the AudioContext synchronously (inside the tap) so iOS lets it play, then loads the
  // microphone and the audio worklet in parallel.
  function startAudio(rate, frame) {
    if (audio) {
      if (audio.rate !== rate) { audio.node.port.postMessage({t: 'rate', net: rate, frame: frame}); audio.rate = rate; }
      return Promise.resolve(audio);
    }
    if (audioStarting) return audioStarting;
    if (!window.isSecureContext) return Promise.reject(new Error('页面必须通过 HTTPS 打开才能使用麦克风'));
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return Promise.reject(new Error('当前环境不支持麦克风'));
    // iOS: a two-way call needs the play-and-record session, or Web Audio follows the silent switch / gets
    // interrupted once the microphone starts.
    try { if (navigator.audioSession) navigator.audioSession.type = 'play-and-record'; } catch (e) {}
    const AC = window.AudioContext || window.webkitAudioContext;
    const ctx = new AC({latencyHint: 'interactive'});
    ctx.resume().catch(() => {});
    if (!ctx.audioWorklet) { ctx.close(); return Promise.reject(new Error('当前浏览器不支持 AudioWorklet')); }
    const gum = navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true,
      autoGainControl: true, channelCount: 1}, video: false});
    const mod = ctx.audioWorklet.addModule(workletUrl);
    audioStarting = (async () => {
      let stream = null;
      try {
        stream = await gum;
        await mod;
        const node = new AudioWorkletNode(ctx, 'bridge-io', {numberOfInputs: 1, numberOfOutputs: 1,
          outputChannelCount: [1], processorOptions: {net: rate, frame: frame}});
        ctx.createMediaStreamSource(stream).connect(node);
        node.connect(ctx.destination);
        node.port.onmessage = (e) => {
          if (st && st.mine && !isMuted() && ws && ws.readyState === 1 && ws.bufferedAmount < 64000) ws.send(e.data);
        };
        // Never wait for resume(): on iOS the microphone starting can leave the context "interrupted" and the
        // promise pending indefinitely, which used to leave the call connected with no sound either way.
        audio = {ctx: ctx, stream: stream, node: node, rate: rate};
        micGranted = true;
        ctx.onstatechange = checkAudio;
        checkAudio();
        try {
          if (navigator.wakeLock) {
            navigator.wakeLock.request('screen').then((l) => {
              if (audio && audio.ctx === ctx) wakeLock = l; else l.release();
            }).catch(() => {});
          }
        } catch (e) {}
        return audio;
      } catch (e) {
        gum.then((s) => s.getTracks().forEach((t) => t.stop())).catch(() => {});
        try { ctx.close(); } catch (_) {}
        throw e;
      } finally {
        audioStarting = null;
      }
    })();
    return audioStarting;
  }

  // Keeps the AudioContext running: the system can suspend or interrupt it (iOS when the mic starts, another
  // app takes audio, the page is backgrounded). Resume quietly, and ask for a tap if that is not allowed.
  const SOUND_HINT = '声音被系统暂停了，轻点屏幕任意位置恢复';
  let soundHint = false, soundTimer = null;
  function checkAudio() {
    if (!audio) return;
    const s = audio.ctx.state;
    if (s === 'running') {
      if (soundHint) { soundHint = false; hint(''); }
      return;
    }
    if (s === 'closed') return;
    audio.ctx.resume().catch(() => {});
    clearTimeout(soundTimer);
    soundTimer = setTimeout(() => {
      if (audio && audio.ctx.state !== 'running' && audio.ctx.state !== 'closed') {
        soundHint = true;
        hint(SOUND_HINT, true);
      }
    }, 800);
  }
  ['touchend', 'click'].forEach((t) => document.addEventListener(t, () => {
    if (audio && audio.ctx.state !== 'running' && audio.ctx.state !== 'closed') audio.ctx.resume().then(checkAudio, () => {});
  }, true));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) checkAudio(); });

  function stopAudio() {
    clearTimeout(soundTimer);
    if (soundHint) { soundHint = false; hint(''); }
    if (!audio) return;
    try { audio.ctx.onstatechange = null; } catch (e) {}
    try { audio.stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
    try { audio.ctx.close(); } catch (e) {}
    audio = null;
    try { wakeLock && wakeLock.release(); } catch (e) {}
    wakeLock = null;
  }

  function send(obj) {
    if (ws && ws.readyState === 1) { ws.send(JSON.stringify(obj)); return true; }
    return false;
  }

  function isMuted() { return pending && pending.action === 'mute' ? pending.muted : !!(st && st.muted); }

  // Show the tap at once: the agent-wechat round trip (it drives the real WeChat client) can take seconds.
  function begin(action, extra) {
    if (pending || !st) return false;
    pending = Object.assign({action: action, phase: st.phase, at: Date.now()}, extra || {});
    const mine = pending;
    setTimeout(() => { if (pending === mine) { pending = null; render(); } }, PENDING_MS);
    haptic(action === 'hangup' || action === 'reject' ? 'heavy' : 'medium');
    hint('');
    render();
    return true;
  }

  function settle() {
    if (!pending || !st) return;
    const p = st.phase;
    if (pending.action === 'mute') { if (!!st.muted === pending.muted || !(p === 'active' || p === 'connecting')) pending = null; }
    else if (pending.action === 'attach') { if (st.mine || !LIVE[p]) pending = null; }
    else if (p !== pending.phase) pending = null;
  }

  function act(action, extra) {
    if (!begin(action, extra)) return;
    if (!send(Object.assign({type: action}, extra || {}))) { pending = null; hint('连接已断开，正在重连…', true); render(); }
  }

  async function withAudio(action) {
    if (!st || !begin(action)) return;
    const early = !!audio || micGranted;
    const started = startAudio(st.rate || 16000, st.frame || 320);
    if (early) send({type: action});
    try {
      await started;
      if (!early && !send({type: action})) { pending = null; hint('连接已断开，正在重连…', true); render(); }
    } catch (e) {
      const name = e && e.name;
      let msg = (e && e.message) || String(e);
      if (name === 'NotAllowedError' || name === 'SecurityError') msg = '没有麦克风权限，请允许 Telegram / 浏览器使用麦克风';
      else if (name === 'NotFoundError') msg = '没有找到麦克风';
      if (!early) pending = null;
      render();
      hint(msg, true);
      browserFallback();
    }
  }

  function button(cls, icon, label, onClick, action) {
    const b = document.createElement('button');
    const busy = pending && pending.action === action;
    b.className = 'btn ' + cls + (busy ? ' busy' : '');
    b.disabled = !!pending;
    b.innerHTML = '<span class="c">' + icon + '</span>';
    b.appendChild(document.createTextNode(label));
    b.onclick = onClick;
    return b;
  }

  function fmt(sec) {
    const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : String(m).padStart(2, '0')) + ':' + String(s).padStart(2, '0');
  }
  function tick() { $('timer').textContent = timerBase == null ? '' : fmt(Math.floor((Date.now() - timerBase) / 1000)); }

  const EMOJI = /^[\s\u2190-\u2BFF\uFE0F\u200D]*(?:[\uD83C-\uD83E][\uDC00-\uDFFF][\uFE0F\u200D\s]*)*/;
  function cleanStatus(s) { return String(s || '').replace(EMOJI, '').trim(); }

  function setStatus(text, tone) {
    const el = $('status');
    el.className = 'status tone-' + tone;
    el.innerHTML = '';
    if (!text) return;
    const d = document.createElement('i'); d.className = 'dot';
    el.appendChild(d);
    el.appendChild(document.createTextNode(text));
  }

  function setClosingConfirmation(on) {
    if (!tg || closingConfirm === on) return;
    closingConfirm = on;
    tg.post('web_app_setup_closing_behavior', {need_confirmation: on});
  }

  function render() {
    if (!st) return;
    const p = st.phase;
    if (p !== lastPhase) { if (lastPhase !== null) hint(''); lastPhase = p; }
    const person = st.name || st.caller || '';
    let title = st.title || '微信通话';
    if ((p === 'connecting' || p === 'active') && title === '微信语音来电') title = '微信语音通话';
    $('title').textContent = title;
    $('name').textContent = person;
    const av = $('avatar');
    const first = Array.from(person.trim())[0];
    const groupIcon = st.phase === 'invite' || st.group;
    if (groupIcon || !first) av.innerHTML = groupIcon ? ICON.group : ICON.phone;
    else av.textContent = first.toUpperCase();
    $('main').className = (p === 'ringing' || p === 'invite' || p === 'calling') ? 'ringing' : (p === 'ended' ? 'ended' : '');

    let text = cleanStatus(st.status), tone = 'off';
    if (p === 'ringing' || p === 'invite' || p === 'calling') tone = 'ring';
    else if (p === 'connecting') tone = 'wait';
    else if (p === 'active') tone = st.elapsed != null ? 'live' : 'wait';
    else if (p === 'dial' && st.dialable) tone = 'live';
    $('timer').hidden = p === 'dial';  // nothing to time yet: keep the hint right under the status
    if (p === 'invite' && st.caller) text = '发起人：' + st.caller + ' · ' + text;
    if (pending && PENDING_TEXT[pending.action]) {
      text = pending.action === 'hangup' && p === 'calling' ? '正在取消…' : PENDING_TEXT[pending.action];
      tone = 'wait';
    }
    setStatus(text, tone);

    if (st.elapsed != null && p !== 'ended') {
      timerBase = Date.now() - st.elapsed * 1000;
      if (!timerId) timerId = setInterval(tick, 1000);
    } else {
      timerBase = null;
      if (timerId) { clearInterval(timerId); timerId = null; }
    }
    tick();

    const a = $('actions');
    a.innerHTML = '';
    if (p === 'ringing') {
      a.appendChild(button('red', ICON.hangup, '拒接', () => act('reject'), 'reject'));
      if (st.answerable) a.appendChild(button('green', ICON.phone, '接听', () => withAudio('answer'), 'answer'));
      else if (!$('hint').textContent) hint('当前 agent-wechat 不支持在网页接听此通话');
    } else if (p === 'invite') {
      if (st.joinable) a.appendChild(button('green', ICON.phone, '加入', () => withAudio('join'), 'join'));
      else if (!$('hint').textContent) hint('当前 agent-wechat 不支持加入群语音通话');
    } else if (p === 'dial') {
      if (st.dialable) a.appendChild(button('green', ICON.phone, '呼叫', () => withAudio('dial'), 'dial'));
      if (!$('hint').classList.contains('err')) hint(st.note || '');
    } else if (p === 'calling') {
      if (!st.mine) a.appendChild(button('green', ICON.headset, '切换到此设备', () => withAudio('attach'), 'attach'));
      a.appendChild(button('red', ICON.hangup, '取消', () => act('hangup'), 'hangup'));
    } else if (p === 'connecting' || p === 'active') {
      if (st.mine) {
        const muted = isMuted();
        a.appendChild(button('glass' + (muted ? ' on' : ''), muted ? ICON.micOff : ICON.mic, muted ? '已静音' : '静音',
          () => act('mute', {muted: !muted}), 'mute'));
      } else {
        a.appendChild(button('green', ICON.headset, '在此接听', () => withAudio('attach'), 'attach'));
      }
      a.appendChild(button('red', ICON.hangup, '挂断', () => act('hangup'), 'hangup'));
      if (audio && st.rate && audio.rate !== st.rate) startAudio(st.rate, st.frame);
    } else if (tg) {
      a.appendChild(button('glass', ICON.close, '关闭', () => tg.post('web_app_close'), 'close'));
    }
    setClosingConfirmation(!!LIVE[p] && !!st.mine);
    if (p === 'ended' || p === 'elsewhere') {
      if (p === 'ended') ended = true;
      stopAudio();
    }
    if (soundHint) hint(SOUND_HINT, true);  // a phase change clears the hint; this one still applies
  }

  function connect() {
    if (!token) { $('name').textContent = '链接无效'; fatal = true; return; }
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(proto + '//' + location.host + CFG.base + '/ws');
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      retry = 0;
      ws.send(JSON.stringify({type: 'auth', token: token, initData: tg ? tg.initData : ''}));
    };
    ws.onmessage = (e) => {
      if (typeof e.data !== 'string') {
        if (!audio || e.data.byteLength < 6) return;
        const id = new DataView(e.data).getUint32(0, true);
        const pcm = new Int16Array(e.data.slice(4));
        audio.node.port.postMessage({t: 'pcm', id: id, pcm: pcm}, [pcm.buffer]);
        return;
      }
      let m; try { m = JSON.parse(e.data); } catch (_) { return; }
      if (m.type === 'state') {
        const wasMine = st && st.mine;
        st = m; settle(); render();
        if (!m.mine && !wasMine && audio && LIVE[m.phase] && reattach) {
          reattach = false; send({type: 'attach'});
        }
      } else if (m.type === 'error') {
        pending = null; render();
        hint(m.message, true);
        haptic('rigid');
      } else if (m.type === 'drop') {
        audio && audio.node.port.postMessage({t: 'drop', id: m.trackId});
      } else if (m.type === 'fatal') {
        fatal = true; pending = null;
        $('name').textContent = ''; setStatus(m.message, 'err'); $('actions').innerHTML = '';
      }
    };
    ws.onclose = () => {
      if (fatal || ended) return;
      if (audio) reattach = true;
      pending = null;
      setStatus('连接中断，正在重连…', 'wait');
      setTimeout(connect, Math.min(5000, 300 * Math.pow(2, retry++)));
    };
  }
  connect();
})();
</script>
</body>
</html>
"""
