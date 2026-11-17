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
import secrets
import struct
import time
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, quote, urlsplit

from aiohttp import WSMsgType, web

from .wechat import WeChatError

if TYPE_CHECKING:  # pragma: no cover
    from .core import Bridge

log = logging.getLogger(__name__)

# How long a connected call survives without any page holding its audio before it is hung up.
DETACH_GRACE_SECONDS = 20.0
# How long to wait for an answered call to reach "connected".
CONNECT_TIMEOUT_SECONDS = 45.0
INIT_DATA_MAX_AGE = 24 * 3600
MAX_PCM_CHUNK = 65536

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

    @property
    def attached(self) -> bool:
        return self.media is not None and not self.closed

    async def run(self) -> None:
        bridge = self.web.bridge
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + CONNECT_TIMEOUT_SECONDS
            while self.entry["stage"] != "connected":
                if self.entry["terminal"] or self.closed:
                    return
                remaining = deadline - loop.time()
                if remaining <= 0:
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
        self.tokens: dict[str, tuple[str, str]] = {}  # web token -> ("call", call_id) | ("invite", invitation_id)
        self.clients: dict[str, set[web.WebSocketResponse]] = {}  # web token -> open pages
        self.sessions: dict[str, CallSession] = {}  # call_id -> session
        self._tasks: set[asyncio.Task] = set()
        self._runner: web.AppRunner | None = None
        self.bound_port = self.port

    # ------------------------------------------------------------------ links / tokens
    def link(self, token: str) -> str:
        if self.app_name and getattr(self.bridge, "bot_me", None) and self.bridge.bot_me.username:
            return f"https://t.me/{self.bridge.bot_me.username}/{quote(self.app_name)}?startapp={token}"
        return f"{self.base_url}/call/{token}"

    def _new_token(self, kind: str, ref: str) -> str:
        self._prune_tokens()
        token = secrets.token_urlsafe(18)
        self.tokens[token] = (kind, ref)
        return token

    def _prune_tokens(self) -> None:
        if len(self.tokens) < 300:
            return
        for token, (kind, ref) in list(self.tokens.items()):
            live = (self.bridge._active_calls.get(ref) if kind == "call" else self.bridge._conf_invites.get(ref))
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
        if entry["terminal"] or entry["stage"] not in ("pending", "offered"):
            raise CallActionError("来电已结束或已在其他设备处理")
        if not self.can_answer(entry):
            raise CallActionError("当前 agent-wechat 不支持接听该通话（仅支持单聊语音来电）")
        if entry.get("answering") or entry.get("rejecting"):
            raise CallActionError("正在处理中…")
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
        if inv.get("joined_call"):
            raise CallActionError("已经加入该群通话")
        if not self.can_join():
            raise CallActionError("当前 agent-wechat 不支持加入群语音通话")
        if inv.get("joining"):
            raise CallActionError("正在加入中…")
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

    def _start_session(self, entry: dict, owner_token: str, readied: bool, ws) -> CallSession:
        sess = CallSession(self, entry, owner_token, readied)
        if ws is not None:
            sess.attach(ws)
        self.sessions[entry["call_id"]] = sess
        sess.task = asyncio.create_task(sess.run())
        return sess

    async def hangup(self, entry: dict, *, quiet: bool = False, status: str = "📴 已在 Telegram 挂断") -> None:
        if entry["terminal"]:
            return
        try:
            res = await self._command({"type": "call.hangup", "callId": entry["call_id"],
                                       "expectedRevision": entry["revision"]})
            if res.get("status") == "error" and res.get("code") == "stale" and not entry["terminal"]:
                res = await self._command({"type": "call.hangup", "callId": entry["call_id"],
                                           "expectedRevision": entry["revision"]})
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
                    "answerable": False, "joinable": False}
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
        st.update(title="微信群语音通话" if conf else ("微信视频来电" if entry.get("call_type") == "video" else "微信语音来电"),
                  name=entry.get("caller_name") or "", status=entry.get("status_text") or "")
        if entry.get("connected_at"):
            st["elapsed"] = max(0, int(time.time() - entry["connected_at"]))
        sess = self.sessions.get(entry["call_id"])
        if entry["terminal"]:
            st["phase"] = "ended"
        elif sess is not None and not sess.closed:
            st.update(phase="active" if sess.attached else "connecting", mine=ws is not None and sess.active_ws is ws,
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
        for task in list(self._tasks):
            task.cancel()
        for conns in list(self.clients.values()):
            for ws in list(conns):
                await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _health(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def _page(self, request: web.Request) -> web.Response:
        cfg = json.dumps({"base": self.prefix}, ensure_ascii=False)
        return web.Response(
            text=PAGE_HTML.replace("__CFG__", cfg).replace("__WORKLET__", json.dumps(WORKLET_JS)),
            content_type="text/html", charset="utf-8",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                     "X-Content-Type-Options": "nosniff"},
        )

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
            entry = self.bridge._active_calls.get(ref) if kind == "call" else None
            if entry is None:
                raise CallActionError("通话已结束")
            sess = self._session_for(token)
            if action == "answer":
                await self.answer(entry, ws)
            elif action == "reject":
                if entry["terminal"] or entry["stage"] not in ("pending", "offered"):
                    raise CallActionError("来电已结束或已处理")
                if entry.get("rejecting") or entry.get("answering"):
                    raise CallActionError("正在处理中…")
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
                await self.hangup(entry)
            elif action == "mute":
                if sess is None:
                    raise CallActionError("通话未由 Telegram 接管或已结束")
                await self.set_mute(sess, bool(cmd.get("muted")))
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
<title>微信通话</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{color-scheme:light dark;--bg:var(--tg-theme-bg-color,#1c1c1e);--fg:var(--tg-theme-text-color,#fff);
--hint:var(--tg-theme-hint-color,#9a9aa0);--accent:var(--tg-theme-button-color,#2ea6ff);}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans CJK SC","Microsoft YaHei",sans-serif}
main{min-height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;
padding:32px 20px calc(32px + env(safe-area-inset-bottom));text-align:center}
.avatar{width:96px;height:96px;border-radius:50%;display:flex;align-items:center;justify-content:center;
font-size:40px;font-weight:600;background:linear-gradient(135deg,#07c160,#05a050);color:#fff;margin-bottom:8px}
.avatar.ringing{animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(7,193,96,.5)}50%{box-shadow:0 0 0 18px rgba(7,193,96,0)}}
.title{font-size:14px;color:var(--hint)}.name{font-size:24px;font-weight:600;word-break:break-all}
.status{font-size:15px;color:var(--hint);min-height:20px}.timer{font-size:17px;font-variant-numeric:tabular-nums;min-height:22px}
.actions{display:flex;gap:36px;margin-top:36px;flex-wrap:wrap;justify-content:center}
.btn{display:flex;flex-direction:column;align-items:center;gap:8px;font-size:13px;color:var(--hint);
background:none;border:0;padding:0;cursor:pointer}
.btn span{width:68px;height:68px;border-radius:50%;display:flex;align-items:center;justify-content:center;
font-size:28px;color:#fff;transition:transform .1s}
.btn:active span{transform:scale(.92)}.btn:disabled{opacity:.5}
.green span{background:#07c160}.red span{background:#fa5151}.gray span{background:#5a5a60}.gray.on span{background:#fff;color:#000}
.hint{font-size:13px;color:var(--hint);max-width:320px;line-height:1.5;min-height:18px;margin-top:16px}
.hint a{color:var(--accent)}
.err{color:#fa5151}
</style>
</head>
<body>
<main>
  <div class="avatar" id="avatar">☎</div>
  <div class="title" id="title">微信通话</div>
  <div class="name" id="name">正在连接…</div>
  <div class="status" id="status"></div>
  <div class="timer" id="timer"></div>
  <div class="actions" id="actions"></div>
  <div class="hint" id="hint"></div>
</main>
<script>
(function () {
  const CFG = __CFG__;
  const WORKLET = __WORKLET__;
  const W = window.Telegram && window.Telegram.WebApp;
  const tg = W && W.initData ? W : null;
  if (tg) { try { tg.ready(); tg.expand(); tg.disableVerticalSwipes && tg.disableVerticalSwipes(); } catch (e) {} }
  const $ = (id) => document.getElementById(id);

  function findToken() {
    const m = location.pathname.match(/\/call\/([A-Za-z0-9_-]{8,})\/?$/);
    if (m) return m[1];
    if (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) return tg.initDataUnsafe.start_param;
    const q = new URLSearchParams(location.search);
    return q.get('tgWebAppStartParam') || q.get('t') || '';
  }
  const token = findToken();
  const pageUrl = location.origin + CFG.base + '/call/' + token;

  let ws = null, st = null, audio = null, ended = false, fatal = false, retry = 0;
  let timerBase = null, timerId = null, wakeLock = null, busy = false;

  function hint(text, isErr, html) {
    const h = $('hint');
    h.className = 'hint' + (isErr ? ' err' : '');
    if (html) h.innerHTML = html; else h.textContent = text || '';
  }

  function browserFallback() {
    if (!tg) return;
    const h = $('hint');
    const a = document.createElement('a');
    a.href = '#'; a.textContent = '在浏览器中打开';
    a.onclick = (e) => { e.preventDefault(); try { tg.openLink(pageUrl); } catch (_) { location.href = pageUrl; } };
    h.appendChild(document.createTextNode(' '));
    h.appendChild(a);
  }

  async function startAudio(rate, frame) {
    if (audio) {
      if (audio.rate !== rate) { audio.node.port.postMessage({t: 'rate', net: rate, frame: frame}); audio.rate = rate; }
      return audio;
    }
    if (!window.isSecureContext) throw new Error('页面必须通过 HTTPS 打开才能使用麦克风');
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) throw new Error('当前环境不支持麦克风');
    const stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true,
      autoGainControl: true, channelCount: 1}, video: false});
    const AC = window.AudioContext || window.webkitAudioContext;
    const ctx = new AC({latencyHint: 'interactive'});
    if (!ctx.audioWorklet) { stream.getTracks().forEach((t) => t.stop()); throw new Error('当前浏览器不支持 AudioWorklet'); }
    const url = URL.createObjectURL(new Blob([WORKLET], {type: 'application/javascript'}));
    await ctx.audioWorklet.addModule(url);
    const node = new AudioWorkletNode(ctx, 'bridge-io', {numberOfInputs: 1, numberOfOutputs: 1,
      outputChannelCount: [1], processorOptions: {net: rate, frame: frame}});
    ctx.createMediaStreamSource(stream).connect(node);
    node.connect(ctx.destination);
    node.port.onmessage = (e) => {
      if (st && st.mine && !st.muted && ws && ws.readyState === 1 && ws.bufferedAmount < 64000) ws.send(e.data);
    };
    await ctx.resume();
    audio = {ctx, stream, node, rate};
    try { if (navigator.wakeLock) wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {}
    return audio;
  }

  function stopAudio() {
    if (!audio) return;
    try { audio.stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
    try { audio.ctx.close(); } catch (e) {}
    audio = null;
    try { wakeLock && wakeLock.release(); } catch (e) {}
    wakeLock = null;
  }

  function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

  async function withAudio(action) {
    if (busy) return;
    busy = true;
    hint('');
    try {
      await startAudio((st && st.rate) || 16000, (st && st.frame) || 320);
      send({type: action});
    } catch (e) {
      const name = e && e.name;
      let msg = (e && e.message) || String(e);
      if (name === 'NotAllowedError' || name === 'SecurityError') msg = '没有麦克风权限，请允许 Telegram / 浏览器使用麦克风';
      else if (name === 'NotFoundError') msg = '没有找到麦克风';
      hint(msg, true);
      browserFallback();
    } finally {
      busy = false;
    }
  }

  function button(cls, icon, label, onClick, disabled) {
    const b = document.createElement('button');
    b.className = 'btn ' + cls;
    b.disabled = !!disabled;
    b.innerHTML = '<span>' + icon + '</span>';
    b.appendChild(document.createTextNode(label));
    b.onclick = onClick;
    return b;
  }

  function fmt(sec) {
    const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0');
  }

  function tick() { $('timer').textContent = timerBase == null ? '' : fmt(Math.floor((Date.now() - timerBase) / 1000)); }

  function render() {
    if (!st) return;
    $('title').textContent = st.title || '微信通话';
    $('name').textContent = st.name || st.caller || '';
    $('avatar').textContent = (st.name || st.caller || '☎').trim().slice(0, 1) || '☎';
    $('avatar').className = 'avatar' + (st.phase === 'ringing' || st.phase === 'invite' ? ' ringing' : '');
    let status = st.status || '';
    if (st.phase === 'invite' && st.caller) status = '发起人：' + st.caller + ' · ' + status;
    $('status').textContent = status;
    if (st.elapsed != null && st.phase !== 'ended') {
      timerBase = Date.now() - st.elapsed * 1000;
      if (!timerId) timerId = setInterval(tick, 1000);
    } else {
      timerBase = null;
      if (timerId) { clearInterval(timerId); timerId = null; }
    }
    tick();
    const a = $('actions');
    a.innerHTML = '';
    const p = st.phase;
    if (p === 'ringing') {
      a.appendChild(button('red', '✕', '拒接', () => send({type: 'reject'})));
      if (st.answerable) a.appendChild(button('green', '✆', '接听', () => withAudio('answer'), busy));
      else hint('当前 agent-wechat 不支持在网页接听此通话');
    } else if (p === 'invite') {
      if (st.joinable) a.appendChild(button('green', '✆', '加入', () => withAudio('join'), busy));
      else hint('当前 agent-wechat 不支持加入群语音通话');
    } else if (p === 'connecting' || p === 'active') {
      if (st.mine) {
        if (p === 'active') {
          a.appendChild(button('gray' + (st.muted ? ' on' : ''), st.muted ? '🔇' : '🎙', st.muted ? '取消静音' : '静音',
            () => send({type: 'mute', muted: !st.muted})));
        }
      } else {
        a.appendChild(button('green', '🎧', '在此继续通话', () => withAudio('attach'), busy));
      }
      a.appendChild(button('red', '✕', '挂断', () => send({type: 'hangup'})));
      if (audio && st.rate && audio.rate !== st.rate) startAudio(st.rate, st.frame);
    }
    const inCall = (p === 'connecting' || p === 'active') && st.mine;
    if (tg) { try { inCall ? tg.enableClosingConfirmation() : tg.disableClosingConfirmation(); } catch (e) {} }
    if (p === 'ended' || p === 'elsewhere' || (!st.mine && audio && (p === 'connecting' || p === 'active'))) {
      if (p === 'ended') ended = true;
      if (p !== 'connecting' && p !== 'active') stopAudio();
    }
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
        st = m; render();
        if (!m.mine && !wasMine && audio && (m.phase === 'connecting' || m.phase === 'active') && reattach) {
          reattach = false; send({type: 'attach'});
        }
      } else if (m.type === 'error') {
        hint(m.message, true);
      } else if (m.type === 'drop') {
        audio && audio.node.port.postMessage({t: 'drop', id: m.trackId});
      } else if (m.type === 'fatal') {
        fatal = true; $('name').textContent = ''; $('status').textContent = m.message; $('actions').innerHTML = '';
      }
    };
    ws.onclose = () => {
      if (fatal || ended) return;
      if (audio) reattach = true;
      $('status').textContent = '连接中断，正在重连…';
      setTimeout(connect, Math.min(5000, 500 * Math.pow(2, retry++)));
    };
  }
  let reattach = false;
  connect();
})();
</script>
</body>
</html>
"""
