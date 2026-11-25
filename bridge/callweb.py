"""Answer WeChat voice calls from a Telegram Mini App (or any browser) and relay the call audio.

    browser page  <--WebSocket: JSON + binary PCM16-->  CallWeb (aiohttp)  <--calls media socket-->  agent-wechat

The page is served by a small aiohttp server that listens on loopback (`call_web_listen`); put an HTTPS
reverse proxy in front of it (`call_web_url`), because browsers only grant microphone access to secure pages.

Every call / group-call invitation gets an unguessable web token. The token in the link authorises the page;
when the page runs as a Telegram Mini App its signed initData must additionally belong to the bridge owner.
"""
from __future__ import annotations

import asyncio
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
from .web import load_text
from .wechat import (
    WXCM_KIND_H264,
    WXCM_KIND_H265,
    WXCM_KIND_JPEG,
    WXCM_KIND_PCM_S16LE,
    WXCM_MAX_VIDEO_PAYLOAD,
    WeChatError,
    encode_wxcm_frame,
)

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
HANDOFF_TICKET_TTL = 1800.0
MAX_PCM_CHUNK = 65536
MAX_VIDEO_FRAME = WXCM_MAX_VIDEO_PAYLOAD + 4096
# Browser <-> CallWeb binary video frame header (20 bytes, big endian):
# magic ("WXV1"), kind (2=h265, 3=h264, 4=jpeg), flags (low 8 bits of WXCM flags), reserved (u16),
# trackId (RX) or generation (TX) (u32), timestampMicros (u64).
WXV1_MAGIC = b"WXV1"
WXV1_HEADER_STRUCT = struct.Struct("!4sBBHIQ")
_WXV1_VIDEO_KINDS = (WXCM_KIND_H265, WXCM_KIND_H264, WXCM_KIND_JPEG)
_WXV1_ALLOWED_CODECS = ("h264", "jpeg")
_DEFAULT_VIDEO_CAPS = {
    "sendCodecs": ["jpeg"],
    "receiveCodecs": ["jpeg"],
    "maxWidth": 640,
    "maxHeight": 480,
    "maxFps": 15,
}
# How long after the media socket attaches to check that audio is flowing both ways (logged, not enforced).
AUDIO_CHECK_SECONDS = 8
# Grace window when /media closes before /socket reports call.state(ending/ended) (e.g. remote conference teardown).
MEDIA_CLOSE_GRACE_SECONDS = 5.0
# Max queued socket bytes before dropping video frames so real-time audio never stalls behind video.
VIDEO_WS_MAX_BUFFER = 64 * 1024
KEYFRAME_REQ_COOLDOWN = 2.0


def encode_wxv1_frame(track_or_gen: int, kind: int, flags: int, timestamp_micros: int, payload: bytes) -> bytes:
    """Pack a browser <-> CallWeb binary WXV1 video frame."""
    if kind not in _WXV1_VIDEO_KINDS or not payload or len(payload) > WXCM_MAX_VIDEO_PAYLOAD:
        raise ValueError("Invalid WXV1 video frame")
    return WXV1_HEADER_STRUCT.pack(
        WXV1_MAGIC, kind, flags & 0x0F, 0, track_or_gen & 0xFFFFFFFF, max(0, int(timestamp_micros)),
    ) + payload


def decode_wxv1_frame(data: bytes) -> dict | None:
    """Parse a browser <-> CallWeb WXV1 binary video frame, or None for non-WXV1 / malformed data."""
    if len(data) <= WXV1_HEADER_STRUCT.size or not data.startswith(WXV1_MAGIC):
        return None
    _, kind, flags, _, track_or_gen, ts_micros = WXV1_HEADER_STRUCT.unpack_from(data, 0)
    payload = data[WXV1_HEADER_STRUCT.size:]
    if kind not in _WXV1_VIDEO_KINDS or not payload or len(payload) > WXCM_MAX_VIDEO_PAYLOAD:
        return None
    return {
        "kind": kind,
        "flags": flags & 0x0F,
        "id": track_or_gen,
        "timestampMicros": ts_micros,
        "payload": payload,
    }


def _sanitize_video_caps(raw: dict | None) -> dict:
    """Validate browser-advertised video codecs/bounds into a daemon-compatible CallVideoCapabilities dict."""
    if not isinstance(raw, dict):
        return dict(_DEFAULT_VIDEO_CAPS)

    def clean_codecs(val) -> list[str]:
        out: list[str] = []
        if isinstance(val, list):
            for c in val:
                if c in _WXV1_ALLOWED_CODECS and c not in out:
                    out.append(c)
        return out or ["jpeg"]

    def clamp(val, lo: int, hi: int, default: int) -> int:
        try:
            return max(lo, min(hi, int(val)))
        except (TypeError, ValueError):
            return default

    return {
        "sendCodecs": clean_codecs(raw.get("sendCodecs")),
        "receiveCodecs": clean_codecs(raw.get("receiveCodecs")),
        "maxWidth": clamp(raw.get("maxWidth"), 64, 640, 640),
        "maxHeight": clamp(raw.get("maxHeight"), 64, 480, 480),
        "maxFps": clamp(raw.get("maxFps"), 1, 15, 15),
    }
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


def make_handoff_ticket(token: str, bot_token: str, *, ttl: float = HANDOFF_TICKET_TTL,
                        now: float | None = None) -> str:
    """Sign a short-lived browser handoff ticket bound to `token` after verified Mini App auth."""
    exp = int((time.time() if now is None else now) + ttl)
    payload = f"{token}:{exp}"
    secret = hmac.new(b"CallHandoff", bot_token.encode(), hashlib.sha256).digest()
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_handoff_ticket(ticket: str, token: str, bot_token: str, *, now: float | None = None) -> bool:
    """Verify a browser handoff ticket issued by `make_handoff_ticket`."""
    if not ticket or not token or not bot_token:
        return False
    exp_s, sep, sig = ticket.partition(".")
    if not sep or not exp_s.isdigit() or not sig:
        return False
    exp = int(exp_s)
    cur = time.time() if now is None else now
    if cur > exp:
        return False
    payload = f"{token}:{exp}"
    secret = hmac.new(b"CallHandoff", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _err_text(code: str | None, default: str) -> str:
    return CALL_ERRORS.get(code or "", f"{default}（{code or 'error'}）")


_DIAG_KEYS = (
    "engine", "ctxState", "sampleRate", "currentTime", "ticks", "hasIn",
    "txFrames", "rxFrames", "stalled", "trackState", "trackMuted", "err",
)


def _sanitize_page_diag(raw: dict) -> dict[str, object]:
    out: dict[str, object] = {}
    for k in _DIAG_KEYS:
        if k not in raw or raw[k] is None:
            continue
        v = raw[k]
        if isinstance(v, bool):
            out[k] = v
        elif isinstance(v, int):
            out[k] = v
        elif isinstance(v, float):
            out[k] = round(v, 3)
        elif isinstance(v, str):
            clean = re.sub(r"[\r\n\t]+", " ", v).strip()[:120]
            if clean:
                out[k] = clean
    return out


def _format_page_diag(diag: dict[str, object]) -> str:
    return ", ".join(f"{k}={diag[k]}" for k in _DIAG_KEYS if k in diag)


def _find_audio_send_track(ready: dict) -> dict:
    """Extract the audio send track from v3 media.ready sendTracks[]."""
    tracks = ready.get("sendTracks")
    if isinstance(tracks, list):
        for t in tracks:
            if isinstance(t, dict) and t.get("media") == "audio" and t.get("direction") == "send":
                return t
    return {}


def _find_video_send_track(ready: dict) -> dict | None:
    """Extract the video send track from v3 media.ready sendTracks[], if present."""
    tracks = ready.get("sendTracks")
    if isinstance(tracks, list):
        for t in tracks:
            if isinstance(t, dict) and t.get("media") == "video" and t.get("direction") == "send":
                return t
    return None


class CallSession:
    """One call answered/joined from Telegram: owns the media socket and the page that plays its audio/video."""

    def __init__(self, web_: CallWeb, entry: dict, owner_token: str, readied: bool, *, camera_enabled: bool = False):
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
        self._tx_seq = 0
        self._tx_samples = 0
        self._tx_last_ts = -1
        # Video tracks (Calls Protocol v3)
        self.camera_enabled: bool = bool(camera_enabled)
        self.video_send_track: int | None = None
        self.video_send_gen: int | None = None
        self.video_send_format: dict | None = None
        self.video_send_active: bool = False
        self._video_started_key: tuple[int, int] | None = None
        self._vtx_seq = 0
        self.video_rx_tracks: dict[int, dict] = {}  # trackId -> track metadata for UI
        self._rx_need_kf: set[int] = set()
        self._rx_last_kf_req: dict[int, float] = {}
        self._tx_last_kf_req: float = 0.0
        self.active_ws: web.WebSocketResponse | None = None
        self.muted = False
        self.changed = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.grace_task: asyncio.Task | None = None
        self.closed = False
        # audio/video flow, for the "no sound/video" diagnosis
        self.rx_chunks = 0
        self.rx_to_page = 0
        self.tx_chunks = 0
        self.rx_video_frames = 0
        self.tx_video_frames = 0
        self.page_diag: dict[str, object] = {}
        self.watch_task: asyncio.Task | None = None

    @property
    def attached(self) -> bool:
        return self.media is not None and not self.closed

    async def _participant_name(self, participant_id: str | None) -> str:
        if not participant_id:
            return self.entry.get("caller_name") or ""
        contact_id = ""
        part_display = ""
        for p in self.entry.get("participants") or []:
            if isinstance(p, dict) and participant_id in (p.get("participantId"), p.get("contactId")):
                contact_id = str(p.get("contactId") or "")
                part_display = str(p.get("displayName") or p.get("nickname") or "").strip()
                break
        cid = contact_id or participant_id
        for mapping in (self.entry.get("invitees"), self.entry.get("participant_names")):
            if isinstance(mapping, dict) and cid in mapping and mapping[cid]:
                return str(mapping[cid])
        if remark := self.web.bridge._known_remark(cid):
            return remark
        thread_id = self.entry.get("thread_id") or ""
        if cached := self.web.bridge.names.get((thread_id, cid)):
            return cached
        if part_display:
            return part_display
        if self.entry.get("kind") != "conference":
            return self.entry.get("caller_name") or cid
        if cid == self.entry.get("caller_id") and self.entry.get("caller_name"):
            return str(self.entry["caller_name"])
        try:
            name = await self.web.bridge._sender_name({
                "threadId": thread_id,
                "senderId": cid,
            })
            if not contact_id and name == cid:
                # Avoid caching an unresolved opaque participantId before call.state maps it to contactId.
                self.web.bridge.names.pop((thread_id, cid), None)
            return name or cid
        except Exception:
            return cid

    async def refresh_participant_names(self) -> None:
        for meta in list(self.video_rx_tracks.values()):
            pid = meta.get("participantId")
            if pid:
                name = await self._participant_name(pid)
                if name and name != meta.get("name"):
                    meta["name"] = name

    async def _register_video_rx_track(self, track: dict) -> None:
        tid = track.get("id")
        if not isinstance(tid, int) or tid <= 0:
            return
        fmt = track.get("format") or {}
        pid = track.get("participantId")
        name = await self._participant_name(str(pid) if pid else None)
        self.video_rx_tracks[tid] = {
            "id": tid,
            "codec": fmt.get("codec") or "jpeg",
            "participantId": str(pid) if pid else "",
            "name": name,
            "active": bool(track.get("active", True)),
        }

    def video_send_state(self) -> dict | None:
        if self.video_send_track is None or self.video_send_gen is None:
            return None
        fmt = self.video_send_format or {}
        return {
            "id": self.video_send_track,
            "generation": self.video_send_gen,
            "codec": fmt.get("codec") or "jpeg",
            "mode": fmt.get("mode") or "transcoded",
            "maxWidth": int(fmt.get("maxWidth") or 640),
            "maxHeight": int(fmt.get("maxHeight") or 480),
            "maxFps": int(fmt.get("maxFps") or 15),
            "bitrateKbps": int(fmt.get("bitrateKbps") or 600),
            "active": bool(self.video_send_active),
        }

    def video_tracks_state(self) -> list[dict]:
        return [dict(t) for t in self.video_rx_tracks.values() if t.get("active", True)]

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
            send = _find_audio_send_track(ready)
            fmt = send.get("format") or bridge._calls_audio_format
            if int(fmt.get("channels") or 1) != 1:
                raise CallActionError(f"不支持的通话音频格式：{fmt}")
            self.rate = int(fmt.get("sampleRate") or 16000)
            self.frame = max(1, self.rate * int(fmt.get("frameMs") or 20) // 1000)
            self.send_track = send.get("id")
            self.send_gen = send.get("generation")
            vsend = _find_video_send_track(ready)
            if vsend is not None:
                self.video_send_track = vsend.get("id")
                self.video_send_gen = vsend.get("generation")
                self.video_send_format = vsend.get("format") if isinstance(vsend.get("format"), dict) else {}
                self.video_send_active = bool(vsend.get("active", self.camera_enabled))
            for rtrack in ready.get("receiveTracks") or []:
                if isinstance(rtrack, dict) and rtrack.get("media") == "video":
                    await self._register_video_rx_track(rtrack)
            if self.closed:
                return
            await self._start_send()
            if self.video_send_active and self.camera_enabled:
                await self._start_video_send()
            await self.web.push_state(self.entry)
            log.info("Call %s media attached: %d Hz, frame %d, send track %s, video send %s",
                     self.call_id, self.rate, self.frame, self.send_track, self.video_send_track)
            self.watch_task = asyncio.create_task(self._watch_audio())
            async for ev in sock.events():
                etype = ev.get("type")
                if etype == "audio.chunk":
                    await self._to_page(ev)
                elif etype == "video.frame":
                    await self._video_to_page(ev)
                elif etype in ("track.added", "track.updated"):
                    await self.on_track_event(ev)
                elif etype == "track.removed":
                    await self._on_track_removed(ev.get("trackId"))
                elif etype == "video.keyframe_requested":
                    await self._send_page_json({
                        "type": "keyframe_requested",
                        "trackId": ev.get("trackId"),
                        "generation": ev.get("generation"),
                    })
            if not self.closed and not self.entry["terminal"] and MEDIA_CLOSE_GRACE_SECONDS > 0:
                grace_deadline = loop.time() + MEDIA_CLOSE_GRACE_SECONDS
                while not self.closed and not self.entry["terminal"]:
                    remaining = grace_deadline - loop.time()
                    if remaining <= 0:
                        break
                    self.changed.clear()
                    try:
                        await asyncio.wait_for(self.changed.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
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
            if not self.entry["terminal"]:
                await self.web.report(self.entry, "通话媒体中转异常中断")
                await self.web.hangup(self.entry, quiet=True)
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
            diag_str = _format_page_diag(self.page_diag)
            extra = f", {diag_str}" if diag_str else ""
            log.warning("Call %s: no microphone audio from the call page %ds after the media socket attached "
                        "(page attached: %s, muted: %s%s)", self.call_id, AUDIO_CHECK_SECONDS,
                        self.active_ws is not None, self.muted, extra)

    async def _start_send(self) -> None:
        if self.media is not None and self.send_track is not None and self.send_gen is not None:
            self._tx_seq = 0
            self._tx_samples = 0
            self._tx_last_ts = -1
            await self.media.send_json({"type": "track.start", "trackId": self.send_track, "generation": self.send_gen})

    async def _start_video_send(self) -> None:
        if self.media is None or self.video_send_track is None or self.video_send_gen is None:
            return
        key = (self.video_send_track, self.video_send_gen)
        if self._video_started_key == key:
            return
        self._vtx_seq = 0
        self._video_started_key = key
        await self.media.send_json({
            "type": "track.start",
            "trackId": self.video_send_track,
            "generation": self.video_send_gen,
        })

    async def _on_track_removed(self, track_id) -> None:
        if isinstance(track_id, int) and track_id in self.video_rx_tracks:
            self.video_rx_tracks.pop(track_id, None)
            self._rx_need_kf.discard(track_id)
            self._rx_last_kf_req.pop(track_id, None)
            await self._send_page_json({"type": "video_drop", "trackId": track_id})
            await self.web.push_state(self.entry)
            return
        await self._send_page_json({"type": "drop", "trackId": track_id})

    async def on_track_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "track.removed":
            await self._on_track_removed(event.get("trackId"))
            return
        track = event.get("track")
        if not isinstance(track, dict):
            return
        media = track.get("media")
        direction = track.get("direction")
        if media == "video":
            if direction == "send":
                self.video_send_track = track.get("id")
                self.video_send_gen = track.get("generation")
                if isinstance(track.get("format"), dict):
                    self.video_send_format = track["format"]
                self.video_send_active = bool(track.get("active", self.camera_enabled))
                if self.video_send_active and self.camera_enabled:
                    try:
                        await self._start_video_send()
                    except (OSError, WeChatError) as e:
                        log.info("Could not restart video send track for %s: %s", self.call_id, e)
                await self.web.push_state(self.entry)
            elif direction == "receive":
                await self._register_video_rx_track(track)
                if not track.get("active", True):
                    await self._send_page_json({"type": "video_drop", "trackId": track.get("id")})
                await self.web.push_state(self.entry)
            return
        await self.on_track_updated(track)

    async def on_track_updated(self, track: dict) -> None:
        if (track.get("media") != "audio"
                or track.get("direction") != "send"
                or track.get("id") != self.send_track):
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
        raw_pcm = ev.get("pcm")
        if not isinstance(raw_pcm, (bytes, bytearray)) or not raw_pcm:
            return
        try:
            await ws.send_bytes(struct.pack("<I", int(ev.get("trackId") or 0)) + bytes(raw_pcm))
            self.rx_to_page += 1
        except (ConnectionError, RuntimeError):
            pass

    async def _maybe_request_keyframe(self, track_id: int) -> None:
        if track_id <= 0:
            return
        now = time.monotonic()
        if now - self._rx_last_kf_req.get(track_id, 0.0) < KEYFRAME_REQ_COOLDOWN:
            return
        self._rx_last_kf_req[track_id] = now
        await self.request_keyframe(track_id)

    async def _video_to_page(self, ev: dict) -> None:
        self.rx_video_frames += 1
        ws = self.active_ws
        if ws is None or ws.closed:
            return
        payload = ev.get("payload")
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            return
        track_id = int(ev.get("trackId") or 0)
        kind = int(ev.get("kind") or WXCM_KIND_JPEG)
        flags = int(ev.get("flags") or 0)
        inter_coded = kind in (WXCM_KIND_H264, WXCM_KIND_H265)
        is_keyframe = bool(flags & 0x01)
        # Drop video frames early if the browser socket write buffer is backed up so audio stays crisp.
        transport = getattr(ws, "_req", None)
        tr = getattr(transport, "transport", None) if transport is not None else None
        if tr is not None and getattr(tr, "get_write_buffer_size", lambda: 0)() > VIDEO_WS_MAX_BUFFER:
            if inter_coded:
                self._rx_need_kf.add(track_id)
                await self._maybe_request_keyframe(track_id)
            return
        if inter_coded and track_id in self._rx_need_kf:
            if not is_keyframe:
                await self._maybe_request_keyframe(track_id)
                return
            self._rx_need_kf.discard(track_id)
        try:
            frame = encode_wxv1_frame(
                track_id,
                kind,
                flags,
                int(ev.get("timestampMicros") or 0),
                bytes(payload),
            )
            await ws.send_bytes(frame)
        except (ConnectionError, RuntimeError, ValueError):
            pass

    async def _send_page_json(self, obj: dict) -> None:
        ws = self.active_ws
        if ws is not None and not ws.closed:
            try:
                await ws.send_json(obj)
            except (ConnectionError, RuntimeError):
                pass

    async def from_page(self, ws: web.WebSocketResponse, data: bytes) -> None:
        if (ws is not self.active_ws or self.muted or self.media is None
                or self.send_track is None or self.send_gen is None):
            return
        if len(data) & 1:
            data = data[:-1]
        if not data or len(data) > MAX_PCM_CHUNK:
            return
        self.tx_chunks += 1
        try:
            self._tx_seq += 1
            ts = self._tx_samples * 1_000_000 // max(1, self.rate)
            if ts <= self._tx_last_ts:
                ts = self._tx_last_ts + 1
            self._tx_last_ts = ts
            self._tx_samples += len(data) // 2
            frame = encode_wxcm_frame(
                self.send_track, self.send_gen, data,
                kind=WXCM_KIND_PCM_S16LE, sequence=self._tx_seq, timestamp_micros=ts,
            )
            await self.media.send_bytes(frame)
        except (OSError, WeChatError):
            pass

    async def from_page_video(self, ws: web.WebSocketResponse, data: bytes) -> None:
        if (ws is not self.active_ws or not self.camera_enabled or not self.video_send_active
                or self.media is None or self.video_send_track is None or self.video_send_gen is None):
            return
        parsed = decode_wxv1_frame(data)
        if parsed is None:
            return
        if parsed["id"] and parsed["id"] != self.video_send_gen:
            return  # drop frame encoded for an older generation
        if getattr(self.media, "write_buffer_size", lambda: 0)() > VIDEO_WS_MAX_BUFFER * 2:
            if parsed["kind"] in (WXCM_KIND_H264, WXCM_KIND_H265):
                now = time.monotonic()
                if now - self._tx_last_kf_req >= KEYFRAME_REQ_COOLDOWN:
                    self._tx_last_kf_req = now
                    await self._send_page_json({
                        "type": "keyframe_requested",
                        "trackId": self.video_send_track,
                        "generation": self.video_send_gen,
                    })
            return
        self._vtx_seq += 1
        self.tx_video_frames += 1
        try:
            frame = encode_wxcm_frame(
                self.video_send_track,
                self.video_send_gen,
                parsed["payload"],
                kind=parsed["kind"],
                flags=parsed["flags"],
                sequence=self._vtx_seq,
                timestamp_micros=parsed["timestampMicros"],
            )
            await self.media.send_bytes(frame)
        except (OSError, WeChatError, ValueError):
            pass

    async def request_keyframe(self, track_id: int) -> None:
        if self.media is None or track_id <= 0:
            return
        try:
            await self.media.send_json({"type": "video.request_keyframe", "trackId": int(track_id)})
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
            diag_str = _format_page_diag(self.page_diag)
            extra = (f" ({diag_str})"
                     if diag_str and (not self.tx_chunks or not self.rx_to_page or self.page_diag.get("err"))
                     else "")
            log.info("Call %s audio: %d chunks from WeChat (%d relayed to the page), %d mic chunks from the page%s",
                     self.call_id, self.rx_chunks, self.rx_to_page, self.tx_chunks, extra)
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
        self.page_diags: dict[str, dict[str, object]] = {}  # web token -> latest client audio diag
        self._ready_holds: int = 0
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
                    self.bridge._dial_requests.get(ref) if kind in ("dial", "vdial")
                    else self.bridge._conf_invites.get(ref))
            if live is None and token not in self.clients:
                self.tokens.pop(token, None)
                self.page_diags.pop(token, None)

    def token_for_call(self, entry: dict) -> str:
        if not entry.get("web_token"):
            entry["web_token"] = self._new_token("call", entry["call_id"])
        return entry["web_token"]

    def token_for_invite(self, inv: dict) -> str:
        if not inv.get("web_token"):
            inv["web_token"] = self._new_token("invite", inv["invitation_id"])
        return inv["web_token"]

    def token_for_dial(self, req: dict, *, video: bool = False) -> str:
        if video:
            if not req.get("video_web_token"):
                req["video_web_token"] = self._new_token("vdial", req["dial_id"])
            return req["video_web_token"]
        if not req.get("web_token"):
            req["web_token"] = self._new_token("dial", req["dial_id"])
        return req["web_token"]

    def can_video(self, conference: bool = False) -> bool:
        caps = self.bridge._calls_caps
        return bool(caps.get("conferenceVideo") if conference else caps.get("directVideo"))

    def can_dial(self, conference: bool = False, *, video: bool = False) -> bool:
        caps = self.bridge._calls_caps
        if conference:
            ok = bool(caps.get("conferenceCreate") and caps.get("conferenceAudio"))
            return ok and (not video or self.can_video(True))
        ok = bool(caps.get("directAudio"))
        return ok and (not video or self.can_video(False))

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
        if entry["kind"] != "direct" or not caps.get("directAudio"):
            return False
        if entry.get("call_type", "voice") == "video":
            return self.can_video(False)
        return True

    def can_join(self, *, video: bool = False) -> bool:
        caps = self.bridge._calls_caps
        ok = bool(caps.get("conferenceJoin") and caps.get("conferenceAudio"))
        return ok and (not video or self.can_video(True))

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
            if not sess.closed and sess.video_rx_tracks:
                await sess.refresh_participant_names()
        await self.push_state(entry)

    async def on_track_event(self, event: dict) -> None:
        sess = self.sessions.get(event.get("callId") or "")
        if sess is not None and not sess.closed:
            await sess.on_track_event(event)

    async def on_track_updated(self, event: dict) -> None:
        await self.on_track_event(event)

    async def on_invite_changed(self, inv: dict) -> None:
        if inv.get("web_token"):
            await self._push_token(inv["web_token"])

    async def on_dial_changed(self, req: dict) -> None:
        if req.get("call_id"):
            return
        for key in ("web_token", "video_web_token"):
            if tok := req.get(key):
                await self._push_token(tok)

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

    async def _ready(self, scope: dict, *, video: dict | None = None) -> dict:
        cmd: dict = {"type": "call.ready", "ready": True,
                     "format": self.bridge._calls_audio_format, "scope": scope}
        if video is not None:
            cmd["video"] = video
        return await self._command(cmd)

    async def withdraw_readiness(self) -> None:
        if self._ready_holds > 0 or getattr(self.bridge, "_reject_ready_holds", 0) > 0:
            return
        if any(s.readied and not s.closed for s in self.sessions.values()):
            return  # another live call still needs it
        sock = self.bridge._calls_sock
        if sock is None or sock.closed:
            return
        try:
            await sock.command({"type": "call.ready", "ready": False, "format": self.bridge._calls_audio_format})
        except Exception:
            pass

    async def answer(self, entry: dict, ws: web.WebSocketResponse | None, *,
                     video_caps: dict | None = None, camera_enabled: bool | None = None) -> None:
        if entry.get("answering") or entry.get("rejecting") or self.has_session(entry["call_id"]):
            raise CallPending("正在处理中…")
        if entry["terminal"] or entry["stage"] not in ("pending", "offered"):
            raise CallActionError("来电已结束或已在其他设备处理")
        if not self.can_answer(entry):
            raise CallActionError("当前 agent-wechat 不支持接听该通话")
        is_video = entry.get("call_type") == "video"
        want_cam = bool(is_video if camera_enabled is None else camera_enabled) if is_video else False
        ready_video = _sanitize_video_caps(video_caps) if (is_video and self.can_video(False)) else None
        entry["answering"] = True
        self._ready_holds += 1
        readied = False
        claimed = False
        try:
            call_id = entry["call_id"]
            if entry["stage"] == "pending":
                entry["offered_event"].clear()
                res = await self._ready(
                    {"kind": "offer", "callId": call_id, "expectedRevision": entry["revision"]},
                    video=ready_video,
                )
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
            elif ready_video is not None:
                res = await self._ready({"kind": "all"}, video=ready_video)
                if res.get("status") != "error":
                    readied = True
            if entry["terminal"] or entry["stage"] != "offered":
                raise CallActionError("来电已结束或已在其他设备处理")

            def _answer_cmd() -> dict:
                cmd: dict = {"type": "call.answer", "callId": call_id, "expectedRevision": entry["revision"]}
                if is_video:
                    cmd["cameraEnabled"] = want_cam
                return cmd

            res = await self._command(_answer_cmd())
            if res.get("status") == "error" and res.get("code") == "media_not_ready" and not readied:
                await self._ready({"kind": "all"}, video=ready_video)
                readied = True
                res = await self._command(_answer_cmd())
            if res.get("status") == "error" and res.get("code") == "stale" and entry["stage"] == "offered":
                res = await self._command(_answer_cmd())
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
            self._start_session(entry, owner_token, readied, ws, camera_enabled=want_cam)
        finally:
            entry["answering"] = False
            self._ready_holds = max(0, self._ready_holds - 1)
            if readied and not claimed:
                await self.withdraw_readiness()
        await self.bridge._sync_call_msg(entry)

    async def join(self, inv: dict, ws: web.WebSocketResponse | None, *,
                   video_caps: dict | None = None, camera_enabled: bool | None = None) -> None:
        if inv.get("joining") or inv.get("joined_call"):
            raise CallPending("正在加入中…")
        if not self.can_join():
            raise CallActionError("当前 agent-wechat 不支持加入群语音通话")
        want_video = self.can_video(True)
        want_cam = bool(camera_enabled) if want_video else False
        ready_video = _sanitize_video_caps(video_caps) if want_video else None
        inv["joining"] = True
        self._ready_holds += 1
        readied = False
        claimed = False
        try:
            res = await self._ready({"kind": "outgoing"}, video=ready_video)
            if res.get("status") == "error":
                raise CallActionError(_err_text(res.get("code"), "加入失败"))
            readied = True
            join_cmd: dict = {"type": "call.join", "invitationId": inv["invitation_id"],
                              "invitationRevision": inv.get("revision", 0)}
            if camera_enabled is not None:
                join_cmd["cameraEnabled"] = want_cam
            res = await self._command(join_cmd)
            if res.get("status") not in ("accepted", "replayed") or not res.get("callId"):
                raise CallActionError(_err_text(res.get("code"), "加入失败"))
            owner_token = res.get("ownerToken")
            if not owner_token:
                raise CallActionError("agent-wechat 未返回通话凭证，无法接管音频")
            claimed = True
            call_id = res["callId"]
            entry = self.bridge._new_call_entry(
                call_id, kind="conference", call_type="video" if want_cam else "voice",
                caller_id=inv.get("caller_id") or "",
                caller_name=inv.get("caller_name") or "", thread_id=inv.get("group_id") or "",
                stage="connecting", revision=0,
            )
            entry.update(chat_id=inv.get("chat_id"), msg_id=inv.get("msg_id"), answered_by_tg=True,
                         status_text="🟡 正在加入…")
            token = self.token_for_invite(inv)
            entry["web_token"] = token
            self.tokens[token] = ("call", call_id)
            inv["joined_call"] = call_id
            self._start_session(entry, owner_token, readied, ws, camera_enabled=want_cam)
            await self.bridge._replay_orphan_call_state(call_id)
        finally:
            inv["joining"] = False
            self._ready_holds = max(0, self._ready_holds - 1)
            if readied and not claimed:
                await self.withdraw_readiness()
        await self.bridge._sync_call_msg(entry)

    async def dial(self, req: dict, ws: web.WebSocketResponse | None, *,
                   call_type: str | None = None, video_caps: dict | None = None,
                   camera_enabled: bool | None = None) -> None:
        """Place the call a /call or /vcall request asked for (one contact, or a group call)."""
        if req.get("dialing") or req.get("call_id"):
            raise CallPending("正在呼叫…")
        if not req.get("live"):
            raise CallActionError("呼叫已取消或已过期，请重新发送 /call")
        conf = req.get("kind") == "conference"
        ctype = "video" if (call_type or req.get("call_type")) == "video" else "voice"
        is_video = ctype == "video"
        if not self.can_dial(conf, video=is_video):
            raise CallActionError(
                ("当前 agent-wechat 不支持发起微信群视频通话" if conf else "当前 agent-wechat 不支持发起微信视频通话")
                if is_video else
                ("当前 agent-wechat 不支持发起微信群语音通话" if conf else "当前 agent-wechat 不支持发起微信语音通话")
            )
        invitees = list(req.get("selected") or ()) if conf else []
        if conf and not invitees:
            raise CallActionError("还没有选择要邀请的群成员")
        if conf and len(invitees) > MAX_INVITEES:
            raise CallActionError(f"一次最多邀请 {MAX_INVITEES} 人")
        if conf:
            target = {"kind": "conference", "groupId": req["thread_id"], "invitees": invitees}
        else:
            target = {"kind": "direct", "contactId": req["contact_id"]}
        want_cam = bool(True if camera_enabled is None else camera_enabled) if is_video else False
        ready_video = _sanitize_video_caps(video_caps) if (is_video or (conf and self.can_video(True))) else None
        req["dialing"] = True
        self._ready_holds += 1
        readied = False
        claimed = False
        entry = None
        try:
            res = await self._ready({"kind": "outgoing"}, video=ready_video)
            if res.get("status") == "error":
                raise CallActionError(_err_text(res.get("code"), "呼叫失败"))
            readied = True
            dial_cmd: dict = {"type": "call.dial", "callType": ctype, "target": target}
            if is_video:
                dial_cmd["cameraEnabled"] = want_cam
            res = await self._command(dial_cmd)
            if res.get("status") not in ("accepted", "replayed") or not res.get("callId"):
                raise CallActionError(_err_text(res.get("code"), "呼叫失败"))
            owner_token = res.get("ownerToken")
            if not owner_token:
                raise CallActionError("agent-wechat 未返回通话凭证，无法接管音频")
            claimed = True
            call_id = res["callId"]
            entry = self.bridge._new_call_entry(
                call_id, kind="conference" if conf else "direct", call_type=ctype,
                caller_id=req["contact_id"], caller_name=req.get("name") or req["contact_id"],
                thread_id=req["thread_id"], stage="dialing", revision=0,
            )
            entry.update(direction="outgoing", chat_id=req.get("chat_id"), msg_id=req.get("msg_id"),
                         answered_by_tg=True, status_text="📞 正在发起群通话…" if conf else "📞 正在呼叫…")
            if conf:
                names = req.get("names") or {}
                entry["invitees"] = {cid: names.get(cid) or cid for cid in invitees}
                entry["dial_request_id"] = res.get("requestId") or ""
            active_token = self.token_for_dial(req, video=is_video)
            entry["web_token"] = active_token
            self.tokens[active_token] = ("call", call_id)
            for other_key in ("web_token", "video_web_token"):
                if other := req.get(other_key):
                    self.tokens[other] = ("call", call_id)
            req["call_id"] = call_id
            self.bridge._finish_dial_request(req)
            self._start_session(entry, owner_token, readied, ws, camera_enabled=want_cam)
            await self.bridge._replay_orphan_call_state(call_id)
        finally:
            req["dialing"] = False
            self._ready_holds = max(0, self._ready_holds - 1)
            if readied and not claimed:
                await self.withdraw_readiness()
        await self.bridge._sync_call_msg(entry)

    def _start_session(self, entry: dict, owner_token: str, readied: bool, ws, *,
                       camera_enabled: bool = False) -> CallSession:
        sess = CallSession(self, entry, owner_token, readied, camera_enabled=camera_enabled)
        if tok := entry.get("web_token"):
            if cached_diag := self.page_diags.get(tok):
                sess.page_diag.update(cached_diag)
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

    async def set_camera(self, sess: CallSession, enabled: bool) -> None:
        res = await self._command({"type": "call.camera", "callId": sess.call_id, "enabled": bool(enabled)})
        if res.get("status") not in ("accepted", "replayed"):
            raise CallActionError(_err_text(res.get("code"), "摄像头切换失败"))
        sess.camera_enabled = bool(enabled)
        if enabled and sess.video_send_track is not None and sess.video_send_gen is not None and sess.video_send_active:
            try:
                await sess._start_video_send()
            except (OSError, WeChatError):
                pass
        await self.push_state(sess.entry)

    # ------------------------------------------------------------------ page state
    def _sync_participant_name(self, entry: dict, p: dict) -> str:
        cid = str(p.get("contactId") or "").strip()
        pid = str(p.get("participantId") or "").strip()
        key = cid or pid
        for mapping in (entry.get("invitees"), entry.get("participant_names")):
            if isinstance(mapping, dict) and key in mapping and mapping[key]:
                return str(mapping[key])
        if cid and (remark := self.bridge._known_remark(cid)):
            return remark
        thread_id = entry.get("thread_id") or ""
        if cid and (cached := self.bridge.names.get((thread_id, cid))):
            return cached
        for field in ("displayName", "nickname"):
            if val := str(p.get(field) or "").strip():
                return val
        if cid and cid == entry.get("caller_id") and entry.get("caller_name"):
            return str(entry["caller_name"])
        return key

    def _state(self, token: str, ws: web.WebSocketResponse | None) -> dict:
        kind, ref = self.tokens.get(token, ("", ""))
        st: dict = {
            "type": "state", "phase": "ended", "title": "微信通话", "name": "", "status": "通话已结束或链接已失效",
            "mine": False, "muted": False, "camera": False, "elapsed": None, "rate": 16000, "frame": 320,
            "answerable": False, "joinable": False, "dialable": False, "group": False,
            "videoCall": False, "videoEnabled": False, "videoSend": None, "videoTracks": [],
            "remoteCamera": None, "remoteMuted": None, "participants": [],
        }
        if kind in ("dial", "vdial"):
            req = self.bridge._dial_requests.get(ref)
            if req is not None:
                conf = req.get("kind") == "conference"
                is_video = kind == "vdial" or req.get("call_type") == "video"
                title = (
                    ("微信群视频通话" if is_video else "微信群语音通话") if conf
                    else ("微信视频通话" if is_video else "微信语音通话")
                )
                st.update(title=title, name=req.get("name") or "", status=req.get("status_text") or "",
                          group=conf, videoCall=is_video, videoEnabled=self.can_video(conf))
                if req.get("live"):
                    # The request's own status is the Telegram card's instruction; the page says it its own way.
                    st["phase"] = "dial"
                    if conf:
                        n = len(req.get("selected") or ())
                        st["status"] = f"将邀请 {n} 人" if n else "尚未选择成员"
                        st["dialable"] = self.can_dial(True, video=is_video) and 0 < n <= MAX_INVITEES
                        st["note"] = (
                            f"当前 agent-wechat 不支持发起群{'视频' if is_video else '语音'}通话"
                            if not self.can_dial(True, video=is_video)
                            else (f"点击「呼叫」并允许使用{'摄像头与' if is_video else ''}麦克风后发起群通话"
                                  if st["dialable"] else "请回到 Telegram 消息里勾选要邀请的成员")
                        )
                    else:
                        st["status"] = "准备就绪"
                        st["dialable"] = self.can_dial(False, video=is_video)
                        st["note"] = (
                            f"点击「呼叫」并允许使用{'摄像头与' if is_video else ''}麦克风后才会拨出"
                            if st["dialable"]
                            else f"当前 agent-wechat 不支持发起微信{'视频' if is_video else '语音'}通话"
                        )
            return st
        if kind == "invite":
            inv = self.bridge._conf_invites.get(ref)
            if inv is not None:
                st.update(phase="invite", title="微信群通话邀请" if self.can_video(True) else "微信群语音通话邀请",
                          name=inv.get("group_name") or "", caller=inv.get("caller_name") or "",
                          status=inv.get("status_text") or "🔔 邀请中…",
                          joinable=self.can_join(), videoEnabled=self.can_video(True), group=True)
            return st
        entry = self.bridge._active_calls.get(ref) if kind == "call" else None
        if entry is None:
            return st
        conf = entry["kind"] == "conference"
        is_video = entry.get("call_type") == "video"
        if conf:
            title = "微信群视频通话" if is_video else "微信群语音通话"
        elif entry.get("direction") == "outgoing":
            title = "微信视频通话" if is_video else "微信语音通话"
        else:
            title = "微信视频来电" if is_video else "微信语音来电"
        st.update(
            title=title, name=entry.get("caller_name") or "", status=entry.get("status_text") or "",
            group=conf, videoCall=is_video, videoEnabled=self.can_video(conf),
            remoteCamera=entry.get("remote_camera"), remoteMuted=entry.get("remote_muted"),
        )
        if conf and entry.get("participants"):
            self_wxid = getattr(self.bridge, "_self_wxid", "") or ""
            st["participants"] = [
                {
                    "id": p.get("participantId") or p.get("contactId") or "",
                    "contactId": p.get("contactId") or "",
                    "name": self._sync_participant_name(entry, p),
                    "joined": p.get("membership") == "joined",
                    "muted": (not p["microphoneEnabled"]) if "microphoneEnabled" in p else None,
                    "camera": p.get("cameraEnabled"),
                    "self": bool(self_wxid and p.get("contactId") == self_wxid),
                }
                for p in entry["participants"] if isinstance(p, dict)
            ]
        if entry.get("connected_at"):
            st["elapsed"] = max(0, int(time.time() - entry["connected_at"]))
        sess = self.sessions.get(entry["call_id"])
        if entry["terminal"]:
            st["phase"] = "ended"
        elif sess is not None and not sess.closed:
            phase = "calling" if self._ringing_out(entry) else ("active" if sess.attached else "connecting")
            st.update(
                phase=phase, mine=ws is not None and sess.active_ws is ws,
                muted=sess.muted, camera=sess.camera_enabled, rate=sess.rate, frame=sess.frame,
                videoSend=sess.video_send_state(), videoTracks=sess.video_tracks_state(),
                videoCall=bool(is_video or sess.camera_enabled or sess.video_send_track is not None or sess.video_rx_tracks),
            )
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
        app.router.add_get(p + "/worklet.js", self._worklet)
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

    async def _worklet(self, request: web.Request) -> web.Response:
        return web.Response(
            text=WORKLET_JS,
            content_type="application/javascript",
            charset="utf-8",
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def _page(self, request: web.Request) -> web.Response:
        start = request.query.get("tgWebAppStartParam", "")
        if start.startswith(READER_PREFIX):  # reader links share the Mini App's single URL
            return await self._reader_response(start[len(READER_PREFIX):])
        nonce = secrets.token_urlsafe(12)
        cfg = json.dumps({"base": self.prefix}, ensure_ascii=False)
        html_body = (
            PAGE_HTML.replace("__NONCE__", nonce)
            .replace("__CFG__", cfg)
            .replace("__WORKLET__", json.dumps(WORKLET_JS))
        )
        return web.Response(
            text=html_body,
            content_type="text/html", charset="utf-8",
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Permissions-Policy": "microphone=(self), camera=(self), speaker-selection=(self)",
                "Content-Security-Policy": (
                    "default-src 'none'; "
                    f"script-src 'nonce-{nonce}' blob: 'self'; "
                    "style-src 'unsafe-inline'; "
                    "connect-src 'self' ws: wss:; "
                    "img-src 'self' blob: data:; "
                    "media-src 'self' blob: mediastream:; "
                    "worker-src 'self' blob:; "
                    "base-uri 'none'; "
                    "form-action 'none'"
                ),
            },
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
        bot_token = self.bridge.cfg.telegram.bot_token
        init_data = str(msg.get("initData") or "")
        if init_data:
            uid = verify_init_data(init_data, bot_token)
            if uid is None:
                return None, "Telegram 身份校验失败，请从机器人消息重新打开"
            if uid != self.bridge.owner_id:
                return None, "只有桥接账号本人可以接听"
            return token, ""
        if self.app_name:
            handoff = str(msg.get("handoff") or msg.get("ticket") or "")
            if handoff:
                if verify_handoff_ticket(handoff, token, bot_token):
                    return token, ""
                return None, "浏览器跳转凭证无效或已过期，请从 Telegram Mini App 重新打开"
            return None, "请从 Telegram Mini App 打开通话页面"
        return token, ""

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=MAX_VIDEO_FRAME)
        await ws.prepare(request)
        token = None
        client_video_caps: dict | None = None
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
            hello_dict = hello if isinstance(hello, dict) else {}
            if isinstance(hello_dict.get("videoCaps"), dict):
                client_video_caps = hello_dict["videoCaps"]
            token, why = self._authorize(hello_dict)
            if token is None:
                await _safe_send(ws, {"type": "fatal", "message": why})
                await ws.close(code=4403, message=b"forbidden")
                return ws
            self.clients.setdefault(token, set()).add(ws)
            init_state = self._state(token, ws)
            if self.app_name:
                init_state["handoff"] = make_handoff_ticket(token, self.bridge.cfg.telegram.bot_token)
            await _safe_send(ws, init_state)
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    sess = self._session_for(token)
                    if sess is not None:
                        if msg.data.startswith(WXV1_MAGIC):
                            await sess.from_page_video(ws, msg.data)
                        else:
                            await sess.from_page(ws, msg.data)
                elif msg.type == WSMsgType.TEXT:
                    try:
                        cmd = json.loads(msg.data)
                    except ValueError:
                        continue
                    if isinstance(cmd, dict):
                        if isinstance(cmd.get("videoCaps"), dict):
                            client_video_caps = cmd["videoCaps"]
                        self._spawn(self._action(token, ws, cmd, client_video_caps))
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

    async def _action(self, token: str, ws: web.WebSocketResponse, cmd: dict,
                      client_video_caps: dict | None = None) -> None:
        action = cmd.get("type")
        if action == "diag":
            clean_diag = _sanitize_page_diag(cmd)
            if clean_diag:
                self.page_diags.setdefault(token, {}).update(clean_diag)
                sess = self._session_for(token)
                if sess is not None and (sess.active_ws is ws or sess.active_ws is None):
                    sess.page_diag.update(clean_diag)
                if clean_diag.get("err"):
                    log.warning("Call page (%s) reported audio issue: %s", token[:8], _format_page_diag(clean_diag))
            return
        kind, ref = self.tokens.get(token, ("", ""))
        vcaps = cmd.get("videoCaps") if isinstance(cmd.get("videoCaps"), dict) else client_video_caps
        cam_opt = bool(cmd["cameraEnabled"]) if "cameraEnabled" in cmd else None
        try:
            if kind == "invite":
                inv = self.bridge._conf_invites.get(ref)
                if inv is None:
                    raise CallActionError("邀请已结束或已过期")
                if action == "join":
                    await self.join(inv, ws, video_caps=vcaps, camera_enabled=cam_opt)
                return
            if kind in ("dial", "vdial"):
                req = self.bridge._dial_requests.get(ref)
                if req is None:
                    raise CallActionError("呼叫已取消或已过期，请重新发送 /call")
                if action == "dial":
                    ctype = "video" if (kind == "vdial" or cmd.get("callType") == "video") else None
                    await self.dial(req, ws, call_type=ctype, video_caps=vcaps, camera_enabled=cam_opt)
                return
            entry = self.bridge._active_calls.get(ref) if kind == "call" else None
            if entry is None:
                raise CallActionError("通话已结束")
            sess = self._session_for(token)
            if action == "answer":
                await self.answer(entry, ws, video_caps=vcaps, camera_enabled=cam_opt)
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
                if prev is not ws:
                    for tid, meta in list(sess.video_rx_tracks.items()):
                        if meta.get("active", True) and meta.get("codec") in ("h264", "h265"):
                            await sess._maybe_request_keyframe(tid)
                if (cam_opt is not None and cam_opt != sess.camera_enabled
                        and (entry.get("call_type") == "video" or sess.video_send_track is not None)):
                    try:
                        await self.set_camera(sess, cam_opt)
                        return  # set_camera already pushes state
                    except CallActionError:
                        pass
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
            elif action == "camera":
                if sess is None:
                    raise CallActionError("通话未由 Telegram 接管或已结束")
                await self.set_camera(sess, bool(cmd.get("enabled")))
            elif action == "request_keyframe":
                if sess is not None and isinstance(cmd.get("trackId"), int):
                    await sess.request_keyframe(cmd["trackId"])
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
WORKLET_JS = load_text("worklet.js")
PAGE_HTML = load_text("call.html")
