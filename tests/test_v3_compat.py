"""Tests for agent-wechat 3.0.0-beta.0 / beta.1 compatibility:
- Calls Protocol v3 (WXCM binary media framing, sendTracks[], track.start, v2 fallback)
- Media-close vs call.state(ended) race guard
- Enterprise WeChat (@im.chatroom) group recognition and TP media error reasons
- Oversized forwarded chat history (49:40) rendering and message.updated in-place refresh
"""
from __future__ import annotations

import asyncio
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import aiohttp
from aiohttp import WSMsgType, web
import httpx

from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.core import Bridge
from bridge.wechat import WeChatClient, WeChatError
from tests.fakes import FakeBot, FakeDaemon, FakeUser, _FakeCallsSocket, _button_urls
from tests.test_groups import GroupDaemon


class _FakeV3MediaSocket:
    """Test double for a v3 CallsSocket recording both JSON and binary WXCM frames."""

    def __init__(self):
        self.sent_json: list[dict] = []
        self.sent_bytes: list[bytes] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.protocol_version = 3

    async def send_json(self, obj: dict) -> None:
        self.sent_json.append(obj)

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


class WxcmCodecTests(unittest.TestCase):
    def test_wxcm_encode_decode_matches_upstream_wire_vector(self):
        from bridge.wechat import (
            WXCM_KIND_JPEG,
            WXCM_KIND_PCM_S16LE,
            decode_wxcm_frame,
            encode_wxcm_frame,
        )

        pcm = struct.pack("<3h", -32768, 32767, -1) + b"\x00" * 154  # 160 bytes total
        encoded = encode_wxcm_frame(
            0x01020304,
            2,
            pcm,
            kind=WXCM_KIND_PCM_S16LE,
            sequence=3,
            timestamp_micros=10_000,
        )
        # Hand-derived literal header from upstream wechat-call-contracts/src/pcm_envelope.rs
        expected_header = bytes([
            0x57, 0x58, 0x43, 0x4D,  # "WXCM"
            3,                       # version = 3
            1,                       # kind = PcmS16le
            0, 0,                    # flags = 0
            1, 2, 3, 4,              # track_id = 0x01020304
            0, 0, 0, 2,              # generation = 2
            0, 0, 0, 0, 0, 0, 0, 3,  # sequence = 3
            0, 0, 0, 0, 0, 0, 0x27, 0x10,  # timestamp_micros = 10_000
            0, 0, 0, 0xA0,           # payload_length = 160
        ])
        self.assertEqual(encoded[:36], expected_header)
        self.assertEqual(encoded[36:42], bytes([0x00, 0x80, 0xFF, 0x7F, 0xFF, 0xFF]))

        decoded = decode_wxcm_frame(encoded)
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded["kind"], WXCM_KIND_PCM_S16LE)
        self.assertEqual(decoded["flags"], 0)
        self.assertEqual(decoded["trackId"], 0x01020304)
        self.assertEqual(decoded["generation"], 2)
        self.assertEqual(decoded["sequence"], 3)
        self.assertEqual(decoded["timestampMicros"], 10_000)
        self.assertEqual(decoded["payload"], pcm)

        # Video frame (JPEG) with keyframe + 90 deg rotation flags decodes cleanly
        vid = encode_wxcm_frame(65538, 1, b"\xff\xd8\xff\xd9", kind=WXCM_KIND_JPEG, flags=0x0005, sequence=1)
        vdec = decode_wxcm_frame(vid)
        self.assertIsNotNone(vdec)
        assert vdec is not None
        self.assertEqual((vdec["kind"], vdec["flags"], vdec["payload"]), (WXCM_KIND_JPEG, 0x0005, b"\xff\xd8\xff\xd9"))

        # Malformed frames must be rejected
        self.assertIsNone(decode_wxcm_frame(encoded[:-1]))           # truncated
        self.assertIsNone(decode_wxcm_frame(encoded + b"\x00"))      # trailing byte
        self.assertIsNone(decode_wxcm_frame(b"NOPE" + encoded[4:]))  # bad magic
        bad_ver = bytearray(encoded)
        bad_ver[4] = 2
        self.assertIsNone(decode_wxcm_frame(bytes(bad_ver)))         # wrong version
        bad_pcm_flags = bytearray(encoded)
        bad_pcm_flags[7] = 1
        self.assertIsNone(decode_wxcm_frame(bytes(bad_pcm_flags)))   # non-zero PCM flags
        zero_track = bytearray(encoded)
        zero_track[8:12] = b"\x00\x00\x00\x00"
        self.assertIsNone(decode_wxcm_frame(bytes(zero_track)))      # track_id == 0
        zero_gen = bytearray(encoded)
        zero_gen[12:16] = b"\x00\x00\x00\x00"
        self.assertIsNone(decode_wxcm_frame(bytes(zero_gen)))        # generation == 0


class CallsProtocolV3SocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_and_media_socket_v3_negotiation_and_binary_wxcm_roundtrip(self):
        from bridge.wechat import (
            WXCM_KIND_JPEG,
            WXCM_KIND_PCM_S16LE,
            decode_wxcm_frame,
            encode_wxcm_frame,
        )

        control_seen: list[dict] = []
        media_seen_json: list[dict] = []
        media_seen_bin: list[bytes] = []

        async def control(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            hello = await ws.receive_json()
            control_seen.append(hello)
            if 3 not in (hello.get("supportedProtocolVersions") or []):
                await ws.send_json({"type": "protocol.error", "code": "unsupported_protocol_version"})
                await ws.close()
                return ws
            await ws.send_json({
                "type": "hello",
                "protocolVersion": 3,
                "clientId": "ctl-v3",
                "capabilities": {"directAudio": True, "directVideo": True},
                "audioFormat": {"sampleRate": 16000, "channels": 1, "frameMs": 20},
            })
            await ws.close()
            return ws

        async def media(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            attach = await ws.receive_json()
            media_seen_json.append(attach)
            if 3 not in (attach.get("supportedProtocolVersions") or []):
                await ws.send_json({"type": "protocol.error", "code": "unsupported_protocol_version"})
                await ws.close()
                return ws
            await ws.send_json({
                "type": "media.ready",
                "protocolVersion": 3,
                "sendTracks": [
                    {"media": "audio", "id": 1, "generation": 1, "direction": "send",
                     "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}},
                    {"media": "video", "id": 65537, "generation": 1, "direction": "send",
                     "format": {"codec": "h265", "mode": "passthrough"}},
                ],
                "receiveTracks": [
                    {"media": "audio", "id": 2, "generation": 1, "direction": "receive",
                     "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}},
                ],
            })
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    media_seen_json.append(json.loads(msg.data))
                elif msg.type == WSMsgType.BINARY:
                    media_seen_bin.append(msg.data)
                    frame = decode_wxcm_frame(msg.data)
                    assert frame is not None
                    # Send a video frame first, then echo PCM back on receive track 2
                    await ws.send_bytes(encode_wxcm_frame(
                        65538, 1, b"\xff\xd8\xff\xd9", kind=WXCM_KIND_JPEG, flags=1, sequence=1,
                    ))
                    await ws.send_bytes(encode_wxcm_frame(
                        2, 1, frame["payload"], kind=WXCM_KIND_PCM_S16LE, sequence=1, timestamp_micros=20_000,
                    ))
                    await ws.close()
            return ws

        app = web.Application()
        app.router.add_get("/v1/accounts/acc1/calls/socket", control)
        app.router.add_get("/v1/accounts/acc1/calls/{call}/media", media)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        wx = WeChatClient(f"http://127.0.0.1:{port}", "tok", "acc1")
        try:
            ctl_sock, hello = await wx.calls_socket()
            self.assertEqual(hello["protocolVersion"], 3)
            self.assertEqual(ctl_sock.protocol_version, 3)
            self.assertEqual(control_seen[0]["supportedProtocolVersions"], [3, 2])
            await ctl_sock.close()

            msock, ready = await wx.call_media("c1", "ctl-v3", "own-1")
            self.assertEqual(ready["protocolVersion"], 3)
            self.assertEqual(msock.protocol_version, 3)
            self.assertEqual(media_seen_json[0]["supportedProtocolVersions"], [3, 2])
            await msock.send_json({"type": "track.start", "trackId": 1, "generation": 1})
            pcm = bytes(range(256)) * 5  # 1280 bytes (> 125-byte WS header threshold)
            await msock.send_bytes(encode_wxcm_frame(1, 1, pcm, sequence=1, timestamp_micros=0))

            events = [ev async for ev in msock.events()]
            await msock.close()
            self.assertEqual(media_seen_json[1], {"type": "track.start", "trackId": 1, "generation": 1})
            self.assertEqual(len(media_seen_bin), 1)
            audio_evs = [e for e in events if e.get("type") == "audio.chunk"]
            self.assertEqual(len(audio_evs), 1)
            self.assertEqual(audio_evs[0]["trackId"], 2)
            self.assertEqual(audio_evs[0]["pcm"], pcm)
        finally:
            await wx.close()
            await runner.cleanup()


class CallWebV3Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
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
        self.media = _FakeV3MediaSocket()

        async def call_media(call_id, client_id, owner_token, timeout=10.0):
            return self.media, {
                "type": "media.ready",
                "protocolVersion": 3,
                "sendTracks": [
                    {"media": "video", "id": 65537, "generation": 1, "direction": "send",
                     "format": {"codec": "h264", "mode": "transcoded"}},
                    {"media": "audio", "id": 1, "generation": 1, "direction": "send",
                     "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}},
                ],
                "receiveTracks": [
                    {"media": "audio", "id": 2, "generation": 1, "direction": "receive",
                     "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}},
                ],
            }

        self.b.wx.call_media = call_media
        await self.b.call_web.start()
        self.http = aiohttp.ClientSession()
        self.base = f"http://127.0.0.1:{self.b.call_web.bound_port}/wxcall"
        await self.b.handle_call_event({
            "type": "hello", "protocolVersion": 3, "clientId": "ctl-v3", "conferenceInvitations": [],
            "capabilities": {"directAudio": True, "directVideo": True, "conferenceJoin": True, "conferenceAudio": True},
            "audioFormat": {"sampleRate": 16000, "channels": 1, "frameMs": 20},
        })

    async def asyncTearDown(self):
        await self.http.close()
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def _page(self, token: str):
        ws = await self.http.ws_connect(self.base + "/ws")
        await ws.send_json({"type": "auth", "token": token, "initData": ""})
        return ws

    async def _until(self, cond, timeout: float = 2.0):
        for _ in range(int(timeout / 0.01)):
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition not met")

    async def _answer_voice_call(self, call_id: str = "v3-call-1"):
        self.daemon.add_thread("wxid_alice", "Alice")

        async def on_cmd(cmd):
            if cmd["type"] == "call.answer":
                async def connected():
                    await asyncio.sleep(0.01)
                    await self.b.handle_call_event({"type": "call.state", "call": {
                        "callId": call_id, "revision": 3, "kind": "direct", "callType": "voice",
                        "direction": "incoming", "state": "connected", "ownedByThisClient": True}})
                asyncio.create_task(connected())
                return {"type": "command.result", "status": "accepted", "callId": call_id, "ownerToken": "own-v3"}
            return None

        self.sock.on_command = on_cmd
        await self.b.handle_call_event({"type": "call.offered", "offer": {
            "callId": call_id, "revision": 2, "kind": "direct", "callType": "voice", "callerId": "wxid_alice"}})
        msg = self.b.bot.sent[-1]
        token = _button_urls(msg)[0].rsplit("/", 1)[1]
        ws = await self._page(token)
        # consume initial ringing state
        await asyncio.wait_for(ws.receive_json(), timeout=2)
        await ws.send_json({"type": "answer"})
        while True:
            st = await asyncio.wait_for(ws.receive_json(), timeout=2)
            if st.get("type") == "state" and st.get("phase") == "active":
                break
        return ws

    async def test_v3_audio_relay_uses_track_start_and_binary_wxcm_frames(self):
        from bridge.wechat import WXCM_KIND_PCM_S16LE, decode_wxcm_frame

        ws = await self._answer_voice_call()
        try:
            # Must use v3 track.start for audio track 1 (ignoring video send track 65537)
            self.assertEqual(self.media.sent_json[0], {"type": "track.start", "trackId": 1, "generation": 1})

            # Page -> daemon: two consecutive PCM chunks become binary WXCM frames with strictly increasing seq & ts
            pcm1 = struct.pack("<320h", *([100] * 320))  # 20ms @ 16kHz = 640 bytes
            pcm2 = struct.pack("<320h", *([-100] * 320))
            await ws.send_bytes(pcm1)
            await ws.send_bytes(pcm2)
            await self._until(lambda: len(self.media.sent_bytes) >= 2)
            f1 = decode_wxcm_frame(self.media.sent_bytes[0])
            f2 = decode_wxcm_frame(self.media.sent_bytes[1])
            assert f1 is not None and f2 is not None
            self.assertEqual((f1["kind"], f1["flags"], f1["trackId"], f1["generation"]), (WXCM_KIND_PCM_S16LE, 0, 1, 1))
            self.assertEqual(f1["payload"], pcm1)
            self.assertEqual(f2["payload"], pcm2)
            self.assertGreater(f2["sequence"], f1["sequence"])
            self.assertGreater(f2["timestampMicros"], f1["timestampMicros"])
            self.assertEqual(f2["timestampMicros"] - f1["timestampMicros"], 20_000)

            # Daemon -> page: v3 audio.chunk carries raw `pcm` bytes (no base64)
            rx_pcm = b"\x11\x00\x22\x00"
            self.media.queue.put_nowait({"type": "audio.chunk", "trackId": 2, "generation": 1, "pcm": rx_pcm})
            got = None
            while got is None:
                m = await asyncio.wait_for(ws.receive(), timeout=2)
                if m.type == WSMsgType.BINARY:
                    got = m.data
            self.assertEqual(got, struct.pack("<I", 2) + rx_pcm)

            # Video track.updated must NOT touch audio send generation; audio track.updated sends track.start
            await self.b.handle_call_event({"type": "track.updated", "callId": "v3-call-1", "track": {
                "media": "video", "id": 65537, "generation": 2, "direction": "send"}})
            self.assertEqual(len(self.media.sent_json), 1)
            await self.b.handle_call_event({"type": "track.updated", "callId": "v3-call-1", "track": {
                "media": "audio", "id": 1, "generation": 2, "direction": "send",
                "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}}})
            self.assertEqual(self.media.sent_json[-1], {"type": "track.start", "trackId": 1, "generation": 2})

            # Next PCM frame carries generation 2
            await ws.send_bytes(pcm1)
            await self._until(lambda: len(self.media.sent_bytes) >= 3)
            f3 = decode_wxcm_frame(self.media.sent_bytes[2])
            assert f3 is not None
            self.assertEqual(f3["generation"], 2)
        finally:
            await ws.close()

    async def test_media_close_racing_before_call_ended_does_not_report_disconnect(self):
        import bridge.callweb as callweb

        ws = await self._answer_voice_call("race-call-1")
        try:
            before_sent = len(self.b.bot.sent)
            before_cmds = len(self.sock.commands)
            with mock.patch.object(callweb, "MEDIA_CLOSE_GRACE_SECONDS", 0.2):
                # Media socket ends slightly before call.state(ended) arrives
                self.media.queue.put_nowait(None)
                await asyncio.sleep(0.03)
                await self.b.handle_call_event({"type": "call.state", "call": {
                    "callId": "race-call-1", "revision": 4, "kind": "direct", "callType": "voice",
                    "direction": "incoming", "state": "ended", "ownedByThisClient": True}})
                await self._until(lambda: not self.b.call_web.has_session("race-call-1"))
            # Neither an error message nor a spurious hangup was sent
            new_texts = [m.text for m in self.b.bot.sent[before_sent:]]
            self.assertFalse(any("通话音频连接已断开" in t for t in new_texts), new_texts)
            new_cmds = [c["type"] for c in self.sock.commands[before_cmds:]]
            self.assertNotIn("call.hangup", new_cmds)
        finally:
            await ws.close()

    async def test_media_close_without_call_ended_reports_disconnect_and_hangs_up(self):
        import bridge.callweb as callweb

        ws = await self._answer_voice_call("drop-call-1")
        try:
            before_sent = len(self.b.bot.sent)
            with mock.patch.object(callweb, "MEDIA_CLOSE_GRACE_SECONDS", 0.05):
                self.media.queue.put_nowait(None)
                await self._until(lambda: any(c["type"] == "call.hangup" for c in self.sock.commands))
            new_texts = [m.text for m in self.b.bot.sent[before_sent:]]
            self.assertTrue(any("通话音频连接已断开" in t for t in new_texts), new_texts)
        finally:
            await ws.close()


class WeComExternalGroupAndMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(
            WeChatConfig(token="secret-token"),
            TelegramConfig(api_id=1, api_hash="x", bot_token="123:abc"),
            BridgeConfig(create_interval_seconds=0, sync_avatar=False),
            Path(self.tmp.name) / "config.toml",
        )
        self.daemon = GroupDaemon()
        self.b = Bridge(cfg, user_client=FakeUser(), bot_client=FakeBot())
        await self.b.wx.http.aclose()
        self.b.wx.http = httpx.AsyncClient(
            transport=httpx.MockTransport(self.daemon.handler),
            headers={"Authorization": "Bearer secret-token"},
        )
        self.b.wx._poll_initial_delay = 0.0
        self.b.wx._avatar_poll_interval = 0.0
        self.b._bot_pair_wait = 0.0
        self.b.bot_input = object()
        self.b.bot_me = SimpleNamespace(username="mybot")
        self.b.tg_ready.set()
        self.b.wx_ready = True
        self.b.owner_id = 999
        await self.b.wx.resolve_account()

    async def asyncTearDown(self):
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def _event(self, mid: str, thread: str, origin: str = "live", direction: str = "incoming"):
        await self.b.handle_event({
            "cursor": "1",
            "kind": "message.committed",
            "data": {"messageId": mid, "threadId": thread, "origin": origin, "direction": direction},
        })

    async def test_im_chatroom_recognized_as_group_across_core_contacts_and_groups(self):
        from bridge.wechat import is_group_id
        from tests.fakes import tg_event

        room = "12345@im.chatroom"
        self.assertTrue(is_group_id(room))
        self.assertTrue(is_group_id("999@chatroom"))
        self.assertFalse(is_group_id("wxid_alice"))
        self.assertFalse(is_group_id("25984@openim"))
        self.assertFalse(is_group_id(""))

        self.daemon.add_thread(room, "外部客户群", kind="group")
        self.daemon.group_members[room] = [
            {"id": "wxid_alice", "roomDisplayName": "Alice群名", "mentionName": "Alice群名",
             "displayName": "Alice群名", "nickname": "Alice"},
            {"id": "25984@openim", "displayName": "企微同事", "mentionName": "企微同事"},
        ]
        self.daemon.groups[room] = {
            "id": room, "name": "外部客户群", "displayName": "外部客户群",
            "ownerId": "25984@openim", "administratorIds": [], "muted": False, "isInGroup": True,
        }
        self.daemon.add_message("im1", room, "hello", sender="wxid_alice")
        await self._event("im1", room)

        # Sender name came from group_member lookup, not raw wxid
        self.assertIn("<b>Alice群名</b>", self.b.bot.sent[-1].text)
        chat = self.b.store.chat_for_thread(room)
        assert chat is not None

        # @mention in Telegram resolves against @im.chatroom members
        await self.b.on_tg_message(tg_event(chat, 10, "@Alice群名 收到"))
        self.assertEqual(self.daemon.sends[-1][0]["mentions"], {"all": False, "userIds": ["wxid_alice"]})

        # /members works in @im.chatroom
        await self.b.on_tg_message(tg_event(chat, 11, "/members"))
        self.assertIn("Alice群名", self.b.bot.sent[-1].text)
        self.assertIn("企微同事", self.b.bot.sent[-1].text)

        # /contact and /remark treat @im.chatroom as a group chat
        card_text, _ = await self.b.contact_card(room)
        self.assertIn("微信群聊", card_text)
        await self.b.cmd_remark(chat, 12, room, "新备注")
        self.assertIn("微信群聊", self.b.bot.sent[-1].text)

        # /group treats @im.chatroom as a group chat (renders group card, not "这是单聊")
        await self.b.on_group_command(chat, 13, room, "/group", "")
        self.assertIn("外部客户群", self.b.bot.sent[-1].text)
        self.assertNotIn("这是单聊", self.b.bot.sent[-1].text)

        # _note_self_id ignores @im.chatroom ids
        self.b._self_wxid = ""
        self.b._note_self_id(room, "wxid_other")
        self.assertEqual(self.b._self_wxid, "")

        # _friend_candidates excludes @im.chatroom threads
        self.daemon.add_thread("wxid_friend", "Friend", kind="direct")
        cands, _ = await self.b._friend_candidates("", set())
        self.assertNotIn(room, [c["id"] for c in cands])

    def test_wecom_tp_media_error_reasons_and_im_chatroom_note(self):
        from bridge.core import MEDIA_REASONS, _media_failure_text

        for code in (
            "tp_media_source_not_allowed",
            "tp_media_dns_failed",
            "tp_media_dns_not_public",
            "tp_media_size_limit",
            "tp_media_encoding_refused",
            "tp_media_fetch_failed",
            "unsupported_media_variant",
        ):
            self.assertIn(code, MEDIA_REASONS)
            rendered = _media_failure_text({"type": 3, "threadId": "12345@im.chatroom", "senderId": "25984@openim"}, code)
            self.assertNotIn(code, rendered)

        note_im_room = _media_failure_text(
            {"type": 3, "threadId": "12345@im.chatroom", "senderId": "wxid_bob"},
            "unsupported_media_transport",
        )
        self.assertIn("3.0", note_im_room)
        self.assertIn("企业微信", note_im_room)


class BigXmlAndMessageUpdatedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(
            WeChatConfig(token="secret-token"),
            TelegramConfig(api_id=1, api_hash="x", bot_token="123:abc"),
            BridgeConfig(create_interval_seconds=0, sync_avatar=False),
            Path(self.tmp.name) / "config.toml",
        )
        self.daemon = GroupDaemon()
        self.b = Bridge(cfg, user_client=FakeUser(), bot_client=FakeBot())
        await self.b.wx.http.aclose()
        self.b.wx.http = httpx.AsyncClient(
            transport=httpx.MockTransport(self.daemon.handler),
            headers={"Authorization": "Bearer secret-token"},
        )
        self.b.wx._poll_initial_delay = 0.0
        self.b.wx._avatar_poll_interval = 0.0
        self.b._bot_pair_wait = 0.0
        self.b.bot_input = object()
        self.b.bot_me = SimpleNamespace(username="mybot")
        self.b.tg_ready.set()
        self.b.wx_ready = True
        self.b.owner_id = 999
        await self.b.wx.resolve_account()

    async def asyncTearDown(self):
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def test_unexpanded_49_40_renders_friendly_fallback_and_quotes(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message(
            "big_preview", "wxid_bob",
            "[Chat History] Chat History for Team\nAlice: first line\n[Chat History unavailable]",
            type_=49, appMessageType=40,
        )
        self.daemon.add_message(
            "big_bare", "wxid_bob", "[Chat History unavailable]",
            type_=49, appMessageType=40,
        )
        self.daemon.add_message(
            "big_null", "wxid_bob", None,
            type_=49, appMessageType=40,
        )
        for mid in ("big_preview", "big_bare", "big_null"):
            await self.b.handle_event({
                "cursor": "1", "kind": "message.committed",
                "data": {"messageId": mid, "threadId": "wxid_bob", "origin": "live", "direction": "incoming"},
            })

        preview, bare, null_msg = (m.text for m in self.b.bot.sent[-3:])
        self.assertTrue(preview.startswith("📜 聊天记录\n"), preview)
        self.assertIn("Chat History for Team", preview)
        self.assertIn("Alice: first line", preview)
        self.assertNotIn("[Chat History unavailable]", preview)
        self.assertIn("暂不可用", preview)

        self.assertTrue(bare.startswith("📜 聊天记录"), bare)
        self.assertNotIn("[Chat History unavailable]", bare)
        self.assertIn("暂不可用", bare)

        self.assertTrue(null_msg.startswith("📜 聊天记录"), null_msg)
        self.assertNotIn("49/40", null_msg)

        # Quote rendering for 49:40 uses [聊天记录] instead of ellipsis
        self.assertEqual(
            self.b._quote_inline_text({"senderName": "Bob", "type": 49, "appMessageType": 40}),
            "[聊天记录]",
        )
        self.assertIn(
            "聊天记录",
            self.b._quote_msg_summary({"type": 49, "appMessageType": 40, "body": "[Chat History unavailable]"}),
        )

    async def test_message_updated_edits_placeholder_in_place_preserving_group_header(self):
        room = "room@chatroom"
        self.daemon.add_thread(room, "Team", kind="group")
        self.daemon.group_members[room] = [{"id": "wxid_alice", "displayName": "Alice"}]
        self.daemon.add_message(
            "big1", room,
            "[Chat History] Team\n[Chat History unavailable]",
            type_=49, appMessageType=40, sender="wxid_alice", server="9001",
        )
        await self.b.handle_event({
            "cursor": "1", "kind": "message.committed",
            "data": {"messageId": "big1", "threadId": room, "origin": "live", "direction": "incoming"},
        })
        sent_before = len(self.b.bot.sent)
        placeholder = self.b.bot.sent[-1]
        self.assertIn("<b>Alice</b>：", placeholder.text)
        self.assertIn("暂不可用", placeholder.text)

        # Daemon background retry succeeds and rewrites the message to 49:19, then emits message.updated
        self.daemon.add_message(
            "big1", room,
            "[Chat History] Team\nAlice: Full message one\nBob: Full message two",
            type_=49, appMessageType=19, sender="wxid_alice", server="9001",
        )
        await self.b.handle_event({
            "cursor": "2", "kind": "message.updated",
            "data": {"messageId": "big1", "threadId": room, "origin": "live", "direction": "incoming"},
        })
        # Edited in place; no duplicate message sent
        self.assertEqual(len(self.b.bot.sent), sent_before)
        self.assertEqual(len(self.b.bot.edited), 1)
        self.assertIn("<b>Alice</b>：", placeholder.text)
        self.assertIn("📜 聊天记录", placeholder.text)
        self.assertIn("Full message one", placeholder.text)
        self.assertIn("Full message two", placeholder.text)
        self.assertNotIn("暂不可用", placeholder.text)

    async def test_message_updated_overflow_edits_first_and_appends_followup_chunks(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message(
            "big_long", "wxid_bob", "[Chat History unavailable]",
            type_=49, appMessageType=40, server="9002",
        )
        await self.b.handle_event({
            "cursor": "1", "kind": "message.committed",
            "data": {"messageId": "big_long", "threadId": "wxid_bob", "origin": "live", "direction": "incoming"},
        })
        first = self.b.bot.sent[-1]
        sent_before = len(self.b.bot.sent)

        long_body = ("A" * 2500) + "\n" + ("B" * 2500)
        self.daemon.add_message(
            "big_long", "wxid_bob", long_body,
            type_=49, appMessageType=19, server="9002",
        )
        await self.b.handle_event({
            "cursor": "2", "kind": "message.updated",
            "data": {"messageId": "big_long", "threadId": "wxid_bob", "origin": "live", "direction": "incoming"},
        })
        self.assertEqual(len(self.b.bot.edited), 1)
        self.assertIn("AAAA", first.text)
        self.assertEqual(len(self.b.bot.sent), sent_before + 1)
        followup = self.b.bot.sent[-1]
        self.assertIn("BBBB", followup.text)
        self.assertEqual(followup.kwargs.get("reply_to"), first.id)

        # Replay of the same message.updated (Telegram returns MessageNotModifiedError) must NOT duplicate overflow chunks
        from telethon import errors as tg_errors

        async def not_modified(*args, **kw):
            raise tg_errors.MessageNotModifiedError(request=None)

        self.b.bot.edit_message = not_modified
        await self.b.handle_event({
            "cursor": "2", "kind": "message.updated",
            "data": {"messageId": "big_long", "threadId": "wxid_bob", "origin": "live", "direction": "incoming"},
        })
        self.assertEqual(len(self.b.bot.sent), sent_before + 1)

    async def test_message_updated_for_unforwarded_message_is_ignored(self):
        self.daemon.add_thread("wxid_bob", "Bob")
        self.daemon.add_message("unseen", "wxid_bob", "Expanded text", type_=49, appMessageType=19)
        await self.b.handle_event({
            "cursor": "1", "kind": "message.updated",
            "data": {"messageId": "unseen", "threadId": "wxid_bob", "origin": "live", "direction": "incoming"},
        })
        self.assertEqual(self.b.bot.sent, [])
        self.assertEqual(self.b.bot.edited, [])


