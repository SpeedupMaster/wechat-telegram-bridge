"""Tests for WeChat video calling support (CallWeb + Calls Protocol v3 + Telegram commands)."""
from __future__ import annotations

import asyncio
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from aiohttp import WSMsgType
import httpx

from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.core import Bridge
from bridge.wechat import (
    WXCM_KIND_H264,
    WXCM_KIND_JPEG,
    WXCM_KIND_PCM_S16LE,
    decode_wxcm_frame,
)
from tests.test_bridge import FakeBot, FakeUser, _FakeCallsSocket, _button_texts, _button_urls, tg_event
from tests.test_groups import GroupDaemon
from tests.test_v3_compat import _FakeV3MediaSocket


class Wxv1BrowserCodecTests(unittest.TestCase):
    def test_wxv1_pack_unpack_roundtrip_and_rejects_audio_pcm(self):
        from bridge.callweb import WXV1_MAGIC, decode_wxv1_frame, encode_wxv1_frame

        payload = b"\x00\x00\x00\x01\x67\x42\x00\x1e"
        raw = encode_wxv1_frame(
            track_or_gen=65538,
            kind=WXCM_KIND_H264,
            flags=0x05,  # keyframe + 90 deg CW rotation
            timestamp_micros=123_456_789,
            payload=payload,
        )
        self.assertTrue(raw.startswith(WXV1_MAGIC))
        self.assertEqual(len(raw), 20 + len(payload))

        parsed = decode_wxv1_frame(raw)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["kind"], WXCM_KIND_H264)
        self.assertEqual(parsed["flags"], 0x05)
        self.assertEqual(parsed["id"], 65538)
        self.assertEqual(parsed["timestampMicros"], 123_456_789)
        self.assertEqual(parsed["payload"], payload)

        # Ordinary PCM audio bytes must NOT parse as WXV1 video
        pcm = struct.pack("<320h", *([0] * 320))
        self.assertIsNone(decode_wxv1_frame(pcm))
        # Truncated or empty-payload WXV1 must be rejected
        self.assertIsNone(decode_wxv1_frame(raw[:20]))
        self.assertIsNone(decode_wxv1_frame(raw[:10]))


class CallWebVideoTests(unittest.IsolatedAsyncioTestCase):
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
        self.daemon = GroupDaemon()
        self.b = Bridge(cfg)
        await self.b.wx.http.aclose()
        self.b.wx.http = httpx.AsyncClient(
            transport=httpx.MockTransport(self.daemon.handler),
            headers={"Authorization": "Bearer secret-token"},
        )
        self.b.user, self.b.bot = FakeUser(), FakeBot()
        self.b.bot_input = object()
        self.b.bot_me = SimpleNamespace(username="mybot")
        self.b.tg_ready.set()
        self.b.wx_ready = True
        self.b.owner_id = 999
        await self.b.wx.resolve_account()
        self.sock = _FakeCallsSocket()
        self.b._calls_sock = self.sock
        self.media = _FakeV3MediaSocket()
        self.ready_payload: dict = {
            "type": "media.ready",
            "protocolVersion": 3,
            "sendTracks": [
                {"media": "audio", "id": 1, "generation": 1, "direction": "send",
                 "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}},
                {"media": "video", "id": 65537, "generation": 1, "direction": "send", "active": True,
                 "format": {"codec": "h264", "mode": "passthrough", "maxWidth": 640, "maxHeight": 480, "maxFps": 15,
                            "bitrateKbps": 600}},
            ],
            "receiveTracks": [
                {"media": "audio", "id": 2, "generation": 1, "direction": "receive",
                 "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}},
                {"media": "video", "id": 65538, "generation": 1, "direction": "receive",
                 "participantId": "wxid_alice", "active": True,
                 "format": {"codec": "jpeg", "mode": "transcoded", "maxWidth": 640, "maxHeight": 480, "maxFps": 15}},
            ],
        }

        async def call_media(call_id, client_id, owner_token, timeout=10.0):
            return self.media, self.ready_payload

        self.b.wx.call_media = call_media
        await self.b.call_web.start()
        self.http = aiohttp.ClientSession()
        self.base = f"http://127.0.0.1:{self.b.call_web.bound_port}/wxcall"
        await self.b.handle_call_event({
            "type": "hello", "protocolVersion": 3, "clientId": "ctl-v3", "conferenceInvitations": [],
            "capabilities": {
                "directAudio": True, "directVideo": True,
                "conferenceCreate": True, "conferenceJoin": True, "conferenceInvite": True,
                "conferenceAudio": True, "conferenceVideo": True,
            },
            "audioFormat": {"sampleRate": 16000, "channels": 1, "frameMs": 20},
            "videoCapabilities": {
                "sendCodecs": ["h265", "h264", "jpeg"],
                "receiveCodecs": ["h265", "h264", "jpeg"],
                "maxWidth": 640, "maxHeight": 480, "maxFps": 15,
            },
        })

    async def asyncTearDown(self):
        await self.http.close()
        await self.b.close()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def _page(self, token: str, video_caps: dict | None = None):
        ws = await self.http.ws_connect(self.base + "/ws")
        hello: dict = {"type": "auth", "token": token, "initData": ""}
        if video_caps is not None:
            hello["videoCaps"] = video_caps
        await ws.send_json(hello)
        return ws

    async def _next_json(self, ws, mtype: str = "state", pred=lambda m: True, timeout: float = 2.0) -> dict:
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
            if msg.type != WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            if m.get("type") == mtype and pred(m):
                return m

    async def _next_bin(self, ws, timeout: float = 2.0) -> bytes:
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
            if msg.type == WSMsgType.BINARY:
                return msg.data

    async def _until(self, cond, timeout: float = 2.0):
        for _ in range(int(timeout / 0.01)):
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition not met")

    async def test_answer_direct_video_call_relays_video_camera_toggle_and_keyframes(self):
        from bridge.callweb import decode_wxv1_frame, encode_wxv1_frame

        self.daemon.add_thread("wxid_alice", "Alice")

        async def on_cmd(cmd):
            if cmd["type"] == "call.ready" and cmd.get("ready"):
                async def offered():
                    await asyncio.sleep(0.01)
                    await self.b.handle_call_event({"type": "call.offered", "offer": {
                        "callId": "vid-1", "revision": 2, "kind": "direct", "callType": "video",
                        "callerId": "wxid_alice"}})
                asyncio.create_task(offered())
            elif cmd["type"] == "call.answer":
                async def connected():
                    await asyncio.sleep(0.01)
                    await self.b.handle_call_event({"type": "call.state", "call": {
                        "callId": "vid-1", "revision": 3, "kind": "direct", "callType": "video",
                        "direction": "incoming", "state": "connected", "ownedByThisClient": True}})
                asyncio.create_task(connected())
                return {"type": "command.result", "status": "accepted", "callId": "vid-1", "ownerToken": "own-vid"}
            return None

        self.sock.on_command = on_cmd
        await self.b.handle_call_event({"type": "call.pending", "offer": {
            "callId": "vid-1", "revision": 1, "kind": "direct", "callType": "video",
            "callerId": "wxid_alice", "phase": "waiting_for_ready"}})

        msg = self.b.bot.sent[-1]
        self.assertEqual(_button_texts(msg), ["📹 接听", "📵 拒接"])
        token = _button_urls(msg)[0].rsplit("/", 1)[1]

        browser_vcaps = {
            "sendCodecs": ["h264", "jpeg"],
            "receiveCodecs": ["h264", "jpeg"],
            "maxWidth": 640, "maxHeight": 480, "maxFps": 15,
        }
        ws = await self._page(token, video_caps=browser_vcaps)
        try:
            st0 = await self._next_json(ws)
            self.assertEqual((st0["phase"], st0["answerable"], st0["videoCall"]), ("ringing", True, True))

            await ws.send_json({"type": "answer", "cameraEnabled": True, "videoCaps": browser_vcaps})
            st = await self._next_json(ws, pred=lambda m: m.get("phase") == "active")
            self.assertTrue(st["camera"])
            self.assertEqual(st["videoSend"]["id"], 65537)
            self.assertEqual(st["videoSend"]["codec"], "h264")
            self.assertEqual(len(st["videoTracks"]), 1)
            self.assertEqual(st["videoTracks"][0]["id"], 65538)
            self.assertEqual(st["videoTracks"][0]["codec"], "jpeg")

            # call.ready carried video capabilities; call.answer carried cameraEnabled=True
            ready_cmd = next(c for c in self.sock.commands if c["type"] == "call.ready")
            self.assertEqual(ready_cmd["video"], browser_vcaps)
            ans_cmd = next(c for c in self.sock.commands if c["type"] == "call.answer")
            self.assertIs(ans_cmd["cameraEnabled"], True)

            # Both audio and video send tracks received track.start
            self.assertIn({"type": "track.start", "trackId": 1, "generation": 1}, self.media.sent_json)
            self.assertIn({"type": "track.start", "trackId": 65537, "generation": 1}, self.media.sent_json)

            # Browser -> daemon: WXV1 H.264 frame (keyframe + 90° CW rotation flag 0x05) becomes WXCM v3
            nal = b"\x00\x00\x00\x01\x65\x88\x84\x21"
            await ws.send_bytes(encode_wxv1_frame(1, WXCM_KIND_H264, 0x05, 40_000, nal))
            await self._until(lambda: len(self.media.sent_bytes) >= 1)
            wxcm_tx = decode_wxcm_frame(self.media.sent_bytes[0])
            assert wxcm_tx is not None
            self.assertEqual(
                (wxcm_tx["kind"], wxcm_tx["flags"], wxcm_tx["trackId"], wxcm_tx["generation"], wxcm_tx["payload"]),
                (WXCM_KIND_H264, 0x05, 65537, 1, nal),
            )

            # Daemon -> browser: incoming WXCM video.frame relayed as WXV1 binary frame preserving rotation
            jpg = b"\xff\xd8\xff\xe0TEST\xff\xd9"
            self.media.queue.put_nowait({
                "type": "video.frame",
                "kind": WXCM_KIND_JPEG,
                "flags": 0x05,
                "trackId": 65538,
                "generation": 1,
                "sequence": 1,
                "timestampMicros": 66_000,
                "payload": jpg,
            })
            rx_raw = await self._next_bin(ws)
            rx_vid = decode_wxv1_frame(rx_raw)
            assert rx_vid is not None
            self.assertEqual(
                (rx_vid["id"], rx_vid["kind"], rx_vid["flags"], rx_vid["timestampMicros"], rx_vid["payload"]),
                (65538, WXCM_KIND_JPEG, 0x05, 66_000, jpg),
            )

            # Keyframe request: browser -> daemon, and daemon -> browser
            await ws.send_json({"type": "request_keyframe", "trackId": 65538})
            await self._until(lambda: any(m.get("type") == "video.request_keyframe" for m in self.media.sent_json))
            self.assertIn({"type": "video.request_keyframe", "trackId": 65538}, self.media.sent_json)

            self.media.queue.put_nowait({
                "type": "video.keyframe_requested", "callId": "vid-1", "trackId": 65537, "generation": 1,
            })
            kf = await self._next_json(ws, mtype="keyframe_requested")
            self.assertEqual(kf["generation"], 1)

            # Toggle camera off -> call.camera(enabled=False), video frames from page dropped
            await ws.send_json({"type": "camera", "enabled": False})
            st_off = await self._next_json(ws, pred=lambda m: m.get("camera") is False)
            self.assertFalse(st_off["camera"])
            self.assertEqual(self.sock.commands[-1], {"type": "call.camera", "callId": "vid-1", "enabled": False})
            tx_count = len(self.media.sent_bytes)
            await ws.send_bytes(encode_wxv1_frame(1, WXCM_KIND_H264, 0x05, 80_000, nal))
            await asyncio.sleep(0.05)
            self.assertEqual(len(self.media.sent_bytes), tx_count)

            # Re-enable camera + daemon advances video send generation -> sends track.start(gen=2)
            await ws.send_json({"type": "camera", "enabled": True})
            await self._next_json(ws, pred=lambda m: m.get("camera") is True)
            await self.b.handle_call_event({"type": "track.updated", "callId": "vid-1", "track": {
                "media": "video", "id": 65537, "generation": 2, "direction": "send", "active": True,
                "format": {"codec": "h264", "mode": "passthrough", "maxWidth": 640, "maxHeight": 480, "maxFps": 15},
            }})
            self.assertIn({"type": "track.start", "trackId": 65537, "generation": 2}, self.media.sent_json)
            await ws.send_bytes(encode_wxv1_frame(2, WXCM_KIND_H264, 0x05, 120_000, nal))
            await self._until(lambda: len(self.media.sent_bytes) > tx_count)
            wxcm_tx2 = decode_wxcm_frame(self.media.sent_bytes[-1])
            assert wxcm_tx2 is not None
            self.assertEqual(wxcm_tx2["generation"], 2)
        finally:
            await ws.close()

    async def test_answer_video_call_with_camera_disabled_fallback(self):
        self.daemon.add_thread("wxid_alice", "Alice")
        self.ready_payload = {
            "type": "media.ready",
            "protocolVersion": 3,
            "sendTracks": [
                {"media": "audio", "id": 1, "generation": 1, "direction": "send",
                 "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}},
                {"media": "video", "id": 65537, "generation": 1, "direction": "send", "active": False,
                 "format": {"codec": "jpeg", "mode": "transcoded", "maxWidth": 640, "maxHeight": 480, "maxFps": 15}},
            ],
            "receiveTracks": [
                {"media": "audio", "id": 2, "generation": 1, "direction": "receive",
                 "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}},
            ],
        }

        async def on_cmd(cmd):
            if cmd["type"] == "call.answer":
                async def connected():
                    await asyncio.sleep(0.01)
                    await self.b.handle_call_event({"type": "call.state", "call": {
                        "callId": "vid-nocam", "revision": 3, "kind": "direct", "callType": "video",
                        "direction": "incoming", "state": "connected", "ownedByThisClient": True}})
                asyncio.create_task(connected())
                return {"type": "command.result", "status": "accepted", "callId": "vid-nocam", "ownerToken": "own-nc"}
            return None

        self.sock.on_command = on_cmd
        await self.b.handle_call_event({"type": "call.offered", "offer": {
            "callId": "vid-nocam", "revision": 2, "kind": "direct", "callType": "video", "callerId": "wxid_alice"}})
        token = _button_urls(self.b.bot.sent[-1])[0].rsplit("/", 1)[1]
        ws = await self._page(token)
        try:
            await self._next_json(ws)
            await ws.send_json({"type": "answer", "cameraEnabled": False,
                                "videoCaps": {"sendCodecs": ["jpeg"], "receiveCodecs": ["jpeg"],
                                              "maxWidth": 640, "maxHeight": 480, "maxFps": 15}})
            st = await self._next_json(ws, pred=lambda m: m.get("phase") == "active")
            self.assertFalse(st["camera"])
            ans_cmd = next(c for c in self.sock.commands if c["type"] == "call.answer")
            self.assertIs(ans_cmd["cameraEnabled"], False)
            # Audio send track started; inactive video send track NOT started
            self.assertIn({"type": "track.start", "trackId": 1, "generation": 1}, self.media.sent_json)
            self.assertNotIn({"type": "track.start", "trackId": 65537, "generation": 1}, self.media.sent_json)
        finally:
            await ws.close()

    async def test_conference_dynamic_video_tracks_and_participant_updates(self):
        room = "room@chatroom"
        self.daemon.add_group(room, "Team")
        self.daemon.group_members[room] = [
            {"id": "wxid_a", "displayName": "Alice"},
            {"id": "wxid_b", "displayName": "Bob"},
        ]
        self.ready_payload = {
            "type": "media.ready",
            "protocolVersion": 3,
            "sendTracks": [
                {"media": "audio", "id": 1, "generation": 1, "direction": "send",
                 "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}},
                {"media": "video", "id": 65537, "generation": 1, "direction": "send", "active": True,
                 "format": {"codec": "jpeg", "mode": "transcoded", "maxWidth": 640, "maxHeight": 480, "maxFps": 15}},
            ],
            "receiveTracks": [
                {"media": "audio", "id": 2, "generation": 1, "direction": "receive",
                 "participantId": "wxid_a", "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}},
            ],
        }

        async def on_cmd(cmd):
            if cmd["type"] == "call.join":
                await self.b.handle_call_event({"type": "call.state", "call": {
                    "callId": "conf-v", "revision": 2, "kind": "conference", "callType": "video",
                    "direction": "outgoing", "state": "connected", "ownedByThisClient": True,
                    "groupId": room, "participants": [
                        {"participantId": "p-a", "contactId": "wxid_a", "membership": "joined", "cameraEnabled": True},
                    ]}})
                return {"type": "command.result", "status": "accepted", "callId": "conf-v", "ownerToken": "own-cv"}
            return None

        self.sock.on_command = on_cmd
        await self.b.handle_call_event({"type": "conference.invitations", "invitations": [
            {"invitationId": "inv-v", "revision": 1, "groupId": room, "callerId": "wxid_a"}]})
        token = _button_urls(self.b.bot.sent[-1])[0].rsplit("/", 1)[1]
        ws = await self._page(token)
        try:
            st0 = await self._next_json(ws)
            self.assertEqual((st0["phase"], st0["joinable"], st0["videoEnabled"]), ("invite", True, True))

            await ws.send_json({"type": "join", "cameraEnabled": True,
                                "videoCaps": {"sendCodecs": ["jpeg"], "receiveCodecs": ["jpeg"],
                                              "maxWidth": 640, "maxHeight": 480, "maxFps": 15}})
            st = await self._next_json(ws, pred=lambda m: m.get("phase") == "active")
            self.assertTrue(st["camera"])
            join_cmd = next(c for c in self.sock.commands if c["type"] == "call.join")
            self.assertIs(join_cmd["cameraEnabled"], True)

            # Remote participant turns on video -> track.added on media socket updates videoTracks
            self.media.queue.put_nowait({
                "type": "track.added",
                "callId": "conf-v",
                "track": {
                    "media": "video", "id": 65539, "generation": 1, "direction": "receive",
                    "participantId": "p-a", "active": True,
                    "format": {"codec": "jpeg", "mode": "transcoded", "maxWidth": 640, "maxHeight": 480, "maxFps": 15},
                },
            })
            st_added = await self._next_json(ws, pred=lambda m: any(t["id"] == 65539 for t in m.get("videoTracks") or []))
            t_a = next(t for t in st_added["videoTracks"] if t["id"] == 65539)
            self.assertEqual(t_a["name"], "Alice")

            # Remote participant leaves -> track.removed drops track and sends video_drop
            self.media.queue.put_nowait({"type": "track.removed", "callId": "conf-v", "trackId": 65539})
            drop = await self._next_json(ws, mtype="video_drop")
            self.assertEqual(drop["trackId"], 65539)
        finally:
            await ws.close()

    async def test_dial_direct_and_group_video_via_call_and_vcall(self):
        self.daemon.add_thread("wxid_alice", "Alice")
        chat = await self.b.ensure_chat(self.daemon.threads["wxid_alice"])
        assert chat is not None

        # /call on v3 daemon with directVideo shows both voice and video buttons
        await self.b.on_tg_message(tg_event(chat, 20, "/call"))
        card = self.b.bot.sent[-1]
        self.assertEqual(_button_texts(card), ["📞 语音呼叫", "📹 视频呼叫", "✖️ 取消"])
        video_url = _button_urls(card)[1]
        video_token = video_url.rsplit("/", 1)[1]

        async def on_cmd(cmd):
            if cmd["type"] == "call.dial":
                async def connected():
                    await asyncio.sleep(0.01)
                    await self.b.handle_call_event({"type": "call.state", "call": {
                        "callId": "out-vid-1", "revision": 2, "kind": "direct", "callType": cmd["callType"],
                        "direction": "outgoing", "state": "connected", "ownedByThisClient": True}})
                asyncio.create_task(connected())
                return {"type": "command.result", "status": "accepted", "callId": "out-vid-1", "ownerToken": "own-ov"}
            return None

        self.sock.on_command = on_cmd
        ws = await self._page(video_token)
        try:
            st0 = await self._next_json(ws)
            self.assertEqual((st0["phase"], st0["dialable"], st0["videoCall"]), ("dial", True, True))
            await ws.send_json({"type": "dial", "cameraEnabled": True,
                                "videoCaps": {"sendCodecs": ["h264", "jpeg"], "receiveCodecs": ["h264", "jpeg"],
                                              "maxWidth": 640, "maxHeight": 480, "maxFps": 15}})
            st = await self._next_json(ws, pred=lambda m: m.get("phase") == "active")
            self.assertTrue(st["videoCall"])
            dial_cmd = next(c for c in self.sock.commands if c["type"] == "call.dial")
            self.assertEqual(dial_cmd["callType"], "video")
            self.assertIs(dial_cmd["cameraEnabled"], True)
            self.assertIn("📹 <b>微信视频通话</b>", card.text)
        finally:
            await ws.close()

        # /vcall shortcut creates a direct video call card directly
        await self.b.on_tg_message(tg_event(chat, 21, "/vcall"))
        vcard = self.b.bot.sent[-1]
        self.assertIn("📹 <b>微信视频通话</b>", vcard.text)
        self.assertEqual(_button_texts(vcard), ["📹 开始视频呼叫", "✖️ 取消"])

        # /vcall when directVideo capability is absent reports an informative upgrade hint
        self.b._calls_caps["directVideo"] = False
        await self.b.on_tg_message(tg_event(chat, 22, "/vcall"))
        self.assertIn("视频通话", self.b.bot.sent[-1].text)
        self.assertIn("3.0", self.b.bot.sent[-1].text)

    async def _press(self, msg, label: str, user: int = 999):
        from tests.test_bridge import _CallbackEvent

        data = next(
            d for row in msg.kwargs["buttons"] for b in row
            if label in b.text
            and (d := getattr(getattr(b, "type", None), "data", None) or getattr(b, "data", None))
        )
        ev = _CallbackEvent(user, data)
        await self.b.on_bot_callback(ev)
        return ev

    async def test_group_vcall_and_call_invite_video_title_and_control_events(self):
        room = "vroom@chatroom"
        self.daemon.add_group(room, "VideoTeam")
        self.daemon.group_members[room] = [
            {"id": "wxid_a", "displayName": "Alice"},
            {"id": "wxid_b", "displayName": "Bob"},
        ]
        chat = await self.b.ensure_chat(self.daemon.threads[room])
        assert chat is not None

        # /vcall in a group opens the group video picker directly
        await self.b.on_tg_message(tg_event(chat, 30, "/vcall"))
        picker = self.b.bot.sent[-1]
        self.assertIn("📹 <b>微信群视频通话</b>", picker.text)

        # Select Alice by pressing the real Telegram inline button
        req = next(iter(self.b._dial_requests.values()))
        await self._press(picker, "Alice")
        self.assertIn("📹 开始视频呼叫（1 人）", _button_texts(picker))

        async def on_cmd(cmd):
            if cmd["type"] == "call.dial":
                async def connected():
                    await asyncio.sleep(0.01)
                    await self.b.handle_call_event({"type": "call.state", "call": {
                        "callId": "gvid-1", "revision": 2, "kind": "conference", "callType": "video",
                        "direction": "outgoing", "state": "connected", "ownedByThisClient": True,
                        "groupId": room, "participants": [
                            {"participantId": "p-a", "contactId": "wxid_a", "membership": "joined", "cameraEnabled": True},
                        ]}})
                asyncio.create_task(connected())
                return {"type": "command.result", "status": "accepted", "callId": "gvid-1", "ownerToken": "own-gv"}
            return None

        self.sock.on_command = on_cmd
        vtoken = self.b.call_web.token_for_dial(req, video=True)
        ws = await self._page(vtoken)
        try:
            await self._next_json(ws)
            await ws.send_json({"type": "dial", "cameraEnabled": True,
                                "videoCaps": {"sendCodecs": ["jpeg"], "receiveCodecs": ["jpeg"],
                                              "maxWidth": 640, "maxHeight": 480, "maxFps": 15}})
            st = await self._next_json(ws, pred=lambda m: m.get("phase") == "active")
            self.assertTrue(st["group"])
            self.assertTrue(st["videoCall"])

            # Inviting more members into an active group VIDEO call uses the video invite title
            await self._press(picker, "邀请更多人")
            inv_msg = self.b.bot.sent[-1]
            self.assertIn("➕ <b>邀请加入群视频通话</b>", inv_msg.text)

            # call.control (upstream CallControl schema) updates remoteCamera; participant.updated updates camera state
            await self.b.handle_call_event({
                "type": "call.control", "callId": "gvid-1", "participantId": "wxid_a",
                "control": {"kind": "camera", "enabled": False},
            })
            st_ctrl = await self._next_json(ws, pred=lambda m: m.get("remoteCamera") is False)
            self.assertIs(st_ctrl["remoteCamera"], False)

            await self.b.handle_call_event({
                "type": "participant.updated", "callId": "gvid-1",
                "participant": {"participantId": "p-a", "contactId": "wxid_a", "membership": "joined",
                                "cameraEnabled": False, "microphoneEnabled": True},
            })
            st_part = await self._next_json(
                ws, pred=lambda m: any(p.get("camera") is False for p in m.get("participants") or []),
            )
            self.assertEqual(st_part["participants"][0]["name"], "Alice")
            self.assertIs(st_part["participants"][0]["camera"], False)
            self.assertIs(st_part["participants"][0]["muted"], False)
        finally:
            await ws.close()

    async def test_v2_daemon_never_receives_v3_video_fields(self):
        self.daemon.add_thread("wxid_alice", "Alice")
        chat = await self.b.ensure_chat(self.daemon.threads["wxid_alice"])
        assert chat is not None
        await self.b.handle_call_event({
            "type": "hello", "protocolVersion": 2, "clientId": "ctl-v2", "conferenceInvitations": [],
            "capabilities": {
                "directAudio": True, "conferenceCreate": True, "conferenceJoin": True, "conferenceAudio": True,
            },
            "audioFormat": {"sampleRate": 16000, "channels": 1, "frameMs": 20},
        })
        self.ready_payload = {
            "type": "media.ready",
            "sendTracks": [{"id": 1, "generation": 1, "direction": "send",
                            "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}}],
            "receiveTracks": [{"id": 2, "generation": 1, "direction": "receive",
                               "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}}],
        }
        # Legacy v2 /call shows single voice button
        await self.b.on_tg_message(tg_event(chat, 40, "/call"))
        card = self.b.bot.sent[-1]
        self.assertEqual(_button_texts(card), ["📞 开始呼叫", "✖️ 取消"])
        token = _button_urls(card)[0].rsplit("/", 1)[1]

        async def on_cmd(cmd):
            if cmd["type"] == "call.dial":
                async def connected():
                    await asyncio.sleep(0.01)
                    await self.b.handle_call_event({"type": "call.state", "call": {
                        "callId": "v2-dial", "revision": 2, "kind": "direct", "callType": "voice",
                        "direction": "outgoing", "state": "connected", "ownedByThisClient": True}})
                asyncio.create_task(connected())
                return {"type": "command.result", "status": "accepted", "callId": "v2-dial", "ownerToken": "own-v2"}
            return None

        self.sock.on_command = on_cmd
        ws = await self._page(token, video_caps={"sendCodecs": ["jpeg"], "receiveCodecs": ["jpeg"]})
        try:
            await self._next_json(ws)
            await ws.send_json({"type": "dial", "cameraEnabled": False,
                                "videoCaps": {"sendCodecs": ["jpeg"], "receiveCodecs": ["jpeg"]}})
            await self._next_json(ws, pred=lambda m: m.get("phase") == "active")
            ready_cmd = next(c for c in self.sock.commands if c["type"] == "call.ready")
            dial_cmd = next(c for c in self.sock.commands if c["type"] == "call.dial")
            self.assertNotIn("video", ready_cmd)
            self.assertNotIn("cameraEnabled", dial_cmd)
        finally:
            await ws.close()

    async def test_attach_device_switch_honors_camera_denied_fallback(self):
        self.daemon.add_thread("wxid_alice", "Alice")

        async def on_cmd(cmd):
            if cmd["type"] == "call.answer":
                async def connected():
                    await asyncio.sleep(0.01)
                    await self.b.handle_call_event({"type": "call.state", "call": {
                        "callId": "vid-sw", "revision": 3, "kind": "direct", "callType": "video",
                        "direction": "incoming", "state": "connected", "ownedByThisClient": True}})
                asyncio.create_task(connected())
                return {"type": "command.result", "status": "accepted", "callId": "vid-sw", "ownerToken": "own-sw"}
            return None

        self.sock.on_command = on_cmd
        await self.b.handle_call_event({"type": "call.offered", "offer": {
            "callId": "vid-sw", "revision": 2, "kind": "direct", "callType": "video", "callerId": "wxid_alice"}})
        token = _button_urls(self.b.bot.sent[-1])[0].rsplit("/", 1)[1]
        ws1 = await self._page(token)
        ws2 = await self._page(token)
        try:
            await self._next_json(ws1)
            await self._next_json(ws2)
            await ws1.send_json({"type": "answer", "cameraEnabled": True,
                                 "videoCaps": {"sendCodecs": ["jpeg"], "receiveCodecs": ["jpeg"]}})
            st1 = await self._next_json(ws1, pred=lambda m: m.get("phase") == "active")
            self.assertTrue(st1["mine"] and st1["camera"])

            # Device B attaches with camera denied (cameraEnabled=False) -> daemon receives call.camera(False)
            await ws2.send_json({"type": "attach", "cameraEnabled": False})
            st2 = await self._next_json(ws2, pred=lambda m: m.get("mine") is True and m.get("camera") is False)
            self.assertFalse(st2["camera"])
            self.assertIn({"type": "call.camera", "callId": "vid-sw", "enabled": False}, self.sock.commands)
        finally:
            await ws1.close()
            await ws2.close()

    async def test_video_page_serves_stage_pip_and_wxv1_media_engine(self):
        async with self.http.get(self.base + "/call/sometoken") as r:
            self.assertEqual(r.status, 200)
            body = await r.text()
        for marker in (
            'id="stage"',
            'id="pip"',
            'id="selfVideo"',
            'avc1.42001e',
            'image/jpeg',
            'request_keyframe',
            '摄像头已开',
            '摄像头已关',
            '麦克风已开',
            '翻转镜头',
            '对方已关闭摄像头',
            "$('pip').onclick = flipCamera",
            'wsOpen',
            'main.video-live .hint{position:fixed',
        ):
            self.assertIn(marker, body)
        # Disconnected WS must not duplicate status+hint text
        self.assertNotIn("hint('连接已断开，正在重连…'", body)
        # iOS Safari requires startAudio() (AudioContext creation) synchronously before awaiting videoCapsReady
        wm = body[body.index("async function withMedia"):body.index("async function toggleCamera")]
        self.assertLess(wm.index("startAudio("), wm.index("await videoCapsReady"))

