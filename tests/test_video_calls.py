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
    decode_wxcm_frame,
)
from tests.fakes import FakeBot, FakeUser, _FakeCallsSocket, _button_texts, _button_urls, tg_event
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
        from tests.fakes import _CallbackEvent

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
            self.assertIn("speaker-selection=(self)", r.headers.get("Permissions-Policy", ""))
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
            '扬声器已开',
            '扬声器已关',
            '翻转镜头',
            '对方已关闭摄像头',
            "$('pip').onclick = flipCamera",
            'wsOpen',
            'main.video-live .hint{position:fixed',
            '.pip video{width:100%;height:100%;object-fit:contain',
            'syncPipLayout',
        ):
            self.assertIn(marker, body)
        self.assertNotIn('.pip video{width:100%;height:100%;object-fit:cover', body)
        # Disconnected WS must not duplicate status+hint text
        self.assertNotIn("hint('连接已断开，正在重连…'", body)
        # iOS Safari requires startAudio() (AudioContext creation) synchronously before awaiting videoCapsReady
        wm = body[body.index("async function withMedia"):body.index("async function toggleCamera")]
        self.assertLess(wm.index("startAudio("), wm.index("await videoCapsReady"))

    async def test_video_backpressure_drops_pframes_and_recovers_on_keyframe(self):
        from bridge.callweb import VIDEO_WS_MAX_BUFFER, decode_wxv1_frame, encode_wxv1_frame

        self.daemon.add_thread("wxid_alice", "Alice")

        async def on_cmd(cmd):
            if cmd["type"] == "call.answer":
                async def connected():
                    await asyncio.sleep(0.01)
                    await self.b.handle_call_event({"type": "call.state", "call": {
                        "callId": "vid-bp", "revision": 3, "kind": "direct", "callType": "video",
                        "direction": "incoming", "state": "connected", "ownedByThisClient": True}})
                asyncio.create_task(connected())
                return {"type": "command.result", "status": "accepted", "callId": "vid-bp", "ownerToken": "own-bp"}
            return None

        self.sock.on_command = on_cmd
        self.ready_payload["receiveTracks"][1]["format"]["codec"] = "h264"
        await self.b.handle_call_event({"type": "call.offered", "offer": {
            "callId": "vid-bp", "revision": 2, "kind": "direct", "callType": "video", "callerId": "wxid_alice"}})
        token = _button_urls(self.b.bot.sent[-1])[0].rsplit("/", 1)[1]
        ws = await self._page(token)
        try:
            await self._next_json(ws)
            await ws.send_json({"type": "answer", "cameraEnabled": True,
                                "videoCaps": {"sendCodecs": ["h264", "jpeg"], "receiveCodecs": ["h264", "jpeg"]}})
            await self._next_json(ws, pred=lambda m: m.get("phase") == "active")
            sess = self.b.call_web.sessions["vid-bp"]
            assert sess.active_ws is not None

            # Simulate browser socket write buffer congestion (> VIDEO_WS_MAX_BUFFER)
            fake_buf = VIDEO_WS_MAX_BUFFER + 1024
            orig_req = sess.active_ws._req
            sess.active_ws._req = SimpleNamespace(
                transport=SimpleNamespace(get_write_buffer_size=lambda: fake_buf)
            )
            try:
                # Congested H.264 frame is dropped AND triggers video.request_keyframe
                self.media.queue.put_nowait({
                    "type": "video.frame", "kind": WXCM_KIND_H264, "flags": 0x04,  # P-frame
                    "trackId": 65538, "generation": 1, "sequence": 1, "timestampMicros": 20_000,
                    "payload": b"\x00\x00\x00\x01\x41drop",
                })
                await self._until(lambda: any(m.get("type") == "video.request_keyframe" for m in self.media.sent_json))

                # Buffer drains, but delta P-frame is STILL dropped until an IDR keyframe arrives
                fake_buf = 0
                self.media.queue.put_nowait({
                    "type": "video.frame", "kind": WXCM_KIND_H264, "flags": 0x04,  # delta P-frame
                    "trackId": 65538, "generation": 1, "sequence": 2, "timestampMicros": 40_000,
                    "payload": b"\x00\x00\x00\x01\x41stale_p",
                })
                self.media.queue.put_nowait({
                    "type": "video.frame", "kind": WXCM_KIND_H264, "flags": 0x05,  # IDR keyframe + rot 90
                    "trackId": 65538, "generation": 1, "sequence": 3, "timestampMicros": 60_000,
                    "payload": b"\x00\x00\x00\x01\x65clean_idr",
                })
                rx = decode_wxv1_frame(await self._next_bin(ws))
                assert rx is not None
                self.assertEqual(rx["payload"], b"\x00\x00\x00\x01\x65clean_idr")
            finally:
                sess.active_ws._req = orig_req

            # Uplink daemon media socket backpressure drops video frame and pushes keyframe_requested to browser
            self.media.write_buffer_size = lambda: VIDEO_WS_MAX_BUFFER * 3
            tx_before = len(self.media.sent_bytes)
            await ws.send_bytes(encode_wxv1_frame(1, WXCM_KIND_H264, 0x04, 100_000, b"\x00\x00\x00\x01\x41up"))
            kf_msg = await self._next_json(ws, mtype="keyframe_requested")
            self.assertEqual(kf_msg["trackId"], 65537)
            self.assertEqual(len(self.media.sent_bytes), tx_before)
        finally:
            await ws.close()

    async def test_remote_muted_late_participant_name_refresh_and_video_invite_titles(self):
        room = "joinroom@chatroom"
        self.daemon.add_group(room, "JoinTeam")
        self.daemon.group_members[room] = [
            {"id": "wxid_a", "displayName": "Alice"},
            {"id": "wxid_b", "displayName": "Bob"},
        ]
        self.daemon.add_contact("wxid_b", "Bob", remark="老鲍", nickname="Bob")
        self.b._self_wxid = "wxid_me"

        # 1) Video conference invitation uses video invite title; unspecified v3 invite uses generic group call title
        await self.b.handle_call_event({"type": "conference.invitations", "invitations": [
            {"invitationId": "inv-vid", "revision": 1, "groupId": room, "callerId": "wxid_a", "callType": "video"},
        ]})
        inv_card = self.b.bot.sent[-1]
        self.assertIn("📹 <b>微信群视频通话邀请</b>", inv_card.text)

        self.ready_payload = {
            "type": "media.ready",
            "protocolVersion": 3,
            "sendTracks": [
                {"media": "audio", "id": 1, "generation": 1, "direction": "send",
                 "format": {"sampleRate": 16000, "channels": 1, "frameMs": 20}},
            ],
            "receiveTracks": [
                # Arrives with opaque participantId "p-b" BEFORE call.state / participant.updated maps p-b -> wxid_b!
                {"media": "video", "id": 65540, "generation": 1, "direction": "receive",
                 "participantId": "p-b", "active": True,
                 "format": {"codec": "h264", "mode": "passthrough", "maxWidth": 640, "maxHeight": 480, "maxFps": 15}},
            ],
        }

        async def on_cmd(cmd):
            if cmd["type"] == "call.join":
                async def delayed_state():
                    await asyncio.sleep(0.02)
                    await self.b.handle_call_event({"type": "call.state", "call": {
                        "callId": "conf-late", "revision": 2, "kind": "conference", "callType": "video",
                        "direction": "outgoing", "state": "connected", "ownedByThisClient": True,
                        "groupId": room, "participants": [
                            {"participantId": "p-me", "contactId": "wxid_me", "membership": "joined"},
                            {"participantId": "p-b", "contactId": "wxid_b", "membership": "joined",
                             "cameraEnabled": True, "microphoneEnabled": True},
                        ]}})
                asyncio.create_task(delayed_state())
                return {"type": "command.result", "status": "accepted", "callId": "conf-late", "ownerToken": "own-l"}
            return None

        self.sock.on_command = on_cmd
        token = _button_urls(inv_card)[0].rsplit("/", 1)[1]
        ws = await self._page(token)
        try:
            await self._next_json(ws)
            await ws.send_json({"type": "join", "cameraEnabled": False})
            # Once call.state arrives, both videoTracks[0].name and participants resolve p-b -> wxid_b -> remark "老鲍"
            st = await self._next_json(
                ws,
                pred=lambda m: m.get("phase") == "active"
                and any(t.get("name") == "老鲍" for t in m.get("videoTracks") or []),
            )
            bob_part = next(p for p in st["participants"] if p["contactId"] == "wxid_b")
            me_part = next(p for p in st["participants"] if p["contactId"] == "wxid_me")
            self.assertEqual(bob_part["name"], "老鲍")
            self.assertFalse(bob_part["self"])
            self.assertTrue(me_part["self"])

            # Self microphone control is ignored for remoteMuted; remote participant mute updates remoteMuted & participant
            await self.b.handle_call_event({
                "type": "call.control", "callId": "conf-late", "participantId": "wxid_me",
                "control": {"kind": "microphone", "muted": True},
            })
            self.assertIsNone(self.b._active_calls["conf-late"].get("remote_muted"))

            await self.b.handle_call_event({
                "type": "call.control", "callId": "conf-late", "participantId": "p-b",
                "control": {"kind": "microphone", "muted": True},
            })
            st_m = await self._next_json(ws, pred=lambda m: m.get("remoteMuted") is True)
            bob_after = next(p for p in st_m["participants"] if p["contactId"] == "wxid_b")
            self.assertIs(bob_after["muted"], True)

            # Attaching a second browser socket requests an IDR keyframe for the active H.264 receive track
            self.media.sent_json.clear()
            ws2 = await self._page(token)
            try:
                await self._next_json(ws2)
                await ws2.send_json({"type": "attach"})
                await self._next_json(ws2, pred=lambda m: m.get("mine") is True)
                self.assertIn({"type": "video.request_keyframe", "trackId": 65540}, self.media.sent_json)
            finally:
                await ws2.close()
        finally:
            await ws.close()


class BrowserAudioAndVideoJsTests(unittest.TestCase):
    def test_worklet_upsampling_downsampling_tx_gate_and_smooth_jitter_buffer(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")

        worklet_path = (Path(__file__).resolve().parent.parent / "bridge" / "web" / "worklet.js").as_posix()
        script = f"""
const fs = require('fs');
const code = fs.readFileSync({json.dumps(worklet_path)}, 'utf8');

function makeEnv(sr) {{
  let Cls = null;
  const fn = new Function('sampleRate', 'AudioWorkletProcessor', 'registerProcessor', code);
  class BaseProc {{
    constructor() {{
      this.emitted = [];
      this.port = {{
        onmessage: null,
        postMessage: (buf) => this.emitted.push(new Int16Array(buf)),
      }};
    }}
  }}
  fn(sr, BaseProc, (name, c) => {{ Cls = c; }});
  return Cls;
}}

// 1) 8 kHz Bluetooth HFP -> 16 kHz upsampling emits exact 2x sample count with linear interpolation and no NaN
const Proc8k = makeEnv(8000);
const p8 = new Proc8k({{processorOptions: {{net: 16000, frame: 320}}}});
const outCh8 = new Float32Array(128);
for (let block = 0; block < 10; block++) {{
  const inp = new Float32Array(80);
  for (let i = 0; i < 80; i++) inp[i] = ((block * 80 + i) / 800) * 0.8;
  p8.process([[inp]], [[outCh8]]);
}}
if (p8.emitted.length !== 5) throw new Error('Expected 5 frames (1600 samples), got ' + p8.emitted.length);
for (const fr of p8.emitted) {{
  for (let i = 0; i < fr.length; i++) {{
    if (Number.isNaN(fr[i])) throw new Error('NaN in upsampled frame');
  }}
}}
if (!(p8.emitted[4][319] > 20000)) throw new Error('Unexpected tail sample: ' + p8.emitted[4][319]);

// 2) tx=false gate suppresses mic frame allocations/messages while muted or before active
const Proc48k = makeEnv(48000);
const p48 = new Proc48k({{processorOptions: {{net: 16000, frame: 320}}}});
p48.port.onmessage({{data: {{t: 'tx', on: false}}}});
for (let k = 0; k < 20; k++) p48.process([[new Float32Array(128).fill(0.5)]], [[new Float32Array(128)]]);
if (p48.emitted.length !== 0) throw new Error('Expected 0 frames while tx=false');

// 3) Jitter buffer: >160ms drifts down smoothly, and underrun decays smoothly without hard jump to 0
const rxPcm = new Int16Array(4800).fill(16384); // 300ms @ 16kHz (> 160ms soft threshold)
p48.port.onmessage({{data: {{t: 'pcm', id: 2, pcm: rxPcm}}}});
const playBuf = new Float32Array(128);
p48.process([[]], [[playBuf]]);
const tr = p48.tracks.get(2);
const expectedNominalR = 128 * (16000 / 48000);
if (!(tr.r > expectedNominalR * 1.01)) throw new Error('Expected drift catch-up > 1.01x, got ' + tr.r);

// Drain until underrun and verify smooth exponential release (non-zero decay sample instead of hard 0 step)
while (tr.w - tr.r >= 2) p48.process([[]], [[playBuf]]);
const tailBuf = new Float32Array(128);
tr.last = 0.5;
tr.on = true;
tr.r = tr.w; // force immediate underrun at sample 0
p48.process([[]], [[tailBuf]]);
if (!(tailBuf[0] > 0.35 && tailBuf[0] < 0.5 && tailBuf[20] < 0.05)) {{
  throw new Error('Expected smooth decay on underrun, got ' + tailBuf[0] + ', ' + tailBuf[20]);
}}

// 4) Residual Echo Suppression: far-end speakerphone bleed (& acoustic tail) is attenuated, while near-end double-talk opens immediately
const pAec = new Proc48k({{processorOptions: {{net: 16000, frame: 320}}}});
const farSpeech = new Int16Array(4800).fill(18000); // ~0.55 amplitude far-end speech (300ms @ 16kHz)
pAec.port.onmessage({{data: {{t: 'pcm', id: 9, pcm: farSpeech}}}});
const echoBleed = new Float32Array(128).fill(0.04); // weak speaker bleed (< dtThresh)
for (let k = 0; k < 20; k++) pAec.process([[echoBleed]], [[new Float32Array(128)]]);
if (pAec.emitted.length < 2) throw new Error('Expected >=2 mic frames during echo suppression, got ' + pAec.emitted.length);
const suppressedPeak = Math.max(...Array.from(pAec.emitted[pAec.emitted.length - 1], (x) => Math.abs(x)));
// Without suppression 0.04 * 32767 = 1310; steady-state after ~3ms click-free ramp (~0.16x) must be < 400
if (!(suppressedPeak < 400)) throw new Error('Expected speakerphone bleed < 400, got ' + suppressedPeak);

// Acoustic tail hold: right after far-end track drops, ~140ms decay envelope still suppresses room reverb bleed
pAec.port.onmessage({{data: {{t: 'drop', id: 9}}}});
pAec.emitted.length = 0;
for (let k = 0; k < 10; k++) pAec.process([[echoBleed]], [[new Float32Array(128)]]);
if (!pAec.emitted.length) throw new Error('Expected frame during acoustic tail');
const tailBleedPeak = Math.max(...Array.from(pAec.emitted[0], (x) => Math.abs(x)));
if (!(tailBleedPeak < 400)) throw new Error('Expected reverb tail bleed < 400, got ' + tailBleedPeak);

// Now local user interrupts loudly (double-talk = 0.5 amplitude): micGain opens right back up
pAec.emitted.length = 0;
pAec.port.onmessage({{data: {{t: 'pcm', id: 9, pcm: farSpeech}}}});
const doubleTalk = new Float32Array(128).fill(0.5);
for (let k = 0; k < 20; k++) pAec.process([[doubleTalk]], [[new Float32Array(128)]]);
const dtPeak = Math.max(...Array.from(pAec.emitted[pAec.emitted.length - 1], (x) => Math.abs(x)));
if (!(dtPeak > 15000)) throw new Error('Expected double-talk speech preserved (>15000), got ' + dtPeak);

// Hands-free speaker=true mode applies even stronger bleed attenuation while preserving double-talk
const pSpk = new Proc48k({{processorOptions: {{net: 16000, frame: 320}}}});
pSpk.port.onmessage({{data: {{t: 'speaker', on: true}}}});
pSpk.port.onmessage({{data: {{t: 'pcm', id: 9, pcm: farSpeech}}}});
for (let k = 0; k < 20; k++) pSpk.process([[echoBleed]], [[new Float32Array(128)]]);
const spkSuppressedPeak = Math.max(...Array.from(pSpk.emitted[pSpk.emitted.length - 1], (x) => Math.abs(x)));
if (!(spkSuppressedPeak < suppressedPeak)) {{
  throw new Error('Expected speaker=true stronger suppression (' + spkSuppressedPeak + ' vs ' + suppressedPeak + ')');
}}

console.log('WORKLET_OK');
"""
        res = subprocess.run([node, "-"], input=script, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        self.assertIn("WORKLET_OK", res.stdout)

    def test_call_html_reconnect_reattach_portrait_h264_and_ui_states_in_node(self):
        import re
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")

        web_dir = Path(__file__).resolve().parent.parent / "bridge" / "web"
        html = (web_dir / "call.html").read_text(encoding="utf-8")
        worklet = (web_dir / "worklet.js").read_text(encoding="utf-8")
        rendered = (
            html.replace("__NONCE__", "testnonce")
            .replace("__CFG__", json.dumps({"base": "/wxcall"}))
            .replace("__WORKLET__", json.dumps(worklet))
        )
        scripts = re.findall(r'<script nonce="testnonce">([\s\S]*?)</script>', rendered)
        self.assertEqual(len(scripts), 2)
        main_js = scripts[1]

        harness = f"""
const vm = require('vm');
const timers = [];
let nextTimerId = 1;
function makeEl(id) {{
  return {{
    id: id || '', className: '', textContent: '', _html: '', hidden: false, disabled: false,
    get innerHTML() {{ return this._html; }},
    set innerHTML(v) {{ this._html = v; if (!v) this.children = []; }},
    style: {{}}, children: [], listeners: {{}},
    videoWidth: 480, videoHeight: 640, readyState: 4, srcObject: null,
    classList: {{
      _set: new Set(),
      add(c) {{ this._set.add(c); }},
      remove(c) {{ this._set.delete(c); }},
      toggle(c, on) {{ if (on === undefined ? !this._set.has(c) : on) this._set.add(c); else this._set.delete(c); }},
      contains(c) {{ return this._set.has(c); }},
    }},
    appendChild(ch) {{ ch.parentNode = this; this.children.push(ch); return ch; }},
    removeChild(ch) {{ this.children = this.children.filter((x) => x !== ch); }},
    addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }},
    play() {{ return Promise.resolve(); }},
    getBoundingClientRect() {{ return {{left: 200, top: 70, width: 100, height: 133}}; }},
    getContext() {{
      return {{
        save() {{}}, restore() {{}}, setTransform() {{}}, translate() {{}}, rotate() {{}}, drawImage() {{}},
      }};
    }},
  }};
}}
const els = {{}};
['stage', 'pip', 'selfVideo', 'main', 'avatar', 'title', 'name', 'status', 'timer', 'peers', 'hint', 'actions'].forEach((id) => {{
  els[id] = makeEl(id);
}});
els.stage.hidden = true; els.pip.hidden = true; els.peers.hidden = true;

const sockets = [];
class FakeWS {{
  constructor(url) {{
    this.url = url; this.readyState = 0; this.bufferedAmount = 0; this.sent = [];
    sockets.push(this);
  }}
  send(d) {{ this.sent.push(d); }}
  close() {{ this.readyState = 3; if (this.onclose) this.onclose(); }}
}}

let camStops = 0, oscCount = 0;
const encoderConfigs = [], encodedKeyFlags = [];
class FakeVideoEncoder {{
  static isConfigSupported(cfg) {{ return Promise.resolve({{supported: cfg.codec === 'avc1.42001e'}}); }}
  constructor(init) {{ this._out = init.output; this.state = 'unconfigured'; this.encodeQueueSize = 0; }}
  configure(cfg) {{ encoderConfigs.push(cfg); this.state = 'configured'; }}
  encode(vf, opts) {{
    encodedKeyFlags.push(!!(opts && opts.keyFrame));
    const payload = new Uint8Array([0, 0, 0, 1, opts && opts.keyFrame ? 0x65 : 0x41]);
    this._out({{
      type: opts && opts.keyFrame ? 'key' : 'delta',
      timestamp: vf.timestamp || 1000,
      byteLength: payload.byteLength,
      copyTo: (dst) => dst.set(payload),
    }});
  }}
  close() {{ this.state = 'closed'; }}
}}
class FakeVideoFrame {{
  constructor(src, opts) {{ this.timestamp = (opts && opts.timestamp) || 0; }}
  close() {{}}
}}

const workletMsgs = [];
let aecAudioPlayed = 0, aecDisconnectedDest = 0, aecClosedCount = 0, lastAnswerSdp = '';
class FakeRTCPeerConnection {{
  constructor() {{
    this.localDescription = null; this.remoteDescription = null;
    this.onicecandidate = null; this.ontrack = null; this.iceConnectionState = 'connected';
    FakeRTCPeerConnection.peers.push(this);
  }}
  addTrack(tr, stream) {{ this._track = tr; this._stream = stream; }}
  addIceCandidate() {{ return Promise.resolve(); }}
  createOffer() {{ return Promise.resolve({{type: 'offer', sdp: 'v=0\\r\\na=rtpmap:111 opus/48000/2\\r\\na=fmtp:111 minptime=10;useinbandfec=1\\r\\n'}}); }}
  createAnswer() {{ return Promise.resolve({{type: 'answer', sdp: 'v=0\\r\\na=rtpmap:111 opus/48000/2\\r\\na=fmtp:111 useinbandfec=1\\r\\n'}}); }}
  setLocalDescription(d) {{
    this.localDescription = d;
    if (d && d.type === 'answer') lastAnswerSdp = d.sdp || '';
    return Promise.resolve();
  }}
  setRemoteDescription(d) {{
    this.remoteDescription = d;
    if (d && d.type === 'offer' && this.ontrack) {{
      const peer1 = FakeRTCPeerConnection.peers[FakeRTCPeerConnection.peers.length - 2];
      Promise.resolve().then(() => this.ontrack && this.ontrack({{
        track: peer1 && peer1._track,
        streams: [{{id: 'loopback-stream'}}],
      }}));
    }}
    return Promise.resolve();
  }}
  close() {{ aecClosedCount++; this.iceConnectionState = 'closed'; }}
}}
FakeRTCPeerConnection.peers = [];

class FakeAudioContext {{
  constructor() {{
    this.state = 'running'; this.currentTime = 0; this.destination = {{isDest: true}};
    this.audioWorklet = {{ addModule: () => Promise.resolve() }};
  }}
  resume() {{ this.state = 'running'; return Promise.resolve(); }}
  close() {{ this.state = 'closed'; }}
  createMediaStreamSource() {{ return {{connect() {{}}}}; }}
  createMediaStreamDestination() {{
    return {{stream: {{getAudioTracks: () => [{{kind: 'audio'}}]}}}};
  }}
  createOscillator() {{
    oscCount++;
    return {{type: 'sine', frequency: {{value: 0}}, connect() {{}}, start() {{}}, stop() {{}}, disconnect() {{}}}};
  }}
  createGain() {{
    const g = {{
      gain: {{
        value: 1,
        setValueAtTime(v) {{ this.value = v; }},
        linearRampToValueAtTime(v) {{ this.value = v; }},
        setTargetAtTime(v) {{ this.value = v; }},
      }},
      connect() {{}},
      disconnect(target) {{ if (target && target.isDest) aecDisconnectedDest++; }},
    }};
    FakeAudioContext.gains.push(g);
    return g;
  }}
  createDynamicsCompressor() {{
    return {{
      threshold: {{value: 0}}, knee: {{value: 0}}, ratio: {{value: 1}}, attack: {{value: 0}}, release: {{value: 0}},
      connect() {{}},
      disconnect(target) {{ if (target && target.isDest) aecDisconnectedDest++; }},
    }};
  }}
}}
FakeAudioContext.gains = [];
class FakeWorkletNode {{
  constructor() {{
    this.port = {{ onmessage: null, postMessage: (m) => workletMsgs.push(m) }};
  }}
  connect() {{}}
}}

const sandbox = {{
  window: null, document: null, navigator: null, location: {{
    protocol: 'https:', host: 'call.example.com', origin: 'https://call.example.com',
    pathname: '/wxcall/call/tok123456', search: '', hash: '',
  }},
  URL: {{ createObjectURL: () => 'blob:worklet', revokeObjectURL: () => {{}} }},
  Blob: class {{}}, WebSocket: FakeWS, DataView, Uint8Array, Int16Array, ArrayBuffer, Math, JSON, Date, Number, String,
  Promise, Set, Map, Array, Object, Error, URLSearchParams, encodeURIComponent, decodeURIComponent,
  performance: {{now: () => 100}},
  VideoEncoder: FakeVideoEncoder, VideoDecoder: {{isConfigSupported: () => Promise.resolve({{supported: true}})}},
  VideoFrame: FakeVideoFrame, AudioWorkletNode: FakeWorkletNode, RTCPeerConnection: FakeRTCPeerConnection,
  setTimeout: (fn, ms) => {{ const id = nextTimerId++; timers.push({{id, fn, ms}}); return id; }},
  clearTimeout: (id) => {{ const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); }},
  setInterval: (fn, ms) => nextTimerId++,
  clearInterval: () => {{}},
}};
sandbox.window = Object.assign(sandbox, {{
  isSecureContext: true, innerWidth: 390, innerHeight: 844, AudioContext: FakeAudioContext,
}});
sandbox.window.parent = sandbox.window;
sandbox.document = {{
  body: makeEl('body'),
  getElementById: (id) => els[id] || makeEl(id),
  createElement: (tag) => {{
    const el = makeEl(tag);
    if (tag === 'audio') el.play = () => {{ aecAudioPlayed++; el.paused = false; return Promise.resolve(); }};
    el.pause = () => {{ el.paused = true; }};
    return el;
  }},
  createTextNode: (t) => ({{textContent: t}}),
  addEventListener: () => {{}},
  hidden: false,
}};
sandbox.navigator = {{
  userAgent: 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/131.0.0.0 Mobile Safari/537.36',
  mediaDevices: {{
    getUserMedia: (c) => Promise.resolve({{
      getTracks: () => [{{stop: () => {{ if (c && c.video) camStops++; }}}}],
    }}),
  }},
}};

vm.runInNewContext({json.dumps(main_js)}, sandbox);

(async () => {{
  const ws1 = sockets[0];
  ws1.readyState = 1;
  await ws1.onopen();
  await new Promise((r) => setImmediate(r));

  // Initial ringing video call state
  ws1.onmessage({{data: JSON.stringify({{
    type: 'state', phase: 'ringing', title: '微信视频来电', name: 'Alice', status: '🔔 响铃中…',
    mine: false, muted: false, camera: false, answerable: true, videoCall: true, videoEnabled: true,
    rate: 16000, frame: 320, videoTracks: [], participants: [],
  }})}});

  // Click "视频接听" -> starts both mic & camera in parallel and sends answer
  const ansBtn = els.actions.children[1];
  ansBtn.onclick();
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
  const ansPayload = JSON.parse(ws1.sent[ws1.sent.length - 1]);
  if (ansPayload.type !== 'answer' || ansPayload.cameraEnabled !== true) {{
    throw new Error('Expected answer with cameraEnabled=true, got ' + JSON.stringify(ansPayload));
  }}

  // Active video call with H.264 send track -> portrait 480x640 encodes as 640x480 with latencyMode='realtime'
  ws1.onmessage({{data: JSON.stringify({{
    type: 'state', phase: 'active', title: '微信视频通话', name: 'Alice', status: '🟢 通话中',
    mine: true, muted: false, camera: true, elapsed: 1, videoCall: true, videoEnabled: true,
    rate: 16000, frame: 320,
    videoSend: {{id: 65537, generation: 1, codec: 'h264', maxWidth: 640, maxHeight: 480, maxFps: 5, bitrateKbps: 600, active: true}},
    videoTracks: [], participants: [],
  }})}});

  // Run 12 capture ticks (at 5 fps, periodic keyframe must fire at frame 0 and frame 10 = fps * 2)
  for (let step = 0; step < 12; step++) {{
    const t = timers.find((x) => x.ms <= 250);
    if (!t) break;
    timers.splice(timers.indexOf(t), 1);
    t.fn();
  }}
  if (!encoderConfigs.length || encoderConfigs[0].width !== 640 || encoderConfigs[0].height !== 480) {{
    throw new Error('Expected portrait 480x640 rotated H.264 encoder to be 640x480, got ' + JSON.stringify(encoderConfigs[0]));
  }}
  if (encoderConfigs[0].latencyMode !== 'realtime') {{
    throw new Error('Expected latencyMode=realtime, got ' + JSON.stringify(encoderConfigs[0]));
  }}
  if (!encodedKeyFlags[0] || !encodedKeyFlags[10]) {{
    throw new Error('Expected periodic IDR keyframe at frame 0 and 10, got ' + JSON.stringify(encodedKeyFlags));
  }}

  // Uplink congestion > 48KB drops video & marks forceKeyframe so next clear frame is keyframe
  ws1.bufferedAmount = 60000;
  const tDrop = timers.find((x) => x.ms <= 250);
  timers.splice(timers.indexOf(tDrop), 1);
  tDrop.fn();
  ws1.bufferedAmount = 0;
  const tRec = timers.find((x) => x.ms <= 250);
  timers.splice(timers.indexOf(tRec), 1);
  tRec.fn();
  if (!encodedKeyFlags[encodedKeyFlags.length - 1]) {{
    throw new Error('Expected forced IDR keyframe immediately after uplink buffer drop');
  }}

  // Simulate WS drop + reconnect: camera must NOT stop, and attach must be sent automatically!
  const stopsBefore = camStops;
  ws1.close();
  if (camStops !== stopsBefore) throw new Error('Camera was erroneously stopped during WS reconnect!');
  const reconnTimer = timers.find((x) => x.ms === 300);
  timers.splice(timers.indexOf(reconnTimer), 1);
  reconnTimer.fn();
  const ws2 = sockets[1];
  ws2.readyState = 1;
  await ws2.onopen();
  await new Promise((r) => setImmediate(r));
  // Server sends initial post-reconnect state with mine=false
  ws2.onmessage({{data: JSON.stringify({{
    type: 'state', phase: 'active', title: '微信视频通话', name: 'Alice', status: '🟢 通话中',
    mine: false, muted: false, camera: true, elapsed: 3, videoCall: true, videoEnabled: true,
    rate: 16000, frame: 320, videoSend: null, videoTracks: [], participants: [],
  }})}});
  if (camStops !== stopsBefore) throw new Error('Camera was stopped before attach completed!');
  const attachMsg = JSON.parse(ws2.sent[ws2.sent.length - 1]);
  if (attachMsg.type !== 'attach' || attachMsg.cameraEnabled !== true) {{
    throw new Error('Expected automatic attach with cameraEnabled=true on reconnect, got ' + JSON.stringify(attachMsg));
  }}

  // Outgoing calling state shows PiP self-preview, triggers 450Hz ringback oscillator, and does NOT show speaker toggle before connected
  ws2.onmessage({{data: JSON.stringify({{
    type: 'state', phase: 'calling', title: '微信视频通话', name: 'Alice', status: '🔔 对方响铃中…',
    mine: true, muted: false, camera: true, videoCall: true, videoEnabled: true, rate: 16000, frame: 320,
  }})}});
  if (els.pip.hidden) throw new Error('Expected self-preview PiP to be visible during outgoing video calling');
  if (oscCount < 1) throw new Error('Expected ringback tone oscillator during outgoing calling');
  const callingLabels = els.actions.children.map((b) => b.children.map((c) => c.textContent || '').join(''));
  if (callingLabels.some((l) => l.includes('扬声器'))) {{
    throw new Error('Outgoing ringing state should not show speaker toggle before connected: ' + JSON.stringify(callingLabels));
  }}

  // 1-on-1 pure voice call on v3 daemon hides camera button, shows speakerphone toggle, and displays remoteMuted status
  ws2.onmessage({{data: JSON.stringify({{
    type: 'state', phase: 'active', title: '微信语音通话', name: 'Alice', status: '🟢 通话中',
    mine: true, muted: false, camera: false, elapsed: 5, group: false,
    videoCall: false, videoEnabled: true, videoSend: null, remoteMuted: true, videoTracks: [], participants: [],
  }})}});
  for (let i = 0; i < 4; i++) await new Promise((r) => setImmediate(r));
  const btnLabels = els.actions.children.map((b) => b.children.map((c) => c.textContent || '').join(''));
  if (btnLabels.some((l) => l.includes('摄像头'))) {{
    throw new Error('Pure 1:1 voice call should not show camera button, got ' + JSON.stringify(btnLabels));
  }}
  if (JSON.stringify(btnLabels) !== JSON.stringify(['麦克风已开', '扬声器已关', '挂断'])) {{
    throw new Error('Expected voice call buttons [麦克风已开, 扬声器已关, 挂断], got ' + JSON.stringify(btnLabels));
  }}
  const statusText = els.status.children.map((c) => c.textContent || '').join('');
  if (statusText !== '对方已静音') throw new Error('Expected 对方已静音, got ' + statusText);
  if (camStops <= stopsBefore) throw new Error('Expected camera hardware stream to be stopped when camera=false');

  // Verify Chromium RTCPeerConnection loopback AEC activated & tuned Opus SDP
  if (aecAudioPlayed < 1 || aecDisconnectedDest < 1) {{
    throw new Error('Expected Chromium RTCPeerConnection loopback AEC to route through <audio>, got played=' + aecAudioPlayed + ', disc=' + aecDisconnectedDest);
  }}
  if (!lastAnswerSdp.includes('maxaveragebitrate=64000') || !lastAnswerSdp.includes('ptime=10') || !lastAnswerSdp.includes('usedtx=0')) {{
    throw new Error('Expected Opus low-latency SDP tuning, got ' + lastAnswerSdp);
  }}

  // Toggle speakerphone ON in Android voice call -> button becomes 扬声器已开, voiceGain boosts to 1.8, worklet speaker=true, and Android routes directly to loudspeaker
  const voiceGainNode = FakeAudioContext.gains[0];
  const closedBeforeSpk = aecClosedCount;
  els.actions.children[1].onclick();
  for (let i = 0; i < 4; i++) await new Promise((r) => setImmediate(r));
  const onLabels = els.actions.children.map((b) => b.children.map((c) => c.textContent || '').join(''));
  if (onLabels[1] !== '扬声器已开' || !els.actions.children[1].className.includes(' on')) {{
    throw new Error('Expected highlighted 扬声器已开 button, got ' + onLabels[1] + ' / ' + els.actions.children[1].className);
  }}
  if (voiceGainNode.gain.value !== 1.8) {{
    throw new Error('Expected hands-free voiceGain=1.8, got ' + voiceGainNode.gain.value);
  }}
  if (!workletMsgs.some((m) => m && m.t === 'speaker' && m.on === true)) {{
    throw new Error('Expected worklet speaker=true message');
  }}
  if (aecClosedCount < closedBeforeSpk + 2) {{
    throw new Error('Expected Android speakerphone ON to exit telephony loopback to media loudspeaker');
  }}

  // Toggle speakerphone OFF -> button returns to 扬声器已关, voiceGain restores to 1, and AEC loopback re-engages
  const playedBeforeOff = aecAudioPlayed;
  els.actions.children[1].onclick();
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
  const offLabels = els.actions.children.map((b) => b.children.map((c) => c.textContent || '').join(''));
  if (offLabels[1] !== '扬声器已关' || voiceGainNode.gain.value !== 1 || aecAudioPlayed <= playedBeforeOff) {{
    throw new Error('Expected speakerphone OFF to restore 扬声器已关, gain=1, and AEC loopback');
  }}

  ws2.onmessage({{data: JSON.stringify({{
    type: 'state', phase: 'ended', title: '微信语音通话', name: 'Alice', status: '⚪ 通话已结束', mine: false,
  }})}});
  if (aecClosedCount < closedBeforeSpk + 4) throw new Error('Expected loopback RTCPeerConnections to close on call end, got ' + aecClosedCount);

  console.log('CALL_HTML_OK');
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
        res = subprocess.run([node, "-"], input=harness, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        self.assertIn("CALL_HTML_OK", res.stdout)

    def test_desktop_voice_call_speakerphone_setsinkid_and_picker_in_node(self):
        import re
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")

        web_dir = Path(__file__).resolve().parent.parent / "bridge" / "web"
        html = (web_dir / "call.html").read_text(encoding="utf-8")
        worklet = (web_dir / "worklet.js").read_text(encoding="utf-8")
        rendered = (
            html.replace("__NONCE__", "testnonce")
            .replace("__CFG__", json.dumps({"base": "/wxcall"}))
            .replace("__WORKLET__", json.dumps(worklet))
        )
        main_js = re.findall(r'<script nonce="testnonce">([\s\S]*?)</script>', rendered)[1]

        harness = f"""
const vm = require('vm');
function makeEl(id) {{
  return {{
    id: id || '', className: '', textContent: '', _html: '', hidden: false, disabled: false,
    get innerHTML() {{ return this._html; }},
    set innerHTML(v) {{ this._html = v; if (!v) this.children = []; }},
    style: {{}}, children: [], listeners: {{}},
    classList: {{ _set: new Set(), add(c) {{ this._set.add(c); }}, remove(c) {{ this._set.delete(c); }}, toggle() {{}}, contains(c) {{ return this._set.has(c); }} }},
    appendChild(ch) {{ ch.parentNode = this; this.children.push(ch); return ch; }},
    removeChild(ch) {{ this.children = this.children.filter((x) => x !== ch); }},
    addEventListener() {{}},
    play() {{ return Promise.resolve(); }},
    pause() {{}},
    getBoundingClientRect() {{ return {{left: 0, top: 0, width: 100, height: 100}}; }},
    getContext() {{ return {{}}; }},
  }};
}}
const els = {{}};
['stage', 'pip', 'selfVideo', 'main', 'avatar', 'title', 'name', 'status', 'timer', 'peers', 'hint', 'actions'].forEach((id) => {{ els[id] = makeEl(id); }});
const sockets = [];
class FakeWS {{
  constructor() {{ this.readyState = 0; this.bufferedAmount = 0; this.sent = []; sockets.push(this); }}
  send(d) {{ this.sent.push(d); }}
  close() {{ this.readyState = 3; }}
}}
const ctxSinks = [], elSinks = [];
let outputs = [
  {{kind: 'audiooutput', deviceId: 'default', label: 'Default - Headphones (USB)'}},
  {{kind: 'audiooutput', deviceId: 'hp-usb', label: 'USB Headset'}},
  {{kind: 'audiooutput', deviceId: 'spk-builtin', label: 'MacBook Pro Speakers'}},
];
let pickerCalls = 0;
class FakeAudioContext {{
  constructor() {{
    this.state = 'running'; this.currentTime = 0; this.destination = {{}};
    this.audioWorklet = {{ addModule: () => Promise.resolve() }};
  }}
  resume() {{ return Promise.resolve(); }}
  close() {{ this.state = 'closed'; }}
  setSinkId(id) {{ ctxSinks.push(id); return Promise.resolve(); }}
  createMediaStreamSource() {{ return {{connect() {{}}}}; }}
  createGain() {{ return {{gain: {{value: 1, setTargetAtTime(v) {{ this.value = v; }}}}, connect() {{}}, disconnect() {{}}}}; }}
}}
class FakeWorkletNode {{
  constructor() {{ this.port = {{ onmessage: null, postMessage() {{}} }}; }}
  connect() {{}}
}}
const sandbox = {{
  window: null, document: null, navigator: null,
  location: {{ protocol: 'https:', host: 'call.example.com', origin: 'https://call.example.com', pathname: '/wxcall/call/tok123456', search: '', hash: '' }},
  URL: {{ createObjectURL: () => 'blob:w', revokeObjectURL: () => {{}} }},
  Blob: class {{}}, WebSocket: FakeWS, DataView, Uint8Array, Int16Array, ArrayBuffer, Math, JSON, Date, Number, String,
  Promise, Set, Map, Array, Object, Error, URLSearchParams, encodeURIComponent, decodeURIComponent,
  performance: {{now: () => 0}}, AudioWorkletNode: FakeWorkletNode,
  setTimeout: () => 1, clearTimeout: () => {{}}, setInterval: () => 1, clearInterval: () => {{}},
}};
sandbox.window = Object.assign(sandbox, {{ isSecureContext: true, innerWidth: 1024, innerHeight: 768, AudioContext: FakeAudioContext }});
sandbox.window.parent = sandbox.window;
sandbox.document = {{
  body: makeEl('body'), getElementById: (id) => els[id] || makeEl(id),
  createElement: (tag) => {{
    const el = makeEl(tag);
    if (tag === 'audio') el.setSinkId = (id) => {{ elSinks.push(id); return Promise.resolve(); }};
    return el;
  }},
  createTextNode: (t) => ({{textContent: t}}), addEventListener: () => {{}}, hidden: false,
}};
sandbox.navigator = {{
  userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36',
  mediaDevices: {{
    getUserMedia: () => Promise.resolve({{ getTracks: () => [{{stop() {{}}}}] }}),
    enumerateDevices: () => Promise.resolve(outputs),
    selectAudioOutput: () => {{ pickerCalls++; return Promise.resolve({{deviceId: 'picked-ext-spk', label: 'External Speaker'}}); }},
  }},
}};
vm.runInNewContext({json.dumps(main_js)}, sandbox);

(async () => {{
  const ws = sockets[0]; ws.readyState = 1; await ws.onopen();
  await new Promise((r) => setImmediate(r));
  ws.onmessage({{data: JSON.stringify({{
    type: 'state', phase: 'ringing', title: '微信语音来电', name: 'Bob', status: '🔔 响铃中…',
    mine: false, muted: false, camera: false, answerable: true, videoCall: false, rate: 16000, frame: 320,
  }})}});
  // Ringing state must NOT show speaker button
  const ringLabels = els.actions.children.map((b) => b.children.map((c) => c.textContent || '').join(''));
  if (ringLabels.some((l) => l.includes('扬声器'))) throw new Error('Ringing should not show speaker toggle');

  // Answer voice call
  els.actions.children[1].onclick();
  for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
  ws.onmessage({{data: JSON.stringify({{
    type: 'state', phase: 'active', title: '微信语音通话', name: 'Bob', status: '🟢 通话中',
    mine: true, muted: false, camera: false, elapsed: 1, videoCall: false, rate: 16000, frame: 320,
  }})}});
  for (let i = 0; i < 4; i++) await new Promise((r) => setImmediate(r));

  // 1) Auto-switches to enumerated built-in speaker ('spk-builtin') without prompting picker
  els.actions.children[1].onclick();
  for (let i = 0; i < 4; i++) await new Promise((r) => setImmediate(r));
  if (ctxSinks[ctxSinks.length - 1] !== 'spk-builtin' || pickerCalls !== 0) {{
    throw new Error('Expected setSinkId(spk-builtin) without picker, got sinks=' + JSON.stringify(ctxSinks) + ', picker=' + pickerCalls);
  }}

  // 2) Toggling off switches right back to non-default headset ('hp-usb')
  els.actions.children[1].onclick();
  for (let i = 0; i < 4; i++) await new Promise((r) => setImmediate(r));
  if (ctxSinks[ctxSinks.length - 1] !== 'hp-usb') {{
    throw new Error('Expected setSinkId(hp-usb) on speaker OFF, got ' + JSON.stringify(ctxSinks));
  }}

  // 3) When enumerateDevices hides outputs (e.g. Firefox), tapping speaker ON invokes selectAudioOutput()
  outputs = [];
  els.actions.children[1].onclick();
  for (let i = 0; i < 4; i++) await new Promise((r) => setImmediate(r));
  if (pickerCalls !== 1 || ctxSinks[ctxSinks.length - 1] !== 'picked-ext-spk') {{
    throw new Error('Expected selectAudioOutput fallback to picked-ext-spk, got picker=' + pickerCalls + ', sinks=' + JSON.stringify(ctxSinks));
  }}

  console.log('DESKTOP_SPEAKER_OK');
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
        res = subprocess.run([node, "-"], input=harness, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)
        self.assertIn("DESKTOP_SPEAKER_OK", res.stdout)


