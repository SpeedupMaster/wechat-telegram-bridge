"""Offline tests: fake agent-wechat daemon (httpx.MockTransport) + fake Telegram clients.

Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from telethon import errors, functions, types

from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.core import Bridge
from bridge.wechat import CursorExpired, WeChatClient

ACC = "acc1"


def _png(w=132, h=132, color=(10, 120, 200)) -> bytes:
    import io
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "PNG")
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
        self.stickers_supported = True
        self.daemon_version = "2.5.0"
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
        self.reject_image_quality = False
        self.group_members: dict[str, list[dict]] = {}
        self.member_searches: list[tuple[str, str | None]] = []
        self.ignore_member_search = False
        self.recalls: list[tuple[str, dict, str]] = []
        self.recall_error: tuple[int, str] | None = None
        self.recall_op = {"id": "rcop", "state": "succeeded"}
        # friend requests / contact settings (agent-wechat >= 2.7)
        self.requests: dict[str, dict] = {}
        self.request_calls: list[tuple[str, str, dict, str | None]] = []  # (method, path, body, idempotency key)
        self.contact_op = {"id": "ctop", "state": "succeeded"}
        self.discovery: dict = {}
        self.privacy: dict[str, dict] = {}
        self.sync_rounds: list[dict] = []  # result of each POST contact-sync; [] = one complete round
        self.sync_remarks: dict[str, str] = {}  # remarks a full contact sync brings in
        self.contact_syncs = 0
        self.relations_supported = True
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
        if path == "status" and m == "GET":
            return httpx.Response(200, json={"state": "ready", "version": {"version": self.daemon_version}})
        if path == "accounts":
            return httpx.Response(200, json={"items": [{"id": ACC, "alias": "me"}]})
        assert parts[:2] == ["accounts", ACC], path
        rest = parts[2:]
        if rest[0] == "capabilities" and m == "GET":
            return httpx.Response(200, json={"nativeRuntime": True, "stickers": self.stickers_supported})
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
            search = request.url.params.get("search")
            self.member_searches.append((rest[1], search))
            members = list(self.group_members.get(rest[1], []))
            if search and not self.ignore_member_search:
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
            if rest[0] == "contacts" and m == "PATCH" and "remark" in body:
                cur = self.contacts.get(rest[1]) or {}
                if body.get("expectedVersion") != cur.get("privacyVersion"):
                    return httpx.Response(412, json={"error": {"code": "stale_contact_privacy"}})
            elif rest[0] == "contacts" and m == "PATCH":
                cur = self.privacy.get(rest[1]) or {}
                if body.get("expectedVersion") != cur.get("version"):
                    return httpx.Response(412, json={"error": {"code": "stale_contact_privacy"}})
            if self.contact_op["state"] == "succeeded":
                if rest[0] == "contact-requests" and len(rest) == 3:  # accept
                    req["state"], req["actionable"], req["isContact"] = "contact", False, True
                elif rest[0] == "contact-requests" and len(rest) == 2:  # delete
                    del self.requests[rest[1]]
                elif rest[0] == "contacts" and m == "PATCH" and "remark" in body:
                    cur.update(remark=body["remark"], displayName=body["remark"] or cur.get("nickname"))
                elif rest[0] == "contacts" and m == "PATCH":
                    self.privacy[rest[1]] = {"privacy": body["privacy"], "version": cur["version"] + "x"}
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
        if rest[0] == "contacts" and len(rest) == 3 and rest[2] == "privacy":
            if rest[1] not in self.privacy:
                return httpx.Response(409, json={"error": {"code": "contact_privacy_unresolved"}})
            return httpx.Response(200, json=self.privacy[rest[1]])
        if rest[0] == "contacts" and len(rest) == 1:
            params = dict(request.url.params)
            self.contact_searches.append(params)
            if "relation" in params and not self.relations_supported:
                return httpx.Response(400, json={"error": {"code": "invalid_request"}})
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
            if self.reject_image_quality and "imageQuality" in body:
                return httpx.Response(400, json={"error": {"code": "invalid_request"}})
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


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(WeChatConfig(token="secret-token"), TelegramConfig(api_id=1, api_hash="x", bot_token="y"),
                     BridgeConfig(create_interval_seconds=0, sync_avatar=True), Path(self.tmp.name) / "config.toml")
        self.daemon = FakeDaemon()
        self.b = Bridge(cfg)
        await self.b.wx.http.aclose()
        self.b.wx.http = httpx.AsyncClient(transport=httpx.MockTransport(self.daemon.handler),
                                           headers={"Authorization": "Bearer secret-token"})
        self.b.user, self.b.bot = FakeUser(), FakeBot()
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

    async def test_incoming_text_creates_group_once(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m1", "wxid_bob", "hello <b>")
        self.daemon.add_message("m2", "wxid_bob", "second")
        await self.event("m1", "wxid_bob")
        await self.event("m2", "wxid_bob")
        await self.event("m2", "wxid_bob")  # duplicate event is ignored
        self.assertEqual(self.b.user.created, ["Bob"])
        chat = self.b.store.chat_for_thread("wxid_bob")
        self.assertEqual(chat, -501)
        texts = [s.text for s in self.b.bot.sent]
        self.assertIn("已连接微信联系人", texts[0])
        self.assertEqual(texts[1:], ["hello &lt;b&gt;", "second"])

    async def test_group_message_shows_sender(self):
        self.daemon.add_thread("123@chatroom", "Team", kind="group")
        self.daemon.add_message("g1", "123@chatroom", "hey", sender="wxid_alice")
        await self.event("g1", "123@chatroom")
        self.assertEqual(self.b.user.created, ["👥 Team"])
        self.assertEqual(self.b.bot.sent[-1].text, "<b>Alice in group</b>：\nhey")

    async def test_non_live_and_official_accounts_skipped(self):
        self.daemon.add_thread("gh_news", "News")
        self.daemon.threads["gh_news"]["classification"] = {"category": "business"}
        self.daemon.add_message("o1", "gh_news")
        self.daemon.add_thread("wxid_old", "Old")
        self.daemon.add_message("h1", "wxid_old")
        await self.event("o1", "gh_news")
        await self.event("h1", "wxid_old", origin="sync")
        self.assertEqual(self.b.user.created, [])

    async def test_caught_up_filehelper_message_forwarded_but_not_history(self):
        import time
        from datetime import datetime, timezone
        self.b.since = time.time()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.daemon.add_thread("filehelper", "文件传输助手", kind="system")
        self.daemon.add_message("old", "filehelper", "old note", direction="outgoing", server="1",
                                createdAt="2020-01-01T00:00:00Z")
        self.daemon.add_message("new", "filehelper", "test", direction="outgoing", server="2", createdAt=now)
        await self.event("old", "filehelper", origin="catchup", direction="outgoing")
        self.assertEqual(self.b.user.created, [])
        await self.event("new", "filehelper", origin="catchup", direction="outgoing")
        self.assertEqual(self.b.user.created, ["文件传输助手"])
        self.assertIn("test", self.b.bot.sent[-1].text)

    async def test_avatar_fetched_from_wechat_when_not_cached(self):
        self.b.user.photos = []

        async def upload_file(f, file_name=None):
            return ("uploaded", f.read())
        self.b.user.upload_file = upload_file
        why = await self.b.sync_avatar(-501, "wxid_bob")
        self.assertEqual(why, "")
        self.assertEqual(self.daemon.avatar_refreshed, ["wxid_bob"])
        [(chat, uploaded)] = self.b.user.photos
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(uploaded))
        self.assertEqual((chat, im.format, im.size), (501, "JPEG", (512, 512)))  # Telegram rejects 132 px

    def test_wxgf_avatar_decoded(self):
        """WeChat may store avatars in its own HEVC-based wxgf format."""
        import io
        import subprocess
        from PIL import Image
        from bridge import stickers
        from bridge.core import _avatar_photo
        ffmpeg = stickers.find_ffmpeg()
        if not ffmpeg or b"libx265" not in subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                                                          capture_output=True).stdout:
            self.skipTest("needs ffmpeg with libx265 to build a wxgf sample")
        with tempfile.TemporaryDirectory() as tmp:
            src, hevc = Path(tmp, "a.png"), Path(tmp, "a.hevc")
            src.write_bytes(_png(132, 132, (200, 30, 30)))
            subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(src), "-c:v", "libx265",
                            "-x265-params", "log-level=none", "-pix_fmt", "yuv420p", "-f", "hevc", str(hevc)], check=True)
            stream = hevc.read_bytes()
        header = b"wxgf" + bytes([19]) + bytes([0, 2]) + (132).to_bytes(2, "big") * 2 + bytes(8)
        wxgf = header + len(stream).to_bytes(4, "big") + stream
        im = Image.open(io.BytesIO(_avatar_photo(wxgf)))
        self.assertEqual(im.size, (512, 512))
        r, g, b = im.getpixel((256, 256))
        self.assertTrue(r > 150 and g < 80 and b < 80, (r, g, b))

    def test_unreadable_avatar_is_an_error_not_uploaded(self):
        from bridge.core import _avatar_photo
        with self.assertRaises(ValueError):
            _avatar_photo(b"JPEGDATA")

    async def test_small_avatar_scaled_up_for_telegram(self):
        import io
        from PIL import Image
        from bridge.core import _avatar_photo
        src = io.BytesIO()
        Image.new("RGB", (132, 120), (10, 120, 200)).save(src, "PNG")
        im = Image.open(io.BytesIO(_avatar_photo(src.getvalue())))
        self.assertEqual((im.format, im.size), ("JPEG", (512, 512)))
        big = io.BytesIO()
        Image.new("RGB", (800, 600)).save(big, "PNG")
        self.assertEqual(Image.open(io.BytesIO(_avatar_photo(big.getvalue()))).size, (600, 600))

    async def test_avatar_read_retried_after_refresh(self):
        self.daemon.avatar_lag = 2
        data, why = await self.b.wx.fetch_avatar("wxid_bob", retry_delays=(0, 0, 0))
        self.assertEqual((data, why), (AVATAR, ""))

    async def test_avatar_failure_reason_reported(self):
        self.daemon.avatar_op = {"id": "avop", "state": "failed", "result": {"code": "avatar_not_advertised"}}
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m1", "wxid_bob", "hi")
        self.b.cfg.bridge.sync_avatar = False
        await self.event("m1", "wxid_bob")
        chat = self.b.store.chat_for_thread("wxid_bob")
        await self.b.on_tg_message(tg_event(chat, 7000, "/avatar"))
        self.assertEqual(self.b.bot.sent[-1].text, "没有拿到这个会话的微信头像（refresh failed: avatar_not_advertised）")

    async def test_image_is_downloaded_and_sent_as_file(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("img1", "wxid_bob", None, type_=3)
        await self.event("img1", "wxid_bob")
        s = self.b.bot.sent[-1]
        self.assertEqual(s.file.read(), b"\x89PNG")
        self.assertEqual(s.file.name, "pic.png")

    async def test_sticker_is_forwarded_as_telegram_sticker(self):
        import io
        from PIL import Image
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("st1", "wxid_bob", None, type_=47)
        png = _png(240, 240, (255, 0, 0))
        self.daemon.media["st1"] = [{"id": "asset-st1", "kind": "sticker", "state": "available", "variant": "original",
                                     "actualVariant": "original", "contentType": "image/png", "byteLength": len(png)}]
        self.daemon.media_bytes["asset-st1"] = (png, "image/png")
        await self.event("st1", "wxid_bob")
        s = self.b.bot.sent[-1]
        self.assertEqual(s.file.name, "sticker.webp")
        self.assertEqual(s.kwargs["mime_type"], "image/webp")
        self.assertFalse(s.kwargs["force_document"])
        self.assertTrue(any(isinstance(a, types.DocumentAttributeSticker) for a in s.kwargs["attributes"]))
        im = Image.open(io.BytesIO(s.file.read()))
        self.assertEqual((im.format, im.size), ("WEBP", (512, 512)))

    async def test_group_animated_sticker_sends_header_then_webm_sticker(self):
        import io
        from PIL import Image
        from bridge import stickers
        if not stickers.find_ffmpeg():
            self.skipTest("ffmpeg not installed")
        frames = [Image.new("RGBA", (120, 120), (i * 40, 200, 0, 255)) for i in range(4)]
        gif_buf = io.BytesIO()
        frames[0].save(gif_buf, "GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
        gif_bytes = gif_buf.getvalue()

        self.daemon.add_thread("123@chatroom", "Team", kind="group")
        self.daemon.add_message("st_anim", "123@chatroom", None, type_=47, sender="wxid_alice")
        self.daemon.media["st_anim"] = [{"id": "asset-st_anim", "kind": "sticker", "state": "available",
                                          "variant": "original", "actualVariant": "original",
                                          "contentType": "image/gif", "byteLength": len(gif_bytes)}]
        self.daemon.media_bytes["asset-st_anim"] = (gif_bytes, "image/gif")
        await self.event("st_anim", "123@chatroom")
        header_msg, sticker_msg = self.b.bot.sent[-2], self.b.bot.sent[-1]
        self.assertEqual(header_msg.text, "<b>Alice in group</b>：")
        self.assertEqual(sticker_msg.file.name, "sticker.webm")
        self.assertEqual(sticker_msg.kwargs["mime_type"], "video/webm")
        self.assertTrue(sticker_msg.kwargs["nosound_video"])
        self.assertTrue(any(isinstance(a, types.DocumentAttributeSticker) for a in sticker_msg.kwargs["attributes"]))
        self.assertTrue(any(isinstance(a, types.DocumentAttributeVideo) and a.nosound
                            for a in sticker_msg.kwargs["attributes"]))

    async def test_animated_wxgf_sticker_fetched_raw_and_sent_as_video_sticker(self):
        """agent-wechat's wxgf preview is one still frame, so stickers fetch the original wxgf bytes;
        when those can't be decoded the bridge falls back to the preview."""
        from bridge import stickers
        anim = _wxgf_sample(stickers.find_ffmpeg(), 160, 160, 12, 12)
        if anim is None:
            self.skipTest("needs ffmpeg with libx265 to build a wxgf sample")
        self.daemon.add_thread("wxid_bob", "Bob")
        preview = _png(160, 160, (255, 0, 0))
        for mid, raw in (("st_wx", anim), ("st_bad", b"wxgf" + bytes([19, 0, 2]) + bytes(40))):
            self.daemon.add_message(mid, "wxid_bob", None, type_=47)
            self.daemon.media[mid] = [{"id": f"asset-{mid}", "kind": "sticker", "state": "available",
                                       "variant": "original", "actualVariant": "original",
                                       "contentType": "image/x-wxgf", "byteLength": len(raw),
                                       "preview": {"contentType": "image/png"}}]
            self.daemon.media_bytes[f"asset-{mid}"] = (raw, "image/x-wxgf")
            self.daemon.media_bytes[f"asset-{mid}#preview"] = (preview, "image/png")

        await self.event("st_wx", "wxid_bob")
        s = self.b.bot.sent[-1]
        self.assertEqual(self.daemon.content_requests, [("asset-st_wx", None)])
        self.assertEqual((s.file.name, s.kwargs["mime_type"]), ("sticker.webm", "video/webm"))
        [video] = [a for a in s.kwargs["attributes"] if isinstance(a, types.DocumentAttributeVideo)]
        self.assertEqual(video.duration, 1.0)

        await self.event("st_bad", "wxid_bob")
        s = self.b.bot.sent[-1]
        self.assertEqual(self.daemon.content_requests[1:], [("asset-st_bad", None), ("asset-st_bad", "preview")])
        self.assertEqual((s.file.name, s.kwargs["mime_type"]), ("sticker.webp", "image/webp"))

    async def test_telegram_stickers_sent_as_native_wechat_stickers_and_echo_dropped(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m0", "wxid_bob", "hi", server="100")
        await self.event("m0", "wxid_bob")
        chat = self.b.store.chat_for_thread("wxid_bob")

        webp = StickerTests.webp()
        async def dl_webp(file=None):
            return webp
        ev_webp = SimpleNamespace(chat_id=chat, message=SimpleNamespace(
            id=8001, message="", action=None, via_bot_id=None, reply_to=None,
            photo=None, sticker=True, voice=None, video=None, video_note=None, gif=None,
            file=SimpleNamespace(size=len(webp), mime_type="image/webp", name=None, ext=".webp"),
            media=types.MessageMediaDocument(), download_media=dl_webp))
        await self.b.on_tg_message(ev_webp)

        self.assertEqual(len(self.daemon.sticker_preparations), 1)
        prep_body, prep_key = self.daemon.sticker_preparations[-1]
        self.assertEqual((prep_body["sourceId"], prep_body["fit"], prep_key), ("src1", True, f"tg-{chat}-8001-prep"))
        send_body, send_key = self.daemon.sends[-1]
        self.assertEqual(send_body, {"kind": "sticker", "threadId": "wxid_bob",
                                     "source": {"kind": "prepared", "preparationId": "prep1"}})
        self.assertEqual(send_key, f"tg-{chat}-8001")
        self.assertEqual(self.b.store.wx_for_tg(chat, 8001), ("local-sticker-1", "801"))
        self.assertFalse(any("发送到微信失败" in (s.text or "") for s in self.b.bot.sent))

        # Echo from daemon is dropped cleanly
        sent_before = len(self.b.bot.sent)
        self.daemon.add_message("echo_st", "wxid_bob", "", type_=47, direction="outgoing", server="801")
        await self.event("echo_st", "wxid_bob", direction="outgoing")
        self.assertEqual(len(self.b.bot.sent), sent_before)

        # Animated .tgs sticker also uses native sticker preparation + send
        tgs = StickerTests.tgs()
        async def dl_tgs(file=None):
            return tgs
        ev_tgs = SimpleNamespace(chat_id=chat, message=SimpleNamespace(
            id=8002, message="", action=None, via_bot_id=None, reply_to=None,
            photo=None, sticker=True, voice=None, video=None, video_note=None, gif=None,
            file=SimpleNamespace(size=len(tgs), mime_type="application/x-tgsticker", name="a.tgs", ext=".tgs"),
            media=types.MessageMediaDocument(), download_media=dl_tgs))
        await self.b.on_tg_message(ev_tgs)
        self.assertEqual(len(self.daemon.sticker_preparations), 2)
        self.assertEqual(self.daemon.sends[-1][0]["kind"], "sticker")
        self.assertEqual(self.b.store.wx_for_tg(chat, 8002), ("local-sticker-2", "802"))

    async def test_sticker_prep_and_disk_cache_reuse_and_expiry_recovery(self):
        from unittest import mock

        self.daemon.add_thread("wxid_bob", "Bob")
        self.b.store.link("wxid_bob", -1002, "Bob")
        chat = -1002
        tgs = StickerTests.tgs()

        def make_tgs_ev(mid):
            async def dl(file=None):
                return tgs
            return SimpleNamespace(chat_id=chat, message=SimpleNamespace(
                id=mid, message="", action=None, via_bot_id=None, reply_to=None,
                photo=None, sticker=True, voice=None, video=None, video_note=None, gif=None,
                file=SimpleNamespace(size=len(tgs), mime_type="application/x-tgsticker", name="a.tgs", ext=".tgs"),
                media=types.MessageMediaDocument(), download_media=dl))

        # 1) First send populates both disk cache and WeChatClient preparationId cache
        await self.b.on_tg_message(make_tgs_ev(8011))
        self.assertEqual(len(self.daemon.uploads), 1)
        self.assertEqual(len(self.daemon.sticker_preparations), 1)
        self.assertEqual(self.daemon.sends[-1][0]["source"]["preparationId"], "prep1")

        # 2) Repeat send reuses converted GIF from disk cache AND reuses preparationId without re-uploading
        with mock.patch("bridge.stickers.tgs_to_wechat_gif", side_effect=AssertionError("should hit disk cache")):
            await self.b.on_tg_message(make_tgs_ev(8012))
        self.assertEqual(len(self.daemon.uploads), 1)
        self.assertEqual(len(self.daemon.sticker_preparations), 1)
        self.assertEqual(self.daemon.sends[-1][0]["source"]["preparationId"], "prep1")

        # 3) If daemon rejects cached preparationId as expired, client invalidates, re-prepares, and retries cleanly
        self.daemon.expired_sticker_preps.add("prep1")
        with mock.patch("bridge.stickers.tgs_to_wechat_gif", side_effect=AssertionError("should hit disk cache")):
            await self.b.on_tg_message(make_tgs_ev(8013))
        self.assertEqual(len(self.daemon.uploads), 2)
        self.assertEqual(len(self.daemon.sticker_preparations), 2)
        self.assertEqual(self.daemon.sends[-1][0]["source"]["preparationId"], "prep2")
        self.assertEqual(self.daemon.sends[-1][1], f"tg-{chat}-8013-retry")

    async def test_telegram_sticker_falls_back_when_daemon_lacks_sticker_support(self):
        self.daemon.stickers_supported = False
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m0", "wxid_bob", "hi", server="100")
        await self.event("m0", "wxid_bob")
        chat = self.b.store.chat_for_thread("wxid_bob")

        webp = StickerTests.webp()
        async def dl_webp(file=None):
            return webp
        ev_webp = SimpleNamespace(chat_id=chat, message=SimpleNamespace(
            id=8003, message="", action=None, via_bot_id=None, reply_to=None,
            photo=None, sticker=True, voice=None, video=None, video_note=None, gif=None,
            file=SimpleNamespace(size=len(webp), mime_type="image/webp", name=None, ext=".webp"),
            media=types.MessageMediaDocument(), download_media=dl_webp))
        await self.b.on_tg_message(ev_webp)
        self.assertEqual(self.daemon.sticker_preparations, [])
        self.assertEqual(self.daemon.sends[-1][0]["kind"], "image")

    async def test_telegram_gif_converted_to_wechat_sticker_with_fallbacks(self):
        import io
        from unittest import mock
        from PIL import Image

        self.daemon.add_thread("wxid_bob", "Bob")
        self.b.store.link("wxid_bob", -1001, "Bob")
        chat = -1001

        frames = [Image.new("RGBA", (120, 120), (i * 80, 120, 200, 255)) for i in range(3)]
        buf = io.BytesIO()
        frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
        gif_bytes = buf.getvalue()

        def make_gif_ev(mid, payload, mime, name, ext):
            async def dl(file=None):
                return payload
            return SimpleNamespace(chat_id=chat, message=SimpleNamespace(
                id=mid, message="", action=None, via_bot_id=None, reply_to=None,
                photo=None, sticker=None, voice=None, video=None, video_note=None, gif=True,
                file=SimpleNamespace(size=len(payload), mime_type=mime, name=name, ext=ext),
                media=types.MessageMediaDocument(), download_media=dl))

        # 1) Telegram GIF -> WeChat native sticker when supported and enabled
        await self.b.on_tg_message(make_gif_ev(8101, gif_bytes, "image/gif", "funny.gif", ".gif"))
        self.assertEqual(len(self.daemon.sticker_preparations), 1)
        self.assertEqual(self.daemon.sends[-1][0]["kind"], "sticker")
        self.assertEqual(self.b.store.wx_for_tg(chat, 8101), ("local-sticker-1", "801"))

        # 2) MP4 Telegram GIF whose sticker conversion fails -> falls back to video
        with mock.patch("bridge.stickers.gif_to_wechat_sticker", return_value="需要 ffmpeg 才能转换动态表情"):
            await self.b.on_tg_message(make_gif_ev(8102, b"mp4-bytes", "video/mp4", "clip.mp4", ".mp4"))
        self.assertEqual(self.daemon.sends[-1][0]["kind"], "video")

        # 3) Disabled via gif_as_sticker=False -> sends as video directly without sticker conversion
        self.b.cfg.bridge.gif_as_sticker = False
        preps_before = len(self.daemon.sticker_preparations)
        with mock.patch("bridge.stickers.gif_to_wechat_sticker") as m_conv:
            await self.b.on_tg_message(make_gif_ev(8103, b"mp4-bytes", "video/mp4", "clip.mp4", ".mp4"))
        m_conv.assert_not_called()
        self.assertEqual(len(self.daemon.sticker_preparations), preps_before)
        self.assertEqual(self.daemon.sends[-1][0]["kind"], "video")

        # 4) Daemon lacks native sticker support -> sends as video directly without sticker conversion
        self.b.cfg.bridge.gif_as_sticker = True
        self.daemon.stickers_supported = False
        self.b.wx._supports_stickers = None
        with mock.patch("bridge.stickers.gif_to_wechat_sticker") as m_conv:
            await self.b.on_tg_message(make_gif_ev(8104, b"mp4-bytes", "video/mp4", "clip.mp4", ".mp4"))
        m_conv.assert_not_called()
        self.assertEqual(len(self.daemon.sticker_preparations), preps_before)
        self.assertEqual(self.daemon.sends[-1][0]["kind"], "video")

    async def test_telegram_stickers_adapt_resolution_to_daemon_version(self):
        import io
        import re
        from PIL import Image

        def extract_gif(content_type: str, body: bytes) -> Image.Image:
            m = re.search(r"boundary=([^\s;]+)", content_type)
            assert m, content_type
            boundary = m.group(1).encode()
            for part in body.split(b"--" + boundary):
                if b"\r\n\r\n" in part:
                    _, payload = part.split(b"\r\n\r\n", 1)
                    payload = payload.rstrip(b"\r\n")
                    if payload.startswith(b"GIF8"):
                        return Image.open(io.BytesIO(payload))
            raise AssertionError("GIF payload not found in multipart upload")

        self.daemon.add_thread("wxid_bob", "Bob")
        self.b.store.link("wxid_bob", -1003, "Bob")
        chat = -1003

        webp = StickerTests.webp()
        tgs = StickerTests.tgs()

        def make_ev(mid: int, payload: bytes, mime: str, ext: str):
            async def dl(file=None):
                return payload
            return SimpleNamespace(chat_id=chat, message=SimpleNamespace(
                id=mid, message="", action=None, via_bot_id=None, reply_to=None,
                photo=None, sticker=True, voice=None, video=None, video_note=None, gif=None,
                file=SimpleNamespace(size=len(payload), mime_type=mime, name=f"s{ext}", ext=ext),
                media=types.MessageMediaDocument(), download_media=dl))

        # 1) agent-wechat >= 2.5.0 -> 512px static WebP, 384px animated TGS
        self.daemon.daemon_version = "2.5.0"
        self.b.wx._daemon_version = None
        await self.b.on_tg_message(make_ev(8201, webp, "image/webp", ".webp"))
        self.assertEqual(extract_gif(*self.daemon.uploads[-1]).size, (512, 512))
        await self.b.on_tg_message(make_ev(8202, tgs, "application/x-tgsticker", ".tgs"))
        self.assertEqual(extract_gif(*self.daemon.uploads[-1]).size, (384, 384))

        # 2) agent-wechat < 2.5.0 -> safely stays at 240px for both
        self.daemon.daemon_version = "2.4.2"
        self.b.wx._daemon_version = None
        await self.b.on_tg_message(make_ev(8203, webp, "image/webp", ".webp"))
        self.assertEqual(extract_gif(*self.daemon.uploads[-1]).size, (240, 240))
        await self.b.on_tg_message(make_ev(8204, tgs, "application/x-tgsticker", ".tgs"))
        self.assertEqual(extract_gif(*self.daemon.uploads[-1]).size, (240, 240))

    async def test_sticker_that_cannot_be_downloaded_says_why(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("st2", "wxid_bob", None, type_=47)
        self.daemon.media["st2"] = [{"id": "asset-st2", "kind": "sticker", "state": "unsupported",
                                     "variant": "original", "reason": "unsupported_sticker_source"}]
        with self.assertLogs("bridge", "INFO") as logs:
            await self.event("st2", "wxid_bob")
        text = self.b.bot.sent[-1].text
        self.assertTrue(text.startswith("[表情，无法获取："), text)
        self.assertIn("自定义", text)
        self.assertTrue(any("unsupported_sticker_source" in line for line in logs.output))

    async def test_telegram_reply_goes_to_wechat_and_echo_is_dropped(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m1", "wxid_bob", "question", server="777")
        await self.event("m1", "wxid_bob")
        chat = self.b.store.chat_for_thread("wxid_bob")
        bridged = self.b.bot.sent[-1].id
        # In basic groups your account sees the bot's message under its own, different id.
        seen = SimpleNamespace(chat_id=chat, message=SimpleNamespace(id=5001, message="question", media=None))
        await self.b.on_user_sees_bot_message(seen)

        await self.b.on_tg_message(tg_event(chat, 9000, "no quote", reply_to=bridged))
        self.assertNotIn("quote", self.daemon.sends[-1][0], "the bot's id must not be read as yours")
        await self.b.on_tg_message(tg_event(chat, 9001, "answer", reply_to=5001))
        body, key = self.daemon.sends[-1]
        self.assertEqual(key, f"tg-{chat}-9001")
        self.assertEqual(body["text"], "answer")
        self.assertEqual(body["quote"], {"messageId": "m1", "serverId": "777"})
        self.assertEqual(self.daemon.marked_read, ["wxid_bob", "wxid_bob"])

        n_sent = len(self.b.bot.sent)
        self.daemon.add_message("echo", "wxid_bob", "answer", direction="outgoing", server="902")
        await self.event("echo", "wxid_bob", direction="outgoing")
        self.assertEqual(len(self.b.bot.sent), n_sent, "echo of our own send must not be forwarded")
        self.assertEqual(self.b.store.wx_for_tg(chat, 9001), ("echo", "902"))

    async def test_wechat_quote_of_tg_sent_messages_basic_group(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m0", "wxid_bob", "hi", server="700")
        await self.event("m0", "wxid_bob")
        chat = self.b.store.chat_for_thread("wxid_bob")

        # 1) Bot sees sticker before on_tg_message finishes -> bot-side ID is persisted for native TG reply
        sticker_user = SimpleNamespace(
            id=8201, message="", action=None, via_bot_id=None, reply_to=None,
            photo=None, voice=None, video_note=None, gif=None, video=None, sticker=True,
            media=SimpleNamespace(document=SimpleNamespace(id=7101)),
            file=SimpleNamespace(size=100, mime_type="image/webp", ext=".webp", name=None),
            download_media=lambda file=None: asyncio.sleep(0, result=StickerTests.webp()),
        )
        sticker_bot = SimpleNamespace(id=8821, message="", media=SimpleNamespace(document=SimpleNamespace(id=7101)))
        await self.b.on_bot_sees_user_message(SimpleNamespace(chat_id=chat, message=sticker_bot))
        await self.b.on_tg_message(SimpleNamespace(chat_id=chat, message=sticker_user))
        st_server = self.b.store.wx_for_tg(chat, 8201)[1]
        self.assertEqual(self.b.store.tg_for_wx("wxid_bob", None, st_server), (chat, 8821))

        self.daemon.add_message(
            "wx_q1", "wxid_bob", "虎子快点把手手放进去", type_=49, appMessageType=57, server="701",
            quote={"senderName": "我", "serverId": st_server, "messageId": "committed-st-1"},
        )
        await self.event("wx_q1", "wxid_bob")
        q1_sent = self.b.bot.sent[-1]
        self.assertEqual(q1_sent.kwargs["reply_to"], 8821)
        self.assertEqual(q1_sent.text, "虎子快点把手手放进去")

        # 2) Text sent from TG, then bot update arrives *after* _record_send -> still pairs & enables native reply
        await self.b.on_tg_message(tg_event(chat, 8202, "first from tg"))
        txt_bot = SimpleNamespace(id=8822, message="first from tg", media=None)
        await self.b.on_bot_sees_user_message(SimpleNamespace(chat_id=chat, message=txt_bot))
        txt_server = self.b.store.wx_for_tg(chat, 8202)[1]
        self.daemon.add_message(
            "wx_q2", "wxid_bob", "got your text", type_=49, appMessageType=57, server="702",
            quote={"senderName": "我", "serverId": txt_server},
        )
        await self.event("wx_q2", "wxid_bob")
        q2_sent = self.b.bot.sent[-1]
        self.assertEqual(q2_sent.kwargs["reply_to"], 8822)
        self.assertEqual(q2_sent.text, "got your text")

        # 3) Basic group without bot pairing -> never uses user-side ID; resolves quoted content for fallback
        await self.b.on_tg_message(tg_event(chat, 8203, "unpaired tg text"))
        unpaired_server = self.b.store.wx_for_tg(chat, 8203)[1]
        self.daemon.add_message("wx_unpaired", "wxid_bob", "unpaired tg text", direction="outgoing", server=unpaired_server)
        self.daemon.add_message(
            "wx_q3", "wxid_bob", "reply to unpaired", type_=49, appMessageType=57, server="703",
            quote={"senderName": "我", "serverId": unpaired_server},
        )
        await self.event("wx_q3", "wxid_bob")
        q3_sent = self.b.bot.sent[-1]
        self.assertIsNone(q3_sent.kwargs["reply_to"])
        self.assertEqual(q3_sent.text, "<blockquote>我：unpaired tg text</blockquote>reply to unpaired")

    async def test_wechat_quote_in_supergroup_and_legacy_user_only_row(self):
        sg = -1001234567890
        self.daemon.add_thread("wxid_sg", "SGTeam", kind="group")
        self.b.store.link("wxid_sg", sg, "SGTeam")

        sticker_user = SimpleNamespace(
            id=3001, message="", action=None, via_bot_id=None, reply_to=None,
            photo=None, voice=None, video_note=None, gif=None, video=None, sticker=True,
            media=SimpleNamespace(document=SimpleNamespace(id=7201)),
            file=SimpleNamespace(size=100, mime_type="image/webp", ext=".webp", name=None),
            download_media=lambda file=None: asyncio.sleep(0, result=StickerTests.webp()),
        )
        await self.b.on_tg_message(SimpleNamespace(chat_id=sg, message=sticker_user))
        st_server = self.b.store.wx_for_tg(sg, 3001)[1]

        self.daemon.add_message(
            "sg_q1", "wxid_sg", "nice sticker", type_=49, appMessageType=57, sender="wxid_alice", server="710",
            quote={"senderName": "我", "serverId": st_server},
        )
        await self.event("sg_q1", "wxid_sg")
        s1 = self.b.bot.sent[-1]
        self.assertEqual(s1.kwargs["reply_to"], 3001)
        self.assertNotIn("<blockquote>", s1.text)

        # Legacy DB row written before bot-side mapping existed (only side='user' in supergroup)
        self.b.store.map_message(sg, "user", 3002, "wxid_sg", "wx-legacy", "711")
        self.daemon.add_message(
            "sg_q2", "wxid_sg", "reply legacy", type_=49, appMessageType=57, sender="wxid_alice", server="712",
            quote={"senderName": "我", "serverId": "711"},
        )
        await self.event("sg_q2", "wxid_sg")
        s2 = self.b.bot.sent[-1]
        self.assertEqual(s2.kwargs["reply_to"], 3002)
        self.assertNotIn("<blockquote>", s2.text)

    async def test_wechat_quote_of_bridged_wx_msg_by_message_id(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m1", "wxid_bob", "local only id", server=None)
        await self.event("m1", "wxid_bob")
        m1_bot_id = self.b.bot.sent[-1].id

        # Same messageId in another thread must not collide
        self.daemon.add_thread("wxid_other", "Other")
        self.daemon.add_message("m1", "wxid_other", "other chat msg", server=None)
        await self.event("m1", "wxid_other")

        self.daemon.add_message(
            "m2", "wxid_bob", "quoting m1 by messageId", type_=49, appMessageType=57, server="720",
            quote={"senderName": "Bob", "messageId": "m1"},
        )
        await self.event("m2", "wxid_bob")
        sent = self.b.bot.sent[-1]
        self.assertEqual(sent.kwargs["reply_to"], m1_bot_id)
        self.assertEqual(sent.text, "quoting m1 by messageId")

    async def test_wechat_quote_fallback_renders_meaningful_label_instead_of_ellipsis(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m0", "wxid_bob", "hi", server="730")
        await self.event("m0", "wxid_bob")

        cases = [
            ("q_st", {"senderName": "Bob", "type": 47}, "[表情]"),
            ("q_im", {"senderName": "Bob", "type": 3}, "[图片]"),
            ("q_vo", {"senderName": "Bob", "type": 34}, "[语音]"),
            ("q_vi", {"senderName": "Bob", "type": 43}, "[视频]"),
            ("q_fi", {"senderName": "Bob", "type": 49, "appMessageType": 6}, "[文件]"),
        ]
        for mid, quote, expected in cases:
            self.daemon.add_message(mid, "wxid_bob", "ok", type_=49, appMessageType=57, server=f"s-{mid}", quote=quote)
            await self.event(mid, "wxid_bob")
            self.assertEqual(self.b.bot.sent[-1].text, f"<blockquote>Bob：{expected}</blockquote>ok")

        # Minimal quote (only senderName + serverId), resolved from daemon history rather than rendering '…'
        self.daemon.add_message("orig_hist", "wxid_bob", "历史消息内容", server="739")
        self.daemon.add_message(
            "q_hist", "wxid_bob", "agree", type_=49, appMessageType=57, server="740",
            quote={"senderName": "Bob", "serverId": "739"},
        )
        await self.event("q_hist", "wxid_bob")
        self.assertEqual(self.b.bot.sent[-1].text, "<blockquote>Bob：历史消息内容</blockquote>agree")

    async def test_telegram_reply_to_own_pending_echo_text_resolves_wx_msg(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m0", "wxid_bob", "hi", server="750")
        await self.event("m0", "wxid_bob")
        chat = self.b.store.chat_for_thread("wxid_bob")

        await self.b.on_tg_message(tg_event(chat, 9100, "first"))
        first_server = self.b.store.wx_for_tg(chat, 9100)[1]
        # Before echo event runs, wx_msg is None; thread_messages has committed wx msg
        self.daemon.add_message("wx-committed-first", "wxid_bob", "first", direction="outgoing", server=first_server)

        await self.b.on_tg_message(tg_event(chat, 9101, "second", reply_to=9100))
        body, _ = self.daemon.sends[-1]
        self.assertEqual(body.get("quote"), {"messageId": "wx-committed-first", "serverId": first_server})

    async def test_phone_outgoing_is_forwarded_silently(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("out1", "wxid_bob", "from phone", direction="outgoing", server="555")
        await self.event("out1", "wxid_bob", direction="outgoing")
        s = self.b.bot.sent[-1]
        self.assertIn("from phone", s.text)
        self.assertTrue(s.text.startswith("<b>我</b>（手机端）：\n"))
        self.assertTrue(s.kwargs["silent"])

    async def test_commands_and_double_slash_not_forwarded(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m1", "wxid_bob")
        await self.event("m1", "wxid_bob")
        chat = self.b.store.chat_for_thread("wxid_bob")
        await self.b.on_tg_message(tg_event(chat, 1, "// note to self"))
        await self.b.on_tg_message(tg_event(chat, 2, "/wx"))
        self.assertEqual(self.daemon.sends, [])
        self.assertIn("wxid_bob", self.b.bot.sent[-1].text)

    async def test_recall_marks_message(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m1", "wxid_bob", "oops", server="321")
        await self.event("m1", "wxid_bob")
        target = self.b.bot.sent[-1]
        await self.b.handle_event({"cursor": "2", "kind": "message.recalled",
                                   "data": {"threadId": "wxid_bob", "messageId": "m1", "serverId": "321"}})
        self.assertEqual(self.b.bot.sent[-1].kwargs["reply_to"], target.id)
        self.assertIn("撤回", self.b.bot.sent[-1].text)

    async def test_media_send_from_telegram(self):
        from bridge.wechat import Media
        r = await self.b.wx.send_media("wxid_bob", "image", Media(b"jpeg", "image/jpeg", "photo.jpg"), "k1")
        self.assertEqual(r["state"], "accepted")
        body, key = self.daemon.sends[-1]
        self.assertEqual((body["kind"], body["sourceId"], body["imageQuality"], key),
                         ("image", "src1", "original", "k1"))
        self.assertIn("multipart/form-data", self.daemon.uploads[0][0])

        self.daemon.reject_image_quality = True
        r_old = await self.b.wx.send_media("wxid_bob", "image", Media(b"jpeg", "image/jpeg", "photo.jpg"), "k-old")
        self.assertEqual(r_old["state"], "accepted")
        old_body, old_key = self.daemon.sends[-1]
        self.assertNotIn("imageQuality", old_body)
        self.assertEqual(old_key, "k-old-std")

    async def test_image_download_prefers_original_over_cached_previews(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("img_orig", "wxid_bob", None, type_=3)
        orig_bytes = _png(1200, 900, (10, 200, 30))
        std_bytes = _png(640, 480, (200, 10, 10))
        self.daemon.media["img_orig"] = [
            {"id": "a-thumb", "variant": "thumbnail", "actualVariant": "thumbnail", "state": "available",
             "contentType": "image/jpeg", "byteLength": 100, "filename": "thumb.jpg"},
            {"id": "a-std", "variant": "standard", "actualVariant": "standard", "state": "available",
             "contentType": "image/png", "byteLength": len(std_bytes), "filename": "std.png"},
            {"id": "a-orig", "variant": "original", "state": "not_cached",
             "contentType": "image/png", "advertisedByteLength": len(orig_bytes), "filename": "orig.png"},
        ]
        self.daemon.media_bytes["a-orig"] = (orig_bytes, "image/png")
        self.daemon.media_bytes["a-std"] = (std_bytes, "image/png")

        await self.event("img_orig", "wxid_bob")
        self.assertEqual(self.daemon.media_transfers[-1][1]["quality"], "original")
        s = self.b.bot.sent[-1]
        self.assertEqual((s.file.name, s.file.read()), ("orig.png", orig_bytes))

        # When `original` transfer fails, fall back to the cached `standard` copy instead of failing.
        self.daemon.add_message("img_fallback", "wxid_bob", None, type_=3)
        self.daemon.transfer_fail_variants.add("original")
        self.daemon.media["img_fallback"] = [
            {"id": "f-std", "variant": "standard", "actualVariant": "standard", "state": "available",
             "contentType": "image/png", "byteLength": len(std_bytes), "filename": "std.png"},
            {"id": "f-orig", "variant": "original", "state": "not_cached",
             "contentType": "image/png", "advertisedByteLength": len(orig_bytes), "filename": "orig.png"},
        ]
        self.daemon.media_bytes["f-std"] = (std_bytes, "image/png")
        await self.event("img_fallback", "wxid_bob")
        s2 = self.b.bot.sent[-1]
        self.assertEqual((s2.file.name, s2.file.read()), ("std.png", std_bytes))

    async def test_video_sent_with_original(self):
        from bridge.wechat import Media
        await self.b.wx.send_media("wxid_bob", "video", Media(b"mp4", "video/mp4", "clip.mp4"), "k2")
        body, _ = self.daemon.sends[-1]
        self.assertEqual((body["kind"], body["videoQuality"]), ("video", "original"))

    async def test_video_download_fetches_video_instead_of_cached_thumbnail(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.b.store.link("wxid_bob", -1001, "Bob")
        thumb_bytes = _png(320, 180, (50, 50, 50))
        mp4_bytes = b"\x00\x00\x00\x18ftypmp42"

        # 1) Cached thumbnail + uncached normal video -> transfers and sends MP4, not thumbnail
        self.daemon.add_message("vid1", "wxid_bob", None, type_=43)
        self.daemon.media["vid1"] = [
            {"id": "v1-thumb", "kind": "image", "variant": "thumbnail", "actualVariant": "thumbnail",
             "state": "available", "contentType": "image/jpeg", "byteLength": len(thumb_bytes),
             "filename": "thumb.jpg"},
            {"id": "v1-norm", "kind": "video", "variant": "normal", "state": "not_cached",
             "contentType": "video/mp4", "advertisedByteLength": len(mp4_bytes), "filename": "clip.mp4"},
        ]
        self.daemon.media_bytes["v1-thumb"] = (thumb_bytes, "image/jpeg")
        self.daemon.media_bytes["v1-norm"] = (mp4_bytes, "video/mp4")
        await self.event("vid1", "wxid_bob")
        self.assertEqual(self.daemon.media_transfers[-1][1]["quality"], "normal")
        s1 = self.b.bot.sent[-1]
        self.assertEqual((s1.file.name, s1.file.read()), ("clip.mp4", mp4_bytes))
        self.assertTrue(s1.kwargs["supports_streaming"])

        # 2) Cached thumbnail + unsupported normal + uncached original -> falls back to original video
        self.daemon.add_message("vid2", "wxid_bob", None, type_=43)
        self.daemon.media["vid2"] = [
            {"id": "v2-thumb", "kind": "image", "variant": "thumbnail", "actualVariant": "thumbnail",
             "state": "available", "contentType": "image/jpeg", "byteLength": len(thumb_bytes),
             "filename": "thumb.jpg"},
            {"id": "v2-norm", "kind": "video", "variant": "normal", "state": "unsupported",
             "contentType": "video/mp4"},
            {"id": "v2-orig", "kind": "video", "variant": "original", "state": "not_cached",
             "contentType": "video/mp4", "advertisedByteLength": len(mp4_bytes), "filename": "raw.mp4"},
        ]
        self.daemon.media_bytes["v2-orig"] = (mp4_bytes, "video/mp4")
        await self.event("vid2", "wxid_bob")
        self.assertEqual(self.daemon.media_transfers[-1][1]["quality"], "original")
        s2 = self.b.bot.sent[-1]
        self.assertEqual((s2.file.name, s2.file.read()), ("raw.mp4", mp4_bytes))
        self.assertTrue(s2.kwargs["supports_streaming"])

        # 3) If video transfer fails, never send the thumbnail as a substitute photo
        self.daemon.add_message("vid3", "wxid_bob", None, type_=43)
        self.daemon.transfer_fail_variants.add("normal")
        self.daemon.media["vid3"] = [
            {"id": "v3-thumb", "kind": "image", "variant": "thumbnail", "actualVariant": "thumbnail",
             "state": "available", "contentType": "image/jpeg", "byteLength": len(thumb_bytes),
             "filename": "thumb.jpg"},
            {"id": "v3-norm", "kind": "video", "variant": "normal", "state": "not_cached",
             "contentType": "video/mp4", "advertisedByteLength": len(mp4_bytes), "filename": "fail.mp4"},
        ]
        await self.event("vid3", "wxid_bob")
        s3 = self.b.bot.sent[-1]
        self.assertIsNone(s3.file)
        self.assertIn("[视频，无法获取：", s3.text)

    async def test_voice_download_accepts_voice_kind_and_prefers_normal_wav(self):
        import io
        import wave
        from unittest.mock import patch
        from bridge import stickers

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 1600)
        wav_bytes = buf.getvalue()
        silk_bytes = b"#!SILK_V3" + b"\x00" * 32

        self.daemon.add_thread("wxid_bob", "Bob")
        self.b.store.link("wxid_bob", -1001, "Bob")

        # 1) Already-available normal voice asset (agent-wechat uses kind="voice", not "audio")
        self.daemon.add_message("vo1", "wxid_bob", "[Voice]", type_=34)
        self.daemon.media["vo1"] = [
            {"id": "vo1-norm", "kind": "voice", "variant": "normal", "actualVariant": "normal",
             "state": "available", "contentType": "audio/wav", "byteLength": len(wav_bytes), "filename": None},
            {"id": "vo1-enc", "kind": "voice", "variant": "encoded", "actualVariant": None,
             "state": "pending", "contentType": None, "advertisedByteLength": len(silk_bytes)},
        ]
        self.daemon.media_bytes["vo1-norm"] = (wav_bytes, "audio/wav")
        transfers_before = len(self.daemon.media_transfers)
        await self.event("vo1", "wxid_bob")
        self.assertEqual(len(self.daemon.media_transfers), transfers_before)
        s1 = self.b.bot.sent[-1]
        self.assertIsNotNone(s1.file)
        if stickers.find_ffmpeg():
            self.assertEqual(s1.file.name, "voice.ogg")
            self.assertTrue(s1.file.read().startswith(b"OggS"))
            self.assertTrue(s1.kwargs["voice_note"])
        else:
            self.assertEqual(s1.file.read(), wav_bytes)
        self.assertFalse(s1.kwargs["force_document"])

        # 2) Pending normal + available encoded SILK -> still transfers normal WAV instead of picking SILK
        self.daemon.add_message("vo2", "wxid_bob", "[Voice]", type_=34)
        self.daemon.media["vo2"] = [
            {"id": "vo2-norm", "kind": "voice", "variant": "normal", "actualVariant": None,
             "state": "pending", "contentType": None, "advertisedByteLength": len(silk_bytes)},
            {"id": "vo2-enc", "kind": "voice", "variant": "encoded", "actualVariant": "encoded",
             "state": "available", "contentType": "audio/silk", "byteLength": len(silk_bytes)},
        ]
        self.daemon.media_bytes["vo2-norm"] = (wav_bytes, "audio/wav")
        self.daemon.media_bytes["vo2-enc"] = (silk_bytes, "audio/silk")

        orig_handler = self.daemon.handler

        def voice_handler(request: httpx.Request) -> httpx.Response:
            resp = orig_handler(request)
            if request.url.path.endswith("/media-transfers") and request.method == "POST":
                for a in self.daemon.media.get("vo2", []):
                    if a["id"] == "vo2-norm" and a["state"] == "available":
                        a["contentType"] = "audio/wav"
                        a["byteLength"] = len(wav_bytes)
            return resp

        await self.b.wx.http.aclose()
        self.b.wx.http = httpx.AsyncClient(transport=httpx.MockTransport(voice_handler),
                                           headers={"Authorization": "Bearer secret-token"})
        await self.event("vo2", "wxid_bob")
        self.assertEqual(self.daemon.media_transfers[-1], (
            "vo2", {"messageId": "vo2", "assetId": "vo2-norm", "quality": "normal"},
        ))
        s2 = self.b.bot.sent[-1]
        self.assertIsNotNone(s2.file)
        self.assertFalse(s2.kwargs["force_document"])
        if stickers.find_ffmpeg():
            self.assertEqual(s2.file.name, "voice.ogg")
            self.assertTrue(s2.kwargs["voice_note"])

        # 3) Normal transfer fails while encoded SILK is available -> report failure, never send raw SILK
        self.daemon.add_message("vo3", "wxid_bob", "[Voice]", type_=34)
        self.daemon.media["vo3"] = [
            {"id": "vo3-norm", "kind": "voice", "variant": "normal", "actualVariant": None,
             "state": "pending", "contentType": None, "advertisedByteLength": len(silk_bytes)},
            {"id": "vo3-enc", "kind": "voice", "variant": "encoded", "actualVariant": "encoded",
             "state": "available", "contentType": "audio/silk", "byteLength": len(silk_bytes)},
        ]
        self.daemon.transfer_fail_reasons["normal"] = "cdn_download_failed"
        await self.event("vo3", "wxid_bob")
        self.daemon.transfer_fail_reasons.clear()
        s3 = self.b.bot.sent[-1]
        self.assertIsNone(s3.file)
        self.assertEqual(s3.text, "[语音，无法获取：微信 CDN 下载失败]")

        # 4) Expired normal while encoded is pending -> report expired without extra transfer
        self.daemon.add_message("vo4", "wxid_bob", "[Voice]", type_=34)
        self.daemon.media["vo4"] = [
            {"id": "vo4-norm", "kind": "voice", "variant": "normal", "actualVariant": None,
             "state": "expired", "reason": "media_expired"},
            {"id": "vo4-enc", "kind": "voice", "variant": "encoded", "actualVariant": None,
             "state": "pending"},
        ]
        transfers_before = len(self.daemon.media_transfers)
        await self.event("vo4", "wxid_bob")
        self.assertEqual(len(self.daemon.media_transfers), transfers_before)
        self.assertEqual(self.b.bot.sent[-1].text, "[语音，无法获取：已过期]")

        # 5) If a job finishes with state="available" but no eligible asset exists, never surface "available"
        self.daemon.add_message("vo5", "wxid_bob", "[Voice]", type_=34)
        self.daemon.media["vo5"] = []
        await self.event("vo5", "wxid_bob")
        self.assertEqual(self.b.bot.sent[-1].text, "[语音，无法获取：下载失败]")

        # 6) Without ffmpeg, legacy/custom kind="audio" still forwards WAV audio cleanly
        self.daemon.add_message("vo6", "wxid_bob", "[Voice]", type_=34)
        self.daemon.media["vo6"] = [
            {"id": "vo6-norm", "kind": "audio", "variant": "normal", "actualVariant": "normal",
             "state": "available", "contentType": "audio/wav", "byteLength": len(wav_bytes)},
        ]
        self.daemon.media_bytes["vo6-norm"] = (wav_bytes, "audio/wav")
        with patch("bridge.stickers.find_ffmpeg", return_value=None):
            await self.event("vo6", "wxid_bob")
        s6 = self.b.bot.sent[-1]
        self.assertEqual((s6.file.read(), s6.kwargs["voice_note"], s6.kwargs["force_document"]),
                         (wav_bytes, False, False))

    async def test_large_video_streamed_to_temp_file_and_removed_after_forward(self):
        import os
        from unittest.mock import patch
        self.daemon.add_thread("wxid_bob", "Bob")
        self.b.store.link("wxid_bob", -1001, "Bob")
        mp4_bytes = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 8
        self.daemon.add_message("bigvid", "wxid_bob", None, type_=43)
        self.daemon.media["bigvid"] = [
            {"id": "bv-norm", "kind": "video", "variant": "normal", "actualVariant": "normal", "state": "available",
             "contentType": "video/mp4", "byteLength": len(mp4_bytes), "filename": "holiday clip.mp4"},
        ]
        self.daemon.media_bytes["bv-norm"] = (mp4_bytes, "video/mp4")
        seen: list[tuple[str, bytes]] = []
        orig_send_file = self.b.bot.send_file

        async def send_file(chat, file, **kw):
            self.assertIsInstance(file, str)  # a path, so Telethon streams the upload from disk
            with open(file, "rb") as f:
                seen.append((file, f.read()))
            return await orig_send_file(chat, file, **kw)

        self.b.bot.send_file = send_file
        with patch("bridge.wechat.MEDIA_SPILL_BYTES", 64):
            await self.event("bigvid", "wxid_bob")
        self.assertEqual(len(seen), 1)
        path, data = seen[0]
        self.assertEqual((os.path.basename(path), data), ("holiday clip.mp4", mp4_bytes))
        self.assertTrue(self.b.bot.sent[-1].kwargs["supports_streaming"])
        self.assertFalse(os.path.exists(os.path.dirname(path)))  # temp copy cleaned up

    async def test_video_metadata_and_thumb_sent_to_telegram(self):
        from unittest.mock import patch

        self.daemon.add_thread("wxid_bob", "Bob")
        self.b.store.link("wxid_bob", -1001, "Bob")
        mp4_bytes = b"\x00\x00\x00\x18ftypmp42"
        thumb_jpeg = b"\xff\xd8\xff\xe0testjpeg"

        self.daemon.add_message("vmeta1", "wxid_bob", None, type_=43)
        self.daemon.media["vmeta1"] = [
            {"id": "vm1", "kind": "video", "variant": "normal", "state": "available",
             "contentType": "video/mp4", "byteLength": len(mp4_bytes), "filename": "clip.mp4"},
        ]
        self.daemon.media_bytes["vm1"] = (mp4_bytes, "video/mp4")

        with patch("bridge.stickers.extract_video_meta_and_thumb", return_value=(720, 1280, 6.0, thumb_jpeg)):
            await self.event("vmeta1", "wxid_bob")

        sent = self.b.bot.sent[-1]
        self.assertEqual(sent.kwargs["mime_type"], "video/mp4")
        self.assertTrue(sent.kwargs["nosound_video"])
        self.assertTrue(sent.kwargs["supports_streaming"])
        attrs = sent.kwargs["attributes"]
        self.assertEqual(len(attrs), 1)
        self.assertEqual(attrs[0], types.DocumentAttributeVideo(duration=6.0, w=720, h=1280, supports_streaming=True))
        self.assertEqual((sent.kwargs["thumb"].name, sent.kwargs["thumb"].read()), ("thumb.jpg", thumb_jpeg))

        # On Telethon RPCError when sending as streamable video, fallback to force_document=True strips video-only kwargs
        self.daemon.add_message("vmeta2", "wxid_bob", None, type_=43)
        self.daemon.media["vmeta2"] = [
            {"id": "vm2", "kind": "video", "variant": "normal", "state": "available",
             "contentType": "video/mp4", "byteLength": len(mp4_bytes), "filename": "fallback.mp4"},
        ]
        self.daemon.media_bytes["vm2"] = (mp4_bytes, "video/mp4")

        orig_send_file = self.b.bot.send_file
        calls = 0

        async def flaky_send_file(chat, file, **kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise errors.RPCError(None, "VIDEO_CONTENT_TYPE_INVALID", 400)
            return await orig_send_file(chat, file, **kw)

        self.b.bot.send_file = flaky_send_file
        with patch("bridge.stickers.extract_video_meta_and_thumb", return_value=(720, 1280, 6.0, thumb_jpeg)):
            await self.event("vmeta2", "wxid_bob")

        self.assertEqual(calls, 2)
        fallback = self.b.bot.sent[-1]
        self.assertTrue(fallback.kwargs["force_document"])
        self.assertNotIn("attributes", fallback.kwargs)
        self.assertNotIn("thumb", fallback.kwargs)
        self.assertIsNone(fallback.kwargs["nosound_video"])

    async def test_latest_cursor(self):
        self.assertEqual(await self.b.wx.latest_cursor(), "42")

    async def test_channels_card_strips_upgrade_placeholder(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message(
            "ch1", "wxid_bob",
            "[Link] 当前版本不支持展示该内容，请升级至最新版本。\n视频文案 <tag>\nhttps://support.weixin.qq.com/security/readtemplate?t=w_security_center_website/upgrade",
            type_=49, appMessageType=51,
            link={
                "title": "当前版本不支持展示该内容，请升级至最新版本。",
                "description": "视频文案 <tag>",
                "sourceName": "老抽搞笑动画",
                "url": "https://support.weixin.qq.com/security/readtemplate?t=w_security_center_website/upgrade",
            },
        )
        self.daemon.add_message(
            "ch2", "wxid_bob",
            "[Link] 当前版本不支持展示该内容，请升级至最新版本。\n仅正文\nhttps://support.weixin.qq.com/security/readtemplate?t=w_security_center_website/upgrade",
            type_=49, appMessageType=51, link=None,
        )
        self.daemon.add_message(
            "lk1", "wxid_bob", "", type_=49, appMessageType=5,
            link={"title": "文章标题", "description": "摘要", "url": "https://example.com", "sourceName": "公众号"},
        )
        self.daemon.add_message(
            "q1", "wxid_bob", "好看", type_=49, appMessageType=57,
            quote={
                "senderName": "Bob",
                "embeddedLink": {
                    "title": "当前版本不支持展示该内容，请升级至最新版本。",
                    "description": "引用的视频文案",
                    "sourceName": "仙桃电视台",
                },
            },
        )
        await self.event("ch1", "wxid_bob")
        await self.event("ch2", "wxid_bob")
        await self.event("lk1", "wxid_bob")
        await self.event("q1", "wxid_bob")

        from bridge import cards as cards_mod

        ch1_sent, ch2_sent, lk1_sent, q1_sent = self.b.bot.sent[-4:]
        self.assertEqual(ch1_sent.text, "🎬 <b>视频号 · 老抽搞笑动画</b>\n视频文案 &lt;tag&gt;")
        self.assertEqual(ch2_sent.text, "🎬 <b>视频号</b>\n仅正文")
        if cards_mod._font_paths()[3]:
            self.assertEqual(lk1_sent.file.name, "link-card.png")
            self.assertIs(lk1_sent.kwargs.get("force_document"), False)
            self.assertIsNone(lk1_sent.kwargs.get("caption"))
        else:
            self.assertIn("🔗 <b>文章标题</b>", lk1_sent.text)
            self.assertIn("来自 公众号", lk1_sent.text)
            self.assertNotIn("https://example.com", lk1_sent.text)
        btn0 = lk1_sent.kwargs["buttons"][0][0]
        self.assertEqual(btn0.type.url if hasattr(btn0, "type") else btn0.url, "https://example.com")
        self.assertEqual(q1_sent.text, "<blockquote>Bob：引用的视频文案</blockquote>好看")

    async def test_channels_v2_4_2_video_cover_and_quotes(self):
        from unittest.mock import patch

        self.daemon.add_thread("wxid_bob", "Bob")
        self.b.store.link("wxid_bob", -1001, "Bob")
        cover_png = _png(320, 180, (20, 90, 160))
        mp4_bytes = b"\x00\x00\x00\x18ftypmp42"
        thumb_jpeg = b"\xff\xd8\xff\xe0chanthumb"

        # 1) Channels v2.4.2 card with playable video asset -> downloads video & streams inline with metadata/caption
        self.daemon.add_message(
            "ch_vid", "wxid_bob",
            "[Channels video] 央视新闻: 晚间新闻 <热点> (1:36)",
            type_=49, appMessageType=51,
            link={"title": None, "url": None, "sourceName": "央视新闻",
                  "description": "晚间新闻 <热点>", "durationSeconds": 96},
        )
        self.daemon.media["ch_vid"] = [
            {"id": "ch_vid_cov", "kind": "image", "variant": "thumbnail", "actualVariant": "thumbnail",
             "state": "available", "contentType": "image/png", "byteLength": len(cover_png), "filename": "link-preview"},
            {"id": "ch_vid_mp4", "kind": "video", "variant": "normal", "actualVariant": None,
             "state": "pending", "contentType": None, "filename": "channels-video.mp4"},
        ]
        self.daemon.media_bytes["ch_vid_cov"] = (cover_png, "image/png")
        self.daemon.media_bytes["ch_vid_mp4"] = (mp4_bytes, "video/mp4")

        with patch("bridge.stickers.extract_video_meta_and_thumb", return_value=(720, 1280, 96.0, thumb_jpeg)):
            await self.event("ch_vid", "wxid_bob")

        self.assertEqual(self.daemon.media_transfers[-1], (
            "ch_vid", {"messageId": "ch_vid", "assetId": "ch_vid_mp4", "quality": "normal"},
        ))
        sent_vid = self.b.bot.sent[-1]
        self.assertEqual((sent_vid.file.name, sent_vid.file.read()), ("channels-video.mp4", mp4_bytes))
        self.assertFalse(sent_vid.kwargs["force_document"])
        self.assertTrue(sent_vid.kwargs["supports_streaming"])
        self.assertTrue(sent_vid.kwargs["nosound_video"])
        self.assertEqual(sent_vid.kwargs["mime_type"], "video/mp4")
        self.assertEqual(sent_vid.kwargs["attributes"], [
            types.DocumentAttributeVideo(duration=96.0, w=720, h=1280, supports_streaming=True),
        ])
        self.assertEqual((sent_vid.kwargs["thumb"].name, sent_vid.kwargs["thumb"].read()), ("thumb.jpg", thumb_jpeg))
        self.assertEqual(sent_vid.kwargs["caption"], "🎬 <b>视频号 · 央视新闻</b> (1:36)\n晚间新闻 &lt;热点&gt;")

        # 2) Channels v2.4.2 card when video transfer fails -> falls back to cover photo + caption + Chinese reason
        self.daemon.add_message(
            "ch_fail", "wxid_bob",
            "[Channels video] 作者A: 已删除视频 (0:45)",
            type_=49, appMessageType=51,
            link={"title": None, "url": None, "sourceName": "作者A",
                  "description": "已删除视频", "durationSeconds": 45},
        )
        self.daemon.media["ch_fail"] = [
            {"id": "ch_fail_cov", "kind": "image", "variant": "thumbnail", "actualVariant": "thumbnail",
             "state": "available", "contentType": "image/png", "byteLength": len(cover_png), "filename": "link-preview"},
            {"id": "ch_fail_mp4", "kind": "video", "variant": "normal", "actualVariant": None,
             "state": "pending", "contentType": None, "filename": "channels-video.mp4"},
        ]
        self.daemon.media_bytes["ch_fail_cov"] = (cover_png, "image/png")
        self.daemon.transfer_fail_reasons["normal"] = "channels_post_unavailable"
        await self.event("ch_fail", "wxid_bob")
        self.daemon.transfer_fail_reasons.clear()

        sent_fail = self.b.bot.sent[-1]
        self.assertEqual((sent_fail.file.name, sent_fail.file.read()), ("channels-cover.png", cover_png))
        self.assertFalse(sent_fail.kwargs["force_document"])
        self.assertFalse(sent_fail.kwargs["supports_streaming"])
        self.assertIn("🎬 <b>视频号 · 作者A</b> (0:45)\n已删除视频", sent_fail.kwargs["caption"])
        self.assertIn("该视频号内容已删除、过期或设为私密", sent_fail.kwargs["caption"])

        # 3) Channels v2.4.2 non-video card (cover only, no video asset) -> sends cover photo without video transfer/error
        self.daemon.add_message(
            "ch_pic", "wxid_bob",
            "[Channels] 图文号: 图文动态",
            type_=49, appMessageType=51,
            link={"title": None, "url": None, "sourceName": "图文号", "description": "图文动态"},
        )
        self.daemon.media["ch_pic"] = [
            {"id": "ch_pic_cov", "kind": "image", "variant": "thumbnail", "actualVariant": "thumbnail",
             "state": "available", "contentType": "image/png", "byteLength": len(cover_png), "filename": "link-preview"},
        ]
        self.daemon.media_bytes["ch_pic_cov"] = (cover_png, "image/png")
        transfers_before = len(self.daemon.media_transfers)
        await self.event("ch_pic", "wxid_bob")
        self.assertEqual(len(self.daemon.media_transfers), transfers_before)
        sent_pic = self.b.bot.sent[-1]
        self.assertEqual((sent_pic.file.name, sent_pic.file.read()), ("channels-cover.png", cover_png))
        self.assertEqual(sent_pic.kwargs["caption"], "🎬 <b>视频号 · 图文号</b>\n图文动态")

        # 4) Quoted Channels v2.4.2 cards (both embeddedText and title=None embeddedLink) + media_integrity_failed
        self.daemon.add_message(
            "ch_q1", "wxid_bob", "看这个", type_=49, appMessageType=57,
            quote={"senderName": "Bob", "embeddedText": "[Channels video] 央视新闻: 晚间新闻 (1:36)"},
        )
        self.daemon.add_message(
            "ch_q2", "wxid_bob", "同感", type_=49, appMessageType=57,
            quote={"senderName": "Bob",
                   "embeddedLink": {"title": None, "description": "新版引用文案",
                                    "sourceName": "央视新闻", "durationSeconds": 96}},
        )
        self.daemon.add_message("v_bad", "wxid_bob", None, type_=43)
        self.daemon.media["v_bad"] = [
            {"id": "vb_norm", "kind": "video", "variant": "normal", "state": "not_cached", "contentType": "video/mp4"},
        ]
        self.daemon.transfer_fail_reasons["normal"] = "media_integrity_failed"
        await self.event("ch_q1", "wxid_bob")
        await self.event("ch_q2", "wxid_bob")
        await self.event("v_bad", "wxid_bob")
        self.daemon.transfer_fail_reasons.clear()

        q1_msg, q2_msg, vbad_msg = self.b.bot.sent[-3:]
        self.assertEqual(q1_msg.text, "<blockquote>Bob：🎬 央视新闻: 晚间新闻 (1:36)</blockquote>看这个")
        self.assertEqual(q2_msg.text, "<blockquote>Bob：新版引用文案</blockquote>同感")
        self.assertIn("[视频，无法获取：文件校验失败]", vbad_msg.text)

    async def test_file_upload_notice_is_skipped(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("up1", "wxid_bob", "[File upload notice]\ninvoice.pdf", type_=49, appMessageType=74)
        self.daemon.add_message("f1", "wxid_bob", "invoice.pdf", type_=49, appMessageType=6)
        self.daemon.media["f1"] = [{"id": "asset-f1", "kind": "file", "state": "available", "variant": "original",
                                    "actualVariant": "original", "contentType": "application/pdf", "byteLength": 4,
                                    "filename": "invoice.pdf"}]
        await self.event("up1", "wxid_bob")
        self.assertEqual(self.b.user.created, [])
        self.assertEqual(self.b.bot.sent, [])
        await self.event("f1", "wxid_bob")
        self.assertEqual(self.b.bot.sent[-1].file.name, "invoice.pdf")

    async def test_wecom_attachment_failure_explains_upstream_limit(self):
        self.daemon.add_thread("room@chatroom", "Team", kind="group")
        self.daemon.add_message("oi1", "room@chatroom", None, type_=3, sender="25984985782492847@openim")
        self.daemon.add_message("of1", "room@chatroom", "报价单.xlsx", type_=49, appMessageType=6,
                                sender="25984985782492847@openim", link={"title": "报价单.xlsx"})
        self.daemon.add_message("wf1", "room@chatroom", "a<b>.pdf", type_=49, appMessageType=6, sender="wxid_bob")
        self.daemon.media_errors.update(oi1="unsupported_media", of1="unsupported_media", wf1="unsupported_media")
        for mid in ("oi1", "of1", "wf1"):
            await self.event(mid, "room@chatroom")
        img, f, plain = (m.text for m in self.b.bot.sent[-3:])
        self.assertTrue(img.endswith("[图片，无法获取：企业微信发来的图片/文件，agent-wechat 暂不支持下载，请在手机微信查看]"), img)
        self.assertTrue(f.endswith("[文件「报价单.xlsx」，无法获取：企业微信发来的图片/文件，agent-wechat 暂不支持下载，请在手机微信查看]"), f)
        # an ordinary sender keeps the generic reason, and the file name is HTML-escaped
        self.assertTrue(plain.endswith("[文件「a&lt;b&gt;.pdf」，无法获取：不支持]"), plain)

    async def test_unsupported_message_shows_human_type_name(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("u1", "wxid_bob", "", type_=49, appMessageType=33)
        self.daemon.add_message("u2", "wxid_bob", "", type_=49, appMessageType=2001)
        self.daemon.add_message("u3", "wxid_bob", "", type_=50)
        self.daemon.add_message("u4", "wxid_bob", "", type_=49, appMessageType=999)
        for mid in ("u1", "u2", "u3", "u4"):
            await self.event(mid, "wxid_bob")
        self.assertEqual([s.text for s in self.b.bot.sent[-4:]], [
            "[暂不支持的微信消息：小程序 (49/33)]",
            "[暂不支持的微信消息：红包 (49/2001)]",
            "[暂不支持的微信消息：语音/视频通话 (50)]",
            "[暂不支持的微信消息类型 49/999]",
        ])

    async def test_group_mentions_and_members_command(self):
        self.daemon.add_thread("123@chatroom", "Team", kind="group")
        self.daemon.group_members["123@chatroom"] = [
            {"id": "lucifer231", "displayName": "永明-Sophia", "mentionName": "永明-Sophia", "roomDisplayName": None},
            {"id": "wxid_jessie", "displayName": "Jessie", "mentionName": "Jessie", "roomDisplayName": None},
            {"id": "wxid_uk", "displayName": "JP Office", "mentionName": "JP EU&UK Manchester Office",
             "roomDisplayName": "JP EU&UK Manchester Office"},
        ]
        self.daemon.add_message("g1", "123@chatroom", "hi", sender="wxid_jessie")
        await self.event("g1", "123@chatroom")
        chat = self.b.store.chat_for_thread("123@chatroom")

        await self.b.on_tg_message(tg_event(chat, 10, "/members"))
        self.assertIn("<code>@永明-Sophia</code>", self.b.bot.sent[-1].text)
        self.assertIn("<code>@JP EU&amp;UK Manchester Office</code>", self.b.bot.sent[-1].text)
        self.assertEqual(self.daemon.sends, [])

        await self.b.on_tg_message(tg_event(chat, 11, "/members@mybot jessie"))
        self.assertIn("<code>@Jessie</code>", self.b.bot.sent[-1].text)
        self.assertNotIn("永明-Sophia", self.b.bot.sent[-1].text)

        await self.b.on_tg_message(tg_event(
            chat, 12, "@jessie你好 @JP EU&UK Manchester Office 看下 @永明-Sophia 邮箱 test@Jessie.com"))
        body, _ = self.daemon.sends[-1]
        self.assertEqual(body["kind"], "text")
        self.assertEqual(body["mentions"], {"all": False, "userIds": ["wxid_jessie", "wxid_uk", "lucifer231"]})

        await self.b.on_tg_message(tg_event(chat, 13, "@所有人 开会了"))
        body, _ = self.daemon.sends[-1]
        self.assertEqual(body["mentions"], {"all": True, "userIds": []})

        await self.b.on_tg_message(tg_event(chat, 14, "mail me at foo@all.com"))
        body, _ = self.daemon.sends[-1]
        self.assertNotIn("mentions", body)

        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m1", "wxid_bob", "hi")
        await self.event("m1", "wxid_bob")
        dm_chat = self.b.store.chat_for_thread("wxid_bob")
        await self.b.on_tg_message(tg_event(dm_chat, 20, "/members"))
        self.assertIn("只有在微信群聊里", self.b.bot.sent[-1].text)
        await self.b.on_tg_message(tg_event(dm_chat, 21, "@Jessie hi"))
        self.assertNotIn("mentions", self.daemon.sends[-1][0])

    async def test_group_member_search_nickname_and_alias(self):
        self.daemon.add_thread("room@chatroom", "Book Club", kind="group")
        self.daemon.group_members["room@chatroom"] = [
            {
                "id": "wxid_a1",
                "username": "wxid_a1",
                "displayName": "Book club",
                "roomDisplayName": "Lantern",
                "nickname": "Serena",
                "alias": "sreads42",
                "mentionName": "Lantern",
            },
            {
                "id": "wxid_b2",
                "username": "wxid_b2",
                "displayName": "Quiet",
                "roomDisplayName": None,
                "nickname": "Other",
                "alias": None,
                "mentionName": "Quiet",
            },
        ]
        self.daemon.add_message("rm1", "room@chatroom", "hi", sender="wxid_b2")
        await self.event("rm1", "room@chatroom")
        chat = self.b.store.chat_for_thread("room@chatroom")

        # 1) Unfiltered /members shows remark + nickname concisely (without noise from alias)
        await self.b.on_tg_message(tg_event(chat, 10, "/members"))
        self.assertIn("<code>@Lantern</code> （Book club · Serena）", self.b.bot.sent[-1].text)
        self.assertNotIn("sreads42", self.b.bot.sent[-1].text)
        self.assertIsNone(self.daemon.member_searches[-1][1])

        # 2) Searching by nickname / alias / multi-term AND sends `?search=` and displays matched alias
        await self.b.on_tg_message(tg_event(chat, 11, "/members serena"))
        self.assertEqual(self.daemon.member_searches[-1], ("room@chatroom", "serena"))
        self.assertIn("<code>@Lantern</code> （Book club · Serena）", self.b.bot.sent[-1].text)
        self.assertNotIn("Quiet", self.b.bot.sent[-1].text)

        await self.b.on_tg_message(tg_event(chat, 12, "/members sreads42"))
        self.assertEqual(self.daemon.member_searches[-1], ("room@chatroom", "sreads42"))
        self.assertIn("<code>@Lantern</code> （Book club · Serena · 微信号 sreads42）", self.b.bot.sent[-1].text)
        self.assertNotIn("Quiet", self.b.bot.sent[-1].text)

        await self.b.on_tg_message(tg_event(chat, 13, "/members serena lantern"))
        self.assertIn("<code>@Lantern</code>", self.b.bot.sent[-1].text)
        self.assertNotIn("Quiet", self.b.bot.sent[-1].text)

        await self.b.on_tg_message(tg_event(chat, 14, "/members serena quiet"))
        self.assertIn("没有匹配的群成员", self.b.bot.sent[-1].text)

        # 3) Fallback when an older daemon ignores `?search=` -> client-side AND filter still matches nickname/alias
        self.daemon.ignore_member_search = True
        await self.b.on_tg_message(tg_event(chat, 15, "/members sreads42"))
        self.assertIn("<code>@Lantern</code> （Book club · Serena · 微信号 sreads42）", self.b.bot.sent[-1].text)
        self.assertNotIn("Quiet", self.b.bot.sent[-1].text)
        self.daemon.ignore_member_search = False

        # 4) @nickname and @alias resolve to the member even behind a remark and roomDisplayName
        await self.b.on_tg_message(tg_event(chat, 16, "@Serena 看下这个"))
        self.assertEqual(self.daemon.sends[-1][0]["mentions"], {"all": False, "userIds": ["wxid_a1"]})
        await self.b.on_tg_message(tg_event(chat, 17, "ping @sreads42"))
        self.assertEqual(self.daemon.sends[-1][0]["mentions"], {"all": False, "userIds": ["wxid_a1"]})

    async def test_bot_commands_menu_and_management_commands(self):
        await self.b.sync_bot_commands()
        scopes = {s: cmds for s, cmds in self.b.bot.commands}
        self.assertIn(types.BotCommandScopeDefault, scopes)
        self.assertIn(types.BotCommandScopeUsers, scopes)
        self.assertIn(types.BotCommandScopeChats, scopes)
        self.assertIn("chats", [c for c, _ in scopes[types.BotCommandScopeUsers]])
        self.assertIn("rename", [c for c, _ in scopes[types.BotCommandScopeChats]])
        self.assertIn("link", [c for c, _ in scopes[types.BotCommandScopeChats]])

        replies = []

        async def private_cmd(text):
            ev = SimpleNamespace(raw_text=text)
            async def reply(msg, **kw):
                replies.append(msg)
            ev.reply = reply
            await self.b.on_bot_command(ev)

        await private_cmd("/chats")
        self.assertIn("还没有任何已连接的会话", replies[-1])

        self.daemon.add_thread("wxid_alice", "Alice Remark")
        self.daemon.add_message("m1", "wxid_alice", "hi")
        await self.event("m1", "wxid_alice")
        chat = self.b.store.chat_for_thread("wxid_alice")

        await private_cmd("/chats alice")
        self.assertIn("Alice Remark", replies[-1])
        self.assertIn("wxid_alice", replies[-1])

        await self.b.on_tg_message(tg_event(chat, 10, "/wx"))
        self.assertIn("Alice Remark", self.b.bot.sent[-1].text)
        self.assertIn("wxid_alice", self.b.bot.sent[-1].text)

        self.daemon.threads["wxid_alice"]["displayName"] = "Alice New"
        await self.b.on_tg_message(tg_event(chat, 11, "/rename"))
        self.assertEqual(self.b.user.titles[-1][1], "Alice New")
        self.assertIn("已更新群标题：Alice New", self.b.bot.sent[-1].text)
        self.assertEqual(self.b.store.list_threads()[0][2], "Alice New")

        await self.b.on_tg_message(tg_event(chat, 12, "/help"))
        self.assertIn("/rename", self.b.bot.sent[-1].text)
        self.assertIn("/unlink", self.b.bot.sent[-1].text)

        await self.b.on_tg_message(tg_event(chat, 13, "/unlink"))
        self.assertIsNone(self.b.store.thread_for_chat(chat))
        self.assertIn("已解除当前群与微信会话", self.b.bot.sent[-1].text)

        await private_cmd(f"/link wxid_alice {chat}")
        self.assertEqual(self.b.store.thread_for_chat(chat), "wxid_alice")
        self.assertIn("已绑定", replies[-1])

    async def test_interactive_link_private_and_group(self):
        self.b.tg_ready.set()
        self.b.owner_id = 999
        self.b.wx_ready = True
        self.daemon.add_thread("wxid_alice", "Alice")
        self.daemon.add_thread("123@chatroom", "Product Team", kind="group")
        self.daemon.add_contact("wxid_charlie", "Charlie Zhang")

        # Existing linked group + two unlinked Telegram groups
        self.b.store.link("wxid_alice", -501, "Alice")
        self.b.user.dialogs = [
            SimpleNamespace(id=-501, title="Alice", is_group=True),
            SimpleNamespace(id=-777, title="My Work Group", is_group=True),
            SimpleNamespace(id=-888, title="Family Chat", is_group=True),
            SimpleNamespace(id=12345, title="Private DM", is_group=False),
        ]

        def btn_data(btn):
            return btn.type.data if hasattr(btn, "type") and hasattr(btn.type, "data") else btn.data

        def make_private_ev(text):
            sent_box = []
            async def reply(msg, **kw):
                sent_box.append((msg, kw))
                return SimpleNamespace(text=msg, kwargs=kw)
            return SimpleNamespace(sender_id=999, raw_text=text, reply=reply), sent_box

        # 1) Private /link (no args): lists TG groups with unlinked first
        ev, box = make_private_ev("/link")
        await self.b.on_bot_private(ev)
        text, kw = box[-1]
        self.assertIn("点选下方 Telegram 群", text)
        buttons = kw["buttons"]
        self.assertEqual(buttons[0][0].text, "💬 My Work Group")
        self.assertIn("（已绑）", buttons[2][0].text)

        # Unauthorized callback click is rejected
        unauth = _CallbackEvent(111, btn_data(buttons[0][0]))
        await self.b.on_bot_callback(unauth)
        self.assertEqual(unauth.answers, [("只有桥接账号本人可以操作", True)])

        # Pick "My Work Group" -> enters WeChat search/recent picker
        cb_tg = _CallbackEvent(999, btn_data(buttons[0][0]))
        await self.b.on_bot_callback(cb_tg)
        self.assertIn("My Work Group", cb_tg.edits[-1][0])
        self.assertIsNotNone(self.b._link_wait)

        # User types a WeChat name in private chat instead of picking from recent
        ev_kw, box_kw = make_private_ev("Charlie")
        await self.b.on_bot_private(ev_kw)
        kw_text, kw_opts = box_kw[-1]
        self.assertIn("Charlie", kw_text)
        charlie_btn = kw_opts["buttons"][0][0]
        self.assertIn("Charlie Zhang", charlie_btn.text)

        # Click Charlie -> binds wxid_charlie <-> -777 and notifies the group
        cb_bind = _CallbackEvent(999, btn_data(charlie_btn))
        await self.b.on_bot_callback(cb_bind)
        self.assertEqual(self.b.store.thread_for_chat(-777), "wxid_charlie")
        self.assertIsNone(self.b._link_wait)
        self.assertIn("Charlie Zhang", cb_bind.edits[-1][0])
        self.assertEqual(self.b.bot.sent[-1].chat, -777)

        # 2) Private /link <keyword>: search WeChat first, then pick TG group
        ev2, box2 = make_private_ev("/link Product")
        await self.b.on_bot_private(ev2)
        wx_btn = box2[-1][1]["buttons"][0][0]
        self.assertIn("Product Team", wx_btn.text)
        cb_wx = _CallbackEvent(999, btn_data(wx_btn))
        await self.b.on_bot_callback(cb_wx)
        tg_btns = cb_wx.edits[-1][1]["buttons"]
        family_btn = next(r[0] for r in tg_btns if "Family Chat" in r[0].text)
        cb_bind2 = _CallbackEvent(999, btn_data(family_btn))
        await self.b.on_bot_callback(cb_bind2)
        self.assertEqual(self.b.store.thread_for_chat(-888), "123@chatroom")

        # 3) Sending /link directly inside an UNLINKED group where bot is not yet a member
        self.b.bot.dead_chats.add(-999)
        self.b.user.on_invite = lambda cid: self.b.bot.dead_chats.discard(cid)
        unlinked_other_cmd = tg_event(-999, 1, "/help")
        unlinked_other_cmd.is_private = False
        await self.b.on_tg_unlinked_command(unlinked_other_cmd)
        self.assertEqual(self.b.user.invited, [])  # unrelated /commands in unlinked groups are ignored

        unlinked_link = tg_event(-999, 2, "/link Alice")
        unlinked_link.is_private = False
        unlinked_link.chat = SimpleNamespace(title="New Manual Group")
        await self.b.on_tg_unlinked_command(unlinked_link)
        self.assertEqual(self.b.user.invited, [-999])
        group_picker = self.b.bot.sent[-1]
        self.assertEqual(group_picker.chat, -999)
        alice_btn = group_picker.kwargs["buttons"][0][0]
        cb_group_bind = _CallbackEvent(999, btn_data(alice_btn))
        await self.b.on_bot_callback(cb_group_bind)
        self.assertEqual(self.b.store.thread_for_chat(-999), "wxid_alice")
        self.assertIn("已将当前群绑定到微信会话", cb_group_bind.edits[-1][0])

        # 4) Sending /link inside an already-linked group switches its binding
        await self.b.on_tg_message(tg_event(-999, 3, "/link Charlie"))
        charlie_group_btn = self.b.bot.sent[-1].kwargs["buttons"][0][0]
        cb_rebind = _CallbackEvent(999, btn_data(charlie_group_btn))
        await self.b.on_bot_callback(cb_rebind)
        self.assertEqual(self.b.store.thread_for_chat(-999), "wxid_charlie")

    async def test_find_and_open_without_raw_ids(self):
        self.b.tg_ready.set()
        self.b.owner_id = 999
        self.b.wx_ready = True
        self.daemon.add_thread("wxid_alice", "Alice Wang")
        self.daemon.add_thread("wxid_alan", "Alan Wu")
        self.daemon.add_contact("wxid_david", "David Li")

        def btn_data(btn):
            return btn.type.data if hasattr(btn, "type") and hasattr(btn.type, "data") else btn.data

        boxes = []
        async def cmd(text):
            ev = SimpleNamespace(sender_id=999, raw_text=text)
            async def reply(msg, **kw):
                boxes.append((msg, kw))
                return SimpleNamespace(text=msg, kwargs=kw)
            ev.reply = reply
            await self.b.on_bot_private(ev)

        # /find with no args shows recent threads with one-click buttons
        await cmd("/find")
        text, kw = boxes[-1]
        self.assertIn("最近的微信会话", text)
        self.assertIn("Alice Wang", text)
        alice_btn = kw["buttons"][0][0]
        cb_open = _CallbackEvent(999, btn_data(alice_btn))
        await self.b.on_bot_callback(cb_open)
        self.assertIsNotNone(self.b.store.chat_for_thread("wxid_alice"))
        self.assertIn("已建立 Telegram 群", cb_open.edits[-1][0])

        # /open with unambiguous keyword opens directly without needing wxid
        await cmd("/open David")
        self.assertIn("已建群：<b>David Li</b>", boxes[-1][0])
        self.assertIsNotNone(self.b.store.chat_for_thread("wxid_david"))

        # /open with no args or ambiguous keyword shows buttons
        await cmd("/open")
        self.assertIn("请点选要发起聊天的微信会话", boxes[-1][0])
        await cmd("/open A")
        self.assertIn("找到多个匹配", boxes[-1][0])
        self.assertGreaterEqual(len(boxes[-1][1]["buttons"]), 3)  # Alice + Alan + Cancel

    async def test_wechat_topics_and_emojis_rendered(self):
        self.daemon.add_thread("wxid_alice", "Alice")
        self.daemon.add_message("m1", "wxid_alice", "", type=49, appMessageType=51, link={
            "title": "当前版本不支持展示该内容",
            "description": "#静电#内容过于真实[捂脸][捂脸]",
            "sourceName": "老抽搞笑动画",
            "url": "https://support.weixin.qq.com/security/readtemplate?t=w_upgrade",
        })
        await self.event("m1", "wxid_alice")
        sent = self.b.bot.sent[-1].text
        self.assertIn("🎬 <b>视频号 · 老抽搞笑动画</b>", sent)
        self.assertIn("#静电 内容过于真实🤦🤦", sent)
        self.assertNotIn("#静电#", sent)

        self.daemon.add_message("m2", "wxid_alice", "看看#日常##搞笑#还挺好[破涕为笑] https://a.b/#x#1 <tag>")
        await self.event("m2", "wxid_alice")
        sent2 = self.b.bot.sent[-1].text
        self.assertIn("看看 #日常 #搞笑 还挺好😂 https://a.b/#x#1 &lt;tag&gt;", sent2)

    async def test_telegram_recall_via_command_and_delete(self):
        self.daemon.add_thread("wxid_alice", "Alice")
        self.daemon.add_message("m0", "wxid_alice", "incoming", server="100")
        await self.event("m0", "wxid_alice")
        chat = self.b.store.chat_for_thread("wxid_alice")

        # User sends a message from Telegram (serverId="901", no localMessageId yet)
        await self.b.on_tg_message(tg_event(chat, 10, "oops typo"))
        self.daemon.add_message("wx_local_10", "wxid_alice", "oops typo", direction="outgoing", server="901")

        # Replying /recall without reply_to shows usage
        await self.b.on_tg_message(tg_event(chat, 11, "/recall"))
        self.assertIn("请回复你要撤回的那条消息", self.b.bot.sent[-1].text)

        # /recall resolves wx_local_10 by serverId="901" and calls recall API
        await self.b.on_tg_message(tg_event(chat, 12, "/recall", reply_to=10))
        self.assertEqual(self.daemon.recalls[-1][0], "wx_local_10")
        self.assertEqual(self.daemon.recalls[-1][1], {"serverId": "901"})
        self.assertIn("↩️ 已从微信撤回", self.b.bot.sent[-1].text)

        # WeChat echoing message.recalled for our own Telegram-triggered recall does not duplicate notice
        sent_before = len(self.b.bot.sent)
        await self.b.handle_event({"cursor": "2", "kind": "message.recalled",
                                   "data": {"threadId": "wxid_alice", "messageId": "wx_local_10", "serverId": "901"}})
        self.assertEqual(len(self.b.bot.sent), sent_before)

        # Deleting a Telegram-sent message (even with chat_id=None as in basic groups) recalls on WeChat
        # and auto-deletes the orphan recall confirmation notice after TTL
        self.b._recall_notice_ttl = 0.01
        await self.b.on_tg_message(tg_event(chat, 20, "delete me"))
        self.daemon.add_message("wx_local_20", "wxid_alice", "delete me", direction="outgoing", server="902")
        await self.event("wx_local_20", "wxid_alice")
        await self.b.on_tg_delete(SimpleNamespace(chat_id=None, deleted_ids=[20]))
        self.assertEqual(self.daemon.recalls[-1][0], "wx_local_20")
        self.assertEqual(self.daemon.recalls[-1][1], {"serverId": "902"})
        notice_msg = self.b.bot.sent[-1]
        self.assertIn("↩️ 已从微信撤回", notice_msg.text)
        await asyncio.sleep(0.05)
        self.assertIn(notice_msg.id, self.b.bot.deleted)

        # Deleting an incoming message (not in `sent`) must NOT call WeChat recall
        recalls_before = len(self.daemon.recalls)
        bot_msg_id = self.b.bot.sent[0].id
        self.b.store.map_message(chat, "user", 99, "wxid_alice", "m0", "100")
        await self.b.on_tg_delete(SimpleNamespace(chat_id=chat, deleted_ids=[99]))
        await self.b.on_tg_delete(SimpleNamespace(chat_id=chat, deleted_ids=[bot_msg_id]))
        self.assertEqual(len(self.daemon.recalls), recalls_before)

        # Clearing history / bulk deleting multiple sent messages must NOT recall on WeChat or spam notices
        await self.b.on_tg_message(tg_event(chat, 22, "bulk 1"))
        self.daemon.add_message("wx_local_22", "wxid_alice", "bulk 1", direction="outgoing", server="903")
        await self.event("wx_local_22", "wxid_alice")
        await self.b.on_tg_message(tg_event(chat, 23, "bulk 2"))
        self.daemon.add_message("wx_local_23", "wxid_alice", "bulk 2", direction="outgoing", server="904")
        await self.event("wx_local_23", "wxid_alice")
        recalls_before = len(self.daemon.recalls)
        sent_before = len(self.b.bot.sent)
        await self.b.on_tg_delete(SimpleNamespace(chat_id=None, deleted_ids=[22, 23]))
        self.assertEqual(len(self.daemon.recalls), recalls_before)
        self.assertEqual(len(self.b.bot.sent), sent_before)

        # Recall window expired reports friendly message for both /recall and TG delete (and stays visible)
        self.daemon.recall_error = (409, "recall_window_expired")
        await self.b.on_tg_message(tg_event(chat, 21, "/recall", reply_to=20))
        self.assertIn("已超过微信 2 分钟撤回时限，无法从微信撤回", self.b.bot.sent[-1].text)
        await self.b.on_tg_delete(SimpleNamespace(chat_id=chat, deleted_ids=[20]))
        err_msg = self.b.bot.sent[-1]
        self.assertIn("已超过微信 2 分钟撤回时限，微信端未撤回（仅从 Telegram 删除）", err_msg.text)
        await asyncio.sleep(0.05)
        self.assertNotIn(err_msg.id, self.b.bot.deleted)

        # Async operation recall_window_expired and unconfirmed states must NOT report success
        self.daemon.recall_error = None
        self.daemon.recall_op = {"id": "rcop", "state": "failed", "error": {"code": "recall_window_expired"}}
        await self.b.on_tg_message(tg_event(chat, 24, "/recall", reply_to=22))
        self.assertIn("已超过微信 2 分钟撤回时限，无法从微信撤回", self.b.bot.sent[-1].text)

        self.daemon.recall_op = {"id": "rcop", "state": "running"}
        await self.b.on_tg_message(tg_event(chat, 25, "/recall", reply_to=22))
        self.assertNotIn("已从微信撤回", self.b.bot.sent[-1].text)
        self.assertIn("撤回状态未确认（running）", self.b.bot.sent[-1].text)

        self.daemon.recall_op = {"id": "rcop", "state": "completed"}
        await self.b.on_tg_message(tg_event(chat, 26, "/recall", reply_to=22))
        self.assertIn("↩️ 已从微信撤回", self.b.bot.sent[-1].text)

    async def test_send_failure_quotes_failed_message_and_describes_target(self):
        self.daemon.add_thread("wxid_alice", "Alice")
        self.daemon.add_message("m0", "wxid_alice", "hi")
        await self.event("m0", "wxid_alice")
        chat = self.b.store.chat_for_thread("wxid_alice")  # basic group (-501)

        # 1) Basic group: two stickers in a row with distinct Telegram document IDs pair accurately
        async def fail_timeout(*_a, **_kw):
            raise httpx.ReadTimeout("")

        self.b.wx.send_sticker = fail_timeout
        sticker1_user = SimpleNamespace(
            id=101, message="", action=None, via_bot_id=None, reply_to=None,
            photo=None, voice=None, video_note=None, gif=None, video=None,
            sticker=True, media=SimpleNamespace(document=SimpleNamespace(id=7001)),
            file=SimpleNamespace(size=100, mime_type="image/webp", ext=".webp", name=None),
            download_media=lambda file=bytes: asyncio.sleep(0, result=b"RIFFwebp"),
        )
        sticker1_bot = SimpleNamespace(
            id=8801, message="", media=SimpleNamespace(document=SimpleNamespace(id=7001)),
        )
        sticker2_user = SimpleNamespace(
            id=102, message="", action=None, via_bot_id=None, reply_to=None,
            photo=None, voice=None, video_note=None, gif=None, video=None,
            sticker=True, media=SimpleNamespace(document=SimpleNamespace(id=7002)),
            file=SimpleNamespace(size=100, mime_type="image/webp", ext=".webp", name=None),
            download_media=lambda file=bytes: asyncio.sleep(0, result=b"RIFFwebp"),
        )
        sticker2_bot = SimpleNamespace(
            id=8802, message="", media=SimpleNamespace(document=SimpleNamespace(id=7002)),
        )

        # Bot sees sticker 2 before sticker 1, yet document ID matches sticker 1 -> 8801
        await self.b.on_bot_sees_user_message(SimpleNamespace(chat_id=chat, message=sticker2_bot))
        await self.b.on_bot_sees_user_message(SimpleNamespace(chat_id=chat, message=sticker1_bot))
        self.b._tg_media = lambda *_a, **_kw: asyncio.sleep(0, result=("sticker", SimpleNamespace(data=b"gif", mime="image/gif", filename="s.gif")))
        await self.b.on_tg_message(SimpleNamespace(chat_id=chat, message=sticker1_user))
        alert1 = self.b.bot.sent[-1]
        self.assertEqual(alert1.kwargs["reply_to"], 8801)
        self.assertEqual(alert1.text, "⚠️ 发送[贴纸]到微信失败：ReadTimeout")

        await self.b.on_tg_message(SimpleNamespace(chat_id=chat, message=sticker2_user))
        alert2 = self.b.bot.sent[-1]
        self.assertEqual(alert2.kwargs["reply_to"], 8802)
        self.assertEqual(alert2.text, "⚠️ 发送[贴纸]到微信失败：ReadTimeout")

        # 2) Basic group: bot update arrives slightly after immediate failure -> waits and pairs cleanly
        self.b._bot_pair_wait = 0.5
        file_user = SimpleNamespace(
            id=103, message="", action=None, via_bot_id=None, reply_to=None,
            photo=None, voice=None, video_note=None, gif=None, video=None, sticker=None,
            media=SimpleNamespace(document=SimpleNamespace(id=7003)),
            file=SimpleNamespace(size=100, mime_type="image/jpeg", ext=".jpg", name="IMG_6186.JPG"),
        )
        file_bot = SimpleNamespace(
            id=8803, message="", media=SimpleNamespace(document=SimpleNamespace(id=7003)),
        )
        self.b._tg_media = lambda *_a, **_kw: asyncio.sleep(0, result=(None, "文件太大"))

        async def bot_arrives_later():
            await asyncio.sleep(0.02)
            await self.b.on_bot_sees_user_message(SimpleNamespace(chat_id=chat, message=file_bot))

        await asyncio.gather(
            self.b.on_tg_message(SimpleNamespace(chat_id=chat, message=file_user)),
            bot_arrives_later(),
        )
        alert3 = self.b.bot.sent[-1]
        self.assertEqual(alert3.kwargs["reply_to"], 8803)
        self.assertEqual(alert3.text, "⚠️ 发送[文件 IMG_6186.JPG]到微信失败：文件太大")

        # 3) Basic group where bot cannot see user messages -> no bogus user-side reply_to, text still identifies message
        self.b._bot_pair_wait = 0.0
        self.b.wx.send_text = fail_timeout
        await self.b.on_tg_message(tg_event(chat, 104, "这是一条非常重要的长消息测试一下截断效果是否正常"))
        alert4 = self.b.bot.sent[-1]
        self.assertIsNone(alert4.kwargs["reply_to"])
        self.assertIn("发送「这是一条非常重要的长消息测试一下截断效果是否正常」到微信失败：ReadTimeout", alert4.text)

        # 4) Supergroup shares message IDs directly without pairing
        super_chat = -1001234567890
        self.b.store.link("wxid_alice", super_chat, "Alice")
        await self.b.on_tg_message(tg_event(super_chat, 555, "hello"))
        alert5 = self.b.bot.sent[-1]
        self.assertEqual(alert5.kwargs["reply_to"], 555)
        self.assertEqual(alert5.text, "⚠️ 发送「hello」到微信失败：ReadTimeout")

    async def test_location_single_venue_message_and_fallback(self):
        from unittest import mock

        self.daemon.add_thread("wxid_alice", "Alice")
        self.daemon.add_thread("123@chatroom", "饭局群", kind="group")
        self.daemon.add_contact("wxid_amir", "Amir")
        self.b.store.link("wxid_alice", -1001, "Alice")
        self.b.store.link("123@chatroom", -1002, "饭局群")

        # 1) Direct chat location -> single InputMediaVenue message
        self.daemon.add_message(
            "loc1", "wxid_alice", "", type_=48,
            location={"name": "新粤新疆菜", "address": "体育西路191号佳兆业广场F3-301",
                      "latitude": 23.133, "longitude": 113.321},
        )
        before = len(self.b.bot.sent)
        await self.event("loc1", "wxid_alice")
        self.assertEqual(len(self.b.bot.sent) - before, 1)
        m1 = self.b.bot.sent[-1]
        self.assertIsInstance(m1.file, types.InputMediaVenue)
        self.assertEqual(m1.file.title, "新粤新疆菜")
        self.assertEqual(m1.file.address, "体育西路191号佳兆业广场F3-301")
        self.assertEqual((m1.file.geo_point.lat, m1.file.geo_point.long), (23.133, 113.321))

        # 2) Group chat location -> sender included in venue title, still single message
        self.daemon.add_message(
            "loc2", "123@chatroom", "", type_=48, sender="wxid_amir",
            location={"name": "新粤新疆菜", "address": "体育西路191号佳兆业广场F3-301",
                      "latitude": 23.133, "longitude": 113.321},
        )
        before = len(self.b.bot.sent)
        await self.event("loc2", "123@chatroom")
        self.assertEqual(len(self.b.bot.sent) - before, 1)
        m2 = self.b.bot.sent[-1]
        self.assertIsInstance(m2.file, types.InputMediaVenue)
        self.assertEqual(m2.file.title, "Alice in group：新粤新疆菜")
        self.assertEqual(m2.file.address, "体育西路191号佳兆业广场F3-301")

        # 3) Outgoing from phone -> "我（手机端）" prefix in venue title
        self.daemon.add_message(
            "loc3", "wxid_alice", "", type_=48, direction="outgoing",
            location={"name": "广州塔", "address": "", "latitude": 23.1064, "longitude": 113.3245},
        )
        before = len(self.b.bot.sent)
        await self.event("loc3", "wxid_alice")
        self.assertEqual(len(self.b.bot.sent) - before, 1)
        m3 = self.b.bot.sent[-1]
        self.assertIsInstance(m3.file, types.InputMediaVenue)
        self.assertEqual(m3.file.title, "我（手机端）：广州塔")
        self.assertEqual(m3.file.address, "23.106400, 113.324500")

        # 4) Missing name with address only -> non-empty title & address
        self.daemon.add_message(
            "loc4", "123@chatroom", "", type_=48, sender="wxid_amir",
            location={"name": "", "address": "体育西路191号", "latitude": 23.133, "longitude": 113.321},
        )
        before = len(self.b.bot.sent)
        await self.event("loc4", "123@chatroom")
        self.assertEqual(len(self.b.bot.sent) - before, 1)
        m4 = self.b.bot.sent[-1]
        self.assertIsInstance(m4.file, types.InputMediaVenue)
        self.assertEqual(m4.file.title, "Alice in group：位置")
        self.assertEqual(m4.file.address, "体育西路191号")

        # 5) Venue rejected by Telegram -> falls back to text + InputMediaGeoPoint
        orig_send_file = self.b.bot.send_file
        async def reject_venue(chat, file, **kw):
            if isinstance(file, types.InputMediaVenue):
                raise errors.MediaInvalidError(None)
            return await orig_send_file(chat, file, **kw)

        self.daemon.add_message(
            "loc5", "wxid_alice", "", type_=48,
            location={"name": "备选地点", "address": "备选地址", "latitude": 23.1, "longitude": 113.3},
        )
        before = len(self.b.bot.sent)
        with mock.patch.object(self.b.bot, "send_file", side_effect=reject_venue):
            await self.event("loc5", "wxid_alice")
        self.assertEqual(len(self.b.bot.sent) - before, 2)
        self.assertEqual(self.b.bot.sent[-2].text, "📍 备选地点 备选地址")
        self.assertIsInstance(self.b.bot.sent[-1].file, types.InputMediaGeoPoint)




FORUM = -1009000000001
OWNER = 4242


class FakeForumBot(FakeBot):
    """The bot as admin of a forum supergroup: creates/edits topics; posting into a deleted topic fails."""

    def __init__(self):
        super().__init__()
        self.topics: dict[int, tuple[str, int]] = {}  # topic id -> (title, icon colour)
        self.topic_edits: list[tuple[int, str]] = []
        self.deleted_topics: set[int] = set()
        self.next_topic = 10 ** 6  # far from Sent ids

    async def get_input_entity(self, chat):
        return types.InputPeerChannel(channel_id=abs(chat) - 10 ** 12, access_hash=0)

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


class TopicModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.daemon = FakeDaemon()
        self.b = await self.make_bridge(forum_chat=FORUM)

    async def make_bridge(self, **bridge_kw):
        cfg = Config(WeChatConfig(token="secret-token"), TelegramConfig(api_id=1, api_hash="x", bot_token="y"),
                     BridgeConfig(mode="topics", create_interval_seconds=0, **bridge_kw),
                     Path(self.tmp.name) / "config.toml")
        b = Bridge(cfg)
        await b.wx.http.aclose()
        b.wx.http = httpx.AsyncClient(transport=httpx.MockTransport(self.daemon.handler),
                                      headers={"Authorization": "Bearer secret-token"})
        b.user = None  # topics mode must never touch your Telegram account
        b.bot = FakeForumBot()
        b.bot_me = SimpleNamespace(username="mybot", id=1)
        b.owner_id = OWNER
        b.wx_ready = True
        await b.wx.resolve_account()
        return b

    async def asyncTearDown(self):
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def event(self, mid, tid, direction="incoming"):
        await self.b.handle_event({"cursor": "1", "kind": "message.committed",
                                   "data": {"messageId": mid, "threadId": tid, "origin": "live",
                                            "direction": direction}})

    def topic_of(self, tid):
        return self.b.store.topic_for_thread(tid)[1]

    async def test_one_topic_per_chat_and_messages_land_in_it(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_thread("123@chatroom", "Team", kind="group")
        self.daemon.add_message("m1", "wxid_bob", "hello")
        self.daemon.add_message("m2", "wxid_bob", "again")
        self.daemon.add_message("g1", "123@chatroom", "hey", sender="wxid_alice")
        await self.event("m1", "wxid_bob")
        await self.event("m2", "wxid_bob")
        await self.event("g1", "123@chatroom")

        bob, team = self.topic_of("wxid_bob"), self.topic_of("123@chatroom")
        self.assertEqual(self.b.bot.topics, {bob: ("Bob", 0x6FB9F0), team: ("👥 Team", 0x8EEE98)})
        self.assertEqual(self.b.store.chat_for_thread("wxid_bob"), None, "groups-mode bindings untouched")
        self.assertTrue(all(s.chat == FORUM for s in self.b.bot.sent))
        by_text = [(s.text, s.kwargs.get("reply_to")) for s in self.b.bot.sent]
        self.assertIn("已连接微信联系人", by_text[0][0])
        self.assertEqual(by_text[0][1], bob)
        self.assertEqual(by_text[1:3], [("hello", bob), ("again", bob)])
        self.assertEqual(by_text[-1], ("<b>Alice in group</b>：\nhey", team))
        self.assertEqual(sorted(where for _, _, where in self.b._link_rows()),
                         sorted([f"{FORUM}#{bob}", f"{FORUM}#{team}"]))

    async def test_your_messages_in_a_topic_go_to_its_wechat_chat(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m1", "wxid_bob", "question", server="777")
        await self.event("m1", "wxid_bob")
        bob = self.topic_of("wxid_bob")
        bridged = self.b.bot.sent[-1].id

        await self.b.on_forum_message(forum_event(FORUM, 9000, "plain", topic=bob))
        body, key = self.daemon.sends[-1]
        self.assertEqual((body["text"], key), ("plain", f"tg-{FORUM}-9000"))
        self.assertNotIn("quote", body, "the topic marker is not a reply")

        await self.b.on_forum_message(forum_event(FORUM, 9001, "answer", topic=bob, reply_to=bridged))
        self.assertEqual(self.daemon.sends[-1][0]["quote"], {"messageId": "m1", "serverId": "777"})

        n = len(self.daemon.sends)
        await self.b.on_forum_message(forum_event(FORUM, 9002, "in General"))
        await self.b.on_forum_message(forum_event(FORUM, 9003, "unbound topic", topic=bob + 5))
        await self.b.on_forum_message(forum_event(-1001234, 9004, "other group", topic=bob))
        self.assertEqual(len(self.daemon.sends), n)

        await self.b.on_forum_message(forum_event(FORUM, 9005, "/wx", topic=bob))
        self.assertIn("wxid_bob", self.b.bot.sent[-1].text)
        self.assertEqual(self.b.bot.sent[-1].kwargs["reply_to"], 9005)

        # Echo of our own send is dropped, and WeChat quoting it maps back into the topic.
        n_sent = len(self.b.bot.sent)
        self.daemon.add_message("echo", "wxid_bob", "plain", direction="outgoing", server="901")
        await self.event("echo", "wxid_bob", direction="outgoing")
        self.assertEqual(len(self.b.bot.sent), n_sent)

    async def test_deleted_topic_is_recreated_and_rename_edits_topic(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m1", "wxid_bob", "one")
        self.daemon.add_message("m2", "wxid_bob", "two")
        await self.event("m1", "wxid_bob")
        old = self.topic_of("wxid_bob")
        self.b.bot.deleted_topics.add(old)
        await self.event("m2", "wxid_bob")
        new = self.topic_of("wxid_bob")
        self.assertNotEqual(new, old)
        self.assertEqual((self.b.bot.sent[-1].text, self.b.bot.sent[-1].kwargs["reply_to"]), ("two", new))
        self.assertIsNone(self.b.store.tg_for_wx("wxid_bob", "m1", None), "messages of the deleted topic forgotten")

        self.daemon.threads["wxid_bob"]["displayName"] = "Bobby"
        await self.b.on_forum_message(forum_event(FORUM, 9100, "/rename", topic=new))
        self.assertEqual(self.b.bot.topic_edits, [(new, "Bobby")])
        self.assertIn("已更新话题标题", self.b.bot.sent[-1].text)
        await self.b.on_forum_message(forum_event(FORUM, 9101, "/rename", topic=new))
        self.assertIn("话题标题已是最新", self.b.bot.sent[-1].text)

        await self.b.on_forum_message(forum_event(FORUM, 9102, "/unlink", topic=new))
        self.assertIsNone(self.b.store.topic_for_thread("wxid_bob"))

    async def test_link_binds_a_topic_and_forum_command(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        await self.b.on_forum_message(forum_event(FORUM, 9200, "/link wxid_bob", topic=555))
        self.assertEqual(self.b.store.topic_for_thread("wxid_bob"), (FORUM, 555))
        self.assertIn("已将当前话题绑定", self.b.bot.sent[-1].text)
        await self.b.on_forum_message(forum_event(FORUM, 9201, "/link", topic=None))
        self.assertIn("请打开要绑定的话题", self.b.bot.sent[-1].text)

        # /forum: refused while TG_FORUM_CHAT pins another group, and in a group without topics.
        await self.b.on_forum_message(forum_event(-1007777, 9202, "/forum"))
        self.assertIn("TG_FORUM_CHAT", self.b.bot.sent[-1].text)
        self.b.cfg.bridge.forum_chat = 0
        await self.b.on_forum_message(forum_event(-1007777, 9203, "/forum@mybot", is_forum=False))
        self.assertIn("还没有开启「话题」", self.b.bot.sent[-1].text)
        await self.b.on_forum_message(forum_event(-1007777, 9204, "/forum"))
        self.assertEqual((self.b.forum_chat, self.b.store.get("forum_chat")), (-1007777, "-1007777"))
        self.assertIsNone(self.b._dest_for_thread("wxid_bob"), "bindings of the old forum are kept but inactive")
        self.assertEqual(self.b.store.topic_for_thread("wxid_bob"), (FORUM, 555))

    async def test_no_forum_yet_tells_owner_once(self):
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.b = await self.make_bridge()
        self.assertIsNone(self.b.forum_chat)
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("m1", "wxid_bob", "lost")
        self.daemon.add_message("m2", "wxid_bob", "lost too")
        await self.event("m1", "wxid_bob")
        await self.event("m2", "wxid_bob")
        self.assertEqual(self.b.bot.topics, {})
        warnings = [s for s in self.b.bot.sent if s.chat == OWNER]
        self.assertEqual(len(warnings), 1)
        self.assertIn("/forum", warnings[0].text)

    async def test_pairing_makes_owner_without_account_login(self):
        self.b.owner_id = None
        replies = []

        async def reply(t, **kw):
            replies.append(t)
        self.b.start_pairing()
        code = self.b.pairing_code
        await self.b.on_bot_private(SimpleNamespace(raw_text=f"/login {code}", sender_id=77, chat_id=77,
                                                    reply=reply, respond=reply))
        self.assertTrue(self.b.tg_ready.is_set())
        self.assertEqual((self.b.owner_id, self.b.store.get("owner")), (77, "77"))
        self.assertIn("配对成功", replies[-1])
        self.assertIsNone(self.b.login_step, "no Telegram account login in topics mode")

    async def test_topics_bot_commands_menu(self):
        await self.b.sync_bot_commands()
        menus = dict(self.b.bot.commands)
        group = [c for c, _ in menus[types.BotCommandScopeChats]]
        self.assertIn("forum", group)
        self.assertNotIn("avatar", group)
        self.assertNotIn("link", [c for c, _ in menus[types.BotCommandScopeUsers]])


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_sse_parsing_and_cursor_expiry(self):
        body = (": keepalive\n\n"
                "id: 5\ndata: {\"kind\":\"message.committed\",\"data\":{\"messageId\":\"a\"}}\n\n"
                "id: 6\ndata: {\"cursor\":\"6\",\"kind\":\"thread.updated\",\n"
                "data: \"data\":{}}\n\n"
                "event: error\ndata: {\"error\":{\"code\":\"event_cursor_expired\"}}\n\n")
        wx = WeChatClient("http://daemon", "secret-token", ACC)
        await wx.http.aclose()
        wx.http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})))
        got = []
        with self.assertRaises(CursorExpired):
            async for ev in wx.stream("4"):
                got.append(ev)
        await wx.close()
        self.assertEqual([(e["cursor"], e["kind"]) for e in got], [("5", "message.committed"), ("6", "thread.updated")])


class ConfigTests(unittest.TestCase):
    def test_env_only_and_token_file(self):
        from bridge.config import load_config
        with tempfile.TemporaryDirectory() as tmp:
            tok = Path(tmp) / "tok"
            tok.write_text("abc123\n", encoding="utf-8")
            env = {"TG_API_ID": "123", "TG_API_HASH": "h", "TG_BOT_TOKEN": "b", "WECHAT_TOKEN_FILE": str(tok),
                   "BRIDGE_DATA_DIR": str(Path(tmp) / "data")}
            cfg = load_config(Path(tmp) / "missing.toml", env)
            self.assertEqual((cfg.telegram.api_id, cfg.wechat.read_token()), (123, "abc123"))
            self.assertTrue(cfg.data_dir.is_dir())
            with self.assertRaisesRegex(ValueError, "TG_BOT_TOKEN"):
                load_config(Path(tmp) / "missing.toml", {k: v for k, v in env.items() if k != "TG_BOT_TOKEN"})
            self.assertEqual((cfg.bridge.mode, cfg.bridge.forum_chat), ("groups", 0))
            cfg = load_config(Path(tmp) / "missing.toml",
                              {**env, "BRIDGE_MODE": "Topic", "TG_FORUM_CHAT": "-1001234567890"})
            self.assertEqual((cfg.bridge.mode, cfg.bridge.forum_chat), ("topics", -1001234567890))
            with self.assertRaisesRegex(ValueError, "mode"):
                load_config(Path(tmp) / "missing.toml", {**env, "BRIDGE_MODE": "channels"})
            with self.assertRaisesRegex(ValueError, "TG_FORUM_CHAT"):
                load_config(Path(tmp) / "missing.toml", {**env, "TG_FORUM_CHAT": "my group"})
            self.assertFalse(cfg.bridge.auto_relogin)
            cfg = load_config(Path(tmp) / "missing.toml", {**env, "AUTO_RELOGIN": "true"})
            self.assertTrue(cfg.bridge.auto_relogin)
            cfg = load_config(Path(tmp) / "missing.toml", {**env, "AUTO_RELOGIN": "0"})
            self.assertFalse(cfg.bridge.auto_relogin)
            with self.assertRaisesRegex(ValueError, "AUTO_RELOGIN"):
                load_config(Path(tmp) / "missing.toml", {**env, "AUTO_RELOGIN": "maybe"})
            toml_path = Path(tmp) / "config.toml"
            toml_path.write_text(
                "[bridge]\n"
                "auto_relogin = true\n"
                "auto_relogin_delay_min = 30\n"
                "auto_relogin_delay_max = 90\n"
                "auto_relogin_timeout_seconds = 45\n"
                "auto_relogin_cooldown_seconds = 1800\n",
                encoding="utf-8",
            )
            cfg = load_config(toml_path, env)
            self.assertTrue(cfg.bridge.auto_relogin)
            self.assertEqual(
                (cfg.bridge.auto_relogin_delay_min, cfg.bridge.auto_relogin_delay_max,
                 cfg.bridge.auto_relogin_timeout_seconds, cfg.bridge.auto_relogin_cooldown_seconds),
                (30.0, 90.0, 45.0, 1800.0),
            )
            # Environment variable overrides config.toml
            cfg = load_config(toml_path, {**env, "AUTO_RELOGIN": "false"})
            self.assertFalse(cfg.bridge.auto_relogin)
            with self.assertRaisesRegex(ValueError, "auto_relogin_delay_min/max"):
                BridgeConfig(auto_relogin_delay_min=100, auto_relogin_delay_max=50)
            with self.assertRaisesRegex(ValueError, "auto_relogin_timeout_seconds"):
                BridgeConfig(auto_relogin_timeout_seconds=0)
            with self.assertRaisesRegex(ValueError, "auto_relogin_cooldown_seconds"):
                BridgeConfig(auto_relogin_cooldown_seconds=-1)
            with self.assertRaisesRegex(ValueError, "auto_relogin must be true or false"):
                BridgeConfig(auto_relogin="false")  # type: ignore[arg-type]


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

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/")
        m = request.method
        if path == "accounts" and m == "GET":
            return httpx.Response(200, json={"items": self.accounts})
        if path == "accounts" and m == "POST":
            self.accounts.append({"id": ACC, "alias": json.loads(request.content)["alias"]})
            return httpx.Response(201, json=self.accounts[-1])
        if path == f"accounts/{ACC}":
            conn = "online" if self.connected else "disconnected"
            return httpx.Response(200, json={"id": ACC, "authenticated": self.authenticated, "connection": conn})
        if path == f"accounts/{ACC}/connect":
            self.connected = True
            return httpx.Response(200, json={})
        if path == f"accounts/{ACC}/auth/login":
            assert request.headers["idempotency-key"]
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


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(WeChatConfig(token="t"), TelegramConfig(api_id=1, api_hash="x", bot_token="y"),
                     BridgeConfig(), Path(self.tmp.name) / "config.toml")
        self.b = Bridge(cfg)
        self.daemon = LoginDaemon()
        await self.b.wx.http.aclose()
        self.b.wx.http = httpx.AsyncClient(transport=httpx.MockTransport(self.daemon.handler))
        self.b.bot = FakeBot()
        self.b.bot.deleted = []

        async def delete_messages(chat, ids):
            self.b.bot.deleted.extend(ids)
        self.b.bot.delete_messages = delete_messages
        self.b.bot_me = SimpleNamespace(username="mybot")
        self.b.login_poll_interval = 0

    async def asyncTearDown(self):
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def test_wechat_account_created_and_qr_relayed(self):
        self.b.owner_id = 42
        await self.b.ensure_wechat()
        self.assertEqual(self.daemon.accounts, [{"id": ACC, "alias": "me"}])
        self.assertTrue(self.b.wx_ready and self.daemon.connected)
        self.assertEqual(self.daemon.qr_fetches, ["1", "2"])
        photos = [s for s in self.b.bot.sent if s.file is not None]
        self.assertEqual(len(photos), 2)
        self.assertTrue(all(s.chat == 42 for s in self.b.bot.sent))
        self.assertEqual(photos[0].file.read(), b"\x89PNG-qr")
        self.assertEqual(sorted(self.b.bot.deleted), sorted(p.id for p in photos))  # old and final QR removed
        texts = [s.text for s in self.b.bot.sent if s.text]
        self.assertTrue(any("手机微信上点" in t for t in texts))
        self.assertIn("微信已登录", texts[-1])

    async def test_startup_without_telegram_login_waits_for_pairing(self):
        from telethon import errors

        class Client:
            def __init__(self, authorized):
                self.authorized = authorized
                self.handlers = []

            async def start(self, **kw):
                pass

            async def connect(self):
                pass

            async def get_me(self):
                return SimpleNamespace(username="mybot", id=1)

            async def is_user_authorized(self):
                return self.authorized

            def add_event_handler(self, *a):
                self.handlers.append(a)

            async def run_until_disconnected(self):
                if not self.authorized:  # what Telegram does for a session that is not logged in
                    raise errors.AuthKeyUnregisteredError(request=None)
                await asyncio.Event().wait()

        self.b.bot, self.b.user = Client(True), Client(False)
        task = asyncio.create_task(self.b.run())
        await asyncio.sleep(0.05)
        self.assertFalse(task.done(), task.done() and task.exception())
        self.assertIsNotNone(self.b.pairing_code)
        task.cancel()

    async def test_pairing_code_gate(self):
        replies = []

        def ev(sender, text):
            async def reply(t, **kw):
                replies.append((sender, t, kw))
            return SimpleNamespace(raw_text=text, sender_id=sender, chat_id=sender, reply=reply, respond=reply)

        self.b.start_pairing()
        code = self.b.pairing_code
        await self.b.on_bot_private(ev(7, "/login wrong"))
        self.assertIsNone(self.b.login_owner)
        self.assertIn("配对码不对", replies[-1][1])
        await self.b.on_bot_private(ev(7, f"/login {code}"))
        self.assertEqual(self.b.login_owner, 7)
        self.assertIn("buttons", replies[-1][2])
        n = len(replies)
        await self.b.on_bot_private(ev(8, "+8613800000000"))  # a stranger cannot drive the login
        self.assertEqual(len(replies), n)
        self.assertEqual(self.b.login_step, "method")

    async def test_phone_push_approval_copy_and_switch_to_qr(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "signed_out"
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        await self.b.wx.resolve_account()

        # Phone push confirmation skips awaiting_scan completely -> must NOT say "已扫码"
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_approval", "result": {}},
                {"state": "awaiting_approval", "result": {}},
            ],
            "qr": [
                {"state": "awaiting_scan", "result": {"qrGeneration": 1, "qrAvailable": True}},
                {"state": "awaiting_approval", "result": {}},
                {"state": "succeeded", "result": {}},
            ],
        }
        self.b.login_poll_interval = 0.05

        def btn_data(btn):
            return btn.type.data if hasattr(btn, "type") and hasattr(btn.type, "data") else btn.data

        login_task = asyncio.create_task(self.b.request_wechat_login())
        # Wait until the awaiting_approval phone push message is sent
        for _ in range(50):
            if any("登录确认请求" in (s.text or "") for s in self.b.bot.sent):
                break
            await asyncio.sleep(0.01)

        push_msg = next(s for s in self.b.bot.sent if s.text and "登录确认请求" in s.text)
        self.assertNotIn("已扫码", push_msg.text)
        switch_btn = push_msg.kwargs["buttons"][0][0]
        self.assertIn("扫码登录", switch_btn.text)

        # Clicking "改用扫码登录" switches immediately to strategy="qr" and completes
        cb = _CallbackEvent(42, btn_data(switch_btn))
        await self.b.on_bot_callback(cb)
        await login_task

        self.assertEqual(self.daemon.login_strategies, ["auto", "qr"])
        self.assertEqual(self.daemon.cancelled_ops, ["op1"])
        self.assertEqual(self.b.wx_state, "online")
        texts = [s.text for s in self.b.bot.sent if s.text]
        self.assertTrue(any("✅ 已扫码" in t for t in texts))
        self.assertIn("微信已登录", texts[-1])

    async def test_signed_out_notification_has_login_button(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "online"
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        await self.b.wx.resolve_account()

        def btn_data(btn):
            return btn.type.data if hasattr(btn, "type") and hasattr(btn.type, "data") else btn.data

        mon = asyncio.create_task(self.b.wechat_monitor(interval=0.01))
        for _ in range(50):
            if self.b.wx_state == "signed_out" and self.b.bot.sent:
                break
            await asyncio.sleep(0.01)
        mon.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await mon

        alert = self.b.bot.sent[-1]
        self.assertIn("微信已退出登录", alert.text)
        login_btn = alert.kwargs["buttons"][0][0]
        self.assertIn("重新登录", login_btn.text)
        self.assertEqual(btn_data(login_btn), b"wxlogin:auto")

        # Stranger clicking the login button is rejected
        unauth = _CallbackEvent(111, btn_data(login_btn))
        await self.b.on_bot_callback(unauth)
        self.assertEqual(unauth.answers, [("只有桥接账号本人可以操作", True)])
        self.assertEqual(self.daemon.login_strategies, [])

        # Owner clicking the login button launches WeChat login and restores online state
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_approval", "result": {}},
                {"state": "succeeded", "result": {}},
            ],
        }
        auth_cb = _CallbackEvent(42, btn_data(login_btn))
        await self.b.on_bot_callback(auth_cb)
        self.assertIsNotNone(self.b._wx_login_task)
        await self.b._wx_login_task
        self.assertEqual(self.daemon.login_strategies, ["auto"])
        self.assertEqual(self.b.wx_state, "online")
        self.assertTrue(self.daemon.authenticated)

    async def test_auto_relogin_succeeds_silently_after_random_delay(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "online"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.02
        self.b.cfg.bridge.auto_relogin_delay_max = 0.03
        self.b.cfg.bridge.auto_relogin_timeout_seconds = 2.0
        self.b.cfg.bridge.auto_relogin_cooldown_seconds = 3600.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_approval", "result": {}},
                {"state": "succeeded", "result": {}},
            ],
        }
        await self.b.wx.resolve_account()

        mon = asyncio.create_task(self.b.wechat_monitor(interval=0.01))
        try:
            for _ in range(100):
                if self.b._wx_auto_relogin_task is not None and self.b._wx_auto_relogin_task.done():
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.03)  # let monitor tick again to ensure no duplicate online alerts
        finally:
            mon.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await mon

        self.assertEqual(self.daemon.login_strategies, ["auto"])
        self.assertTrue(self.daemon.authenticated)
        self.assertTrue(self.daemon.connected)
        self.assertEqual(self.b.wx_state, "online")
        self.assertFalse(self.b._wx_auto_relogin_attempted)
        self.assertIsNotNone(self.b.store.get("auto_relogin_last_at"))
        texts = [s.text for s in self.b.bot.sent if s.text]
        self.assertTrue(any("自动尝试重新登录一次" in t for t in texts))
        # Must NOT send the noisy phone confirmation prompt when silent auto-login succeeds
        self.assertFalse(any("登录确认请求" in t for t in texts))
        self.assertEqual(sum("自动重新登录" in t for t in texts), 1)
        self.assertEqual(sum("恢复在线" in t for t in texts), 1)

    async def test_auto_relogin_only_tries_once_and_cancels_on_qr_fallback(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "online"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.01
        self.b.cfg.bridge.auto_relogin_delay_max = 0.01
        self.b.cfg.bridge.auto_relogin_timeout_seconds = 2.0
        self.b.cfg.bridge.auto_relogin_cooldown_seconds = 0.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_scan", "result": {"qrGeneration": 1, "qrAvailable": True}},
            ],
        }
        await self.b.wx.resolve_account()

        mon = asyncio.create_task(self.b.wechat_monitor(interval=0.01))
        try:
            for _ in range(100):
                if self.b._wx_auto_relogin_task is not None and self.b._wx_auto_relogin_task.done():
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # multiple subsequent monitor ticks must NOT retry
        finally:
            mon.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await mon

        self.assertEqual(self.daemon.login_strategies, ["auto"])
        self.assertEqual(self.daemon.cancelled_ops, ["op1"])
        self.assertEqual(self.daemon.qr_fetches, [])
        self.assertEqual(self.b.wx_state, "signed_out")
        self.assertTrue(self.b._wx_auto_relogin_attempted)
        last = self.b.bot.sent[-1]
        self.assertIn("微信自动登录失败", last.text)
        self.assertIn("扫码登录", last.text)
        self.assertIn("不再自动重试", last.text)
        self.assertEqual(len(last.kwargs["buttons"][0]), 2)

    async def test_auto_relogin_times_out_waiting_for_approval_and_reminds_manual(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "online"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.0
        self.b.cfg.bridge.auto_relogin_delay_max = 0.0
        self.b.cfg.bridge.auto_relogin_timeout_seconds = 0.04
        self.b.cfg.bridge.auto_relogin_cooldown_seconds = 0.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_approval", "result": {}},
                {"state": "awaiting_approval", "result": {}},
            ],
        }
        await self.b.wx.resolve_account()

        mon = asyncio.create_task(self.b.wechat_monitor(interval=0.01))
        try:
            for _ in range(100):
                if self.b._wx_auto_relogin_task is not None and self.b._wx_auto_relogin_task.done():
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.04)
        finally:
            mon.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await mon

        self.assertEqual(self.daemon.login_strategies, ["auto"])
        self.assertEqual(self.daemon.cancelled_ops, ["op1"])
        self.assertEqual(self.b.wx_state, "signed_out")
        last = self.b.bot.sent[-1]
        self.assertIn("自动登录该设备", last.text)
        self.assertIn("手动登录", last.text)

    async def test_auto_relogin_respects_cooldown_on_repeat_signout(self):
        import time as _time
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "online"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.0
        self.b.cfg.bridge.auto_relogin_delay_max = 0.0
        self.b.cfg.bridge.auto_relogin_cooldown_seconds = 3600.0
        self.b.store.set("auto_relogin_last_at", str(_time.time() - 60))
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        await self.b.wx.resolve_account()

        mon = asyncio.create_task(self.b.wechat_monitor(interval=0.01))
        try:
            for _ in range(50):
                if self.b.wx_state == "signed_out" and self.b.bot.sent:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.03)
        finally:
            mon.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await mon

        self.assertEqual(self.daemon.login_strategies, [])
        self.assertIsNone(self.b._wx_auto_relogin_task)
        last = self.b.bot.sent[-1]
        self.assertIn("冷却", last.text)
        self.assertIn("不会自动登录", last.text)

    async def test_auto_relogin_cancel_button_and_manual_override(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "online"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 5.0
        self.b.cfg.bridge.auto_relogin_delay_max = 5.0
        self.b.cfg.bridge.auto_relogin_cooldown_seconds = 3600.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        await self.b.wx.resolve_account()

        def btn_data(btn):
            return btn.type.data if hasattr(btn, "type") and hasattr(btn.type, "data") else btn.data

        mon = asyncio.create_task(self.b.wechat_monitor(interval=0.01))
        try:
            for _ in range(50):
                if self.b._wx_auto_relogin_task is not None:
                    break
                await asyncio.sleep(0.01)
            # /status should report waiting auto-relogin while pending
            replies = []
            async def reply(text, **kw):
                replies.append((text, kw))
            await self.b.on_bot_command(SimpleNamespace(raw_text="/status", reply=reply))
            self.assertIn("自动尝试登录", replies[-1][0])
            self.assertIn("掉线自动登录：已开启", replies[-1][0])

            alert = self.b.bot.sent[-1]
            cancel_btn = alert.kwargs["buttons"][0][1]
            self.assertEqual(btn_data(cancel_btn), b"wxlogin:cancel_auto")
            cb = _CallbackEvent(42, btn_data(cancel_btn))
            await self.b.on_bot_callback(cb)
            await self.b._wx_auto_relogin_task
            self.assertEqual(cb.answers, [("已取消本次自动登录", False)])
            self.assertTrue(cb.edits and "已取消本次自动登录" in cb.edits[-1][0])
            self.assertEqual(self.daemon.login_strategies, [])
            # Cancelling before calling WeChat should not burn the cooldown
            self.assertIsNone(self.b.store.get("auto_relogin_last_at"))
        finally:
            mon.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await mon

        # Now test clicking "立即手动登录" while waiting: cancels timer & runs manual login once
        self.b._mark_wx_online()
        self.b.bot.sent.clear()
        self.b._wx_auto_relogin_task = None
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_approval", "result": {}},
                {"state": "succeeded", "result": {}},
            ],
        }
        self.b.wx_state = "signed_out"
        await self.b._on_wx_signed_out()
        self.assertIsNotNone(self.b._wx_auto_relogin_task)
        wait_task = self.b._wx_auto_relogin_task
        manual_btn = self.b.bot.sent[-1].kwargs["buttons"][0][0]
        self.assertEqual(btn_data(manual_btn), b"wxlogin:auto")
        cb2 = _CallbackEvent(42, btn_data(manual_btn))
        await self.b.on_bot_callback(cb2)
        await wait_task
        await self.b._wx_login_task
        self.assertEqual(self.daemon.login_strategies, ["auto"])
        self.assertEqual(self.b.wx_state, "online")
        self.assertIsNone(self.b.store.get("auto_relogin_last_at"))

    async def test_auto_relogin_aborts_if_account_recovers_during_delay(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "signed_out"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.03
        self.b.cfg.bridge.auto_relogin_delay_max = 0.03
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        await self.b.wx.resolve_account()

        await self.b._on_wx_signed_out()
        self.daemon.authenticated = True
        self.daemon.connected = True
        await self.b._wx_auto_relogin_task

        self.assertEqual(self.daemon.login_strategies, [])
        self.assertEqual(self.b.wx_state, "online")
        self.assertIn("无需自动重新登录", self.b.bot.sent[-1].text)

    async def test_auto_relogin_qr_override_while_running(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "signed_out"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.0
        self.b.cfg.bridge.auto_relogin_delay_max = 0.0
        self.b.cfg.bridge.auto_relogin_timeout_seconds = 5.0
        self.b.login_poll_interval = 0.05
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "awaiting_approval", "result": {}},
                {"state": "awaiting_approval", "result": {}},
            ],
            "qr": [
                {"state": "awaiting_scan", "result": {"qrGeneration": 1, "qrAvailable": True}},
                {"state": "succeeded", "result": {}},
            ],
        }
        await self.b.wx.resolve_account()

        await self.b._on_wx_signed_out()
        for _ in range(50):
            if self.b._wx_auto_relogin_phase == "running" and self.daemon.login_strategies == ["auto"]:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.b._wx_auto_relogin_phase, "running")

        cb = _CallbackEvent(42, b"wxlogin:qr")
        await self.b.on_bot_callback(cb)
        await self.b._wx_auto_relogin_task
        await self.b._wx_login_task

        self.assertEqual(self.daemon.login_strategies, ["auto", "qr"])
        self.assertEqual(self.daemon.cancelled_ops, ["op1"])
        self.assertEqual(self.b.wx_state, "online")
        self.assertFalse(any("自动登录失败" in (s.text or "") for s in self.b.bot.sent))

    async def test_auto_relogin_failure_state_does_not_retry_and_reminds_manual(self):
        self.b.tg_ready.set()
        self.b.owner_id = 42
        self.b.wx_ready = True
        self.b.wx_state = "online"
        self.b.cfg.bridge.auto_relogin = True
        self.b.cfg.bridge.auto_relogin_delay_min = 0.0
        self.b.cfg.bridge.auto_relogin_delay_max = 0.0
        self.b.cfg.bridge.auto_relogin_cooldown_seconds = 0.0
        self.daemon.accounts = [{"id": ACC, "alias": "me"}]
        self.daemon.authenticated = False
        self.daemon.ops_by_strategy = {
            "auto": [
                {"state": "failed", "result": {"code": "device_auth_expired"}},
            ],
        }
        await self.b.wx.resolve_account()

        mon = asyncio.create_task(self.b.wechat_monitor(interval=0.01))
        try:
            for _ in range(100):
                if self.b._wx_auto_relogin_task is not None and self.b._wx_auto_relogin_task.done():
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
        finally:
            mon.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await mon

        self.assertEqual(self.daemon.login_strategies, ["auto"])
        self.assertEqual(self.b.wx_state, "signed_out")
        last = self.b.bot.sent[-1]
        self.assertIn("微信自动登录失败", last.text)
        self.assertIn("device_auth_expired", last.text)
        self.assertIn("不再自动重试", last.text)
        self.assertIn("/wxlogin", last.text)

    async def test_telegram_code_login_keypad_one_tap_phone_and_2fa(self):
        from telethon import errors, types

        deleted_events = []

        class AuthUser(FakeUser):
            def __init__(self):
                super().__init__()
                self.code_requests: list[str] = []
                self.sign_ins: list[dict] = []
                self.send_types = [
                    SimpleNamespace(phone_code_hash="h1", type=types.auth.SentCodeTypeApp(length=5)),
                    SimpleNamespace(phone_code_hash="h2", type=types.auth.SentCodeTypeSms(length=5)),
                ]

            async def send_code_request(self, phone):
                self.code_requests.append(phone)
                return self.send_types[len(self.code_requests) - 1]

            async def sign_in(self, **kw):
                self.sign_ins.append(kw)
                if "password" in kw:
                    if kw["password"] != "goodpass":
                        raise errors.PasswordHashInvalidError(request=None)
                    return
                if kw["code"] == "99999":
                    raise errors.PhoneCodeInvalidError(request=None)
                if kw["code"] == "12345":
                    raise errors.SessionPasswordNeededError(request=None)

            async def get_me(self):
                return SimpleNamespace(id=7, first_name="Owner", last_name=None, username="owner")

        self.b.user = AuthUser()
        self.b.start_pairing()
        code = self.b.pairing_code

        replies = []

        def make_ev(sender, text="", *, contact=None):
            async def reply(msg, **kw):
                replies.append((sender, msg, kw))
                return SimpleNamespace(text=msg, kwargs=kw)

            async def delete():
                deleted_events.append((sender, text, contact is not None))

            return SimpleNamespace(
                sender_id=sender, chat_id=sender, raw_text=text,
                message=SimpleNamespace(contact=contact),
                reply=reply, respond=reply, delete=delete,
            )

        # Pair -> method chooser
        await self.b.on_bot_private(make_ev(7, f"/login {code}"))
        self.assertEqual(self.b.login_step, "method")

        # Stranger callback ignored
        stranger_cb = _CallbackEvent(99, b"tg:phone")
        await self.b.on_bot_callback(stranger_cb)
        self.assertEqual(self.b.login_step, "method")
        self.assertEqual(stranger_cb.answers, [(None, False)])

        # Owner picks verification code login -> one-tap phone ReplyKeyboard
        await self.b.on_bot_callback(_CallbackEvent(7, b"tg:phone"))
        self.assertEqual(self.b.login_step, "phone")
        phone_prompt = self.b.bot.sent[-1]
        self.assertIn("一键使用当前手机号", phone_prompt.text)
        self.assertIsInstance(phone_prompt.kwargs["buttons"][0][0].button.type, types.ButtonTypeRequestPhone)

        # Invalid manual phone rejected
        await self.b.on_bot_private(make_ev(7, "abc"))
        self.assertIn("手机号格式不对", replies[-1][1])

        # One-tap contact sharing deletes contact message and displays inline keypad
        contact = SimpleNamespace(phone_number="8613800138000")
        await self.b.on_bot_private(make_ev(7, "", contact=contact))
        self.assertIn((7, "", True), deleted_events)
        self.assertEqual(self.b.user.code_requests, ["+8613800138000"])
        self.assertEqual(self.b.login_step, "code")
        keypad_msg = self.b.bot.sent[-1]
        self.assertIn("Telegram 客户端内", keypad_msg.text)
        self.assertIn("<code>_ _ _ _ _</code>", keypad_msg.text)
        self.assertEqual(keypad_msg.kwargs["buttons"][4][0].type.data, b"tg:k:noop")

        # Tapping noop before 4 digits or back on empty buffer
        noop_cb = _CallbackEvent(7, b"tg:k:noop")
        await self.b.on_bot_callback(noop_cb)
        self.assertIn("至少 4 位", noop_cb.answers[0][0])
        back_empty_cb = _CallbackEvent(7, b"tg:k:back")
        await self.b.on_bot_callback(back_empty_cb)
        self.assertIn("没有已输入的数字", back_empty_cb.answers[0][0])

        # Tapping digits + backspace updates keypad in place and unlocks confirm
        for key in ("1", "2", "3", "back", "9", "4", "5"):
            cb = _CallbackEvent(7, f"tg:k:{key}".encode())
            await self.b.on_bot_callback(cb)
        last_edit_text, last_edit_kw = cb.edits[-1]
        self.assertIn("<code>1 2 9 4 5</code>", last_edit_text)
        self.assertEqual(last_edit_kw["buttons"][4][0].type.data, b"tg:k:submit")

        # Clear empties buffer
        clear_cb = _CallbackEvent(7, b"tg:k:clear")
        await self.b.on_bot_callback(clear_cb)
        self.assertIn("<code>_ _ _ _ _</code>", clear_cb.edits[-1][0])

        # Wrong code via keypad resets buffer with error notice
        for key in ("9", "9", "9", "9", "9", "submit"):
            err_cb = _CallbackEvent(7, f"tg:k:{key}".encode())
            await self.b.on_bot_callback(err_cb)
        self.assertIn("验证码不正确", err_cb.edits[-1][0])
        self.assertIn("<code>_ _ _ _ _</code>", err_cb.edits[-1][0])

        # Resend code uses new delivery channel label (SMS)
        await self.b.on_bot_callback(_CallbackEvent(7, b"tg:resend"))
        self.assertEqual(self.b.user.code_requests, ["+8613800138000", "+8613800138000"])
        self.assertIn("手机短信", self.b.bot.sent[-1].text)

        # Valid code triggering 2FA clears keypad and enters password step
        for key in ("1", "2", "3", "4", "5", "submit"):
            ok_cb = _CallbackEvent(7, f"tg:k:{key}".encode())
            await self.b.on_bot_callback(ok_cb)
        self.assertEqual(self.b.user.sign_ins[-1], {"phone": "+8613800138000", "code": "12345", "phone_code_hash": "h2"})
        self.assertIsNone(ok_cb.edits[-1][1]["buttons"])
        self.assertEqual(self.b.login_step, "password")
        self.assertIn("两步验证密码", self.b.bot.sent[-1].text)

        # Wrong password -> deleted and prompts retry; right password -> completes login
        await self.b.on_bot_private(make_ev(7, "badpass"))
        self.assertIn((7, "badpass", False), deleted_events)
        self.assertIn("密码不正确", replies[-1][1])
        await self.b.on_bot_private(make_ev(7, "goodpass"))
        self.assertIn((7, "goodpass", False), deleted_events)
        self.assertTrue(self.b.tg_ready.is_set())
        self.assertIsNone(self.b.login_step)
        self.assertIn("Telegram 已登录", self.b.bot.sent[-1].text)
        self.assertIsInstance(self.b.bot.sent[-1].kwargs["buttons"], types.ReplyKeyboardHide)

    async def test_telegram_login_method_switching_and_text_code_fallback(self):
        from datetime import datetime, timedelta, timezone
        from telethon import types

        qr_wait_started = asyncio.Event()
        qr_cancelled = False

        class FakeQR:
            url = "tg://login?token=abc"
            expires = datetime.now(timezone.utc) + timedelta(minutes=2)

            async def wait(self, timeout=None):
                nonlocal qr_cancelled
                qr_wait_started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    qr_cancelled = True
                    raise

            async def recreate(self):
                pass

        class AuthUser(FakeUser):
            def __init__(self):
                super().__init__()
                self.sign_ins: list[dict] = []

            async def qr_login(self):
                return FakeQR()

            async def send_code_request(self, phone):
                return SimpleNamespace(phone_code_hash="h9", type=types.auth.SentCodeTypeApp(length=5))

            async def sign_in(self, **kw):
                self.sign_ins.append(kw)

            async def get_me(self):
                return SimpleNamespace(id=7, first_name="Owner", last_name=None, username="owner")

        self.b.user = AuthUser()
        self.b.start_pairing()
        deleted = []

        def make_ev(sender, text):
            async def reply(msg, **kw):
                return SimpleNamespace(text=msg, kwargs=kw)

            async def delete():
                deleted.append(text)

            return SimpleNamespace(
                sender_id=sender, chat_id=sender, raw_text=text,
                message=SimpleNamespace(contact=None), reply=reply, respond=reply, delete=delete,
            )

        await self.b.on_bot_private(make_ev(7, f"/login {self.b.pairing_code}"))
        await self.b.on_bot_callback(_CallbackEvent(7, b"tg:phone"))
        self.assertEqual(self.b.login_step, "phone")

        # Switch from phone ReplyKeyboard button to QR login
        await self.b.on_bot_private(make_ev(7, "📷 改用扫码登录"))
        await asyncio.wait_for(qr_wait_started.wait(), timeout=1.0)
        self.assertEqual(self.b.login_step, "qr")
        qr_msg = next(s for s in self.b.bot.sent if s.file is not None)
        self.assertEqual(qr_msg.kwargs["buttons"][0][0].type.data, b"tg:phone")

        # Switch back from QR inline button to verification code login -> cancels QR task & clears QR msg
        await self.b.on_bot_callback(_CallbackEvent(7, b"tg:phone"))
        await asyncio.sleep(0)
        self.assertTrue(qr_cancelled)
        self.assertIn(qr_msg.id, self.b.bot.deleted)
        self.assertEqual(self.b.login_step, "phone")

        # Manual phone + spaced text verification code fallback still works and clears keypad buttons
        await self.b.on_bot_private(make_ev(7, "+8613900001111"))
        self.assertEqual(self.b.login_step, "code")
        keypad_id = self.b.login_code_msg_id
        await self.b.on_bot_private(make_ev(7, " 1 2 3 4 5 "))
        self.assertIn(" 1 2 3 4 5 ", deleted)
        self.assertEqual(self.b.user.sign_ins[-1], {"phone": "+8613900001111", "code": "12345", "phone_code_hash": "h9"})
        self.assertTrue(any(cid == 7 and mid == keypad_id and kw.get("buttons") is None
                            for cid, mid, _, kw in self.b.bot.edited))
        self.assertTrue(self.b.tg_ready.is_set())


if __name__ == "__main__":
    unittest.main()


class StickerTests(unittest.IsolatedAsyncioTestCase):
    """Telegram stickers become a white-background JPEG or a short MP4 (WeChat stickers can't be sent)."""

    def setUp(self):
        from bridge import stickers
        self.stickers = stickers
        self.ffmpeg = stickers.find_ffmpeg()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    @staticmethod
    def webp() -> bytes:
        import io
        from PIL import Image
        im = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        im.paste((255, 0, 0, 255), (100, 100, 400, 400))
        out = io.BytesIO()
        im.save(out, "WEBP")
        return out.getvalue()

    @staticmethod
    def tgs() -> bytes:
        import gzip
        still = lambda v: {"a": 0, "k": v}
        lottie = {"v": "5.5.2", "fr": 30, "ip": 0, "op": 30, "w": 512, "h": 512, "layers": [{
            "ty": 4, "ip": 0, "op": 30, "st": 0,
            "ks": {"o": still(100), "p": still([256, 256, 0]), "a": still([0, 0, 0]), "s": still([100, 100, 100]),
                   "r": still(0)},
            "shapes": [{"ty": "gr", "it": [
                {"ty": "rc", "p": still([0, 0]), "s": still([200, 200]), "r": still(0)},
                {"ty": "fl", "c": still([1, 0, 0, 1]), "o": still(100)},
                {"ty": "tr", "p": still([0, 0]), "a": still([0, 0]), "s": still([100, 100]),
                 "r": {"a": 1, "k": [{"t": 0, "s": [0], "e": [90], "i": {"x": [1], "y": [1]}, "o": {"x": [0], "y": [0]}},
                                     {"t": 30, "s": [90]}]},
                 "o": still(100)}
            ]}]}]}
        return gzip.compress(json.dumps(lottie).encode())

    def frame(self, mp4: bytes):
        import subprocess
        from PIL import Image
        src, png = Path(self.tmp.name, "out.mp4"), Path(self.tmp.name, "frame.png")
        src.write_bytes(mp4)
        subprocess.run([self.ffmpeg, "-y", "-loglevel", "error", "-i", str(src), "-vf", r"select=eq(n\,5)",
                        "-frames:v", "1", str(png)], check=True)
        return Image.open(png).convert("RGB")

    def assertColor(self, got, want):
        self.assertTrue(all(abs(a - b) <= 12 for a, b in zip(got, want)), f"{got} != {want}")

    async def test_static_sticker_flattened_on_white(self):
        import io
        from PIL import Image
        kind, data = await self.stickers.convert(self.webp(), "image/webp")
        im = Image.open(io.BytesIO(data))
        self.assertEqual((kind, im.format), ("image", "JPEG"))
        self.assertColor(im.getpixel((5, 5)), (255, 255, 255))
        self.assertColor(im.getpixel((250, 250)), (255, 0, 0))

    async def test_video_sticker_keeps_transparency(self):
        if not self.ffmpeg:
            self.skipTest("ffmpeg not installed")
        import subprocess
        webm = Path(self.tmp.name, "in.webm")
        subprocess.run([self.ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "color=c=green:s=512x512:d=1,format=rgba,"
                        "geq=r=0:g=200:b=0:a='if(between(X,100,300)*between(Y,100,300),255,0)'",
                        "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", str(webm)], check=True)
        kind, data = await self.stickers.convert(webm.read_bytes(), "video/webm")
        self.assertEqual(kind, "video")
        im = self.frame(data)
        self.assertEqual(im.size, (384, 384))
        self.assertColor(im.getpixel((5, 5)), (255, 255, 255))
        self.assertColor(im.getpixel((150, 150)), (0, 200, 0))

    async def test_animated_sticker_rendered_to_video(self):
        if not self.ffmpeg:
            self.skipTest("ffmpeg not installed")
        kind, data = await self.stickers.convert(self.tgs(), "application/x-tgsticker")
        self.assertEqual(kind, "video")
        im = self.frame(data)
        self.assertColor(im.getpixel((5, 5)), (255, 255, 255))
        self.assertColor(im.getpixel((192, 192)), (255, 0, 0))

    async def test_animated_sticker_falls_back_to_first_frame(self):
        import io
        from unittest import mock
        from PIL import Image
        with mock.patch.object(self.stickers, "find_ffmpeg", return_value=None):
            kind, data = await self.stickers.convert(self.tgs(), "application/x-tgsticker")
        self.assertEqual(kind, "image")
        self.assertColor(Image.open(io.BytesIO(data)).getpixel((192, 192)), (255, 0, 0))

    async def test_bridge_sends_sticker_as_native_gif_and_legacy_fallback(self):
        b = Bridge.__new__(Bridge)
        b.max_media = 50 * 1048576
        data = self.webp()

        async def download_media(file=None):
            return data
        msg = SimpleNamespace(file=SimpleNamespace(size=len(data), mime_type="image/webp", name=None, ext=".webp"),
                              sticker=True, photo=None, download_media=download_media)
        kind, media = await b._tg_media(msg, native_stickers=True)
        self.assertEqual((kind, media.mime, media.filename), ("sticker", "image/gif", "sticker.gif"))
        self.assertTrue(media.data.startswith(b"GIF8"))

        kind_old, media_old = await b._tg_media(msg, native_stickers=False)
        self.assertEqual((kind_old, media_old.mime, media_old.filename), ("image", "image/jpeg", "sticker.jpg"))
        self.assertTrue(media_old.data.startswith(b"\xff\xd8"))

    async def test_to_wechat_sticker_preserves_alpha_and_limits(self):
        import io
        from unittest import mock
        from PIL import Image

        # Static WebP -> <=512px transparent GIF (keeps native 512x512 Telegram resolution)
        kind, gif = await self.stickers.to_wechat_sticker(self.webp(), "image/webp")
        self.assertEqual(kind, "sticker")
        self.assertLessEqual(len(gif), self.stickers.WX_STICKER_MAX_BYTES)
        im = Image.open(io.BytesIO(gif))
        self.assertEqual((im.format, im.size), ("GIF", (512, 512)))
        rgba = im.convert("RGBA")
        self.assertEqual(rgba.getpixel((5, 5))[3], 0)
        self.assertColor(rgba.getpixel((256, 256))[:3], (255, 0, 0))

        # Animated .tgs -> <=384px multi-frame transparent GIF (both with and without ffmpeg)
        for use_ffmpeg in (True, False):
            if use_ffmpeg and not self.ffmpeg:
                continue
            ctx = mock.patch.object(self.stickers, "find_ffmpeg", return_value=self.ffmpeg if use_ffmpeg else None)
            with ctx:
                kind, tgs_gif = await self.stickers.to_wechat_sticker(self.tgs(), "application/x-tgsticker")
            self.assertEqual(kind, "sticker")
            self.assertLessEqual(len(tgs_gif), self.stickers.WX_STICKER_MAX_BYTES)
            gim = Image.open(io.BytesIO(tgs_gif))
            self.assertGreater(gim.n_frames, 1)
            self.assertLessEqual(gim.n_frames, self.stickers.WX_STICKER_MAX_FRAMES)
            self.assertEqual(gim.size, (384, 384))
            self.assertEqual(gim.convert("RGBA").getpixel((5, 5))[3], 0)
            self.assertColor(gim.convert("RGBA").getpixel((192, 192))[:3], (255, 0, 0))

        # Video .webm (VP9 with alpha) -> <=384px multi-frame transparent GIF
        if self.ffmpeg:
            import subprocess
            webm = Path(self.tmp.name, "in_wx.webm")
            subprocess.run([self.ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "color=c=green:s=512x512:d=1,format=rgba,"
                            "geq=r=0:g=200:b=0:a='if(between(X,100,300)*between(Y,100,300)"
                            "+between(X,50+T*200,150+T*200)*between(Y,350,450),255,0)'",
                            "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", str(webm)], check=True)
            kind, webm_gif = await self.stickers.to_wechat_sticker(webm.read_bytes(), "video/webm")
            self.assertEqual(kind, "sticker")
            self.assertLessEqual(len(webm_gif), self.stickers.WX_STICKER_MAX_BYTES)
            wim = Image.open(io.BytesIO(webm_gif))
            self.assertGreater(wim.n_frames, 1)
            self.assertEqual(wim.size, (384, 384))
            self.assertEqual(wim.convert("RGBA").getpixel((5, 5))[3], 0)
            self.assertColor(wim.convert("RGBA").getpixel((150, 150))[:3], (0, 200, 0))

    async def test_tgs_unpremultiplied_alpha_and_aspect_ratio(self):
        import gzip
        import io
        from PIL import Image

        still = lambda v: {"a": 0, "k": v}
        # 512x256 non-square white shape at 55% opacity (alpha >= 128 -> opaque in GIF, must stay white not grey)
        lottie = {"v": "5.5.2", "fr": 30, "ip": 0, "op": 20, "w": 512, "h": 256, "layers": [{
            "ty": 4, "ip": 0, "op": 20, "st": 0,
            "ks": {"o": still(55), "p": still([256, 128, 0]), "a": still([0, 0, 0]), "s": still([100, 100, 100]),
                   "r": still(0)},
            "shapes": [{"ty": "gr", "it": [
                {"ty": "rc", "p": still([0, 0]), "s": still([200, 100]), "r": still(0)},
                {"ty": "fl", "c": still([1, 1, 1, 1]), "o": still(100)},
                {"ty": "tr", "p": still([0, 0]), "a": still([0, 0]), "s": still([100, 100]), "r": still(0),
                 "o": still(100)},
            ]}],
        }]}
        tgs_bytes = gzip.compress(json.dumps(lottie).encode())
        kind, gif = await self.stickers.to_wechat_sticker(tgs_bytes, "application/x-tgsticker")
        self.assertEqual(kind, "sticker")
        gim = Image.open(io.BytesIO(gif))
        # All frames are identical -> collapses to 1 static frame at 512px preserving 2:1 aspect ratio.
        self.assertEqual(gim.size, (512, 256))
        rgba = gim.convert("RGBA")
        self.assertEqual(rgba.getpixel((5, 5))[3], 0)
        self.assertColor(rgba.getpixel((256, 128))[:3], (255, 255, 255))

    async def test_animated_webp_resamples_fps_and_preserves_384px_quality(self):
        import io
        from unittest import mock
        from PIL import Image

        # Build a 25fps (40ms/frame) 1s animated WebP at 512x512
        src_frames = []
        for i in range(25):
            im = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
            im.paste((255, (i * 10) % 256, 40, 255), (60 + i * 3, 90, 320 + i * 3, 350))
            src_frames.append(im)
        webp_buf = io.BytesIO()
        src_frames[0].save(webp_buf, "WEBP", save_all=True, append_images=src_frames[1:], duration=40, loop=0)
        webp_data = webp_buf.getvalue()

        seen_calls: list[tuple[int, int, int, int, str]] = []

        def fake_ffmpeg(frames, w, h, fps, ffmpeg, *, max_colors=256, stats_mode="full", dither="none"):
            seen_calls.append((w, h, len(frames), max_colors, dither))
            # All 256-color attempts return 700KB (> 550KB fast budget, <= 1MB max):
            # must try lower FPS at 384px/256-colors, then stop BEFORE dropping to 192 colors.
            return b"GIF89a" + (b"x" * (700 * 1024))

        with mock.patch.object(self.stickers, "find_ffmpeg", return_value="ffmpeg"), \
             mock.patch.object(self.stickers, "find_gifsicle", return_value=None), \
             mock.patch.object(self.stickers, "_ffmpeg_rgba_to_gif", side_effect=fake_ffmpeg):
            out = self.stickers.webp_to_wechat_gif(webp_data, "ffmpeg")

        self.assertEqual(len(out), 6 + 700 * 1024)
        self.assertEqual(len(seen_calls), 3)
        self.assertTrue(all(w == 384 and h == 384 and colors == 256 for w, h, _, colors, _ in seen_calls))
        self.assertGreater(seen_calls[0][2], seen_calls[1][2])
        self.assertGreater(seen_calls[1][2], seen_calls[2][2])

    async def test_from_wechat_sticker_static_and_animated(self):
        import io
        from unittest import mock
        from PIL import Image

        # Static PNG -> 512px RGBA WebP
        png_buf = io.BytesIO()
        src = Image.new("RGBA", (240, 120), (0, 0, 0, 0))
        src.paste((20, 100, 240, 255), (20, 20, 100, 100))
        src.save(png_buf, "PNG")
        res = self.stickers.from_wechat_sticker(png_buf.getvalue(), "image/png")
        self.assertIsNotNone(res)
        kind, webp_bytes, meta = res
        self.assertEqual((kind, meta), ("static", {"w": 512, "h": 256}))
        wim = Image.open(io.BytesIO(webp_bytes))
        self.assertEqual((wim.format, wim.size), ("WEBP", (512, 256)))
        self.assertEqual(wim.convert("RGBA").getpixel((5, 5))[3], 0)

        # Animated GIF without ffmpeg -> None (falls back to sending original GIF)
        frames = [Image.new("RGBA", (120, 120), (i * 50, 180, 0, 255)) for i in range(4)]
        gif_buf = io.BytesIO()
        frames[0].save(gif_buf, "GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
        with mock.patch.object(self.stickers, "find_ffmpeg", return_value=None):
            self.assertIsNone(self.stickers.from_wechat_sticker(gif_buf.getvalue(), "image/gif"))

        # Animated GIF with ffmpeg -> 512px VP9 WebM <= 256 KB
        if self.ffmpeg:
            res_anim = self.stickers.from_wechat_sticker(gif_buf.getvalue(), "image/gif")
            self.assertIsNotNone(res_anim)
            akind, webm_bytes, ameta = res_anim
            self.assertEqual(akind, "video")
            self.assertEqual((ameta["w"], ameta["h"]), (512, 512))
            self.assertLessEqual(len(webm_bytes), self.stickers.TG_VIDEO_STICKER_MAX_BYTES)
            self.assertLessEqual(ameta["duration"], self.stickers.TG_VIDEO_STICKER_MAX_SECONDS)

    async def test_from_wechat_sticker_keeps_wxgf_animation_and_transparency(self):
        """Animated wxgf stickers (HEVC colour + gray mask frames) become transparent Telegram video stickers."""
        import io
        import subprocess
        from PIL import Image
        anim = _wxgf_sample(self.ffmpeg, 240, 240, 24, 12)
        if anim is None:
            self.skipTest("needs ffmpeg with libx265 to build a wxgf sample")
        colour, mask = self.stickers._wxgf_streams(anim)
        self.assertEqual((len(colour), len(mask)), (24, 24))
        kind, webm, meta = self.stickers.from_wechat_sticker(anim, "image/x-wxgf")
        self.assertEqual((kind, meta), ("video", {"w": 512, "h": 512, "duration": 2.0}))  # 24 frames at 12 fps
        self.assertLessEqual(len(webm), self.stickers.TG_VIDEO_STICKER_MAX_BYTES)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "s.webm").write_bytes(webm)

            def alpha_at(x: int, y: int) -> bytes:
                return subprocess.run([self.ffmpeg, "-loglevel", "error", "-c:v", "libvpx-vp9", "-i",
                                       str(Path(tmp, "s.webm")), "-vf", f"alphaextract,crop=1:1:{x}:{y}",
                                       "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
            corner, centre = alpha_at(0, 0), alpha_at(256, 256)
        self.assertEqual(len(corner), 24)  # every frame survives
        self.assertTrue(max(corner) < 20 and min(centre) > 235, (set(corner), set(centre)))

        # One masked frame -> transparent static sticker; no mask -> still animated, just opaque.
        kind, webp, meta = self.stickers.from_wechat_sticker(_wxgf_sample(self.ffmpeg, 200, 150, 1, 25), "image/x-wxgf")
        im = Image.open(io.BytesIO(webp)).convert("RGBA")
        self.assertEqual((kind, im.size), ("static", (512, 384)))
        self.assertEqual(im.getpixel((2, 2))[3], 0)
        self.assertEqual(im.getpixel((256, 192))[3], 255)
        kind, _, meta = self.stickers.from_wechat_sticker(_wxgf_sample(self.ffmpeg, 160, 160, 10, 10, mask=False),
                                                          "image/x-wxgf")
        self.assertEqual((kind, meta["duration"]), ("video", 1.0))

    async def test_picture_sent_as_file_becomes_original_wechat_image(self):
        """A picture sent as a Telegram document goes to WeChat as an original image, not a file."""
        import io
        from PIL import Image

        b = Bridge.__new__(Bridge)
        b.max_media = 50 * 1048576

        def make_msg(payload: bytes, mime: str, name: str, ext: str):
            async def download_media(file=None):
                return payload
            return SimpleNamespace(
                file=SimpleNamespace(size=len(payload), mime_type=mime, name=name, ext=ext),
                sticker=None, photo=None, voice=None, video=None, video_note=None, gif=None,
                download_media=download_media,
            )

        # 1) JPEG document -> exact source bytes sent as `image`
        jpg_buf = io.BytesIO()
        Image.new("RGB", (2400, 1600), (20, 140, 220)).save(jpg_buf, "JPEG", quality=92)
        jpg = jpg_buf.getvalue()
        kind, media = await b._tg_media(make_msg(jpg, "image/jpeg", "IMG_7462.JPG", ".jpg"))
        self.assertEqual((kind, media.mime, media.filename, media.data), ("image", "image/jpeg", "IMG_7462.JPG", jpg))

        # 2) PNG document <= 4096 px -> sent as `image` preserving PNG bytes
        png = _png(1920, 1080, (240, 60, 40))
        kind_png, media_png = await b._tg_media(make_msg(png, "application/octet-stream", "shot.png", ".png"))
        self.assertEqual((kind_png, media_png.mime, media_png.filename, media_png.data),
                         ("image", "image/png", "shot.png", png))

        # 3) Wide PNG > 4096 px (exceeds agent-wechat's non-JPEG limit) -> full-res JPEG `image`
        big_png = _png(5000, 16, (10, 180, 90))
        kind_big, media_big = await b._tg_media(make_msg(big_png, "image/png", "wide.png", ".png"))
        self.assertEqual((kind_big, media_big.mime, media_big.filename), ("image", "image/jpeg", "wide.jpg"))
        self.assertEqual(Image.open(io.BytesIO(media_big.data)).size, (5000, 16))

        # 4) Animated GIF document & non-image document -> stay `file`
        frames = [Image.new("RGBA", (64, 64), (i * 60, 100, 200, 255)) for i in range(3)]
        gif_buf = io.BytesIO()
        frames[0].save(gif_buf, "GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
        gif = gif_buf.getvalue()
        kind_gif, media_gif = await b._tg_media(make_msg(gif, "image/gif", "anim.gif", ".gif"))
        self.assertEqual((kind_gif, media_gif.filename, media_gif.data), ("file", "anim.gif", gif))

        pdf = b"%PDF-1.4..."
        kind_pdf, media_pdf = await b._tg_media(make_msg(pdf, "application/pdf", "doc.pdf", ".pdf"))
        self.assertEqual((kind_pdf, media_pdf.filename, media_pdf.data), ("file", "doc.pdf", pdf))

    async def test_gif_to_wechat_sticker_mp4_and_gif(self):
        import io
        from PIL import Image

        # 1) Already-compliant <=384px GIF passes through directly
        small_frames = [Image.new("RGBA", (320, 240), (i * 80, 180, 40, 255)) for i in range(3)]
        sbuf = io.BytesIO()
        small_frames[0].save(sbuf, "GIF", save_all=True, append_images=small_frames[1:], duration=80, loop=0)
        small_gif = sbuf.getvalue()
        kind1, out1 = await self.stickers.gif_to_wechat_sticker(small_gif, "image/gif")
        self.assertEqual((kind1, out1), ("sticker", small_gif))

        # 2) Oversized >384px animated GIF is resized to <=384px and <=1MB
        big_frames = [Image.new("RGBA", (600, 450), (i * 70, 50, 200, 255)) for i in range(4)]
        bbuf = io.BytesIO()
        big_frames[0].save(bbuf, "GIF", save_all=True, append_images=big_frames[1:], duration=80, loop=0)
        kind2, out2 = await self.stickers.gif_to_wechat_sticker(bbuf.getvalue(), "image/gif")
        self.assertEqual(kind2, "sticker")
        self.assertLessEqual(len(out2), self.stickers.WX_STICKER_MAX_BYTES)
        im2 = Image.open(io.BytesIO(out2))
        self.assertEqual(im2.size, (384, 288))
        self.assertGreater(im2.n_frames, 1)

        # 3) MP4 Telegram GIF is transcoded to <=384px <=1MB animated GIF via ffmpeg
        if self.ffmpeg:
            import subprocess
            mp4 = Path(self.tmp.name, "tg_gif.mp4")
            subprocess.run([self.ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc=size=480x270:rate=24:duration=1.5",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(mp4)], check=True)
            kind3, out3 = await self.stickers.gif_to_wechat_sticker(mp4.read_bytes(), "video/mp4")
            self.assertEqual(kind3, "sticker")
            self.assertLessEqual(len(out3), self.stickers.WX_STICKER_MAX_BYTES)
            im3 = Image.open(io.BytesIO(out3))
            self.assertEqual(im3.size, (384, 216))
            self.assertGreater(im3.n_frames, 1)
            self.assertLessEqual(im3.n_frames, self.stickers.WX_STICKER_MAX_FRAMES)

    async def test_still_frame_merging_preserves_duration_for_tgs_webp_and_mp4(self):
        import gzip
        import io
        from PIL import Image, ImageDraw, ImageSequence

        # 1) Animated WebP with 10 identical leading frames (600ms) + 5 moving frames (300ms) = 900ms
        webp_frames = []
        for i in range(15):
            im = Image.new("RGBA", (384, 384), (0, 0, 0, 0))
            x = 30 if i < 10 else 30 + (i - 9) * 35
            ImageDraw.Draw(im).rectangle((x, 90, x + 90, 210), fill=(255, 40, 40, 255))
            webp_frames.append(im)
        wbuf = io.BytesIO()
        webp_frames[0].save(wbuf, "WEBP", save_all=True, append_images=webp_frames[1:], duration=60, loop=0)
        kind_w, out_w = await self.stickers.to_wechat_sticker(wbuf.getvalue(), "image/webp")
        self.assertEqual(kind_w, "sticker")
        wim = Image.open(io.BytesIO(out_w))
        wdurs = [fr.info.get("duration") or 0 for fr in ImageSequence.Iterator(wim)]
        self.assertEqual(wim.size, (384, 384))
        self.assertLess(wim.n_frames, 10)
        self.assertGreater(wim.n_frames, 1)
        self.assertAlmostEqual(sum(wdurs), 900, delta=80)
        self.assertGreaterEqual(wdurs[0], 450)
        self.assertEqual(wim.convert("RGBA").getpixel((5, 5))[3], 0)

        # 1) Static 1-frame .tgs -> 512px transparent GIF
        still = lambda v: {"a": 0, "k": v}
        one_frame_tgs = gzip.compress(json.dumps({
            "v": "5.5.2", "fr": 30, "ip": 0, "op": 1, "w": 512, "h": 512, "layers": [{
                "ty": 4, "ip": 0, "op": 1, "st": 0,
                "ks": {"o": still(100), "p": still([256, 256, 0]), "a": still([0, 0, 0]),
                       "s": still([100, 100, 100]), "r": still(0)},
                "shapes": [{"ty": "gr", "it": [
                    {"ty": "rc", "p": still([0, 0]), "s": still([200, 200]), "r": still(0)},
                    {"ty": "fl", "c": still([0, 0.8, 0.2, 1]), "o": still(100)},
                    {"ty": "tr", "p": still([0, 0]), "a": still([0, 0]), "s": still([100, 100]), "r": still(0),
                     "o": still(100)},
                ]}],
            }],
        }).encode())
        kind_s, static_tgs_gif = await self.stickers.to_wechat_sticker(one_frame_tgs, "application/x-tgsticker")
        self.assertEqual(kind_s, "sticker")
        self.assertEqual(Image.open(io.BytesIO(static_tgs_gif)).size, (512, 512))

        # 2) Animated .tgs with static first half (0..14) + moving second half (15..23) at 24fps (1000ms)
        still = lambda v: {"a": 0, "k": v}
        lottie = {"v": "5.5.2", "fr": 24, "ip": 0, "op": 24, "w": 512, "h": 512, "layers": [{
            "ty": 4, "ip": 0, "op": 24, "st": 0,
            "ks": {"o": still(100),
                   "p": {"a": 1, "k": [{"t": 0, "s": [150, 256, 0], "h": 1},
                                       {"t": 15, "s": [360, 256, 0]}]},
                   "a": still([0, 0, 0]), "s": still([100, 100, 100]), "r": still(0)},
            "shapes": [{"ty": "rc", "p": still([0, 0]), "s": still([140, 140]), "r": still(0)},
                       {"ty": "fl", "c": still([0, 0.5, 1, 1]), "o": still(100)}],
        }]}
        tgs_bytes = gzip.compress(json.dumps(lottie).encode())
        kind_t, out_t = await self.stickers.to_wechat_sticker(tgs_bytes, "application/x-tgsticker")
        self.assertEqual(kind_t, "sticker")
        tim = Image.open(io.BytesIO(out_t))
        tdurs = [fr.info.get("duration") or 0 for fr in ImageSequence.Iterator(tim)]
        self.assertEqual(tim.size, (384, 384))
        self.assertLess(tim.n_frames, 14)
        self.assertGreater(tim.n_frames, 1)
        self.assertAlmostEqual(sum(tdurs), 1000, delta=100)

        # 3) MP4 Telegram GIF with 10 trailing still frames: must collapse frames WITHOUT truncating trailing hold
        if self.ffmpeg:
            import subprocess
            mp4 = Path(self.tmp.name, "tg_still_trail.mp4")
            raw_mp4 = []
            for i in range(18):
                im = Image.new("RGB", (320, 240), (20, 30, 50))
                x = 20 + min(i, 7) * 20
                ImageDraw.Draw(im).rectangle((x, 60, x + 70, 150), fill=(255, 200, 20))
                raw_mp4.append(im.tobytes())
            subprocess.run([self.ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                            "-s", "320x240", "-r", "12", "-i", "pipe:0",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(mp4)],
                           input=b"".join(raw_mp4), check=True)
            kind_m, out_m = await self.stickers.gif_to_wechat_sticker(mp4.read_bytes(), "video/mp4")
            self.assertEqual(kind_m, "sticker")
            mim = Image.open(io.BytesIO(out_m))
            mdurs = [fr.info.get("duration") or 0 for fr in ImageSequence.Iterator(mim)]
            self.assertEqual(mim.size, (320, 240))
            self.assertLess(mim.n_frames, 12)
            self.assertGreaterEqual(sum(mdurs), 1300)
            self.assertGreaterEqual(mdurs[-1], 600)

    async def test_384px_preserved_when_192_colors_fits_within_1mb_and_steps_to_320_before_240(self):
        import io
        from unittest import mock
        from PIL import Image

        src_frames = []
        for i in range(12):
            im = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
            im.paste((255, (i * 20) % 256, 60, 255), (40 + i * 8, 60, 320 + i * 8, 340))
            src_frames.append(im)
        wbuf = io.BytesIO()
        src_frames[0].save(wbuf, "WEBP", save_all=True, append_images=src_frames[1:], duration=80, loop=0)

        seen_calls: list[tuple[int, int]] = []

        def fake_ffmpeg_192(frames, w, h, fps, ffmpeg, *, max_colors=256, stats_mode="full", dither="none"):
            seen_calls.append((w, max_colors))
            # All 256-color attempts exceed 1 MB; 192-color at 384px fits under 1 MB.
            size = 1100 * 1024 if max_colors >= 256 else 400 * 1024
            return b"GIF89a" + (b"x" * size)

        with mock.patch.object(self.stickers, "find_ffmpeg", return_value="ffmpeg"), \
             mock.patch.object(self.stickers, "find_gifsicle", return_value=None), \
             mock.patch.object(self.stickers, "_ffmpeg_rgba_to_gif", side_effect=fake_ffmpeg_192):
            out = self.stickers.webp_to_wechat_gif(wbuf.getvalue(), "ffmpeg")

        self.assertEqual(len(out), 6 + 400 * 1024)
        self.assertTrue(all(edge == 384 for edge, _ in seen_calls))
        self.assertEqual(seen_calls[-1], (384, 192))

        # When all 384px attempts exceed 1 MB, fallback steps through 320px before 240px
        seen_fallback: list[tuple[int, int]] = []

        def fake_ffmpeg_320(frames, w, h, fps, ffmpeg, *, max_colors=256, stats_mode="full", dither="none"):
            seen_fallback.append((w, max_colors))
            size = 1100 * 1024 if w >= 384 else 400 * 1024
            return b"GIF89a" + (b"x" * size)

        with mock.patch.object(self.stickers, "find_ffmpeg", return_value="ffmpeg"), \
             mock.patch.object(self.stickers, "find_gifsicle", return_value=None), \
             mock.patch.object(self.stickers, "_ffmpeg_rgba_to_gif", side_effect=fake_ffmpeg_320):
            out2 = self.stickers.webp_to_wechat_gif(wbuf.getvalue(), "ffmpeg")

        self.assertEqual(len(out2), 6 + 400 * 1024)
        self.assertEqual(seen_fallback[-1], (320, 192))

    async def test_sticker_disk_cache_hits_isolates_edges_and_lru_eviction(self):
        import io
        import os
        import time
        from unittest import mock
        from PIL import Image

        cache_dir = Path(self.tmp.name, "scache")
        # 1) Disk cache hits skip re-converting identical sticker inputs and isolate by edge limit
        cache = self.stickers.StickerDiskCache(cache_dir, max_bytes=10 * 1024 * 1024)
        webp = self.webp()
        k1, out1 = await self.stickers.to_wechat_sticker(webp, "image/webp", cache=cache)
        self.assertEqual(Image.open(io.BytesIO(out1)).size, (512, 512))
        with mock.patch.object(self.stickers, "webp_to_wechat_gif", side_effect=AssertionError("cache miss")):
            k2, out2 = await self.stickers.to_wechat_sticker(webp, "image/webp", cache=cache)
        self.assertEqual((k1, out1), (k2, out2))

        k_legacy, out_legacy = await self.stickers.to_wechat_sticker(
            webp, "image/webp", cache=cache, max_edge=240, static_max_edge=240,
        )
        self.assertEqual(k_legacy, "sticker")
        self.assertEqual(Image.open(io.BytesIO(out_legacy)).size, (240, 240))

        # 2) LRU eviction keeps newest entries within max_bytes budget
        small_cache = self.stickers.StickerDiskCache(Path(self.tmp.name, "slru"), max_bytes=220)
        small_cache.put("old", b"G" * 100)
        os.utime(small_cache._path_for("old"), (time.time() - 30, time.time() - 30))
        small_cache.put("mid", b"M" * 100)
        os.utime(small_cache._path_for("mid"), (time.time() - 20, time.time() - 20))
        # Touching "old" makes "mid" the least recently used entry
        self.assertEqual(small_cache.get("old"), b"G" * 100)
        small_cache.put("new", b"N" * 100)
        self.assertEqual(small_cache.get("old"), b"G" * 100)
        self.assertIsNone(small_cache.get("mid"))
        self.assertEqual(small_cache.get("new"), b"N" * 100)

    def test_extract_video_meta_and_thumb(self) -> None:
        self.assertIsNone(self.stickers.extract_video_meta_and_thumb(b"not a video", "bad.mp4"))
        if self.ffmpeg:
            import io
            import subprocess
            from PIL import Image
            mp4 = Path(self.tmp.name, "portrait_video.mp4")
            subprocess.run([self.ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc=size=480x854:rate=15:duration=2.0",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(mp4)], check=True)
            meta = self.stickers.extract_video_meta_and_thumb(mp4.read_bytes(), "portrait_video.mp4")
            self.assertIsNotNone(meta)
            w, h, dur, thumb = meta  # type: ignore[misc]
            self.assertEqual((w, h), (480, 854))
            self.assertAlmostEqual(dur, 2.0, delta=0.3)
            with Image.open(io.BytesIO(thumb)) as im:
                self.assertEqual(im.format, "JPEG")
                self.assertLessEqual(max(im.size), 320)
                self.assertEqual(im.size, (180, 320))


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


class CallBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(WeChatConfig(token="secret-token"), TelegramConfig(api_id=1, api_hash="x", bot_token="y"),
                     BridgeConfig(create_interval_seconds=0, sync_avatar=False), Path(self.tmp.name) / "config.toml")
        self.daemon = FakeDaemon()
        self.b = Bridge(cfg)
        await self.b.wx.http.aclose()
        self.b.wx.http = httpx.AsyncClient(transport=httpx.MockTransport(self.daemon.handler),
                                           headers={"Authorization": "Bearer secret-token"})
        self.b.user, self.b.bot = FakeUser(), FakeBot()
        self.b.bot_input = object()
        self.b.bot_me = SimpleNamespace(username="mybot")
        self.b.tg_ready.set()
        self.b.owner_id = 999
        await self.b.wx.resolve_account()

    async def asyncTearDown(self):
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def test_pending_offered_and_one_click_reject_from_pending(self):
        self.daemon.add_thread("wxid_alice", "Alice")
        sock = _FakeCallsSocket()
        self.b._calls_sock = sock

        async def handle_cmd(cmd: dict):
            if cmd["type"] == "call.ready" and cmd.get("ready"):
                # Daemon transitions pending offer to offered asynchronously after scoped call.ready
                async def push_offered():
                    await asyncio.sleep(0.01)
                    await self.b.handle_call_event({
                        "type": "call.offered",
                        "offer": {
                            "callId": "call-1",
                            "revision": 3,
                            "kind": "direct",
                            "callType": "video",
                            "callerId": "wxid_alice",
                        },
                    })
                    await self.b.handle_call_event({"type": "call.pending", "offer": None})
                asyncio.create_task(push_offered())
                return {"type": "command.result", "requestId": "req-ready", "status": "accepted"}
            return {"type": "command.result", "requestId": "req-cmd", "status": "accepted"}

        sock.on_command = handle_cmd

        await self.b.handle_call_event({
            "type": "hello",
            "protocolVersion": 2,
            "clientId": "c1",
            "conferenceInvitations": [],
            "audioFormat": {"sampleRate": 16000, "channels": 1, "frameMs": 20},
            "currentPendingOffer": {
                "callId": "call-1",
                "revision": 1,
                "kind": "direct",
                "callType": "video",
                "callerId": "wxid_alice",
                "phase": "waiting_for_ready",
            },
        })

        call_msg = self.b.bot.sent[-1]
        self.assertIn("微信视频来电", call_msg.text)
        self.assertIn("Alice", call_msg.text)
        self.assertIn("响铃中", call_msg.text)
        buttons = call_msg.kwargs["buttons"]
        self.assertEqual(len(buttons), 1)
        btn = buttons[0][0]
        cb_data = btn.type.data if hasattr(btn, "type") and hasattr(btn.type, "data") else btn.data
        self.assertTrue(cb_data.startswith(b"callrej:"))

        # Unauthorized click is rejected cleanly
        unauth = _CallbackEvent(111, cb_data)
        await self.b.on_bot_callback(unauth)
        self.assertEqual(unauth.answers, [("只有桥接账号本人可以操作", True)])
        self.assertEqual(sock.commands, [])

        # Authorized click sends scoped call.ready -> call.reject(revision=3) -> call.ready(false)
        auth = _CallbackEvent(999, cb_data)
        await self.b.on_bot_callback(auth)
        self.assertEqual([c["type"] for c in sock.commands], ["call.ready", "call.reject", "call.ready"])
        self.assertEqual(sock.commands[0]["scope"], {"kind": "offer", "callId": "call-1", "expectedRevision": 1})
        self.assertEqual(sock.commands[1]["callId"], "call-1")
        self.assertEqual(sock.commands[1]["expectedRevision"], 3)
        self.assertFalse(sock.commands[2]["ready"])

        self.assertIn("已在 Telegram 拒接", call_msg.text)
        self.assertIsNone(call_msg.kwargs["buttons"])

        # Subsequent ended state keeps the rejected status rather than overwriting with missed
        await self.b.handle_call_event({
            "type": "call.state",
            "call": {"callId": "call-1", "revision": 5, "kind": "direct", "callType": "voice",
                     "direction": "incoming", "state": "ended", "ownedByThisClient": False},
        })
        self.assertIn("已在 Telegram 拒接", call_msg.text)

    async def test_call_command_needs_call_page(self):
        self.daemon.add_thread("wxid_alice", "Alice")
        self.b.store.link("wxid_alice", -700, "Alice")
        self.b.wx_ready = True
        self.assertIsNone(self.b.call_web)
        await self.b.on_tg_message(tg_event(-700, 10, "/call"))
        self.assertIn("CALL_WEB_URL", self.b.bot.sent[-1].text)

    async def test_call_lifecycle_updates_and_pending_cancelled(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        sock = _FakeCallsSocket()
        self.b._calls_sock = sock

        # 1) Pending call cancelled before ready/offered
        await self.b.handle_call_event({
            "type": "call.pending",
            "offer": {"callId": "c-cancel", "revision": 1, "kind": "direct", "callType": "voice",
                      "callerId": "wxid_bob", "phase": "waiting_for_ready"},
        })
        msg1 = self.b.bot.sent[-1]
        self.assertIn("微信语音来电", msg1.text)
        self.assertIsNotNone(msg1.kwargs["buttons"])
        await self.b.handle_call_event({"type": "call.pending", "offer": None})
        self.assertIn("已取消", msg1.text)
        self.assertIsNone(msg1.kwargs["buttons"])

        # 2) Direct offered call answered elsewhere -> connected -> ended
        await self.b.handle_call_event({
            "type": "call.offered",
            "offer": {"callId": "c-ans", "revision": 3, "kind": "direct", "callType": "voice",
                      "callerId": "wxid_bob"},
        })
        msg2 = self.b.bot.sent[-1]
        await self.b.handle_call_event({
            "type": "call.state",
            "call": {"callId": "c-ans", "revision": 4, "kind": "direct", "callType": "voice",
                     "direction": "incoming", "state": "connected", "ownedByThisClient": False},
        })
        self.assertIn("通话中", msg2.text)
        self.assertIsNone(msg2.kwargs["buttons"])
        await self.b.handle_call_event({
            "type": "call.state",
            "call": {"callId": "c-ans", "revision": 5, "kind": "direct", "callType": "voice",
                     "direction": "incoming", "state": "ended", "ownedByThisClient": False},
        })
        self.assertIn("通话已结束", msg2.text)

    async def test_call_card_names_by_remark_and_shows_connect_time(self):
        from bridge import tghtml
        # An older profile: the thread still reports the nickname, the contact record has the remark.
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_contact("wxid_bob", "Bob", remark="冯宝儿", nickname="Bob")
        self.b._calls_sock = _FakeCallsSocket()

        await self.b.handle_call_event({
            "type": "call.offered",
            "offer": {"callId": "c-rm", "revision": 1, "kind": "direct", "callType": "voice", "callerId": "wxid_bob"},
        })
        msg = self.b.bot.sent[-1]
        self.assertIn("<blockquote>来电人：<b>冯宝儿</b>\n状态：🔔 响铃中…</blockquote>", msg.text)
        self.assertIs(msg.kwargs["parse_mode"], tghtml.HTML)
        self.assertEqual(self.b._link_title("wxid_bob"), "冯宝儿")  # the Telegram chat is named by the remark too

        await self.b.handle_call_event({"type": "call.state", "call": {
            "callId": "c-rm", "revision": 2, "kind": "direct", "callType": "voice",
            "direction": "incoming", "state": "connected", "ownedByThisClient": False}})
        self.assertIn('接通：<tg-time unix="', msg.text)
        text, entities = tghtml.HTML.parse(msg.text)
        dates = [e for e in entities if isinstance(e, types.MessageEntityFormattedDate)]
        self.assertEqual(len(dates), 1)
        self.assertTrue(dates[0].short_time)
        self.assertNotIn("<", text)

        self.b._active_calls["c-rm"]["connected_at"] -= 192
        await self.b.handle_call_event({"type": "call.state", "call": {
            "callId": "c-rm", "revision": 3, "kind": "direct", "callType": "voice",
            "direction": "incoming", "state": "ended", "ownedByThisClient": False}})
        self.assertIn("状态：⚪ 通话已结束\n时长：<b>3:12</b>（<tg-time", msg.text)

    async def test_call_card_drops_date_entity_telegram_refuses(self):
        from bridge import tghtml
        self.daemon.add_thread("wxid_bob", "Bob")
        self.b._calls_sock = _FakeCallsSocket()
        edit = self.b.bot.edit_message

        async def picky_edit(chat, msg_id, text, **kw):
            if kw.get("parse_mode") is tghtml.HTML and "<tg-time" in text:
                raise errors.BadRequestError(None, "ENTITY_BOUNDS_INVALID")
            return await edit(chat, msg_id, text, **kw)

        self.b.bot.edit_message = picky_edit
        await self.b.handle_call_event({
            "type": "call.offered",
            "offer": {"callId": "c-old", "revision": 1, "kind": "direct", "callType": "voice", "callerId": "wxid_bob"},
        })
        msg = self.b.bot.sent[-1]
        await self.b.handle_call_event({"type": "call.state", "call": {
            "callId": "c-old", "revision": 2, "kind": "direct", "callType": "voice",
            "direction": "incoming", "state": "connected", "ownedByThisClient": False}})
        self.assertIn("接通：", msg.text)
        self.assertNotIn("<tg-time", msg.text)
        self.assertEqual(msg.kwargs["parse_mode"], "html")
        self.assertFalse(self.b._tg_time_ok)

    async def test_group_sender_named_by_remark_and_follows_changes(self):
        self.daemon.add_thread("room@chatroom", "Team", kind="group")
        self.daemon.group_members["room@chatroom"] = [
            {"id": "wxid_x", "username": "wxid_x", "displayName": "Xavier", "roomDisplayName": "X in group",
             "nickname": "Xavier", "mentionName": "X in group"},
        ]
        self.daemon.add_contact("wxid_x", "Xavier", remark="小夏", nickname="Xavier")
        self.daemon.add_message("gx1", "room@chatroom", "hi", sender="wxid_x")
        await self.b.handle_event({"cursor": "1", "kind": "message.committed", "data": {
            "messageId": "gx1", "threadId": "room@chatroom", "origin": "live", "direction": "incoming"}})
        self.assertIn("<b>小夏</b>", self.b.bot.sent[-1].text)

        self.daemon.contacts["wxid_x"]["remark"] = "夏老师"
        await self.b.handle_event({"cursor": "2", "kind": "contacts.updated", "data": {"contactId": "wxid_x"}})
        self.daemon.add_message("gx2", "room@chatroom", "again", sender="wxid_x")
        await self.b.handle_event({"cursor": "1", "kind": "message.committed", "data": {
            "messageId": "gx2", "threadId": "room@chatroom", "origin": "live", "direction": "incoming"}})
        self.assertIn("<b>夏老师</b>", self.b.bot.sent[-1].text)

    async def test_conference_invitation_notifies_and_clears(self):
        self.daemon.add_thread("123@chatroom", "Team", kind="group")
        await self.b.handle_call_event({
            "type": "conference.invitations",
            "invitations": [{"invitationId": "inv-1", "revision": 1, "groupId": "123@chatroom", "callerId": "wxid_a"}],
        })
        msg = self.b.bot.sent[-1]
        self.assertIn("微信群语音通话邀请", msg.text)
        self.assertIn("Alice in group", msg.text)
        self.assertIn("邀请中", msg.text)
        self.assertIsNone(msg.kwargs.get("buttons"))

        await self.b.handle_call_event({"type": "conference.invitations", "invitations": []})
        self.assertIn("已结束或已过期", msg.text)

    async def test_offered_direct_reject_and_expired_error_and_loop_unavailable(self):
        from bridge.wechat import WeChatError
        self.daemon.add_thread("wxid_alice", "Alice")
        sock = _FakeCallsSocket()
        self.b._calls_sock = sock

        # Direct offered reject with stale retry -> accepted
        sock.responses = [
            {"type": "command.result", "requestId": "r1", "status": "error", "code": "stale"},
            {"type": "command.result", "requestId": "r2", "status": "accepted"},
        ]
        await self.b.handle_call_event({
            "type": "call.offered",
            "offer": {"callId": "call-dir", "revision": 3, "kind": "direct", "callType": "voice",
                      "callerId": "wxid_alice"},
        })
        msg = self.b.bot.sent[-1]
        b1 = msg.kwargs["buttons"][0][0]
        cb = _CallbackEvent(999, b1.type.data if hasattr(b1, "type") and hasattr(b1.type, "data") else b1.data)
        await self.b.on_bot_callback(cb)
        self.assertEqual([c["type"] for c in sock.commands], ["call.reject", "call.reject"])
        self.assertIn("已在 Telegram 拒接", msg.text)
        self.assertIsNone(msg.kwargs["buttons"])

        # Offered call whose reject returns offer_expired clears button & marks ended
        sock.commands.clear()
        sock.responses = [{"type": "command.result", "requestId": "r3", "status": "error", "code": "offer_expired"}]
        await self.b.handle_call_event({
            "type": "call.offered",
            "offer": {"callId": "call-exp", "revision": 3, "kind": "direct", "callType": "voice",
                      "callerId": "wxid_alice"},
        })
        msg_exp = self.b.bot.sent[-1]
        b2 = msg_exp.kwargs["buttons"][0][0]
        cb_exp = _CallbackEvent(999, b2.type.data if hasattr(b2, "type") and hasattr(b2.type, "data") else b2.data)
        await self.b.on_bot_callback(cb_exp)
        self.assertIn("已结束或已过期", msg_exp.text)
        self.assertIsNone(msg_exp.kwargs["buttons"])

        # calls_loop handles 404 unsupported daemon gracefully without crashing
        async def fail_calls():
            raise WeChatError(404, "not_found", False)

        self.b.wx.calls_socket = fail_calls
        self.b._calls_retry_sleep = 60.0
        task = asyncio.create_task(self.b.calls_loop())
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_calls_websocket_handshake_ping_pong_and_commands(self):
        import base64
        import hashlib
        import struct

        received_client_msgs: list[dict] = []
        pong_received = asyncio.Event()

        def ws_frame(opcode: int, payload: bytes) -> bytes:
            head = bytearray([0x80 | opcode])
            n = len(payload)
            if n < 126:
                head.append(n)
            else:
                head.append(126)
                head.extend(struct.pack("!H", n))
            return bytes(head) + payload

        async def read_client_frame(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
            try:
                hdr = await reader.readexactly(2)
            except asyncio.IncompleteReadError:
                return None
            opcode = hdr[0] & 0x0F
            masked = (hdr[1] & 0x80) != 0
            length = hdr[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", await reader.readexactly(2))[0]
            mask = await reader.readexactly(4) if masked else b""
            data = await reader.readexactly(length) if length else b""
            if masked and data:
                data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
            return opcode, data

        async def handle_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                req = await reader.readuntil(b"\r\n\r\n")
                lines = req.decode("latin-1").split("\r\n")
                hdrs = {k.strip().lower(): v.strip() for ln in lines[1:] if ":" in ln for k, _, v in [ln.partition(":")]}
                if "forbidden" in lines[0]:
                    body = b'{"error":{"code":"forbidden"}}'
                    writer.write(
                        f"HTTP/1.1 403 Forbidden\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
                    )
                    await writer.drain()
                    return
                accept = base64.b64encode(
                    hashlib.sha1((hdrs["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
                ).decode()
                writer.write((
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode())
                await writer.drain()

                op, hello_bytes = await read_client_frame(reader)
                self.assertEqual(op, 0x1)
                received_client_msgs.append(json.loads(hello_bytes))
                writer.write(ws_frame(0x1, json.dumps({
                    "type": "hello",
                    "protocolVersion": 2,
                    "clientId": "c-ws",
                    "audioFormat": {"sampleRate": 16000, "channels": 1, "frameMs": 20},
                }).encode()))
                # Send Ping immediately followed by an offered call
                writer.write(ws_frame(0x9, b"hb"))
                writer.write(ws_frame(0x1, json.dumps({
                    "type": "call.offered",
                    "offer": {"callId": "ws-call", "revision": 3, "kind": "direct",
                              "callType": "voice", "callerId": "wxid_bob"},
                }).encode()))
                await writer.drain()

                while (frame := await read_client_frame(reader)) is not None:
                    fop, fdata = frame
                    if fop == 0x8:
                        break
                    if fop == 0xA:
                        if fdata == b"hb":
                            pong_received.set()
                        continue
                    if fop == 0x1:
                        msg = json.loads(fdata)
                        received_client_msgs.append(msg)
                        if msg.get("type") == "call.reject":
                            writer.write(ws_frame(0x1, json.dumps({
                                "type": "command.result",
                                "requestId": msg["requestId"],
                                "status": "accepted",
                            }).encode()))
                            await writer.drain()
                            writer.write(ws_frame(0x8, b"\x03\xe8"))
                            await writer.drain()
                            break
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

        server = await asyncio.start_server(handle_conn, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            wx = WeChatClient(f"http://127.0.0.1:{port}", "secret-token", ACC)
            try:
                sock, hello = await wx.calls_socket()
                self.assertEqual(hello["protocolVersion"], 2)
                events_iter = sock.events()
                ev = await anext(events_iter)
                self.assertEqual(ev["type"], "call.offered")
                await asyncio.wait_for(pong_received.wait(), timeout=2.0)
                res = await sock.command({"type": "call.reject", "callId": "ws-call", "expectedRevision": 3})
                self.assertEqual(res["status"], "accepted")
                # Close frame from server terminates sock.events() cleanly
                rest = [e async for e in events_iter]
                self.assertEqual(rest, [])
                await sock.close()
                self.assertEqual([m["type"] for m in received_client_msgs], ["call.hello", "call.reject"])
            finally:
                await wx.close()

            # 403 forbidden error parsing
            wx_bad = WeChatClient(f"http://127.0.0.1:{port}", "secret-token", "forbidden")
            try:
                from bridge.wechat import WeChatError
                with self.assertRaises(WeChatError) as ctx:
                    await wx_bad.calls_socket()
                self.assertEqual((ctx.exception.status, ctx.exception.code), (403, "forbidden"))
            finally:
                await wx_bad.close()
        finally:
            server.close()
            await server.wait_closed()


class WeChatClientTimeoutAndRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_relaxed_timeouts_and_send_timeout(self):
        from bridge.wechat import Media

        seen_timeouts: list[tuple[str, str, dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            ext = request.extensions.get("timeout", {})
            seen_timeouts.append((request.method, request.url.path, dict(ext)))
            path = request.url.path.removeprefix("/v1/")
            if path.endswith("/uploads"):
                return httpx.Response(201, json={"id": "src1"})
            if path.endswith("/sticker-preparations") and request.method == "POST":
                return httpx.Response(202, json={"id": "prep1", "state": "available"})
            if path.endswith("/sends") and request.method == "POST":
                return httpx.Response(202, json={"id": "send1", "state": "accepted"})
            return httpx.Response(200, json={"id": "msg1"})

        wx = WeChatClient("http://localhost:6174", "secret-token", ACC, read_retry_delays=(0.0, 0.0))
        wx.http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                    headers={"Authorization": "Bearer secret-token"},
                                    timeout=wx.http.timeout)
        try:
            self.assertEqual(wx.http.timeout.read, 60.0)
            self.assertEqual(wx.http.timeout.connect, 10.0)
            await wx.message("m1")
            await wx.send_text("wxid_alice", "hi", "k1")
            await wx.send_sticker("wxid_alice", Media(b"GIF89a", "image/gif", "s.gif"), "k2")
        finally:
            await wx.close()

        msg_call = [t for t in seen_timeouts if t[1].endswith("/messages/m1")][0]
        self.assertEqual(msg_call[2]["read"], 60.0)
        send_calls = [t for t in seen_timeouts if t[0] == "POST" and t[1].endswith("/sends")]
        self.assertEqual(len(send_calls), 2)
        for _, _, ext in send_calls:
            self.assertEqual(ext["read"], 120.0)
            self.assertEqual(ext["connect"], 10.0)
        prep_call = [t for t in seen_timeouts if t[0] == "POST" and t[1].endswith("/sticker-preparations")][0]
        self.assertEqual(prep_call[2]["read"], 120.0)

    async def test_read_only_retries_and_no_write_retries(self):
        from bridge.wechat import WeChatError

        attempts: dict[str, int] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            key = f"{request.method} {request.url.path}"
            attempts[key] = attempts.get(key, 0) + 1
            n = attempts[key]
            path = request.url.path.removeprefix("/v1/")
            if path.endswith("/messages/flaky_timeout"):
                if n < 3:
                    raise httpx.ReadTimeout("timed out", request=request)
                return httpx.Response(200, json={"id": "flaky_timeout", "body": "ok"})
            if path.endswith("/messages/flaky_503"):
                if n == 1:
                    return httpx.Response(503, json={"error": {"code": "busy", "retryable": True}})
                return httpx.Response(200, json={"id": "flaky_503", "body": "ok"})
            if path.endswith("/messages/always_timeout"):
                raise httpx.ReadTimeout("still timed out", request=request)
            if path.endswith("/messages/not_found"):
                return httpx.Response(404, json={"error": {"code": "not_found", "retryable": False}})
            if path.endswith("/messages/non_retryable_500"):
                return httpx.Response(500, json={"error": {"code": "internal", "retryable": False}})
            if path.endswith("/sends") and request.method == "POST":
                raise httpx.ReadTimeout("send timed out", request=request)
            raise AssertionError(f"unexpected {key}")

        wx = WeChatClient("http://localhost:6174", "secret-token", ACC, read_retry_delays=(0.0, 0.0))
        wx.http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                    headers={"Authorization": "Bearer secret-token"},
                                    timeout=wx.http.timeout)
        try:
            # GET recovers after two ReadTimeouts (3 attempts total)
            msg = await wx.message("flaky_timeout")
            self.assertEqual(msg["body"], "ok")
            self.assertEqual(attempts[f"GET /v1/accounts/{ACC}/messages/flaky_timeout"], 3)

            # GET recovers after retryable 503
            msg2 = await wx.message("flaky_503")
            self.assertEqual(msg2["body"], "ok")
            self.assertEqual(attempts[f"GET /v1/accounts/{ACC}/messages/flaky_503"], 2)

            # GET raises after exhausting retries
            with self.assertRaises(httpx.ReadTimeout):
                await wx.message("always_timeout")
            self.assertEqual(attempts[f"GET /v1/accounts/{ACC}/messages/always_timeout"], 3)

            # Non-retryable 404 and non-retryable 500 fail immediately without retry
            with self.assertRaises(WeChatError) as ctx404:
                await wx.message("not_found")
            self.assertEqual(ctx404.exception.status, 404)
            self.assertEqual(attempts[f"GET /v1/accounts/{ACC}/messages/not_found"], 1)

            with self.assertRaises(WeChatError) as ctx500:
                await wx.message("non_retryable_500")
            self.assertEqual(ctx500.exception.status, 500)
            self.assertEqual(attempts[f"GET /v1/accounts/{ACC}/messages/non_retryable_500"], 1)

            # Mutating POST sends must NOT be retried on ReadTimeout
            with self.assertRaises(httpx.ReadTimeout):
                await wx.send_text("wxid_alice", "hi", "k-no-retry")
            self.assertEqual(attempts[f"POST /v1/accounts/{ACC}/sends"], 1)
        finally:
            await wx.close()

    async def test_supports_stickers_does_not_cache_transient_failure(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503, json={"error": {"code": "starting", "retryable": False}})
            return httpx.Response(200, json={"stickers": True})

        wx = WeChatClient("http://localhost:6174", "secret-token", ACC, read_retry_delays=())
        wx.http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                    headers={"Authorization": "Bearer secret-token"},
                                    timeout=wx.http.timeout)
        try:
            self.assertFalse(await wx.supports_stickers())
            self.assertTrue(await wx.supports_stickers())
            self.assertTrue(await wx.supports_stickers())
            self.assertEqual(calls, 2)
        finally:
            await wx.close()

    async def test_sticker_edge_limits_detects_daemon_version_and_retries_transient_failure(self):
        calls = 0
        ver = "2.5.0"

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            assert request.url.path == "/v1/status"
            calls += 1
            if calls == 1:
                return httpx.Response(503, json={"error": {"code": "starting", "retryable": False}})
            return httpx.Response(200, json={"state": "ready", "version": {"version": ver}})

        wx = WeChatClient("http://localhost:6174", "secret-token", ACC, read_retry_delays=())
        wx.http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                    headers={"Authorization": "Bearer secret-token"},
                                    timeout=wx.http.timeout)
        try:
            # Transient failure returns safe legacy 240px limits without caching failure
            self.assertEqual(await wx.sticker_edge_limits(), (240, 240))
            # 2.5.0 unlocks 384px animated / 512px static and caches result
            self.assertEqual(await wx.sticker_edge_limits(), (384, 512))
            self.assertEqual(await wx.sticker_edge_limits(), (384, 512))
            self.assertEqual(calls, 2)

            # < 2.5.0 stays at 240px
            ver = "2.4.2"
            wx._daemon_version = None
            self.assertEqual(await wx.sticker_edge_limits(), (240, 240))

            # Pre-release / v-prefixed 2.5+ versions also unlock high-res limits
            ver = "v2.5.1-rc.1"
            wx._daemon_version = None
            self.assertEqual(await wx.sticker_edge_limits(), (384, 512))
        finally:
            await wx.close()


class LinkCardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(
            WeChatConfig(token="secret-token"),
            TelegramConfig(api_id=1, api_hash="x", bot_token="y"),
            BridgeConfig(create_interval_seconds=0, sync_avatar=False, link_card_images=True),
            Path(self.tmp.name) / "config.toml",
        )
        self.daemon = FakeDaemon()
        self.b = Bridge(cfg)
        await self.b.wx.http.aclose()
        self.b.wx.http = httpx.AsyncClient(
            transport=httpx.MockTransport(self.daemon.handler),
            headers={"Authorization": "Bearer secret-token"},
        )
        self.b.user, self.b.bot = FakeUser(), FakeBot()
        self.b.bot_input = object()
        self.b.bot_me = SimpleNamespace(username="mybot")
        await self.b.wx.resolve_account()

    async def asyncTearDown(self):
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def event(self, mid, tid, origin="live", direction="incoming"):
        await self.b.handle_event({
            "cursor": "1",
            "kind": "message.committed",
            "data": {"messageId": mid, "threadId": tid, "origin": origin, "direction": direction},
        })

    def test_identify_link_source(self):
        from bridge import cards

        xhs = cards.identify_link_source(
            {"url": "https://www.xiaohongshu.com/discovery/item/66f1?xsec_token=abc", "sourceName": None}, 5
        )
        self.assertEqual((xhs.label, xhs.brand, xhs.button_label), ("小红书", "xhs", "📕 打开小红书"))

        xhs_short = cards.identify_link_source({"url": "http://xhslink.com/o/12345"}, 5)
        self.assertEqual((xhs_short.label, xhs_short.brand), ("小红书", "xhs"))

        wxmp = cards.identify_link_source({"url": "https://mp.weixin.qq.com/s/xyz", "sourceName": "量子位"}, 5)
        self.assertEqual((wxmp.label, wxmp.brand, wxmp.button_label), ("量子位", "wxmp", "📄 阅读全文"))

        wxmp_anon = cards.identify_link_source({"url": "https://mp.weixin.qq.com/s/xyz"}, 5)
        self.assertEqual(wxmp_anon.label, "微信公众号")

        bili = cards.identify_link_source({"url": "https://b23.tv/abc"}, 5)
        self.assertEqual((bili.label, bili.brand), ("哔哩哔哩", "bili"))

        zhihu = cards.identify_link_source({"url": "https://zhuanlan.zhihu.com/p/123"}, 5)
        self.assertEqual((zhihu.label, zhihu.brand), ("知乎", "zhihu"))

        music = cards.identify_link_source({"url": "https://example.org/song", "sourceName": "网易云音乐"}, 3)
        self.assertEqual((music.label, music.brand), ("网易云音乐", "music"))

        video = cards.identify_link_source({"url": "https://example.org/watch"}, 4)
        self.assertEqual((video.label, video.brand), ("example.org", "video"))

        generic = cards.identify_link_source({"url": "https://news.ycombinator.com/item?id=1"}, 5)
        self.assertEqual((generic.label, generic.brand, generic.button_label),
                         ("news.ycombinator.com", "link", "🔗 打开链接"))

    def test_render_link_card_and_font_fallback(self):
        import io
        from unittest import mock
        from PIL import Image
        from bridge import cards

        # Without a CJK font, non-ASCII text returns None so caller uses clean HTML text fallback,
        # while pure-ASCII cards can still render via Latin fallback fonts.
        with mock.patch("bridge.cards._font_paths", return_value=(None, None, None, False)):
            self.assertIsNone(cards.render_link_card(
                title="小红书笔记",
                description="摘要内容",
                url="https://www.xiaohongshu.com/discovery/item/1",
            ))
            ascii_png = cards.render_link_card(
                title="GitHub Repository Title",
                description="Short ASCII summary for CI hosts without CJK fonts.",
                url="https://github.com/octocat/Hello-World",
                thumbnail_bytes=_png(120, 120, (30, 30, 30)),
            )
            self.assertIsNotNone(ascii_png)
            im_ascii = Image.open(io.BytesIO(ascii_png))
            self.assertEqual(im_ascii.size[0], cards.CARD_WIDTH)
            self.assertEqual(im_ascii.mode, "RGB")

        # When CJK fonts are installed on the host, render full Xiaohongshu card with thumbnail & emoji.
        if cards._font_paths()[3]:
            full_png = cards.render_link_card(
                title="周末去哪玩🌟 这家咖啡店超好看",
                description="分享一段很长很长的探店笔记描述，用来测试三行省略号截断效果以及右侧圆角缩略图排版是否紧凑清晰。",
                url="https://www.xiaohongshu.com/discovery/item/66f1?xsec_token=tok123",
                source_name=None,
                app_type=5,
                thumbnail_bytes=_png(180, 140, (240, 80, 90)),
            )
            self.assertIsNotNone(full_png)
            im = Image.open(io.BytesIO(full_png))
            self.assertEqual(im.size[0], cards.CARD_WIDTH)
            self.assertTrue(160 <= im.size[1] <= 380, im.size)
            self.assertEqual(im.mode, "RGB")

            # Missing or corrupt thumbnail must still produce a valid card without crashing.
            no_thumb = cards.render_link_card(
                title="无缩略图卡片",
                description="只有正文和来源栏",
                url="https://mp.weixin.qq.com/s/1",
                thumbnail_bytes=b"not-an-image",
            )
            self.assertIsNotNone(no_thumb)

    async def test_download_link_thumbnail(self):
        thumb_ok = _png(120, 120, (255, 60, 80))
        self.daemon.media["m_avail"] = [{
            "id": "th1", "kind": "image", "state": "available",
            "variant": "thumbnail", "actualVariant": "thumbnail",
            "contentType": "image/png", "byteLength": len(thumb_ok),
        }]
        self.daemon.media_bytes["th1"] = (thumb_ok, "image/png")
        got1 = await self.b.wx.download_link_thumbnail({"id": "m_avail"})
        self.assertEqual(got1, thumb_ok)
        self.assertEqual(self.daemon.media_transfers, [])

        # Lazy / uncached thumbnail triggers media-transfers with quality="thumbnail".
        thumb_lazy = _png(100, 100, (50, 180, 90))
        self.daemon.media["m_lazy"] = [{
            "id": "th2", "kind": "image", "state": "not_cached",
            "variant": "thumbnail", "advertisedByteLength": len(thumb_lazy),
            "contentType": "image/png",
        }]
        self.daemon.media_bytes["th2"] = (thumb_lazy, "image/png")
        got2 = await self.b.wx.download_link_thumbnail({"id": "m_lazy"})
        self.assertEqual(got2, thumb_lazy)
        self.assertEqual(
            self.daemon.media_transfers[-1],
            ("m_lazy", {"messageId": "m_lazy", "assetId": "th2", "quality": "thumbnail"}),
        )

        # Missing or non-image media returns None cleanly.
        self.daemon.media["m_empty"] = []
        self.assertIsNone(await self.b.wx.download_link_thumbnail({"id": "m_empty"}))
        self.daemon.media["m_video"] = [{
            "id": "vid1", "kind": "video", "state": "available",
            "variant": "thumbnail", "contentType": "video/mp4", "byteLength": 100,
        }]
        self.assertIsNone(await self.b.wx.download_link_thumbnail({"id": "m_video"}))

    async def test_xiaohongshu_link_card_end_to_end_and_text_mode(self):
        from unittest import mock

        xhs_url = (
            "https://www.xiaohongshu.com/discovery/item/66f1234567890"
            "?app_platform=ios&xsec_token=AB1234567890xyz&xsec_source=pc_feed"
        )
        thumb = _png(150, 150, (255, 36, 66))
        self.daemon.add_thread("wxid_alice", "Alice")
        self.daemon.add_message(
            "xhs1", "wxid_alice", "[Link] 标题", type_=49, appMessageType=5,
            link={
                "title": "#周末探店# [捂脸] 超好吃的店",
                "description": "打卡成功[破涕为笑] 人均50",
                "sourceName": None,
                "url": xhs_url,
            },
        )
        self.daemon.media["xhs1"] = [{
            "id": "xhs-thumb", "kind": "image", "state": "available",
            "variant": "thumbnail", "actualVariant": "thumbnail",
            "contentType": "image/png", "byteLength": len(thumb),
        }]
        self.daemon.media_bytes["xhs-thumb"] = (thumb, "image/png")

        def _btn_url(btn):
            return btn.type.url if hasattr(btn, "type") and hasattr(btn.type, "url") else btn.url

        # 1) Photo card + inline button (hides raw xsec_token URL from bubble text)
        fake_card = _png(800, 220, (255, 255, 255))
        with mock.patch("bridge.cards.render_link_card", return_value=fake_card) as rmock:
            await self.event("xhs1", "wxid_alice")
        rmock.assert_called_once_with(
            title="#周末探店 🤦 超好吃的店",
            description="打卡成功😂 人均50",
            url=xhs_url,
            source_name="",
            app_type=5,
            thumbnail_bytes=thumb,
        )
        sent = self.b.bot.sent[-1]
        self.assertEqual(sent.file.name, "link-card.png")
        self.assertEqual(sent.file.getvalue(), fake_card)
        self.assertIs(sent.kwargs["force_document"], False)
        self.assertIsNone(sent.kwargs["caption"])
        self.assertEqual(sent.kwargs["buttons"][0][0].text, "📕 打开小红书")
        self.assertEqual(_btn_url(sent.kwargs["buttons"][0][0]), xhs_url)

        # 2) Group message preserves sender name as photo caption
        self.daemon.add_thread("123@chatroom", "Friends", kind="group")
        self.daemon.add_contact("wxid_bob", "Bob")
        self.daemon.add_message(
            "xhs2", "123@chatroom", "", type_=49, appMessageType=5, sender="wxid_bob",
            link={"title": "群里分享的笔记", "description": "内容", "sourceName": None, "url": xhs_url},
        )
        self.daemon.media["xhs2"] = []
        with mock.patch("bridge.cards.render_link_card", return_value=fake_card):
            await self.event("xhs2", "123@chatroom")
        grp_sent = self.b.bot.sent[-1]
        self.assertEqual(grp_sent.file.name, "link-card.png")
        self.assertEqual(grp_sent.kwargs["caption"], "<b>Alice in group</b>：")
        self.assertEqual(_btn_url(grp_sent.kwargs["buttons"][0][0]), xhs_url)

        # 3) Text-only mode (link_card_images = False) uses clean HTML + CTA button without raw URL text
        self.b.cfg.bridge.link_card_images = False
        self.daemon.add_message(
            "xhs3", "wxid_alice", "", type_=49, appMessageType=5,
            link={"title": "纯文字卡片测试", "description": "简洁摘要", "sourceName": None, "url": xhs_url},
        )
        await self.event("xhs3", "wxid_alice")
        txt_sent = self.b.bot.sent[-1]
        self.assertIsNone(txt_sent.file)
        self.assertEqual(txt_sent.text, "📕 <b>纯文字卡片测试</b>\n简洁摘要\n<i>来自 小红书</i>")
        self.assertNotIn("xsec_token", txt_sent.text)
        self.assertEqual(txt_sent.kwargs["buttons"][0][0].text, "📕 打开小红书")
        self.assertEqual(_btn_url(txt_sent.kwargs["buttons"][0][0]), xhs_url)

    async def test_link_card_rpc_fallbacks(self):
        from unittest import mock

        def _btn_url(btn):
            return btn.type.url if hasattr(btn, "type") and hasattr(btn.type, "url") else btn.url

        xhs_url = "https://www.xiaohongshu.com/discovery/item/999?xsec_token=t1"
        self.daemon.add_thread("wxid_alice", "Alice")
        self.daemon.media["xhs_btn_err"] = []
        self.daemon.media["xhs_photo_err"] = []
        self.daemon.add_message(
            "xhs_btn_err", "wxid_alice", "", type_=49, appMessageType=5,
            link={"title": "按钮降级测试", "description": "摘要", "url": xhs_url},
        )
        self.daemon.add_message(
            "xhs_photo_err", "wxid_alice", "", type_=49, appMessageType=5,
            link={"title": "图片降级测试", "description": "摘要", "url": xhs_url},
        )
        self.daemon.add_message(
            "xhs_txt_btn_err", "wxid_alice", "", type_=49, appMessageType=5,
            link={"title": "文本按钮降级测试", "description": "摘要", "url": xhs_url},
        )

        # Case 1: send_file rejects inline buttons -> retries photo with HTML link in caption
        orig_send_file = self.b.bot.send_file
        async def reject_buttons_on_file(chat, file, **kw):
            if kw.get("buttons"):
                raise errors.ButtonDataInvalidError(None)
            return await orig_send_file(chat, file, **kw)

        fake_card = _png(800, 200, (255, 255, 255))
        with mock.patch("bridge.cards.render_link_card", return_value=fake_card), \
             mock.patch.object(self.b.bot, "send_file", side_effect=reject_buttons_on_file):
            await self.event("xhs_btn_err", "wxid_alice")
        s1 = self.b.bot.sent[-1]
        self.assertEqual(s1.file.name, "link-card.png")
        self.assertIsNone(s1.kwargs["buttons"])
        self.assertIn(f'<a href="{xhs_url}"><b>📕 打开小红书</b></a>', s1.kwargs["caption"])

        # Case 2: send_file fails completely -> falls back to clean text card + button
        async def fail_send_file(*_a, **_kw):
            raise errors.PhotoInvalidError(None)

        with mock.patch("bridge.cards.render_link_card", return_value=fake_card), \
             mock.patch.object(self.b.bot, "send_file", side_effect=fail_send_file):
            await self.event("xhs_photo_err", "wxid_alice")
        s2 = self.b.bot.sent[-1]
        self.assertIsNone(s2.file)
        self.assertIn("📕 <b>图片降级测试</b>", s2.text)
        self.assertEqual(_btn_url(s2.kwargs["buttons"][0][0]), xhs_url)

        # Case 3: text-mode send_message rejects buttons -> retries text with clickable title link
        self.b.cfg.bridge.link_card_images = False
        orig_send_msg = self.b.bot.send_message
        async def reject_buttons_on_msg(chat, text, **kw):
            if kw.get("buttons"):
                raise errors.ButtonDataInvalidError(None)
            return await orig_send_msg(chat, text, **kw)

        with mock.patch.object(self.b.bot, "send_message", side_effect=reject_buttons_on_msg):
            await self.event("xhs_txt_btn_err", "wxid_alice")
        s3 = self.b.bot.sent[-1]
        self.assertIn(f'<a href="{xhs_url}"><b>文本按钮降级测试</b></a>', s3.text)
        self.assertNotIn("buttons", s3.kwargs)


class _FakeMediaSocket:
    """Stands in for the agent-wechat media socket of one call."""

    def __init__(self):
        self.sent: list[dict] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send_json(self, obj: dict) -> None:
        self.sent.append(obj)

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


class CallWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiohttp
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(WeChatConfig(token="secret-token"), TelegramConfig(api_id=1, api_hash="x", bot_token="123:abc"),
                     BridgeConfig(create_interval_seconds=0, sync_avatar=False,
                                  call_web_url="http://127.0.0.1/wxcall", call_web_listen="127.0.0.1:0"),
                     Path(self.tmp.name) / "config.toml")
        self.daemon = FakeDaemon()
        self.b = Bridge(cfg)
        await self.b.wx.http.aclose()
        self.b.wx.http = httpx.AsyncClient(transport=httpx.MockTransport(self.daemon.handler),
                                           headers={"Authorization": "Bearer secret-token"})
        self.b.user, self.b.bot = FakeUser(), FakeBot()
        self.b.bot_input = object()
        self.b.bot_me = SimpleNamespace(username="mybot")
        self.b.tg_ready.set()
        self.b.owner_id = 999
        await self.b.wx.resolve_account()
        self.sock = _FakeCallsSocket()
        self.b._calls_sock = self.sock
        self.media = _FakeMediaSocket()
        self.media_calls: list[tuple[str, str, str]] = []

        async def call_media(call_id, client_id, owner_token, timeout=10.0):
            self.media_calls.append((call_id, client_id, owner_token))
            return self.media, {"type": "media.ready", "protocolVersion": 2,
                                "sendTrack": {"id": 1, "generation": 1, "direction": "send",
                                              "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}},
                                "receiveTracks": [{"id": 2, "generation": 1, "direction": "receive",
                                                   "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}}]}

        self.b.wx.call_media = call_media
        await self.b.call_web.start()
        self.http = aiohttp.ClientSession()
        self.base = f"http://127.0.0.1:{self.b.call_web.bound_port}/wxcall"
        await self.b.handle_call_event({
            "type": "hello", "protocolVersion": 2, "clientId": "ctl-1", "conferenceInvitations": [],
            "capabilities": {"directAudio": True, "conferenceJoin": True, "conferenceAudio": True},
            "audioFormat": {"sampleRate": 16000, "channels": 1, "frameMs": 20},
        })

    async def asyncTearDown(self):
        await self.http.close()
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def _page(self, token: str, init_data: str = ""):
        ws = await self.http.ws_connect(self.base + "/ws")
        await ws.send_json({"type": "auth", "token": token, "initData": init_data})
        return ws

    async def _next(self, ws, mtype: str = "state", pred=lambda m: True, timeout: float = 2.0) -> dict:
        import aiohttp
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    raise AssertionError("page socket closed")
                continue
            m = json.loads(msg.data)
            if m.get("type") == mtype and pred(m):
                return m

    async def _until(self, cond, timeout: float = 2.0):
        for _ in range(int(timeout / 0.01)):
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition not met")

    def test_verify_init_data(self):
        from bridge.callweb import verify_init_data
        good = _init_data("123:abc", 999)
        self.assertEqual(verify_init_data(good, "123:abc"), 999)
        self.assertIsNone(verify_init_data(good, "123:other"))
        self.assertIsNone(verify_init_data(good.replace("999", "998"), "123:abc"))
        self.assertIsNone(verify_init_data(_init_data("123:abc", 999, auth_date=1000), "123:abc"))
        self.assertIsNone(verify_init_data("", "123:abc"))
        self.assertIsNone(verify_init_data("hash=abc&hash=def", "123:abc"))

    async def test_page_served_under_prefix(self):
        async with self.http.get(self.base + "/call/sometoken") as r:
            self.assertEqual(r.status, 200)
            body = await r.text()
            self.assertEqual(r.headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("registerProcessor('bridge-io'", body)
        self.assertIn('"base": "/wxcall"', body)
        self.assertNotIn("__CFG__", body)
        self.assertNotIn("__WORKLET__", body)

    async def test_answer_pending_call_relay_audio_mute_and_hangup(self):
        import aiohttp
        import base64
        import struct
        self.daemon.add_thread("wxid_alice", "Alice")

        async def on_cmd(cmd):
            if cmd["type"] == "call.ready" and cmd.get("ready"):
                async def offered():
                    await asyncio.sleep(0.01)
                    await self.b.handle_call_event({"type": "call.offered", "offer": {
                        "callId": "call-1", "revision": 3, "kind": "direct", "callType": "voice",
                        "callerId": "wxid_alice"}})
                asyncio.create_task(offered())
            if cmd["type"] == "call.answer":
                async def connected():
                    await asyncio.sleep(0.01)
                    await self.b.handle_call_event({"type": "call.state", "call": {
                        "callId": "call-1", "revision": 4, "kind": "direct", "callType": "voice",
                        "direction": "incoming", "state": "connected", "ownedByThisClient": True}})
                asyncio.create_task(connected())
                return {"type": "command.result", "status": "accepted", "callId": "call-1", "ownerToken": "own-1"}
            return None

        self.sock.on_command = on_cmd
        await self.b.handle_call_event({"type": "call.pending", "offer": {
            "callId": "call-1", "revision": 1, "kind": "direct", "callType": "voice",
            "callerId": "wxid_alice", "phase": "waiting_for_ready"}})
        msg = self.b.bot.sent[-1]
        self.assertEqual(_button_texts(msg), ["📞 接听", "📵 拒接"])
        self.assertEqual([len(row) for row in msg.kwargs["buttons"]], [1, 1])  # stacked, full width
        url = _button_urls(msg)[0]
        self.assertTrue(url.startswith("http://127.0.0.1/wxcall/call/"))
        token = url.rsplit("/", 1)[1]

        # A Mini App opened by somebody else is refused even with the right link
        intruder = await self._page(token, _init_data("123:abc", 111))
        fatal = await self._next(intruder, "fatal")
        self.assertIn("本人", fatal["message"])
        await intruder.close()

        ws = await self._page(token, _init_data("123:abc", 999))
        st = await self._next(ws)
        self.assertEqual((st["phase"], st["name"], st["answerable"]), ("ringing", "Alice", True))

        await ws.send_json({"type": "answer"})
        await ws.send_json({"type": "answer"})  # double tap: ignored without an error on the page
        seen = []
        while True:
            m = json.loads((await asyncio.wait_for(ws.receive(), timeout=2)).data)
            seen.append(m["type"])
            if m["type"] == "state" and m["phase"] == "active":
                st = m
                break
        self.assertNotIn("error", seen)
        self.assertTrue(st["mine"])
        self.assertEqual(st["rate"], 16000)
        self.assertEqual([c["type"] for c in self.sock.commands[:2]], ["call.ready", "call.answer"])
        self.assertEqual(self.sock.commands[0]["scope"], {"kind": "offer", "callId": "call-1", "expectedRevision": 1})
        self.assertEqual(self.sock.commands[1]["expectedRevision"], 3)
        self.assertEqual(self.media_calls, [("call-1", "ctl-1", "own-1")])
        self.assertEqual(self.media.sent[0], {"type": "audio.start", "trackId": 1, "generation": 1})
        self.assertIn("通话中（Telegram 接听）", msg.text)
        self.assertEqual(_button_texts(msg), ["🎧 通话界面", "📴 挂断"])

        # page -> WeChat
        pcm = struct.pack("<4h", 1, -1, 32767, -32768)
        await ws.send_bytes(pcm)
        await self._until(lambda: any(m["type"] == "audio.chunk" for m in self.media.sent))
        chunk = next(m for m in self.media.sent if m["type"] == "audio.chunk")
        self.assertEqual((chunk["trackId"], chunk["generation"]), (1, 1))
        self.assertEqual(base64.b64decode(chunk["pcmBase64"]), pcm)

        # WeChat -> page: track id header + PCM
        self.media.queue.put_nowait({"type": "audio.chunk", "trackId": 2, "generation": 1, "sequence": 1,
                                     "sampleOffset": 0, "pcmBase64": base64.b64encode(b"\x01\x00\x02\x00").decode()})
        got = None
        while got is None:
            m = await asyncio.wait_for(ws.receive(), timeout=2)
            if m.type == aiohttp.WSMsgType.BINARY:
                got = m.data
        self.assertEqual(got, struct.pack("<I", 2) + b"\x01\x00\x02\x00")

        # mute -> call.mute; the daemon then moves the send track to a new generation
        await ws.send_json({"type": "mute", "muted": True})
        st = await self._next(ws, pred=lambda m: m.get("muted"))
        self.assertEqual(self.sock.commands[-1], {"type": "call.mute", "callId": "call-1", "muted": True})
        await self.b.handle_call_event({"type": "track.updated", "callId": "call-1", "track": {
            "id": 1, "generation": 2, "direction": "send",
            "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}}})
        self.assertEqual(self.media.sent[-1], {"type": "audio.start", "trackId": 1, "generation": 2})
        n_chunks = sum(m["type"] == "audio.chunk" for m in self.media.sent)
        await ws.send_bytes(pcm)  # muted: dropped
        await asyncio.sleep(0.05)
        self.assertEqual(sum(m["type"] == "audio.chunk" for m in self.media.sent), n_chunks)

        # hang up from the page
        await ws.send_json({"type": "hangup"})
        await self._until(lambda: self.sock.commands[-1]["type"] == "call.hangup")
        self.assertEqual(self.sock.commands[-1]["expectedRevision"], 4)
        await self._until(lambda: "已在 Telegram 挂断" in msg.text)
        await self.b.handle_call_event({"type": "call.state", "call": {
            "callId": "call-1", "revision": 6, "kind": "direct", "callType": "voice",
            "direction": "incoming", "state": "ended", "ownedByThisClient": True}})
        st = await self._next(ws, pred=lambda m: m["phase"] == "ended")
        self.assertIn("已在 Telegram 挂断", msg.text)
        self.assertIsNone(msg.kwargs["buttons"])
        self.assertTrue(self.media.closed)
        # readiness advertised for the pending offer is withdrawn once the call is over
        self.assertEqual(self.sock.commands[-1]["type"], "call.ready")
        self.assertFalse(self.sock.commands[-1]["ready"])
        self.assertFalse(self.b.call_web.has_session("call-1"))
        await ws.close()

    async def test_unknown_token_video_call_and_answered_elsewhere(self):
        ws = await self._page("nope-not-a-token")
        fatal = await self._next(ws, "fatal")
        self.assertIn("失效", fatal["message"])
        await ws.close()

        self.daemon.add_thread("wxid_bob", "Bob")
        await self.b.handle_call_event({"type": "call.offered", "offer": {
            "callId": "vid-1", "revision": 2, "kind": "direct", "callType": "video", "callerId": "wxid_bob"}})
        self.assertEqual(_button_texts(self.b.bot.sent[-1]), ["📵 拒接"])  # video cannot be answered

        await self.b.handle_call_event({"type": "call.offered", "offer": {
            "callId": "v-2", "revision": 2, "kind": "direct", "callType": "voice", "callerId": "wxid_bob"}})
        msg = self.b.bot.sent[-1]
        token = _button_urls(msg)[0].rsplit("/", 1)[1]
        ws = await self._page(token)  # plain browser: the link alone authorises
        self.assertEqual((await self._next(ws))["phase"], "ringing")
        await self.b.handle_call_event({"type": "call.state", "call": {
            "callId": "v-2", "revision": 3, "kind": "direct", "callType": "voice",
            "direction": "incoming", "state": "connected", "ownedByThisClient": False}})
        st = await self._next(ws, pred=lambda m: m["phase"] != "ringing")
        self.assertEqual(st["phase"], "elsewhere")
        await ws.send_json({"type": "answer"})
        err = await self._next(ws, "error")
        self.assertIn("已在其他设备处理", err["message"])
        self.assertEqual(self.sock.commands, [])
        await ws.close()

    async def test_reject_from_page(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        await self.b.handle_call_event({"type": "call.offered", "offer": {
            "callId": "r-1", "revision": 2, "kind": "direct", "callType": "voice", "callerId": "wxid_bob"}})
        msg = self.b.bot.sent[-1]
        ws = await self._page(_button_urls(msg)[0].rsplit("/", 1)[1])
        await self._next(ws)
        await ws.send_json({"type": "reject"})
        st = await self._next(ws, pred=lambda m: m["phase"] == "ended")
        self.assertIn("已在 Telegram 拒接", st["status"])
        self.assertEqual([c["type"] for c in self.sock.commands], ["call.reject"])
        self.assertIn("已在 Telegram 拒接", msg.text)
        await ws.close()

    async def test_join_group_call_and_auto_hangup_when_page_closes(self):
        import bridge.callweb as callweb
        self.daemon.add_thread("123@chatroom", "Team", kind="group")

        async def on_cmd(cmd):
            if cmd["type"] == "call.join":
                # The daemon reports the new call before the command result reaches us
                await self.b.handle_call_event({"type": "call.state", "call": {
                    "callId": "conf-1", "revision": 2, "kind": "conference", "callType": "voice",
                    "direction": "outgoing", "state": "connected", "ownedByThisClient": True,
                    "groupId": "123@chatroom", "participants": []}})
                return {"type": "command.result", "status": "accepted", "callId": "conf-1", "ownerToken": "own-c"}
            return None

        self.sock.on_command = on_cmd
        await self.b.handle_call_event({"type": "conference.invitations", "invitations": [
            {"invitationId": "inv-1", "revision": 4, "groupId": "123@chatroom", "callerId": "wxid_a"}]})
        msg = self.b.bot.sent[-1]
        self.assertEqual(_button_texts(msg), ["📞 加入"])
        token = _button_urls(msg)[0].rsplit("/", 1)[1]

        ws = await self._page(token)
        st = await self._next(ws)
        self.assertEqual((st["phase"], st["joinable"], st["name"]), ("invite", True, "Team"))
        await ws.send_json({"type": "join"})
        st = await self._next(ws, pred=lambda m: m["phase"] == "active")
        self.assertTrue(st["mine"])
        self.assertEqual([c["type"] for c in self.sock.commands[:2]], ["call.ready", "call.join"])
        self.assertEqual(self.sock.commands[0]["scope"], {"kind": "outgoing"})
        self.assertEqual(self.sock.commands[1]["invitationRevision"], 4)
        self.assertEqual(self.media_calls, [("conf-1", "ctl-1", "own-c")])
        self.assertIn("微信群语音通话", msg.text)
        self.assertIn("通话中（Telegram 接听）", msg.text)
        self.assertEqual(_button_texts(msg), ["🎧 通话界面", "📴 挂断"])

        # The invitation disappears once joined: the message keeps showing the call
        await self.b.handle_call_event({"type": "conference.invitations", "invitations": []})
        self.assertIn("通话中", msg.text)

        # Closing the page hangs up after the grace period
        callweb.DETACH_GRACE_SECONDS, saved = 0.05, callweb.DETACH_GRACE_SECONDS
        try:
            await ws.close()
            await self._until(lambda: self.sock.commands[-1]["type"] == "call.hangup")
        finally:
            callweb.DETACH_GRACE_SECONDS = saved
        self.assertEqual(self.sock.commands[-1]["callId"], "conf-1")
        await self._until(lambda: "自动挂断" in msg.text)

    async def test_reconnecting_page_takes_over_within_grace(self):
        import bridge.callweb as callweb
        self.daemon.add_thread("wxid_alice", "Alice")

        async def on_cmd(cmd):
            if cmd["type"] == "call.answer":
                return {"type": "command.result", "status": "accepted", "callId": "c-9", "ownerToken": "own-9"}
            return None

        self.sock.on_command = on_cmd
        await self.b.handle_call_event({"type": "call.offered", "offer": {
            "callId": "c-9", "revision": 2, "kind": "direct", "callType": "voice", "callerId": "wxid_alice"}})
        token = _button_urls(self.b.bot.sent[-1])[0].rsplit("/", 1)[1]
        ws = await self._page(token)
        await self._next(ws)
        await ws.send_json({"type": "answer"})
        st = await self._next(ws, pred=lambda m: m["phase"] == "connecting")
        self.assertTrue(st["mine"])
        await self.b.handle_call_event({"type": "call.state", "call": {
            "callId": "c-9", "revision": 3, "kind": "direct", "callType": "voice",
            "direction": "incoming", "state": "connected", "ownedByThisClient": True}})
        await self._next(ws, pred=lambda m: m["phase"] == "active")

        callweb.DETACH_GRACE_SECONDS, saved = 0.3, callweb.DETACH_GRACE_SECONDS
        try:
            await ws.close()
            ws2 = await self._page(token)
            st = await self._next(ws2)
            self.assertEqual((st["phase"], st["mine"]), ("active", False))
            await ws2.send_json({"type": "attach"})
            st = await self._next(ws2, pred=lambda m: m["mine"])
            await asyncio.sleep(0.4)
        finally:
            callweb.DETACH_GRACE_SECONDS = saved
        self.assertNotIn("call.hangup", [c["type"] for c in self.sock.commands])
        self.assertTrue(self.b.call_web.has_session("c-9"))
        await ws2.close()

    # ---------------------------------------------------------------- outgoing calls
    def _link_alice(self) -> int:
        self.daemon.add_thread("wxid_alice", "Alice")
        self.b.store.link("wxid_alice", -700, "Alice")
        self.b.wx_ready = True
        return -700

    def _on_dial(self, call_id: str, *states: str):
        async def on_cmd(cmd):
            if cmd["type"] == "call.dial":
                # The daemon reports the new call before the command result reaches us
                for i, state in enumerate(states):
                    await self.b.handle_call_event({"type": "call.state", "call": {
                        "callId": call_id, "revision": i + 1, "kind": "direct", "callType": "voice",
                        "direction": "outgoing", "state": state, "ownedByThisClient": True}})
                return {"type": "command.result", "status": "accepted", "callId": call_id, "ownerToken": "own-" + call_id}
            return None
        self.sock.on_command = on_cmd

    async def _call_card(self, chat: int):
        await self.b.on_tg_message(tg_event(chat, 10, "/call"))
        msg = self.b.bot.sent[-1]
        return msg, _button_urls(msg)[0].rsplit("/", 1)[1]

    async def test_dial_ring_and_cancel_from_page(self):
        chat = self._link_alice()
        self._on_dial("out-1", "dialing")
        msg, token = await self._call_card(chat)
        self.assertIn("微信语音通话", msg.text)
        self.assertIn("呼叫：<b>Alice</b>", msg.text)
        self.assertEqual(_button_texts(msg), ["📞 开始呼叫", "✖️ 取消"])
        self.assertEqual(self.sock.commands, [])  # nothing is dialled until the page asks for it

        ws = await self._page(token, _init_data("123:abc", 999))
        st = await self._next(ws)
        self.assertEqual((st["phase"], st["dialable"], st["name"], st["title"]), ("dial", True, "Alice", "微信语音通话"))
        await ws.send_json({"type": "dial"})
        await ws.send_json({"type": "dial"})  # double tap: dialled once
        st = await self._next(ws, pred=lambda m: m["phase"] == "calling")
        self.assertTrue(st["mine"])
        self.assertEqual([c["type"] for c in self.sock.commands], ["call.ready", "call.dial"])
        self.assertEqual(self.sock.commands[0]["scope"], {"kind": "outgoing"})
        self.assertEqual(self.sock.commands[1]["target"], {"kind": "direct", "contactId": "wxid_alice"})
        self.assertEqual(self.sock.commands[1]["callType"], "voice")
        await self._until(lambda: "正在呼叫" in msg.text)
        self.assertEqual(_button_texts(msg), ["🎧 通话界面", "📴 取消呼叫"])
        self.assertEqual(_button_urls(msg)[0].rsplit("/", 1)[1], token)  # same link keeps working

        await self.b.handle_call_event({"type": "call.state", "call": {
            "callId": "out-1", "revision": 2, "kind": "direct", "callType": "voice",
            "direction": "outgoing", "state": "ringing", "ownedByThisClient": True}})
        self.assertIn("对方响铃中", msg.text)

        await ws.send_json({"type": "hangup"})
        await self._until(lambda: self.sock.commands[-1]["type"] == "call.cancel")
        self.assertEqual(self.sock.commands[-1]["expectedRevision"], 2)
        await self._until(lambda: "已在 Telegram 取消呼叫" in msg.text)
        await self.b.handle_call_event({"type": "call.state", "call": {
            "callId": "out-1", "revision": 3, "kind": "direct", "callType": "voice",
            "direction": "outgoing", "state": "ended", "ownedByThisClient": True}})
        await self._next(ws, pred=lambda m: m["phase"] == "ended")
        self.assertIn("已在 Telegram 取消呼叫", msg.text)
        self.assertIsNone(msg.kwargs["buttons"])
        self.assertEqual(self.media_calls, [])
        self.assertFalse(self.sock.commands[-1]["ready"])  # readiness withdrawn
        await ws.close()

    async def test_dial_connects_relays_audio_and_hangs_up(self):
        chat = self._link_alice()
        self._on_dial("out-2", "dialing", "ringing")
        msg, token = await self._call_card(chat)
        ws = await self._page(token)
        await self._next(ws)
        await ws.send_json({"type": "dial"})
        await self._next(ws, pred=lambda m: m["phase"] == "calling")
        await self._until(lambda: "对方响铃中" in msg.text)  # state that arrived before the command result

        await self.b.handle_call_event({"type": "call.state", "call": {
            "callId": "out-2", "revision": 3, "kind": "direct", "callType": "voice",
            "direction": "outgoing", "state": "connected", "ownedByThisClient": True}})
        st = await self._next(ws, pred=lambda m: m["phase"] == "active")
        self.assertTrue(st["mine"])
        self.assertEqual(self.media_calls, [("out-2", "ctl-1", "own-out-2")])
        self.assertIn("通话中（Telegram 发起）", msg.text)
        self.assertEqual(_button_texts(msg), ["🎧 通话界面", "📴 挂断"])

        await self.b.on_bot_callback(_CallbackEvent(999, f"callhup:{self.b._active_calls['out-2']['token']}".encode()))
        self.assertEqual(self.sock.commands[-1]["type"], "call.hangup")
        self.assertIn("已在 Telegram 挂断", msg.text)
        await ws.close()

    async def test_dial_unanswered_busy_cancel_and_expiry(self):
        import bridge.callweb as callweb
        chat = self._link_alice()

        # The other side never picks up
        self._on_dial("out-3", "dialing", "ringing", "ended")
        msg, token = await self._call_card(chat)
        ws = await self._page(token)
        await self._next(ws)
        await ws.send_json({"type": "dial"})
        st = await self._next(ws, pred=lambda m: m["phase"] == "ended")
        self.assertIn("呼叫未接通", st["status"])
        await self._until(lambda: msg.kwargs["buttons"] is None)
        self.assertIn("呼叫未接通", msg.text)
        await ws.close()

        # Busy: the error is shown and the card stays usable
        async def busy(cmd):
            if cmd["type"] == "call.dial":
                return {"type": "command.result", "status": "error", "code": "busy"}
            return None
        self.sock.on_command = busy
        self.sock.commands.clear()
        msg, token = await self._call_card(chat)
        ws = await self._page(token)
        await self._next(ws)
        await ws.send_json({"type": "dial"})
        err = await self._next(ws, "error")
        self.assertIn("其他未结束的通话", err["message"])
        self.assertEqual([c["type"] for c in self.sock.commands], ["call.ready", "call.dial", "call.ready"])
        self.assertFalse(self.sock.commands[-1]["ready"])
        self.assertEqual(_button_texts(msg), ["📞 开始呼叫", "✖️ 取消"])

        # Cancel button on the card: only the owner, then the page shows it ended
        data = next(d for row in msg.kwargs["buttons"] for b in row
                    if (d := getattr(getattr(b, "type", None), "data", None) or getattr(b, "data", None)))
        self.assertTrue(data.startswith(b"dialx:"))
        intruder = _CallbackEvent(111, data)
        await self.b.on_bot_callback(intruder)
        self.assertEqual(intruder.answers, [("只有桥接账号本人可以操作", True)])
        await self.b.on_bot_callback(_CallbackEvent(999, data))
        self.assertIn("已取消", msg.text)
        self.assertIsNone(msg.kwargs["buttons"])
        st = await self._next(ws, pred=lambda m: m["phase"] == "ended")
        self.assertIn("已取消", st["status"])
        await ws.send_json({"type": "dial"})
        err = await self._next(ws, "error")
        self.assertIn("已取消或已过期", err["message"])
        await ws.close()

        # A new /call retires the previous live card; an unused card expires
        callweb.DIAL_REQUEST_TTL, saved = 0.5, callweb.DIAL_REQUEST_TTL
        try:
            first, _ = await self._call_card(chat)
            second, _ = await self._call_card(chat)
            self.assertIn("此链接已失效", first.text)
            self.assertIsNone(first.kwargs["buttons"])
            await self._until(lambda: "已过期" in second.text)
            self.assertIsNone(second.kwargs["buttons"])
        finally:
            callweb.DIAL_REQUEST_TTL = saved

    def _team(self) -> int:
        self.b._calls_caps = {**self.b._calls_caps, "conferenceCreate": True}
        self.b.wx_ready = True
        self.daemon.add_thread("123@chatroom", "Team", kind="group")
        self.b.store.link("123@chatroom", -900, "Team")
        self.daemon.group_members["123@chatroom"] = (
            [{"id": "wxid_me", "displayName": "Me"},
             {"id": "wxid_a", "displayName": "Alice"},
             {"id": "wxid_b", "displayName": "Bob", "alias": "bobby"},
             {"id": "wxid_c", "displayName": "Carol"}]
            + [{"id": f"wxid_m{i:02}", "displayName": f"Member {i:02}"} for i in range(1, 12)])
        return -900

    async def _press(self, msg, label: str, user: int = 999):
        data = next(d for row in msg.kwargs["buttons"] for b in row if label in b.text
                    and (d := getattr(getattr(b, "type", None), "data", None) or getattr(b, "data", None)))
        ev = _CallbackEvent(user, data)
        await self.b.on_bot_callback(ev)
        return ev

    async def test_group_dial_member_picker_and_call(self):
        chat = self._team()
        # Our own WeChat ID is learned from a message we sent from the phone, and left out of the picker
        self.daemon.add_message("o1", "123@chatroom", "hi", direction="outgoing", sender="wxid_me")
        await self.b.handle_event({"cursor": "1", "kind": "message.committed", "data": {
            "messageId": "o1", "threadId": "123@chatroom", "origin": "live", "direction": "outgoing"}})
        self.assertEqual(self.b._self_wxid, "wxid_me")
        self.assertEqual(self.b.store.get("self_wxid"), "wxid_me")

        await self.b.on_tg_message(tg_event(chat, 10, "/call bobby"))
        msg = self.b.bot.sent[-1]
        self.assertIn("微信群语音通话", msg.text)
        self.assertIn("群：<b>Team</b>", msg.text)
        self.assertIn("邀请（1 人）：Bob", msg.text)
        texts = _button_texts(msg)
        self.assertEqual(texts[:2], ["✅ Bob", "▫️ Alice"])  # matches first, ticked
        self.assertNotIn("▫️ Me", texts)
        self.assertIn("1/2", texts)
        self.assertIn("📞 开始呼叫（1 人）", texts)
        token = _button_urls(msg)[0].rsplit("/", 1)[1]

        intruder = await self._press(msg, "Alice", user=111)
        self.assertEqual(intruder.answers, [("只有桥接账号本人可以操作", True)])
        await self._press(msg, "Alice")
        self.assertIn("邀请（2 人）：Bob、Alice", msg.text)
        await self._press(msg, "▶️")
        self.assertIn("2/2", _button_texts(msg))
        self.assertIn("▫️ Member 11", _button_texts(msg))
        await self._press(msg, "◀️")

        ws = await self._page(token)
        st = await self._next(ws)
        self.assertEqual((st["phase"], st["group"], st["dialable"], st["status"], st["title"]),
                         ("dial", True, True, "将邀请 2 人", "微信群语音通话"))
        await self._press(msg, "清空")
        self.assertNotIn("开始呼叫", " ".join(_button_texts(msg)))
        st = await self._next(ws, pred=lambda m: not m["dialable"])
        await self._press(msg, "全选")
        self.assertIn("邀请（14 人）", msg.text)
        await self._press(msg, "清空")
        await self._press(msg, "Bob")
        await self._press(msg, "Alice")
        st = await self._next(ws, pred=lambda m: m["status"] == "将邀请 2 人")

        async def on_cmd(cmd):
            if cmd["type"] == "call.dial":
                await self.b.handle_call_event({"type": "call.invitation_result", "callId": "conf-o",
                                                "requestId": cmd.get("requestId", "r"), "results": [
                    {"contactId": "wxid_b", "status": "submitted", "verificationRequired": False},
                    {"contactId": "wxid_a", "status": "rejected", "verificationRequired": False}]})
                await self.b.handle_call_event({"type": "call.state", "call": {
                    "callId": "conf-o", "revision": 2, "kind": "conference", "callType": "voice",
                    "direction": "outgoing", "state": "connected", "ownedByThisClient": True,
                    "groupId": "123@chatroom", "participants": [
                        {"participantId": "p1", "contactId": "wxid_b", "membership": "joined"},
                        {"participantId": "p2", "contactId": "wxid_a", "membership": "invited"}]}})
                return {"type": "command.result", "status": "accepted", "callId": "conf-o", "ownerToken": "own-c"}
            return None
        self.sock.on_command = on_cmd
        await ws.send_json({"type": "dial"})
        st = await self._next(ws, pred=lambda m: m["phase"] == "active")
        self.assertTrue(st["mine"])
        self.assertTrue(st["group"])
        dial = next(c for c in self.sock.commands if c["type"] == "call.dial")
        self.assertEqual(dial["target"], {"kind": "conference", "groupId": "123@chatroom",
                                          "invitees": ["wxid_b", "wxid_a"]})
        self.assertEqual(self.media_calls, [("conf-o", "ctl-1", "own-c")])
        await self._until(lambda: "群通话中（Telegram 发起）" in msg.text)
        self.assertIn("邀请（2 人）：Bob、Alice", msg.text)
        self.assertIn("已加入：1 人", msg.text)
        self.assertIn("邀请异常：Alice（被拒绝）", msg.text)
        self.assertEqual(_button_texts(msg), ["🎧 通话界面", "📴 挂断"])

        await ws.send_json({"type": "hangup"})
        await self._until(lambda: self.sock.commands[-1]["type"] == "call.hangup")  # leave, not cancel
        await ws.close()

    async def test_group_call_invites_more_members(self):
        chat = self._team()
        self.b._calls_caps = {**self.b._calls_caps, "conferenceInvite": True}
        self.b._self_wxid = "wxid_me"
        await self.b.on_tg_message(tg_event(chat, 10, "/call bobby"))
        card = self.b.bot.sent[-1]
        token = _button_urls(card)[0].rsplit("/", 1)[1]
        ws = await self._page(token)
        await self._next(ws)

        def state(rev, st, participants):
            return {"type": "call.state", "call": {
                "callId": "conf-i", "revision": rev, "kind": "conference", "callType": "voice",
                "direction": "outgoing", "state": st, "ownedByThisClient": True, "groupId": "123@chatroom",
                "participants": participants}}

        async def on_cmd(cmd):
            if cmd["type"] == "call.dial":
                await self.b.handle_call_event(state(1, "connected", [
                    {"participantId": "p1", "contactId": "wxid_b", "membership": "joined"}]))
                return {"type": "command.result", "status": "accepted", "callId": "conf-i", "ownerToken": "own-i"}
            if cmd["type"] == "call.invite":
                return {"type": "command.result", "status": "accepted", "callId": "conf-i"}
            return None
        self.sock.on_command = on_cmd
        await ws.send_json({"type": "dial"})
        await self._next(ws, pred=lambda m: m["phase"] == "active")
        await self._until(lambda: "群通话中（Telegram 发起）" in card.text)
        self.assertEqual(_button_texts(card), ["🎧 通话界面", "➕ 邀请更多人", "📴 挂断"])

        intruder = await self._press(card, "邀请更多人", user=111)
        self.assertEqual(intruder.answers, [("只有桥接账号本人可以操作", True)])
        await self._press(card, "邀请更多人")
        picker = self.b.bot.sent[-1]
        self.assertIsNot(picker, card)
        self.assertEqual(picker.kwargs.get("reply_to"), card.id)
        self.assertIn("邀请加入群语音通话", picker.text)
        self.assertIn("邀请：尚未选择", picker.text)
        texts = _button_texts(picker)
        self.assertNotIn("▫️ Bob", texts)  # already in the call
        self.assertNotIn("▫️ Me", texts)
        self.assertIn("▫️ Alice", texts)
        self.assertNotIn("发送邀请", " ".join(texts))
        await self._press(picker, "Alice")
        await self._press(picker, "Carol")
        self.assertIn("➕ 发送邀请（2 人）", _button_texts(picker))

        await self._press(picker, "发送邀请")
        invite = self.sock.commands[-1]
        self.assertEqual(invite, {**invite, "type": "call.invite", "callId": "conf-i",
                                  "contactIds": ["wxid_a", "wxid_c"]})
        self.assertIn("已邀请 2 人", picker.text)
        self.assertIsNone(picker.kwargs["buttons"])
        self.assertIn("邀请（3 人）：Bob、Alice、Carol", card.text)

        await self.b.handle_call_event({"type": "call.invitation_result", "callId": "conf-i", "results": [
            {"contactId": "wxid_a", "status": "submitted", "verificationRequired": False},
            {"contactId": "wxid_c", "status": "submitted", "verificationRequired": True}]})
        self.assertIn("邀请异常：Carol（需要验证）", card.text)

        # A pending invitation is left out of the next list; a failed one can be retried
        await self._press(card, "邀请更多人")
        picker = self.b.bot.sent[-1]
        texts = _button_texts(picker)
        self.assertNotIn("▫️ Alice", texts)
        self.assertIn("▫️ Carol", texts)

        async def refuse(cmd):
            if cmd["type"] == "call.invite":
                return {"type": "command.result", "status": "error", "code": "not_owner"}
            return None
        self.sock.on_command = refuse
        await self._press(picker, "Carol")
        await self._press(picker, "发送邀请")
        self.assertIn("邀请失败", self.b.bot.sent[-1].text)
        self.assertIn("➕ 发送邀请（1 人）", _button_texts(picker))  # still usable

        # The call ends: the open list closes and the card loses its buttons
        await self.b.handle_call_event(state(2, "ended", []))
        await self._until(lambda: picker.kwargs["buttons"] is None)
        self.assertIn("群通话已结束", picker.text)
        await self._until(lambda: card.kwargs["buttons"] is None)
        await ws.close()

    async def test_group_dial_no_match_and_empty(self):
        chat = self._team()
        await self.b.on_tg_message(tg_event(chat, 10, "/call nobody"))
        self.assertIn("没有匹配「nobody」的群成员", self.b.bot.sent[-1].text)
        await self.b.on_tg_message(tg_event(chat, 11, "/call"))
        msg = self.b.bot.sent[-1]
        self.assertIn("邀请：尚未选择", msg.text)
        self.assertNotIn("开始呼叫", " ".join(_button_texts(msg)))
        self.daemon.group_members["123@chatroom"] = []
        await self.b.on_tg_message(tg_event(chat, 12, "/call"))
        self.assertIn("没有拿到群成员", self.b.bot.sent[-1].text)

    async def test_call_command_refused_when_unavailable(self):
        chat = self._link_alice()
        self.daemon.add_thread("123@chatroom", "Team", kind="group")
        self.b.store.link("123@chatroom", -701, "Team")
        await self.b.on_tg_message(tg_event(-701, 10, "/call"))
        self.assertIn("不支持发起微信群语音通话", self.b.bot.sent[-1].text)  # no conferenceCreate

        self.daemon.add_thread("filehelper", "文件传输助手")
        self.b.store.link("filehelper", -702, "文件传输助手")
        await self.b.on_tg_message(tg_event(-702, 10, "/call"))
        self.assertIn("文件传输助手和公众号不能接听微信电话", self.b.bot.sent[-1].text)

        self.b._calls_caps = {}
        await self.b.on_tg_message(tg_event(chat, 11, "/call"))
        self.assertIn("不支持发起微信语音通话", self.b.bot.sent[-1].text)

        self.b._calls_caps = {"directAudio": True}
        self.b._calls_sock = None
        await self.b.on_tg_message(tg_event(chat, 12, "/call"))
        self.assertIn("通话信令未连接", self.b.bot.sent[-1].text)

        self.b._calls_sock = self.sock
        self.b.wx_ready = False
        await self.b.on_tg_message(tg_event(chat, 13, "/call"))
        self.assertIn("微信尚未登录", self.b.bot.sent[-1].text)
        self.assertEqual(self.sock.commands, [])
        self.assertEqual(self.b._dial_requests, {})


class CallMediaSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_media_attach_and_audio_roundtrip(self):
        import base64
        from aiohttp import WSMsgType, web
        from bridge.wechat import WeChatError
        seen: list[dict] = []
        auth: list[str] = []

        async def media(request):
            auth.append(request.headers.get("Authorization", ""))
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            attach = await ws.receive_json()
            seen.append(attach)
            if attach.get("ownerToken") != "own-1":
                await ws.send_json({"type": "call.error", "code": "not_owner"})
                await ws.close()
                return ws
            await ws.send_json({"type": "media.ready", "protocolVersion": 2,
                                "sendTrack": {"id": 1, "generation": 1, "direction": "send",
                                              "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}},
                                "receiveTracks": []})
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                m = json.loads(msg.data)
                seen.append(m)
                if m["type"] == "audio.chunk":
                    await ws.send_json({"type": "audio.chunk", "trackId": 2, "generation": 1, "sequence": 0,
                                        "sampleOffset": 0, "pcmBase64": m["pcmBase64"]})
                    await ws.close()
            return ws

        app = web.Application()
        app.router.add_get("/v1/accounts/acc1/calls/{call}/media", media)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        wx = WeChatClient(f"http://127.0.0.1:{port}", "tok", "acc1")
        try:
            sock, ready = await wx.call_media("c1", "ctl-1", "own-1")
            self.assertEqual(ready["sendTrack"]["id"], 1)
            await sock.send_json({"type": "audio.start", "trackId": 1, "generation": 1})
            pcm = bytes(range(256)) * 5  # > 125 bytes: exercises the 16-bit length + masking paths
            await sock.send_json({"type": "audio.chunk", "trackId": 1, "generation": 1,
                                  "pcmBase64": base64.b64encode(pcm).decode()})
            events = [ev async for ev in sock.events()]
            await sock.close()
            self.assertEqual(auth[0], "Bearer tok")
            self.assertEqual(seen[0], {"type": "media.attach", "supportedProtocolVersions": [2],
                                       "clientId": "ctl-1", "ownerToken": "own-1"})
            self.assertEqual([m["type"] for m in seen[1:]], ["audio.start", "audio.chunk"])
            self.assertEqual(base64.b64decode(events[0]["pcmBase64"]), pcm)

            with self.assertRaises(WeChatError) as cm:
                await wx.call_media("c1", "ctl-1", "wrong")
            self.assertEqual(cm.exception.code, "not_owner")
        finally:
            await wx.close()
            await runner.cleanup()


class MediaStreamingTests(unittest.IsolatedAsyncioTestCase):
    """Downloads stream from the daemon into memory or a temp file and resume with `Range`."""

    PAYLOAD = bytes(range(256)) * 160  # 40 KiB

    def client(self, handler) -> WeChatClient:
        wx = WeChatClient("http://localhost:6174", "secret-token", ACC, read_retry_delays=(0.0, 0.0))
        wx.http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                    headers={"Authorization": "Bearer secret-token"})
        return wx

    @staticmethod
    def assets(byte_length=None) -> dict:
        a = {"id": "a1", "kind": "file", "variant": "original", "state": "available",
             "contentType": "application/pdf", "filename": "report.pdf"}
        if byte_length is not None:
            a["byteLength"] = byte_length
        return {"items": [a]}

    @staticmethod
    def broken_body(data: bytes, cut: int):
        async def gen():
            yield data[:cut]
            raise httpx.ReadError("connection reset")
        return gen()

    async def download(self, wx, max_bytes, spill=10_000):
        from unittest.mock import patch
        msg = {"id": "m1", "type": 49}
        with patch("bridge.wechat.MEDIA_SPILL_BYTES", spill):
            return await wx.download_media(msg, max_bytes)

    async def test_resumes_with_range_after_dropped_connection(self):
        import os
        ranges: list[str | None] = []
        data = self.PAYLOAD

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/messages/m1/media"):
                return httpx.Response(200, json=self.assets(len(data)))
            ranges.append(request.headers.get("range"))
            if len(ranges) == 1:
                return httpx.Response(200, headers={"content-type": "application/pdf"},
                                      content=self.broken_body(data, 15_000))
            start = int(request.headers["range"].removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, headers={"content-type": "application/pdf",
                                                "content-range": f"bytes {start}-{len(data) - 1}/{len(data)}"},
                                  content=data[start:])

        wx = self.client(handler)
        try:
            media = await self.download(wx, 1 << 20)
        finally:
            await wx.close()
        self.assertEqual(ranges, [None, "bytes=15000-"])
        self.assertEqual(media.data, b"")  # larger than the spill size: kept on disk
        self.assertEqual(os.path.basename(media.path), "report.pdf")
        self.assertEqual((media.read(), media.size, media.mime), (data, len(data), "application/pdf"))
        folder = os.path.dirname(media.path)
        media.close()
        self.assertFalse(os.path.exists(folder))
        self.assertIsNone(media.path)

    async def test_restarts_when_daemon_ignores_range(self):
        calls = 0
        data = self.PAYLOAD

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            if request.url.path.endswith("/messages/m1/media"):
                return httpx.Response(200, json=self.assets(len(data)))
            calls += 1
            if calls == 1:
                return httpx.Response(200, content=self.broken_body(data, 15_000))
            return httpx.Response(200, content=data)  # an older daemon answers 200 with the whole file

        wx = self.client(handler)
        try:
            media = await self.download(wx, 1 << 20)
        finally:
            await wx.close()
        try:
            self.assertEqual(calls, 2)
            self.assertEqual(media.read(), data)
        finally:
            media.close()

    async def test_restarts_when_partial_response_starts_elsewhere(self):
        ranges: list[str | None] = []
        encodings: list[str | None] = []
        data = self.PAYLOAD

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/messages/m1/media"):
                return httpx.Response(200, json=self.assets(len(data)))
            ranges.append(request.headers.get("range"))
            encodings.append(request.headers.get("accept-encoding"))
            if len(ranges) == 1:
                return httpx.Response(200, content=self.broken_body(data, 15_000))
            if len(ranges) == 2:  # wrong piece: must not be appended after the first 15000 bytes
                return httpx.Response(206, headers={"content-range": f"bytes 0-{len(data) - 1}/{len(data)}"},
                                      content=data)
            return httpx.Response(200, content=data)

        wx = self.client(handler)
        try:
            media = await self.download(wx, 1 << 20)
        finally:
            await wx.close()
        try:
            self.assertEqual(ranges, [None, "bytes=15000-", None])
            self.assertEqual(set(encodings), {"identity"})
            self.assertEqual(media.read(), data)
        finally:
            media.close()

    async def test_small_download_stays_in_memory(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/messages/m1/media"):
                return httpx.Response(200, json=self.assets(5))
            self.assertNotIn("range", request.headers)
            return httpx.Response(200, content=b"%PDF-")

        wx = self.client(handler)
        try:
            media = await self.download(wx, 1 << 20)
        finally:
            await wx.close()
        self.assertIsNone(media.path)
        self.assertEqual((media.data, media.read()), (b"%PDF-", b"%PDF-"))
        f = media.tg_file()
        self.assertEqual((f.name, f.read()), ("report.pdf", b"%PDF-"))

    async def test_stream_larger_than_limit_is_aborted_and_cleaned_up(self):
        import os
        import tempfile as tf
        from unittest.mock import patch
        made: list[str] = []
        real_mkdtemp = tf.mkdtemp

        def mkdtemp(*a, **kw):
            made.append(real_mkdtemp(*a, **kw))
            return made[-1]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/messages/m1/media"):
                return httpx.Response(200, json=self.assets())  # size not advertised

            async def pieces():
                for i in range(0, len(self.PAYLOAD), 4096):
                    yield self.PAYLOAD[i:i + 4096]
            return httpx.Response(200, content=pieces())

        wx = self.client(handler)
        try:
            with patch("bridge.wechat.tempfile.mkdtemp", mkdtemp):
                res = await self.download(wx, 20_000, spill=1_000)
        finally:
            await wx.close()
        self.assertEqual(res, "文件太大")
        self.assertTrue(made)
        self.assertFalse(any(os.path.exists(d) for d in made))

    async def test_gives_up_after_retries_and_cleans_up(self):
        import os
        import tempfile as tf
        from unittest.mock import patch
        made: list[str] = []
        real_mkdtemp = tf.mkdtemp

        def mkdtemp(*a, **kw):
            made.append(real_mkdtemp(*a, **kw))
            return made[-1]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/messages/m1/media"):
                return httpx.Response(200, json=self.assets(len(self.PAYLOAD)))
            start = int((request.headers.get("range") or "bytes=0-").removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206 if start else 200,
                                  content=self.broken_body(self.PAYLOAD[start:], 5_000))

        wx = self.client(handler)
        try:
            with patch("bridge.wechat.tempfile.mkdtemp", mkdtemp):
                with self.assertRaises(httpx.ReadError):
                    await self.download(wx, 1 << 20, spill=1_000)
        finally:
            await wx.close()
        self.assertTrue(made)
        self.assertFalse(any(os.path.exists(d) for d in made))


class SafeFilenameTests(unittest.TestCase):
    def test_strips_paths_and_unsafe_characters(self):
        from bridge.wechat import _safe_filename
        self.assertEqual(_safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(_safe_filename(r"C:\Users\x\a:b?.mp4"), "ab.mp4")
        self.assertEqual(_safe_filename("-rf.mp4"), "rf.mp4")
        self.assertEqual(_safe_filename(""), "file.bin")
        self.assertEqual(_safe_filename("download.part"), "file.bin")
        long = _safe_filename("视" * 100 + ".mp4")
        self.assertTrue(long.endswith(".mp4"))
        self.assertLessEqual(len(long.encode("utf-8")), 200)
