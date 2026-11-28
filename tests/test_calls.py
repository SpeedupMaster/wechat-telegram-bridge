"""Tests for WeChat incoming/outgoing calls, CallWeb Mini App, and call media WebSocket."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from telethon import errors, types

from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.core import Bridge
from bridge.wechat import WeChatClient
from tests.fakes import (
    ACC,
    FakeBot,
    FakeDaemon,
    FakeUser,
    _CallbackEvent,
    _FakeCallsSocket,
    _FakeMediaSocket,
    _button_texts,
    _button_urls,
    _init_data,
    tg_event,
)


class CallBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(WeChatConfig(token="secret-token"), TelegramConfig(api_id=1, api_hash="x", bot_token="y"),
                     BridgeConfig(create_interval_seconds=0, sync_avatar=False), Path(self.tmp.name) / "config.toml")
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
            "protocolVersion": 3,
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
                    "protocolVersion": 3,
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
                self.assertEqual(hello["protocolVersion"], 3)
                self.assertEqual(received_client_msgs[0]["supportedProtocolVersions"], [3])
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


class CallWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiohttp
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(WeChatConfig(token="secret-token"), TelegramConfig(api_id=1, api_hash="x", bot_token="123:abc"),
                     BridgeConfig(create_interval_seconds=0, sync_avatar=False,
                                  call_web_url="http://127.0.0.1/wxcall", call_web_listen="127.0.0.1:0"),
                     Path(self.tmp.name) / "config.toml")
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
        self.b.tg_ready.set()
        self.b.owner_id = 999
        await self.b.wx.resolve_account()
        self.sock = _FakeCallsSocket()
        self.b._calls_sock = self.sock
        self.media = _FakeMediaSocket()
        self.media_calls: list[tuple[str, str, str]] = []

        async def call_media(call_id, client_id, owner_token, timeout=10.0):
            self.media_calls.append((call_id, client_id, owner_token))
            return self.media, {"type": "media.ready", "protocolVersion": 3,
                                "sendTracks": [{"media": "audio", "id": 1, "generation": 1, "direction": "send",
                                                "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}}],
                                "receiveTracks": [{"media": "audio", "id": 2, "generation": 1, "direction": "receive",
                                                   "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}}]}

        self.b.wx.call_media = call_media
        await self.b.call_web.start()
        self.http = aiohttp.ClientSession()
        self.base = f"http://127.0.0.1:{self.b.call_web.bound_port}/wxcall"
        await self.b.handle_call_event({
            "type": "hello", "protocolVersion": 3, "clientId": "ctl-1", "conferenceInvitations": [],
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
        import struct
        from bridge.wechat import WXCM_KIND_PCM_S16LE, decode_wxcm_frame
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
        self.assertEqual(self.media.sent[0], {"type": "track.start", "trackId": 1, "generation": 1})
        self.assertIn("通话中（Telegram 接听）", msg.text)
        self.assertEqual(_button_texts(msg), ["🎧 通话界面", "📴 挂断"])

        # page -> WeChat (WXCM v3 binary frame)
        pcm = struct.pack("<4h", 1, -1, 32767, -32768)
        await ws.send_bytes(pcm)
        await self._until(lambda: len(self.media.sent_bytes) >= 1)
        chunk = decode_wxcm_frame(self.media.sent_bytes[0])
        assert chunk is not None
        self.assertEqual((chunk["kind"], chunk["trackId"], chunk["generation"]), (WXCM_KIND_PCM_S16LE, 1, 1))
        self.assertEqual(chunk["payload"], pcm)

        # WeChat -> page: track id header + PCM
        self.media.queue.put_nowait({"type": "audio.chunk", "trackId": 2, "generation": 1, "sequence": 1,
                                     "timestampMicros": 0, "pcm": b"\x01\x00\x02\x00"})
        got = None
        while got is None:
            m = await asyncio.wait_for(ws.receive(), timeout=2)
            if m.type == aiohttp.WSMsgType.BINARY:
                got = m.data
        self.assertEqual(got, struct.pack("<I", 2) + b"\x01\x00\x02\x00")
        sess = self.b.call_web.sessions["call-1"]  # flow counters behind the "no sound" log lines
        self.assertEqual((sess.rx_chunks, sess.rx_to_page, sess.tx_chunks), (1, 1, 1))

        # mute -> call.mute; the daemon then moves the send track to a new generation
        await ws.send_json({"type": "mute", "muted": True})
        st = await self._next(ws, pred=lambda m: m.get("muted"))
        self.assertEqual(self.sock.commands[-1], {"type": "call.mute", "callId": "call-1", "muted": True})
        await self.b.handle_call_event({"type": "track.updated", "callId": "call-1", "track": {
            "media": "audio", "id": 1, "generation": 2, "direction": "send",
            "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}}})
        self.assertEqual(self.media.sent[-1], {"type": "track.start", "trackId": 1, "generation": 2})
        n_chunks = len(self.media.sent_bytes)
        await ws.send_bytes(pcm)  # muted: dropped
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.media.sent_bytes), n_chunks)

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
        from aiohttp import WSMsgType, web
        from bridge.wechat import (
            WXCM_KIND_PCM_S16LE,
            WeChatError,
            decode_wxcm_frame,
            encode_wxcm_frame,
        )
        seen: list[dict] = []
        auth: list[str] = []

        async def media(request):
            auth.append(request.headers.get("Authorization", ""))
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            attach = await ws.receive_json()
            seen.append(attach)
            if request.match_info["call"] == "v2-call":
                await ws.send_json({"type": "media.ready", "protocolVersion": 2, "sendTracks": [], "receiveTracks": []})
                await ws.close()
                return ws
            if attach.get("ownerToken") != "own-1":
                await ws.send_json({"type": "call.error", "code": "not_owner"})
                await ws.close()
                return ws
            await ws.send_json({"type": "media.ready", "protocolVersion": 3,
                                "sendTracks": [{"media": "audio", "id": 1, "generation": 1, "direction": "send",
                                                "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}}],
                                "receiveTracks": []})
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    seen.append(json.loads(msg.data))
                elif msg.type == WSMsgType.BINARY:
                    frame = decode_wxcm_frame(msg.data)
                    assert frame is not None
                    await ws.send_bytes(encode_wxcm_frame(
                        2, 1, frame["payload"], kind=WXCM_KIND_PCM_S16LE, sequence=1, timestamp_micros=0,
                    ))
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
            self.assertEqual(ready["sendTracks"][0]["id"], 1)
            await sock.send_json({"type": "track.start", "trackId": 1, "generation": 1})
            pcm = bytes(range(256)) * 5  # > 125 bytes: exercises the 16-bit length + masking paths
            await sock.send_bytes(encode_wxcm_frame(1, 1, pcm, sequence=1, timestamp_micros=0))
            events = [ev async for ev in sock.events()]
            await sock.close()
            self.assertEqual(auth[0], "Bearer tok")
            self.assertEqual(seen[0], {"type": "media.attach", "supportedProtocolVersions": [3],
                                       "clientId": "ctl-1", "ownerToken": "own-1"})
            self.assertEqual([m["type"] for m in seen[1:]], ["track.start"])
            self.assertEqual(events[0]["pcm"], pcm)

            with self.assertRaises(WeChatError) as cm:
                await wx.call_media("c1", "ctl-1", "wrong")
            self.assertEqual(cm.exception.code, "not_owner")

            with self.assertRaises(WeChatError) as v2_cm:
                await wx.call_media("v2-call", "ctl-1", "own-1")
            self.assertEqual(v2_cm.exception.code, "unsupported_protocol_version")
        finally:
            await wx.close()
            await runner.cleanup()

    async def test_calls_socket_active_ping_keepalive_and_dead_peer_timeout(self):
        import base64
        import hashlib
        import struct
        from bridge.wechat import CallsSocket

        client_pings = 0
        answer_pings = True

        def ws_frame(opcode: int, payload: bytes) -> bytes:
            return bytes([0x80 | opcode, len(payload)]) + payload

        async def read_client_frame(reader: asyncio.StreamReader):
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
            nonlocal client_pings
            try:
                req = await reader.readuntil(b"\r\n\r\n")
                lines = req.decode("latin-1").split("\r\n")
                hdrs = {k.strip().lower(): v.strip() for ln in lines[1:] if ":" in ln for k, _, v in [ln.partition(":")]}
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
                op, _ = await read_client_frame(reader)
                self.assertEqual(op, 0x1)
                writer.write(ws_frame(0x1, json.dumps({"type": "hello", "protocolVersion": 3, "clientId": "kp"}).encode()))
                await writer.drain()

                while (frame := await read_client_frame(reader)) is not None:
                    fop, fdata = frame
                    if fop == 0x8:
                        break
                    if fop == 0x9:
                        client_pings += 1
                        if answer_pings:
                            writer.write(ws_frame(0xA, fdata))
                            if client_pings == 2:
                                writer.write(ws_frame(0x1, json.dumps({"type": "call.pending", "offer": None}).encode()))
                            await writer.drain()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

        server = await asyncio.start_server(handle_conn, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            # 1) Quiet server that answers Pings stays connected across multiple idle intervals and delivers events
            sock = await CallsSocket.connect(f"http://127.0.0.1:{port}/v1/accounts/acc1/calls/socket", "tok")
            sock.idle_ping_seconds = 0.04
            sock.pong_timeout_seconds = 0.08
            await sock.handshake()
            it = sock.events()
            ev = await asyncio.wait_for(anext(it), timeout=1.0)
            self.assertEqual(ev["type"], "call.pending")
            self.assertGreaterEqual(client_pings, 2)
            await sock.close()

            # 2) Half-open dead server that stops answering Pings raises TimeoutError cleanly
            answer_pings = False
            sock2 = await CallsSocket.connect(f"http://127.0.0.1:{port}/v1/accounts/acc1/calls/socket", "tok")
            sock2.idle_ping_seconds = 0.04
            sock2.pong_timeout_seconds = 0.04
            await sock2.handshake()
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(anext(sock2.events()), timeout=1.0)
            await sock2.close()
        finally:
            server.close()
            await server.wait_closed()


class CallConcurrencyAndCrashHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiohttp
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(
            WeChatConfig(token="secret-token"),
            TelegramConfig(api_id=1, api_hash="x", bot_token="123:abc"),
            BridgeConfig(
                create_interval_seconds=0, sync_avatar=False,
                call_web_url="http://127.0.0.1/wxcall", call_web_listen="127.0.0.1:0",
            ),
            Path(self.tmp.name) / "config.toml",
        )
        self.daemon = FakeDaemon()
        self.b = Bridge(cfg, user_client=FakeUser(), bot_client=FakeBot())
        await self.b.wx.http.aclose()
        self.b.wx.http = httpx.AsyncClient(
            transport=httpx.MockTransport(self.daemon.handler),
            headers={"Authorization": "Bearer secret-token"},
        )
        self.b.bot_input = object()
        self.b.bot_me = SimpleNamespace(username="mybot")
        self.b.tg_ready.set()
        self.b.wx_ready = True
        self.b.owner_id = 999
        await self.b.wx.resolve_account()
        self.sock = _FakeCallsSocket()
        self.b._calls_sock = self.sock
        await self.b.call_web.start()
        self.http = aiohttp.ClientSession()
        self.base = f"http://127.0.0.1:{self.b.call_web.bound_port}/wxcall"
        await self.b.handle_call_event({
            "type": "hello", "protocolVersion": 3, "clientId": "ctl-race", "conferenceInvitations": [],
            "capabilities": {"directAudio": True, "directVideo": True},
            "audioFormat": {"sampleRate": 16000, "channels": 1, "frameMs": 20},
        })

    async def asyncTearDown(self):
        await self.http.close()
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def test_withdraw_readiness_does_not_revoke_inflight_answer_or_reject(self):
        self.daemon.add_thread("wxid_alice", "Alice")
        self.daemon.add_thread("wxid_bob", "Bob")

        # Call A is pending (will be rejected) while Call B is simultaneously mid-answer awaiting call.offered
        await self.b.handle_call_event({"type": "call.pending", "offer": {
            "callId": "call-a", "revision": 1, "kind": "direct", "callType": "voice", "callerId": "wxid_alice"}})
        await self.b.handle_call_event({"type": "call.pending", "offer": {
            "callId": "call-b", "revision": 1, "kind": "direct", "callType": "voice", "callerId": "wxid_bob"}})

        b_ready_sent = asyncio.Event()
        release_b_offer = asyncio.Event()

        async def on_cmd(cmd):
            if cmd["type"] == "call.ready" and cmd.get("ready"):
                cid = (cmd.get("scope") or {}).get("callId")
                if cid == "call-a":
                    await self.b.handle_call_event({"type": "call.offered", "offer": {
                        "callId": "call-a", "revision": 2, "kind": "direct", "callType": "voice",
                        "callerId": "wxid_alice"}})
                elif cid == "call-b":
                    b_ready_sent.set()
                    async def deliver_b():
                        await release_b_offer.wait()
                        await self.b.handle_call_event({"type": "call.offered", "offer": {
                            "callId": "call-b", "revision": 2, "kind": "direct", "callType": "voice",
                            "callerId": "wxid_bob"}})
                    asyncio.create_task(deliver_b())
                return {"type": "command.result", "status": "accepted"}
            if cmd["type"] == "call.answer":
                return {"type": "command.result", "status": "accepted", "callId": "call-b", "ownerToken": "own-b"}
            return {"type": "command.result", "status": "accepted"}

        self.sock.on_command = on_cmd
        entry_a = self.b._active_calls["call-a"]
        entry_b = self.b._active_calls["call-b"]

        ans_task = asyncio.create_task(self.b.call_web.answer(entry_b, None))
        await asyncio.wait_for(b_ready_sent.wait(), timeout=2.0)

        # Reject Call A while Call B is still awaiting offered_event (before Call B enters sessions)
        await self.b._reject_call(entry_a)
        ready_false_mid = [c for c in self.sock.commands if c["type"] == "call.ready" and not c.get("ready")]
        self.assertEqual(ready_false_mid, [], "must not revoke call.ready while Call B is mid-answer")

        # Now let Call B finish answering and then end -> readiness is cleanly withdrawn at the end
        release_b_offer.set()
        await asyncio.wait_for(ans_task, timeout=2.0)
        self.assertTrue(self.b.call_web.has_session("call-b"))
        await self.b.handle_call_event({"type": "call.state", "call": {
            "callId": "call-b", "revision": 3, "kind": "direct", "callType": "voice",
            "direction": "incoming", "state": "ended", "ownedByThisClient": True}})
        ready_false_end = [c for c in self.sock.commands if c["type"] == "call.ready" and not c.get("ready")]
        self.assertEqual(len(ready_false_end), 1)

    async def test_unexpected_media_crash_reports_and_hangs_up_call(self):
        self.daemon.add_thread("wxid_alice", "Alice")

        async def broken_call_media(call_id, client_id, owner_token, timeout=10.0):
            raise RuntimeError("unexpected codec explosion")

        self.b.wx.call_media = broken_call_media

        async def on_cmd(cmd):
            if cmd["type"] == "call.answer":
                async def connected():
                    await asyncio.sleep(0.01)
                    await self.b.handle_call_event({"type": "call.state", "call": {
                        "callId": "crash-1", "revision": 3, "kind": "direct", "callType": "voice",
                        "direction": "incoming", "state": "connected", "ownedByThisClient": True}})
                asyncio.create_task(connected())
                return {"type": "command.result", "status": "accepted", "callId": "crash-1", "ownerToken": "own-cr"}
            return {"type": "command.result", "status": "accepted"}

        self.sock.on_command = on_cmd
        await self.b.handle_call_event({"type": "call.offered", "offer": {
            "callId": "crash-1", "revision": 2, "kind": "direct", "callType": "voice", "callerId": "wxid_alice"}})
        entry = self.b._active_calls["crash-1"]
        await self.b.call_web.answer(entry, None)

        for _ in range(100):
            if any(c["type"] == "call.hangup" for c in self.sock.commands):
                break
            await asyncio.sleep(0.01)
        self.assertIn("call.hangup", [c["type"] for c in self.sock.commands])
        self.assertTrue(any("媒体中转异常中断" in m.text for m in self.b.bot.sent))

    async def test_same_origin_worklet_csp_and_page_audio_diag_logged_on_no_mic(self):
        import bridge.callweb as callweb
        from tests.fakes import _FakeMediaSocket, _button_urls, _init_data

        async with self.http.get(self.base + "/worklet.js") as rw:
            self.assertEqual(rw.status, 200)
            self.assertIn("javascript", rw.headers.get("Content-Type", ""))
            wbody = await rw.text()
        self.assertIn("registerProcessor('bridge-io'", wbody)

        async with self.http.get(self.base + "/call/sometoken") as rp:
            self.assertEqual(rp.status, 200)
            csp = rp.headers.get("Content-Security-Policy", "")
        self.assertIn("worker-src 'self' blob:", csp)
        self.assertIn("media-src 'self' blob: mediastream:", csp)

        self.daemon.add_thread("wxid_alice", "Alice")
        media = _FakeMediaSocket()

        async def call_media(call_id, client_id, owner_token, timeout=10.0):
            return media, {
                "type": "media.ready",
                "protocolVersion": 3,
                "sendTracks": [{"media": "audio", "id": 1, "generation": 1, "direction": "send",
                                "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}}],
                "receiveTracks": [{"media": "audio", "id": 2, "generation": 1, "direction": "receive",
                                   "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}}],
            }

        self.b.wx.call_media = call_media

        async def on_cmd(cmd):
            if cmd["type"] == "call.answer":
                async def connected():
                    await asyncio.sleep(0.01)
                    await self.b.handle_call_event({"type": "call.state", "call": {
                        "callId": "ios-diag-1", "revision": 3, "kind": "direct", "callType": "voice",
                        "direction": "incoming", "state": "connected", "ownedByThisClient": True}})
                asyncio.create_task(connected())
                return {"type": "command.result", "status": "accepted", "callId": "ios-diag-1", "ownerToken": "own-d"}
            return {"type": "command.result", "status": "accepted"}

        self.sock.on_command = on_cmd
        await self.b.handle_call_event({"type": "call.offered", "offer": {
            "callId": "ios-diag-1", "revision": 2, "kind": "direct", "callType": "voice", "callerId": "wxid_alice"}})
        token = _button_urls(self.b.bot.sent[-1])[0].rsplit("/", 1)[1]

        ws = await self.http.ws_connect(self.base + "/ws")
        callweb.AUDIO_CHECK_SECONDS, saved_chk = 0.05, callweb.AUDIO_CHECK_SECONDS
        try:
            await ws.send_json({"type": "auth", "token": token, "initData": _init_data("123:abc", 999)})
            await asyncio.wait_for(ws.receive(), timeout=2.0)
            with self.assertLogs("bridge.callweb", level="INFO") as cm:
                await ws.send_json({"type": "answer"})
                await ws.send_json({
                    "type": "diag",
                    "engine": "worklet",
                    "ctxState": "interrupted",
                    "sampleRate": 48000,
                    "currentTime": 0.0,
                    "ticks": 0,
                    "trackState": "live",
                    "trackMuted": False,
                })
                media.queue.put_nowait({
                    "type": "audio.chunk", "trackId": 2, "generation": 1, "sequence": 1,
                    "timestampMicros": 0, "pcm": b"\x10\x00" * 320,
                })
                await asyncio.sleep(0.12)
                await self.b.handle_call_event({"type": "call.state", "call": {
                    "callId": "ios-diag-1", "revision": 4, "kind": "direct", "callType": "voice",
                    "direction": "incoming", "state": "ended", "ownedByThisClient": True}})
            joined_logs = "\n".join(cm.output)
            self.assertIn("no microphone audio from the call page", joined_logs)
            self.assertIn("ctxState=interrupted", joined_logs)
            self.assertIn("engine=worklet", joined_logs)
        finally:
            callweb.AUDIO_CHECK_SECONDS = saved_chk
            await ws.close()

    async def test_silent_mic_chunks_warn_with_diag_and_ending_retains_media_until_ended(self):
        import struct
        import bridge.callweb as callweb
        from tests.fakes import _FakeMediaSocket, _button_urls, _init_data

        self.daemon.add_thread("wxid_alice", "Alice")
        media = _FakeMediaSocket()

        async def call_media(call_id, client_id, owner_token, timeout=10.0):
            return media, {
                "type": "media.ready",
                "protocolVersion": 3,
                "sendTracks": [{"media": "audio", "id": 1, "generation": 1, "direction": "send",
                                "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}}],
                "receiveTracks": [{"media": "audio", "id": 2, "generation": 1, "direction": "receive",
                                   "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}}],
            }

        self.b.wx.call_media = call_media

        async def on_cmd(cmd):
            if cmd["type"] == "call.answer":
                async def connected():
                    await asyncio.sleep(0.01)
                    await self.b.handle_call_event({"type": "call.state", "call": {
                        "callId": "silent-end-1", "revision": 3, "kind": "direct", "callType": "voice",
                        "direction": "incoming", "state": "connected", "ownedByThisClient": True}})
                asyncio.create_task(connected())
                return {"type": "command.result", "status": "accepted", "callId": "silent-end-1", "ownerToken": "own-s"}
            return {"type": "command.result", "status": "accepted"}

        self.sock.on_command = on_cmd
        await self.b.handle_call_event({"type": "call.offered", "offer": {
            "callId": "silent-end-1", "revision": 2, "kind": "direct", "callType": "voice", "callerId": "wxid_alice"}})
        token = _button_urls(self.b.bot.sent[-1])[0].rsplit("/", 1)[1]

        ws = await self.http.ws_connect(self.base + "/ws")
        callweb.AUDIO_CHECK_SECONDS, saved_chk = 0.06, callweb.AUDIO_CHECK_SECONDS
        try:
            await ws.send_json({"type": "auth", "token": token, "initData": _init_data("123:abc", 999)})
            await asyncio.wait_for(ws.receive(), timeout=2.0)
            with self.assertLogs("bridge.callweb", level="INFO") as cm:
                await ws.send_json({"type": "answer"})
                while True:
                    m = json.loads((await asyncio.wait_for(ws.receive(), timeout=2.0)).data)
                    if m.get("type") == "state" and m.get("phase") == "active":
                        break
                await ws.send_json({
                    "type": "diag",
                    "engine": "worklet",
                    "ctxState": "running",
                    "sampleRate": 48000,
                    "txFrames": 50,
                    "txNonZero": 0,
                    "inPeak": 0,
                    "txPeak": 0,
                    "aec": False,
                    "recovery": "raw_mic",
                    "trackState": "live",
                    "trackMuted": True,
                    "ageMs": 3412,
                    "gumMs": 640,
                })
                media.queue.put_nowait({
                    "type": "audio.chunk", "trackId": 2, "generation": 1, "sequence": 1,
                    "timestampMicros": 0, "pcm": b"\x10\x00" * 320,
                })
                # Send 5 all-zero PCM chunks from the page before AUDIO_CHECK_SECONDS fires
                for _ in range(5):
                    await ws.send_bytes(b"\x00\x00" * 320)
                await asyncio.sleep(0.10)

                # Now call enters "ending": page sees ended state, late mic chunks stop forwarding,
                # and /media stays open until "ended" so agent-wechat never drops /calls/socket!
                await self.b.handle_call_event({"type": "call.state", "call": {
                    "callId": "silent-end-1", "revision": 4, "kind": "direct", "callType": "voice",
                    "direction": "incoming", "state": "ending", "ownedByThisClient": True}})
                self.assertFalse(media.closed, "media socket must stay open during 'ending' until 'ended'")
                sent_before_ending = len(media.sent_bytes)
                await ws.send_bytes(struct.pack("<h", 12000) * 320)
                await asyncio.sleep(0.02)
                self.assertEqual(len(media.sent_bytes), sent_before_ending)

                # Terminal "ended" arrives -> media socket closes cleanly and logs nonzero/peak + diag
                await self.b.handle_call_event({"type": "call.state", "call": {
                    "callId": "silent-end-1", "revision": 5, "kind": "direct", "callType": "voice",
                    "direction": "incoming", "state": "ended", "ownedByThisClient": True}})
                self.assertTrue(media.closed)

            joined_logs = "\n".join(cm.output)
            self.assertIn("microphone chunks arrived", joined_logs)
            self.assertIn("audio is silent (nonzero: 0, peak: 0", joined_logs)
            self.assertIn("recovery=raw_mic", joined_logs)
            self.assertIn("trackMuted=True", joined_logs)
            self.assertIn("ageMs=3412, gumMs=640", joined_logs)
            self.assertIn("5 mic chunks from the page (nonzero 0, peak 0)", joined_logs)
        finally:
            callweb.AUDIO_CHECK_SECONDS = saved_chk
            await ws.close()


