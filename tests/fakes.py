"""Shared fake agent-wechat daemons, Telegram clients, sockets, and helpers for offline tests."""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
from telethon import errors, functions, types

from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.core import Bridge

ACC = "acc1"


def _png(w=132, h=132, color=(10, 120, 200)) -> bytes:
    import io
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "PNG")
    return out.getvalue()


def _webp() -> bytes:
    import io
    from PIL import Image
    im = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    im.paste((255, 0, 0, 255), (100, 100, 400, 400))
    out = io.BytesIO()
    im.save(out, "WEBP")
    return out.getvalue()


AVATAR = _png()  # WeChat hands out ~132 px avatars


def _wxgf_sample(ffmpeg: str | None, w: int, h: int, frames: int, fps: int, *, mask: bool = True) -> bytes | None:
    """A wxgf laid out like WeChat's: one partition per frame, alternating gray alpha mask and colour frame,
    with parameter sets only on each stream's first frame. None when ffmpeg lacks libx265."""
    import re
    import subprocess
    if not ffmpeg or b"libx265" not in subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                                                      capture_output=True).stdout:
        return None

    def encode(tmp: str, name: str, src: list[str], pix_fmt: str) -> bytes:
        path = Path(tmp, name)
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", *src, "-frames:v", str(frames), "-c:v", "libx265",
                        "-x265-params", "log-level=none:bframes=0", "-r", str(fps), "-pix_fmt", pix_fmt,
                        "-f", "hevc", str(path)], check=True)
        return path.read_bytes()

    def access_units(stream: bytes) -> list[bytes]:
        starts = [m.start() for m in re.finditer(rb"\x00\x00\x00\x01|(?<!\x00)\x00\x00\x01", stream)]
        units, cur, has_slice = [], b"", False
        for i, s in enumerate(starts):
            nal = stream[s:starts[i + 1] if i + 1 < len(starts) else len(stream)]
            hdr = nal[nal.index(b"\x00\x00\x01") + 3:]
            kind = (hdr[0] >> 1) & 0x3F
            if has_slice and (kind > 31 or hdr[2] & 0x80):
                units.append(cur)
                cur, has_slice = b"", False
            cur += nal
            has_slice |= kind <= 31
        return units + [cur]

    with tempfile.TemporaryDirectory() as tmp:
        lavfi = ["-f", "lavfi", "-i"]
        colour = access_units(encode(tmp, "c.hevc", [*lavfi, f"testsrc2=s={w}x{h}:r={fps}"], "yuv420p"))
        alpha = access_units(encode(tmp, "m.hevc", [*lavfi, f"color=c=black:s={w}x{h}:r={fps}", "-vf",
                                                    f"geq=lum='if(lt(hypot(X-{w / 2},Y-{h / 2}),{min(w, h) / 3}),255,0)'"],
                                    "gray"))
    assert len(colour) == len(alpha) == frames, (len(colour), len(alpha))
    body = b"".join((len(m).to_bytes(4, "big") + m if mask else b"") + len(c).to_bytes(4, "big") + c
                    for c, m in zip(colour, alpha))
    return b"wxgf" + bytes([19, 0, 2]) + w.to_bytes(2, "big") + h.to_bytes(2, "big") + bytes(8) + body


class FakeDaemon:
    def __init__(self):
        self.messages: dict[str, dict] = {}
        self.threads: dict[str, dict] = {}
        self.contacts: dict[str, dict] = {}
        self.sends: list[tuple[dict, str]] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.sticker_preparations: list[tuple[dict, str]] = []
        self.expired_sticker_preps: set[str] = set()
        self.sse_body = ""
        self.marked_read: list[str] = []
        self.avatar_refreshed: list[str] = []
        self.avatar_op = {"id": "avop", "state": "succeeded"}
        self.avatar_lag = 0  # reads after a successful refresh that still miss (profile reader lag)
        self.media: dict[str, list[dict]] = {}  # message id -> media assets (default: one available PNG)
        self.media_bytes: dict[str, tuple[bytes, str]] = {}  # asset id -> (bytes, content_type)
        self.content_requests: list[tuple[str, str | None]] = []  # (asset id, representation)
        self.media_transfers: list[tuple[str, dict]] = []
        self.transfer_fail_variants: set[str] = set()
        self.transfer_fail_reasons: dict[str, str] = {}
        self.transfer_jobs: dict[str, dict] = {}
        self.group_members: dict[str, list[dict]] = {}
        self.member_searches: list[tuple[str, str | None]] = []
        self.recalls: list[tuple[str, dict, str]] = []
        self.recall_error: tuple[int, str] | None = None
        self.recall_op = {"id": "rcop", "state": "succeeded"}
        # friend requests / contact settings
        self.requests: dict[str, dict] = {}
        self.request_calls: list[tuple[str, str, dict, str | None]] = []  # (method, path, body, idempotency key)
        self.contact_op = {"id": "ctop", "state": "succeeded"}
        self.discovery: dict = {}
        self.sync_rounds: list[dict] = []  # result of each POST contact-sync; [] = one complete round
        self.sync_remarks: dict[str, str] = {}  # remarks a full contact sync brings in
        self.contact_syncs = 0
        self.contact_searches: list[dict] = []
        self.media_errors: dict[str, str] = {}  # message id -> 409 error code for its media list

    def add_thread(self, tid, name, kind="direct"):
        self.threads[tid] = {"id": tid, "accountId": ACC, "peerId": tid, "kind": kind, "displayName": name,
                             "unreadCount": 1, "readStateVersion": "v1"}

    def add_contact(self, cid, name, **extra):
        self.contacts[cid] = {"id": cid, "displayName": name, **extra}

    def add_request(self, rid, peer, name, message="hi", direction="incoming", actionable=True, **extra):
        self.requests[rid] = {"id": rid, "peerId": peer, "displayName": name, "direction": direction,
                              "message": message, "updatedAt": 1, "version": "v1", "isContact": False,
                              "actionable": actionable, "attention": actionable,
                              "state": "request_received" if direction == "incoming" else "request_sent",
                              "history": [], **extra}

    def add_message(self, mid, tid, body="hi", type_=1, direction="incoming", sender=None, server=None, **extra):
        self.messages[mid] = {"id": mid, "accountId": ACC, "threadId": tid, "serverId": server, "type": type_,
                              "senderId": sender or tid, "direction": direction, "body": body, **extra}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-token"
        path = request.url.path.removeprefix("/v1/")
        parts = path.split("/")
        m = request.method
        if path == "accounts":
            return httpx.Response(200, json={"items": [{"id": ACC, "alias": "me"}]})
        assert parts[:2] == ["accounts", ACC], path
        rest = parts[2:]
        if rest[0] == "messages" and len(rest) == 2:
            return httpx.Response(200, json=self.messages[rest[1]])
        if rest[0] == "messages" and len(rest) == 3 and rest[2] == "recall" and m == "POST":
            if self.recall_error:
                st, code = self.recall_error
                return httpx.Response(st, json={"error": {"code": code}})
            body = json.loads(request.content)
            self.recalls.append((rest[1], body, request.headers["idempotency-key"]))
            return httpx.Response(202, json={"id": "rcop", "state": "queued"})
        if rest[:2] == ["operations", "rcop"]:
            return httpx.Response(200, json=self.recall_op)
        if rest[0] == "messages" and len(rest) == 3 and rest[2] == "media-transfers" and m == "POST":
            body = json.loads(request.content)
            mid = rest[1]
            self.media_transfers.append((mid, body))
            jid = f"dljob{len(self.media_transfers)}"
            q = body.get("quality")
            if q in self.transfer_fail_variants or q in self.transfer_fail_reasons:
                self.transfer_jobs[jid] = {
                    "id": jid, "state": "failed", "reason": self.transfer_fail_reasons.get(q, "cdn_failed"),
                }
            else:
                for a in self.media.get(mid, []):
                    if (body.get("assetId") and a.get("id") == body["assetId"]) or (
                        not body.get("assetId") and q in (a.get("variant"), "best")
                    ):
                        a["state"] = "available"
                        a.setdefault("actualVariant", a.get("variant"))
                        break
                self.transfer_jobs[jid] = {"id": jid, "state": "available"}
            return httpx.Response(202, json={"id": jid, "state": "queued"})
        if rest[0] == "messages" and rest[2] == "media" and rest[1] in self.media_errors:
            return httpx.Response(409, json={"error": {"code": self.media_errors[rest[1]], "retryable": False}})
        if rest[0] == "messages" and rest[2] == "media" and rest[1] in self.media:
            return httpx.Response(200, json={"items": self.media[rest[1]]})
        if rest[0] == "messages" and rest[2] == "media":
            return httpx.Response(200, json={"items": [{"id": "asset-" + rest[1], "state": "available",
                                                        "variant": "original", "actualVariant": "original",
                                                        "contentType": "image/png", "byteLength": 4,
                                                        "filename": "pic.png"}]})
        if rest[0] == "media" and rest[2] == "content":
            representation = request.url.params.get("representation")
            self.content_requests.append((rest[1], representation))
            if representation and f"{rest[1]}#{representation}" in self.media_bytes:
                raw, ctype = self.media_bytes[f"{rest[1]}#{representation}"]
                return httpx.Response(200, content=raw, headers={"content-type": ctype})
            if rest[1] in self.media_bytes:
                raw, ctype = self.media_bytes[rest[1]]
                return httpx.Response(200, content=raw, headers={"content-type": ctype})
            return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
        if rest[0] == "threads" and len(rest) == 1:
            limit = int(request.url.params.get("limit", "20"))
            return httpx.Response(200, json={"items": list(self.threads.values())[:limit]})
        if rest[0] == "threads" and len(rest) == 2:
            t = self.threads.get(rest[1])
            return httpx.Response(200, json=t) if t else httpx.Response(404, json={"error": {"code": "not_found"}})
        if rest[0] == "threads" and len(rest) == 3 and rest[2] == "messages":
            items = [m for m in self.messages.values() if m.get("threadId") == rest[1]]
            return httpx.Response(200, json={"items": items})
        if rest[0] == "threads" and rest[2] == "read-state":
            self.marked_read.append(rest[1])
            return httpx.Response(200, json={})
        if rest[0] == "avatars" and len(rest) == 3 and m == "POST":  # .../refresh
            self.avatar_refreshed.append(rest[1])
            return httpx.Response(202, json={"id": "avop", "state": "running"})
        if rest[:2] == ["operations", "avop"]:
            return httpx.Response(200, json=self.avatar_op)
        if rest[0] == "avatars":
            if rest[1] in self.avatar_refreshed and self.avatar_op["state"] == "succeeded":
                if self.avatar_lag:
                    self.avatar_lag -= 1
                    return httpx.Response(404, json={"error": {"code": "avatar_unavailable"}})
                return httpx.Response(200, content=AVATAR, headers={"content-type": "image/png"})
            return httpx.Response(404, json={"error": {"code": "avatar_unavailable"}})
        if rest[0] == "groups" and len(rest) == 1:
            q = (request.url.params.get("search") or "").lower()
            items = [t for t in self.threads.values() if t.get("kind") == "group" and (
                not q or q in (t.get("displayName") or "").lower() or q in t["id"].lower())]
            return httpx.Response(200, json={"items": items})
        if rest[0] == "groups" and rest[2] == "members" and len(rest) == 3:
            limit_raw = request.url.params.get("limit")
            if limit_raw is not None:
                lim = int(limit_raw)
                if lim == 0 or lim > 200:
                    return httpx.Response(400, json={"error": {"code": "invalid_request"}})
            search = request.url.params.get("search")
            self.member_searches.append((rest[1], search))
            members = list(self.group_members.get(rest[1], []))
            if search:
                terms = search.lower().split()
                members = [m for m in members if all(
                    any(
                        term in str(m.get(k) or "").lower()
                        for k in ("id", "username", "displayName", "roomDisplayName", "nickname", "alias", "mentionName")
                    )
                    for term in terms
                )]
            cur = request.url.params.get("cursor")
            if cur is None and len(members) > 2:
                return httpx.Response(200, json={"items": members[:2], "nextCursor": "p2"})
            if cur == "p2":
                return httpx.Response(200, json={"items": members[2:], "nextCursor": None})
            return httpx.Response(200, json={"items": members, "nextCursor": None})
        if rest[0] == "groups" and rest[2] == "members":
            return httpx.Response(200, json={"state": "present", "member": {"displayName": "Alice in group"}})
        if rest[0] == "contact-requests" and len(rest) == 1 and m == "GET":
            params = dict(request.url.params)
            items = list(self.requests.values())
            if params.get("direction"):
                items = [r for r in items if r["direction"] == params["direction"]]
            if params.get("actionable"):
                items = [r for r in items if r["actionable"] == (params["actionable"] == "true")]
            pending = sum(1 for r in self.requests.values() if r["direction"] == "incoming" and r["actionable"])
            limit = int(params.get("limit", "20"))
            return httpx.Response(200, json={"items": items[:limit] + [{"id": "gone", "deleted": True}],
                                             "pendingIncomingCount": pending, "nextCursor": None})
        if rest[0] in ("contact-requests", "search", "contacts") and m in ("POST", "PATCH", "DELETE"):
            body = json.loads(request.content) if request.content else {}
            self.request_calls.append((m, "/".join(rest), body, request.headers.get("idempotency-key")))
            if rest == ["search", "contacts"]:
                if isinstance(self.discovery, tuple):
                    st, code = self.discovery
                    return httpx.Response(st, json={"error": {"code": code}})
                return httpx.Response(200, json={"displayName": body["query"], **self.discovery})
            if rest[0] == "contact-requests" and len(rest) >= 2:
                req = self.requests.get(rest[1])
                if req is None:
                    return httpx.Response(404, json={"error": {"code": "not_found"}})
                if body.get("expectedVersion") != req["version"]:
                    return httpx.Response(412, json={"error": {"code": "stale_contact_request"}})
            if rest[0] == "contacts" and m == "PATCH":
                cur = self.contacts.get(rest[1]) or {}
                if body.get("expectedVersion") != cur.get("privacyVersion"):
                    return httpx.Response(412, json={"error": {"code": "stale_contact_privacy"}})
            if self.contact_op["state"] == "succeeded":
                if rest[0] == "contact-requests" and len(rest) == 3:  # accept
                    req["state"], req["actionable"], req["isContact"] = "contact", False, True
                elif rest[0] == "contact-requests" and len(rest) == 2:  # delete
                    del self.requests[rest[1]]
                elif rest[0] == "contacts" and m == "PATCH" and "remark" in body:
                    cur.update(remark=body["remark"], displayName=body["remark"] or cur.get("nickname"))
                elif rest[0] == "contacts" and m == "PATCH":
                    cur["privacy"] = body["privacy"]
                    cur["privacyVersion"] = str(cur.get("privacyVersion") or "") + "x"
                elif rest[0] == "contacts" and m == "DELETE":
                    self.contacts.setdefault(rest[1], {"id": rest[1]})["relation"] = "non_contact"
            return httpx.Response(202, json={"id": "ctop", "state": "queued"})
        if rest[:2] == ["operations", "ctop"]:
            return httpx.Response(200, json=self.contact_op)
        if rest == ["contact-sync"] and m == "POST":
            self.contact_syncs += 1
            result = self.sync_rounds.pop(0) if self.sync_rounds else {"complete": True}
            if result.get("complete"):
                for cid, remark in self.sync_remarks.items():
                    self.contacts.setdefault(cid, {"id": cid}).update(remark=remark, displayName=remark)
            return httpx.Response(202, json={"id": "syncop", "state": "succeeded", "result": result})
        if rest[0] == "contact-requests" and len(rest) == 2:
            req = self.requests.get(rest[1])
            return httpx.Response(200, json=req) if req else httpx.Response(404, json={"error": {"code": "not_found"}})
        if rest[0] == "contacts" and len(rest) == 1:
            params = dict(request.url.params)
            self.contact_searches.append(params)
            q = (params.get("search") or "").lower()
            pool = {**{k: v for k, v in self.threads.items() if v.get("kind") != "group"}, **self.contacts}
            items = [c for c in pool.values() if not q or q in (c.get("displayName") or "").lower() or q in c["id"].lower()]
            if params.get("relation", "friend") != "all":
                items = [c for c in items if c.get("relation", "friend") == params.get("relation", "friend")]
            return httpx.Response(200, json={"items": items})
        if rest[0] == "contacts":
            if rest[1] in self.contacts:
                return httpx.Response(200, json=self.contacts[rest[1]])
            return httpx.Response(200, json={"id": rest[1], "displayName": "Contact " + rest[1]})
        if rest[0] == "uploads" and m == "POST":
            self.uploads.append((request.headers["content-type"], request.content))
            return httpx.Response(201, json={"id": f"src{len(self.uploads)}", "name": "x", "mediaType": "image/gif"})
        if rest[0] == "sticker-preparations" and m == "POST":
            body = json.loads(request.content)
            self.sticker_preparations.append((body, request.headers["idempotency-key"]))
            pid = f"prep{len(self.sticker_preparations)}"
            return httpx.Response(202, json={"id": pid, "kind": "sticker_prepare", "state": "queued"})
        if rest[0] == "sticker-preparations" and m == "GET":
            return httpx.Response(200, json={"id": rest[1], "kind": "sticker_prepare", "state": "available",
                                             "result": {"id": rest[1], "width": 240, "height": 240}})
        if rest[0] == "sends" and m == "POST":
            body = json.loads(request.content)
            if body.get("kind") == "sticker":
                pid = (body.get("source") or {}).get("preparationId")
                if pid in self.expired_sticker_preps:
                    self.expired_sticker_preps.discard(pid)
                    return httpx.Response(410, json={"error": {"code": "sticker_preparation_expired"}})
            self.sends.append((body, request.headers["idempotency-key"]))
            n = len(self.sends)
            if body.get("kind") == "sticker":
                return httpx.Response(202, json={"id": f"stjob{n}", "state": "queued", "kind": "sticker_send"})
            if "sourceId" in body:
                return httpx.Response(202, json={"id": f"job{n}", "state": "queued", "kind": "send"})
            return httpx.Response(202, json={"id": f"send{n}", "state": "queued"})
        if rest[0] == "sends" and m == "GET":
            n = rest[1].removeprefix("send")
            return httpx.Response(200, json={"id": rest[1], "state": "accepted", "serverId": f"90{n}"})
        if rest[0] == "jobs":
            if rest[1] in self.transfer_jobs:
                return httpx.Response(200, json=self.transfer_jobs[rest[1]])
            if rest[1].startswith("stjob"):
                n = rest[1].removeprefix("stjob")
                return httpx.Response(200, json={"id": rest[1], "state": "available", "kind": "sticker_send",
                                                 "result": {"serverAcceptance": "accepted",
                                                            "serverMessageId": f"80{n}",
                                                            "messageId": f"local-sticker-{n}"}})
            n = rest[1].removeprefix("job")
            return httpx.Response(200, json={"id": rest[1], "state": "accepted", "serverMessageId": f"80{n}",
                                             "result": {"messageIds": [f"local-media-{n}"]}})
        if rest[0] == "events" and len(rest) == 1:
            if request.url.params.get("newest") == "true":
                return httpx.Response(200, json={"items": [{"cursor": "42"}], "nextCursor": "42"})
            after = int(request.url.params.get("after", "0"))
            if after < 42:
                return httpx.Response(200, json={"items": [{"cursor": "42"}], "nextCursor": "42"})
            return httpx.Response(200, json={"items": [], "nextCursor": "42"})
        if rest[:2] == ["events", "stream"]:
            return httpx.Response(200, text=self.sse_body, headers={"content-type": "text/event-stream"})
        if rest[0] == "consumers":
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected {m} {path}")


class Sent:
    _next = 100

    def __init__(self, chat, text=None, file=None, kwargs=None):
        Sent._next += 1
        self.id = Sent._next
        self.chat, self.text, self.file, self.kwargs = chat, text, file, kwargs or {}
        # what Telethon exposes on the sent message (used to pair the bot's and your account's copies)
        self.message = text if text is not None else (self.kwargs.get("caption") or "")
        self.media = None if file is None else types.MessageMediaDocument()


class FakeBot:
    def __init__(self):
        self.sent: list[Sent] = []
        self.edited: list[tuple[int, int, str, dict]] = []
        self.deleted: list[int] = []
        self.commands: list = []
        self.dead_chats: set[int] = set()

    async def __call__(self, request):
        if isinstance(request, functions.bots.SetBotCommandsRequest):
            self.commands.append((type(request.scope), [(c.command, c.description) for c in request.commands]))
            return True
        raise AssertionError(f"unexpected bot request {request}")

    async def send_message(self, chat, text, **kw):
        if chat in self.dead_chats:
            raise ValueError("Could not find the input entity")
        s = Sent(chat, text=text, kwargs=kw)
        self.sent.append(s)
        return s

    async def edit_message(self, chat, msg_id, text, **kw):
        self.edited.append((chat, msg_id, text, kw))
        for s in self.sent:
            if s.chat == chat and s.id == msg_id:
                s.text = text
                s.message = text
                s.kwargs.update(kw)
                return s
        return Sent(chat, text=text, kwargs=kw)

    async def delete_messages(self, chat, ids):
        self.deleted.extend(ids)

    async def send_file(self, chat, file, **kw):
        s = Sent(chat, file=file, kwargs=kw)
        self.sent.append(s)
        return s


class FakeUser:
    def __init__(self):
        self.created: list[str] = []
        self.photos: list = []
        self.titles: list = []
        self.dialogs: list = []
        self.invited: list[int] = []
        self.sent_msgs: list[tuple[int, str]] = []
        self.on_invite = None

    async def iter_dialogs(self, limit=80):
        for d in self.dialogs[:limit]:
            yield d

    async def send_message(self, chat, text, **kw):
        self.sent_msgs.append((chat, text))
        return Sent(chat, text=text, kwargs=kw)

    async def __call__(self, request):
        if isinstance(request, functions.messages.CreateChatRequest):
            self.created.append(request.title)
            chat = types.Chat(id=500 + len(self.created), title=request.title, photo=types.ChatPhotoEmpty(),
                              participants_count=2, date=None, version=1)
            return types.messages.InvitedUsers(
                updates=types.Updates(updates=[], users=[], chats=[chat], date=None, seq=0), missing_invitees=[])
        if isinstance(request, functions.messages.EditChatPhotoRequest):
            self.photos.append((request.chat_id, request.photo.file[1]))
            return None
        if isinstance(request, functions.messages.EditChatTitleRequest):
            self.titles.append((request.chat_id, request.title))
            return None
        if isinstance(request, functions.messages.AddChatUserRequest):
            self.invited.append(-request.chat_id)
            if self.on_invite:
                self.on_invite(-request.chat_id)
            return None
        if isinstance(request, functions.channels.InviteToChannelRequest):
            self.invited.append(request.channel)
            if self.on_invite:
                self.on_invite(request.channel)
            return None
        raise AssertionError(f"unexpected request {request}")


def tg_event(chat_id, msg_id, text, reply_to=None):
    msg = SimpleNamespace(id=msg_id, message=text, action=None, via_bot_id=None, media=None,
                          reply_to=SimpleNamespace(reply_to_msg_id=reply_to) if reply_to else None)
    return SimpleNamespace(message=msg, chat_id=chat_id)


FORUM = -1009000000001
OWNER = 4242


class FakeForumBot(FakeBot):
    """The bot as admin of a forum supergroup (or standalone group): creates/edits topics and group info."""

    def __init__(self):
        super().__init__()
        self.topics: dict[int, tuple[str, int]] = {}  # topic id -> (title, icon colour)
        self.topic_edits: list[tuple[int, str]] = []
        self.group_titles: dict[int, str] = {}
        self.title_edits: list[tuple[int, str]] = []
        self.photo_edits: list[tuple[int, bytes]] = []
        self.deleted_topics: set[int] = set()
        self.next_topic = 10 ** 6  # far from Sent ids

    async def get_input_entity(self, chat):
        return types.InputPeerChannel(channel_id=abs(chat) - 10 ** 12, access_hash=0)

    async def upload_file(self, f, file_name=None):
        return ("uploaded", f.read())

    async def __call__(self, request):
        if isinstance(request, functions.messages.CreateForumTopicRequest):
            self.next_topic += 10
            tid = self.next_topic
            self.topics[tid] = (request.title, request.icon_color)
            created = types.MessageService(
                id=tid, peer_id=types.PeerChannel(9000000001),
                action=types.MessageActionTopicCreate(title=request.title, icon_color=request.icon_color))
            return types.Updates(updates=[types.UpdateMessageID(id=tid, random_id=1),
                                          types.UpdateNewChannelMessage(message=created, pts=1, pts_count=1)],
                                 users=[], chats=[], date=None, seq=0)
        if isinstance(request, functions.messages.EditForumTopicRequest):
            if self.topics.get(request.topic_id, ("",))[0] == request.title:
                raise errors.BadRequestError(request=None, message="TOPIC_NOT_MODIFIED")
            self.topic_edits.append((request.topic_id, request.title))
            self.topics[request.topic_id] = (request.title, self.topics[request.topic_id][1])
            return types.Updates(updates=[], users=[], chats=[], date=None, seq=0)
        if isinstance(request, functions.messages.EditChatTitleRequest):
            if self.group_titles.get(request.chat_id) == request.title:
                raise errors.ChatNotModifiedError(request=None)
            self.group_titles[request.chat_id] = request.title
            self.title_edits.append((request.chat_id, request.title))
            return None
        if isinstance(request, functions.channels.EditTitleRequest):
            cid = request.channel.channel_id
            if self.group_titles.get(cid) == request.title:
                raise errors.ChatNotModifiedError(request=None)
            self.group_titles[cid] = request.title
            self.title_edits.append((cid, request.title))
            return None
        if isinstance(request, functions.messages.EditChatPhotoRequest):
            self.photo_edits.append((request.chat_id, request.photo.file[1]))
            return None
        if isinstance(request, functions.channels.EditPhotoRequest):
            self.photo_edits.append((request.channel.channel_id, request.photo.file[1]))
            return None
        return await super().__call__(request)

    async def send_message(self, chat, text, **kw):
        if kw.get("reply_to") in self.deleted_topics:
            raise errors.TopicDeletedError(request=None)
        return await super().send_message(chat, text, **kw)


def forum_event(chat_id, msg_id, text, topic=None, reply_to=None, *, is_forum=True):
    """A message of yours in a forum group, as the bot sees it. topic=None: the General topic."""
    if topic is None:
        rt = SimpleNamespace(reply_to_msg_id=reply_to, reply_to_top_id=None, forum_topic=False) if reply_to else None
    elif reply_to:
        rt = SimpleNamespace(forum_topic=True, reply_to_msg_id=reply_to, reply_to_top_id=topic)
    else:
        rt = SimpleNamespace(forum_topic=True, reply_to_msg_id=topic, reply_to_top_id=None)
    msg = SimpleNamespace(id=msg_id, message=text, action=None, via_bot_id=None, media=None, reply_to=rt)

    async def get_chat():
        return SimpleNamespace(forum=is_forum, title="WeChat")
    return SimpleNamespace(message=msg, chat_id=chat_id, sender_id=OWNER, get_chat=get_chat)


OA_FORUM = -1009000000002


class LoginDaemon:
    """agent-wechat with no account yet; QR login goes awaiting_scan(1) -> awaiting_scan(2) -> succeeded."""

    def __init__(self):
        self.accounts: list[dict] = []
        self.authenticated = False
        self.ops = [{"state": "awaiting_scan", "result": {"qrGeneration": 1, "qrAvailable": True}},
                    {"state": "awaiting_scan", "result": {"qrGeneration": 1, "qrAvailable": True}},
                    {"state": "awaiting_scan", "result": {"qrGeneration": 2, "qrAvailable": True}},
                    {"state": "awaiting_approval", "result": {}},
                    {"state": "succeeded", "result": {}}]
        self.ops_by_strategy: dict[str, list[dict]] = {}
        self.login_strategies: list[str] = []
        self.cancelled_ops: list[str] = []
        self.qr_fetches: list[str] = []
        self.connected = False
        self.account_infos: list[dict] = []  # optional scripted responses for GET accounts/{ACC}
        self.login_errors: list[tuple[int, str]] = []  # optional scripted errors for POST auth/login
        self.connect_errors: list[tuple[int, str]] = []  # optional scripted errors for POST connect

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/")
        m = request.method
        if path == "accounts" and m == "GET":
            return httpx.Response(200, json={"items": self.accounts})
        if path == "accounts" and m == "POST":
            self.accounts.append({"id": ACC, "alias": json.loads(request.content)["alias"]})
            return httpx.Response(201, json=self.accounts[-1])
        if path == f"accounts/{ACC}":
            if self.account_infos:
                info = self.account_infos.pop(0) if len(self.account_infos) > 1 else self.account_infos[0]
                return httpx.Response(200, json={"id": ACC, **info})
            conn = "online" if self.connected else "disconnected"
            return httpx.Response(200, json={"id": ACC, "authenticated": self.authenticated, "connection": conn})
        if path == f"accounts/{ACC}/connect":
            if self.connect_errors:
                st, code = self.connect_errors.pop(0)
                return httpx.Response(st, json={"error": {"code": code}})
            self.connected = True
            return httpx.Response(200, json={})
        if path == f"accounts/{ACC}/auth/login":
            assert request.headers["idempotency-key"]
            if self.login_errors:
                st, code = self.login_errors.pop(0)
                return httpx.Response(st, json={"error": {"code": code}})
            strategy = json.loads(request.content).get("strategy", "auto")
            self.login_strategies.append(strategy)
            if strategy in self.ops_by_strategy:
                self.ops = list(self.ops_by_strategy[strategy])
            return httpx.Response(202, json={"id": "op1", "kind": "login", **self.ops[0]})
        if path == f"accounts/{ACC}/operations/op1" and m == "DELETE":
            self.cancelled_ops.append("op1")
            return httpx.Response(200, json={})
        if path == f"accounts/{ACC}/operations/op1":
            if len(self.ops) > 1:
                self.ops.pop(0)
            if self.ops[0]["state"] == "succeeded":
                self.authenticated = True
            return httpx.Response(200, json={"id": "op1", **self.ops[0]})
        if path == f"accounts/{ACC}/operations/op1/qr":
            self.qr_fetches.append(request.url.params.get("generation"))
            return httpx.Response(200, content=b"\x89PNG-qr", headers={"content-type": "image/png"})
        if path == f"accounts/{ACC}/contact-requests" and m == "GET":
            return httpx.Response(200, json={"items": [], "pendingIncomingCount": 0})
        raise AssertionError(f"unexpected {m} {path}")


class _FakeCallsSocket:
    def __init__(self):
        self.closed = False
        self.commands: list[dict] = []
        self.responses: list[dict] = []
        self.on_command = None

    async def command(self, payload: dict, timeout: float = 10.0) -> dict:
        self.commands.append(payload)
        if self.on_command:
            resp = await self.on_command(payload)
            if resp is not None:
                return resp
        if self.responses:
            return self.responses.pop(0)
        return {"type": "command.result", "requestId": payload.get("requestId", "r1"), "status": "accepted"}

    async def close(self) -> None:
        self.closed = True


class _CallbackEvent:
    def __init__(self, sender_id: int, data: bytes, chat_id: int | None = None):
        self.sender_id = sender_id
        self.chat_id = sender_id if chat_id is None else chat_id
        self.data = data
        self.answers: list[tuple[str | None, bool]] = []
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, message: str | None = None, *, alert: bool = False) -> None:
        self.answers.append((message, alert))

    async def edit(self, text: str, **kw) -> None:
        self.edits.append((text, kw))


class _FakeMediaSocket:
    """Stands in for the agent-wechat media socket of one call."""

    def __init__(self):
        self.sent: list[dict] = []
        self.sent_bytes: list[bytes] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send_json(self, obj: dict) -> None:
        self.sent.append(obj)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def events(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(None)


def _init_data(bot_token: str, user_id: int, auth_date: int | None = None) -> str:
    import hashlib
    import hmac
    import time
    from urllib.parse import urlencode
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAE",
              "user": json.dumps({"id": user_id, "first_name": "Me"}, separators=(",", ":"))}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def _button_urls(sent) -> list[str]:
    urls = [getattr(getattr(b, "type", None), "url", None) or getattr(b, "url", None)
            for row in (sent.kwargs.get("buttons") or []) for b in row]
    return [u for u in urls if u]


def _button_texts(sent) -> list[str]:
    return [b.text for row in (sent.kwargs.get("buttons") or []) for b in row]


class BridgeTestMixin:
    """Shared Bridge + FakeDaemon + FakeUser + FakeBot fixture for bridge tests."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(WeChatConfig(token="secret-token"), TelegramConfig(api_id=1, api_hash="x", bot_token="y"),
                     BridgeConfig(create_interval_seconds=0, sync_avatar=True), Path(self.tmp.name) / "config.toml")
        self.daemon = FakeDaemon()
        self.b = Bridge(cfg, user_client=FakeUser(), bot_client=FakeBot())
        await self.b.wx.http.aclose()
        self.b.wx.http = httpx.AsyncClient(transport=httpx.MockTransport(self.daemon.handler),
                                           headers={"Authorization": "Bearer secret-token"})
        self.b.wx._poll_initial_delay = 0.0
        self.b.wx._avatar_poll_interval = 0.0
        self.b._bot_pair_wait = 0.0
        self.b.bot_input = object()
        self.b.bot_me = SimpleNamespace(username="mybot")
        await self.b.wx.resolve_account()

    async def asyncTearDown(self):
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def event(self, mid, tid, origin="live", direction="incoming"):
        await self.b.handle_event({"cursor": "1", "kind": "message.committed",
                                   "data": {"messageId": mid, "threadId": tid, "origin": origin,
                                            "direction": direction}})
