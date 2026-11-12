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
from telethon import functions, types

from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.core import Bridge
from bridge.wechat import CursorExpired, WeChatClient

ACC = "acc1"


class FakeDaemon:
    def __init__(self):
        self.messages: dict[str, dict] = {}
        self.threads: dict[str, dict] = {}
        self.sends: list[tuple[dict, str]] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.sse_body = ""
        self.marked_read: list[str] = []
        self.avatar_refreshed: list[str] = []
        self.avatar_op = {"id": "avop", "state": "succeeded"}
        self.avatar_lag = 0  # reads after a successful refresh that still miss (profile reader lag)
        self.media: dict[str, list[dict]] = {}  # message id -> media assets (default: one available PNG)

    def add_thread(self, tid, name, kind="direct"):
        self.threads[tid] = {"id": tid, "accountId": ACC, "peerId": tid, "kind": kind, "displayName": name,
                             "unreadCount": 1, "readStateVersion": "v1"}

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
        if rest[0] == "messages" and len(rest) == 2:
            return httpx.Response(200, json=self.messages[rest[1]])
        if rest[0] == "messages" and rest[2] == "media" and rest[1] in self.media:
            return httpx.Response(200, json={"items": self.media[rest[1]]})
        if rest[0] == "messages" and rest[2] == "media":
            return httpx.Response(200, json={"items": [{"id": "asset-" + rest[1], "state": "available",
                                                        "variant": "original", "actualVariant": "original",
                                                        "contentType": "image/png", "byteLength": 4,
                                                        "filename": "pic.png"}]})
        if rest[0] == "media" and rest[2] == "content":
            return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
        if rest[0] == "threads" and len(rest) == 2:
            t = self.threads.get(rest[1])
            return httpx.Response(200, json=t) if t else httpx.Response(404, json={"error": {"code": "not_found"}})
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
                return httpx.Response(200, content=b"JPEGDATA", headers={"content-type": "image/jpeg"})
            return httpx.Response(404, json={"error": {"code": "avatar_unavailable"}})
        if rest[0] == "groups" and rest[2] == "members":
            return httpx.Response(200, json={"state": "present", "member": {"displayName": "Alice in group"}})
        if rest[0] == "contacts":
            return httpx.Response(200, json={"id": rest[1], "displayName": "Contact " + rest[1]})
        if rest[0] == "uploads" and m == "POST":
            self.uploads.append((request.headers["content-type"], request.content))
            return httpx.Response(201, json={"id": "src1", "name": "x", "mediaType": "image/jpeg"})
        if rest[0] == "sends" and m == "POST":
            body = json.loads(request.content)
            self.sends.append((body, request.headers["idempotency-key"]))
            n = len(self.sends)
            if "sourceId" in body:
                return httpx.Response(202, json={"id": f"job{n}", "state": "queued", "kind": "send"})
            return httpx.Response(202, json={"id": f"send{n}", "state": "queued"})
        if rest[0] == "sends" and m == "GET":
            n = rest[1].removeprefix("send")
            return httpx.Response(200, json={"id": rest[1], "state": "accepted", "serverId": f"90{n}"})
        if rest[0] == "jobs":
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

    async def send_message(self, chat, text, **kw):
        s = Sent(chat, text=text, kwargs=kw)
        self.sent.append(s)
        return s

    async def send_file(self, chat, file, **kw):
        s = Sent(chat, file=file, kwargs=kw)
        self.sent.append(s)
        return s


class FakeUser:
    def __init__(self):
        self.created: list[str] = []
        self.photos: list = []

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
        self.assertEqual(self.b.user.photos, [(501, b"JPEGDATA")])

    async def test_avatar_read_retried_after_refresh(self):
        self.daemon.avatar_lag = 2
        data, why = await self.b.wx.fetch_avatar("wxid_bob", retry_delays=(0, 0, 0))
        self.assertEqual((data, why), (b"JPEGDATA", ""))

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

    async def test_sticker_is_forwarded_as_its_image(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("st1", "wxid_bob", None, type_=47)
        self.daemon.media["st1"] = [{"id": "asset-st1", "kind": "sticker", "state": "available", "variant": "original",
                                     "actualVariant": "original", "contentType": "image/gif", "byteLength": 4}]
        await self.event("st1", "wxid_bob")
        s = self.b.bot.sent[-1]
        self.assertEqual(s.file.name, "st1.gif")
        self.assertFalse(s.kwargs["force_document"])

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
        self.assertIn("我", s.text)
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
        self.assertEqual((body["kind"], body["sourceId"], key), ("image", "src1", "k1"))
        self.assertIn("multipart/form-data", self.daemon.uploads[0][0])

    async def test_video_sent_with_original(self):
        from bridge.wechat import Media
        await self.b.wx.send_media("wxid_bob", "video", Media(b"mp4", "video/mp4", "clip.mp4"), "k2")
        body, _ = self.daemon.sends[-1]
        self.assertEqual((body["kind"], body["videoQuality"]), ("video", "original"))

    async def test_latest_cursor(self):
        self.assertEqual(await self.b.wx.latest_cursor(), "42")


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
            return httpx.Response(202, json={"id": "op1", "kind": "login", **self.ops[0]})
        if path == f"accounts/{ACC}/operations/op1":
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
                   "r": {"a": 1, "k": [{"t": 0, "s": [0], "e": [90], "i": {"x": [1], "y": [1]}, "o": {"x": [0], "y": [0]}},
                                       {"t": 30, "s": [90]}]}},
            "shapes": [{"ty": "rc", "p": still([0, 0]), "s": still([200, 200]), "r": still(0)},
                       {"ty": "fl", "c": still([1, 0, 0, 1]), "o": still(100)}]}]}
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

    async def test_bridge_sends_sticker_as_jpeg(self):
        b = Bridge.__new__(Bridge)
        b.max_media = 50 * 1048576
        data = self.webp()

        async def download_media(file=None):
            return data
        msg = SimpleNamespace(file=SimpleNamespace(size=len(data), mime_type="image/webp", name=None, ext=".webp"),
                              sticker=True, photo=None, download_media=download_media)
        kind, media = await b._tg_media(msg)
        self.assertEqual((kind, media.mime, media.filename), ("image", "image/jpeg", "sticker.jpg"))
        self.assertTrue(media.data.startswith(b"\xff\xd8"))

    async def test_picture_sent_as_file_stays_a_file(self):
        """agent-wechat shrinks images to ~64 KB; a Telegram document keeps its bytes as a WeChat file."""
        b = Bridge.__new__(Bridge)
        b.max_media = 50 * 1048576
        data = b"\xff\xd8" + b"x" * 300_000

        async def download_media(file=None):
            return data
        msg = SimpleNamespace(file=SimpleNamespace(size=len(data), mime_type="image/jpeg", name="IMG_7462.JPG", ext=".jpg"),
                              sticker=None, photo=None, voice=None, video=None, video_note=None, gif=None,
                              download_media=download_media)
        kind, media = await b._tg_media(msg)
        self.assertEqual((kind, media.filename, media.data), ("file", "IMG_7462.JPG", data))
