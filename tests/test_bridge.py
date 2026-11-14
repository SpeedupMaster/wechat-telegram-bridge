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


class FakeDaemon:
    def __init__(self):
        self.messages: dict[str, dict] = {}
        self.threads: dict[str, dict] = {}
        self.contacts: dict[str, dict] = {}
        self.sends: list[tuple[dict, str]] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.sticker_preparations: list[tuple[dict, str]] = []
        self.stickers_supported = True
        self.sse_body = ""
        self.marked_read: list[str] = []
        self.avatar_refreshed: list[str] = []
        self.avatar_op = {"id": "avop", "state": "succeeded"}
        self.avatar_lag = 0  # reads after a successful refresh that still miss (profile reader lag)
        self.media: dict[str, list[dict]] = {}  # message id -> media assets (default: one available PNG)
        self.media_bytes: dict[str, tuple[bytes, str]] = {}  # asset id -> (bytes, content_type)
        self.media_transfers: list[tuple[str, dict]] = []
        self.transfer_fail_variants: set[str] = set()
        self.transfer_jobs: dict[str, dict] = {}
        self.reject_image_quality = False
        self.group_members: dict[str, list[dict]] = {}
        self.recalls: list[tuple[str, dict, str]] = []
        self.recall_error: tuple[int, str] | None = None
        self.recall_op = {"id": "rcop", "state": "succeeded"}

    def add_thread(self, tid, name, kind="direct"):
        self.threads[tid] = {"id": tid, "accountId": ACC, "peerId": tid, "kind": kind, "displayName": name,
                             "unreadCount": 1, "readStateVersion": "v1"}

    def add_contact(self, cid, name):
        self.contacts[cid] = {"id": cid, "displayName": name}

    def add_message(self, mid, tid, body="hi", type_=1, direction="incoming", sender=None, server=None, **extra):
        self.messages[mid] = {"id": mid, "accountId": ACC, "threadId": tid, "serverId": server, "type": type_,
                              "senderId": sender or tid, "direction": direction, "body": body, **extra}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-token"
        path = request.url.path.removeprefix("/v1/")
        parts = path.split("/")
        if path == "accounts":
            return httpx.Response(200, json={"items": [{"id": ACC, "alias": "me"}]})
        assert parts[:2] == ["accounts", ACC], path
        rest = parts[2:]
        m = request.method
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
            if q in self.transfer_fail_variants:
                self.transfer_jobs[jid] = {"id": jid, "state": "failed", "reason": "cdn_failed"}
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
        if rest[0] == "messages" and rest[2] == "media" and rest[1] in self.media:
            return httpx.Response(200, json={"items": self.media[rest[1]]})
        if rest[0] == "messages" and rest[2] == "media":
            return httpx.Response(200, json={"items": [{"id": "asset-" + rest[1], "state": "available",
                                                        "variant": "original", "actualVariant": "original",
                                                        "contentType": "image/png", "byteLength": 4,
                                                        "filename": "pic.png"}]})
        if rest[0] == "media" and rest[2] == "content":
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
            members = self.group_members.get(rest[1], [])
            cur = request.url.params.get("cursor")
            if cur is None and len(members) > 2:
                return httpx.Response(200, json={"items": members[:2], "nextCursor": "p2"})
            if cur == "p2":
                return httpx.Response(200, json={"items": members[2:], "nextCursor": None})
            return httpx.Response(200, json={"items": members, "nextCursor": None})
        if rest[0] == "groups" and rest[2] == "members":
            return httpx.Response(200, json={"state": "present", "member": {"displayName": "Alice in group"}})
        if rest[0] == "contacts" and len(rest) == 1:
            q = (request.url.params.get("search") or "").lower()
            pool = {**{k: v for k, v in self.threads.items() if v.get("kind") != "group"}, **self.contacts}
            items = [c for c in pool.values() if not q or q in (c.get("displayName") or "").lower() or q in c["id"].lower()]
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

        ch1_text, ch2_text, lk1_text, q1_text = [s.text for s in self.b.bot.sent[-4:]]
        self.assertEqual(ch1_text, "🎬 <b>视频号 · 老抽搞笑动画</b>\n视频文案 &lt;tag&gt;")
        self.assertEqual(ch2_text, "🎬 <b>视频号</b>\n仅正文")
        self.assertIn("🔗 <b>文章标题</b>", lk1_text)
        self.assertIn("https://example.com", lk1_text)
        self.assertEqual(q1_text, "<blockquote>Bob：引用的视频文案</blockquote>好看")

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
        await self.b.on_tg_message(tg_event(chat, 20, "delete me"))
        self.daemon.add_message("wx_local_20", "wxid_alice", "delete me", direction="outgoing", server="902")
        await self.event("wx_local_20", "wxid_alice")
        await self.b.on_tg_delete(SimpleNamespace(chat_id=None, deleted_ids=[20]))
        self.assertEqual(self.daemon.recalls[-1][0], "wx_local_20")
        self.assertEqual(self.daemon.recalls[-1][1], {"serverId": "902"})
        self.assertIn("↩️ 已从微信撤回", self.b.bot.sent[-1].text)

        # Deleting an incoming message (not in `sent`) must NOT call WeChat recall
        recalls_before = len(self.daemon.recalls)
        bot_msg_id = self.b.bot.sent[0].id
        self.b.store.map_message(chat, "user", 99, "wxid_alice", "m0", "100")
        await self.b.on_tg_delete(SimpleNamespace(chat_id=chat, deleted_ids=[99, bot_msg_id]))
        self.assertEqual(len(self.daemon.recalls), recalls_before)

        # Recall failure reports error cleanly
        self.daemon.recall_error = (409, "recall_window_expired")
        await self.b.on_tg_message(tg_event(chat, 21, "/recall", reply_to=20))
        self.assertIn("recall_window_expired", self.b.bot.sent[-1].text)

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

        # Static WebP -> <=240px transparent GIF
        kind, gif = await self.stickers.to_wechat_sticker(self.webp(), "image/webp")
        self.assertEqual(kind, "sticker")
        self.assertLessEqual(len(gif), self.stickers.WX_STICKER_MAX_BYTES)
        im = Image.open(io.BytesIO(gif))
        self.assertEqual((im.format, im.size), ("GIF", (240, 240)))
        rgba = im.convert("RGBA")
        self.assertEqual(rgba.getpixel((5, 5))[3], 0)
        self.assertColor(rgba.getpixel((120, 120))[:3], (255, 0, 0))

        # Animated .tgs -> multi-frame transparent GIF (both with and without ffmpeg)
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
            self.assertEqual(gim.size, (240, 240))
            self.assertEqual(gim.convert("RGBA").getpixel((5, 5))[3], 0)
            self.assertColor(gim.convert("RGBA").getpixel((120, 120))[:3], (255, 0, 0))

        # Video .webm (VP9 with alpha) -> multi-frame transparent GIF
        if self.ffmpeg:
            import subprocess
            webm = Path(self.tmp.name, "in_wx.webm")
            subprocess.run([self.ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "color=c=green:s=512x512:d=1,format=rgba,"
                            "geq=r=0:g=200:b=0:a='if(between(X,100,300)*between(Y,100,300),255,0)'",
                            "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", str(webm)], check=True)
            kind, webm_gif = await self.stickers.to_wechat_sticker(webm.read_bytes(), "video/webm")
            self.assertEqual(kind, "sticker")
            self.assertLessEqual(len(webm_gif), self.stickers.WX_STICKER_MAX_BYTES)
            wim = Image.open(io.BytesIO(webm_gif))
            self.assertGreater(wim.n_frames, 1)
            self.assertEqual(wim.size, (240, 240))
            self.assertEqual(wim.convert("RGBA").getpixel((5, 5))[3], 0)
            self.assertColor(wim.convert("RGBA").getpixel((100, 100))[:3], (0, 200, 0))

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
        self.assertEqual(gim.size, (240, 120))
        rgba = gim.convert("RGBA")
        self.assertEqual(rgba.getpixel((5, 5))[3], 0)
        self.assertColor(rgba.getpixel((120, 60))[:3], (255, 255, 255))

    async def test_animated_webp_resamples_fps_and_preserves_240px_quality(self):
        import io
        from unittest import mock
        from PIL import Image

        # Build a 25fps (40ms/frame) 1s animated WebP
        src_frames = []
        for i in range(25):
            im = Image.new("RGBA", (320, 320), (0, 0, 0, 0))
            im.paste((255, (i * 10) % 256, 40, 255), (40 + i * 2, 60, 220 + i * 2, 240))
            src_frames.append(im)
        webp_buf = io.BytesIO()
        src_frames[0].save(webp_buf, "WEBP", save_all=True, append_images=src_frames[1:], duration=40, loop=0)
        webp_data = webp_buf.getvalue()

        seen_calls: list[tuple[int, int, int, int, str]] = []

        def fake_ffmpeg(frames, w, h, fps, ffmpeg, *, max_colors=256, stats_mode="full", dither="none"):
            seen_calls.append((w, h, len(frames), max_colors, dither))
            # 20fps (20 frames) is > FAST_BYTES (450KB) but <= MAX_BYTES (1MB);
            # 16.67fps (17 frames) is also > FAST_BYTES; 12.5fps (12/13 frames) is STILL 600KB;
            # then 160-color attempt must NOT run because we already have a 240px/256-color GIF <= 1MB!
            return b"GIF89a" + (b"x" * (600 * 1024))

        with mock.patch.object(self.stickers, "find_ffmpeg", return_value="ffmpeg"), \
             mock.patch.object(self.stickers, "find_gifsicle", return_value=None), \
             mock.patch.object(self.stickers, "_ffmpeg_rgba_to_gif", side_effect=fake_ffmpeg):
            out = self.stickers.webp_to_wechat_gif(webp_data, "ffmpeg")

        self.assertEqual(len(out), 6 + 600 * 1024)
        self.assertEqual(len(seen_calls), 3)
        self.assertTrue(all(w == 240 and h == 240 and colors == 256 for w, h, _, colors, _ in seen_calls))
        # Verify FPS actually decreased across the three 240px attempts
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
