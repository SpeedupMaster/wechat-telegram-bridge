"""Core bridge tests: forwarding, avatars, quotes, recall, streams, config, retries, and media streaming.

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
from tests.fakes import (  # noqa: F401 (re-exported for backwards compatibility)
    ACC,
    AVATAR,
    FORUM,
    OA_FORUM,
    OWNER,
    BridgeTestMixin,
    FakeBot,
    FakeDaemon,
    FakeForumBot,
    FakeUser,
    LoginDaemon,
    Sent,
    _CallbackEvent,
    _FakeCallsSocket,
    _FakeMediaSocket,
    _button_texts,
    _button_urls,
    _init_data,
    _png,
    _webp,
    _wxgf_sample,
    forum_event,
    tg_event,
)


class BridgeTests(BridgeTestMixin, unittest.IsolatedAsyncioTestCase):
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
        self.b.cfg.bridge.bridge_official_accounts = False
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
            download_media=lambda file=None: asyncio.sleep(0, result=_webp()),
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
            download_media=lambda file=None: asyncio.sleep(0, result=_webp()),
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

    async def test_voice_note_attaches_duration_and_5bit_waveform_metadata(self):
        import io
        import math
        import struct
        import wave
        from unittest.mock import patch
        from telethon import utils as tg_utils
        from bridge.formatting import wav_voice_meta

        # Build a 2.6s 16kHz 16-bit mono WAV with loud speech in the middle and silence at the edges
        rate = 16000
        nframes = int(rate * 2.6)
        samples = [
            int(22000 * math.sin(2 * math.pi * 440 * i / rate)) if (rate * 0.5 <= i < rate * 2.0) else 0
            for i in range(nframes)
        ]
        pcm = struct.pack(f"<{nframes}h", *samples)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(pcm)
        speech_wav = buf.getvalue()

        meta_mem = wav_voice_meta(speech_wav, bars=63)
        self.assertIsNotNone(meta_mem)
        assert meta_mem is not None
        dur, wf_bytes = meta_mem
        self.assertEqual(dur, 3)
        decoded = tg_utils.decode_waveform(wf_bytes)
        self.assertGreaterEqual(len(decoded), 63)
        self.assertEqual(decoded[0], 0)
        self.assertEqual(max(decoded), 31)

        # File-backed WAV produces identical metadata without loading full file via data
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            wav_path = Path(tmp) / "voice.wav"
            wav_path.write_bytes(speech_wav)
            self.assertEqual(wav_voice_meta(path=str(wav_path), bars=63), meta_mem)

        # Malformed WAV returns None cleanly
        self.assertIsNone(wav_voice_meta(b"not-a-wav"))

        # Forwarding a WeChat voice message attaches DocumentAttributeAudio(voice=True, duration, waveform)
        self.daemon.add_thread("wxid_bob", "Bob")
        self.b.store.link("wxid_bob", -1001, "Bob")
        self.daemon.add_message("vo_meta", "wxid_bob", "[Voice]", type_=34)
        self.daemon.media["vo_meta"] = [
            {"id": "vom-1", "kind": "voice", "variant": "normal", "actualVariant": "normal",
             "state": "available", "contentType": "audio/wav", "byteLength": len(speech_wav)},
        ]
        self.daemon.media_bytes["vom-1"] = (speech_wav, "audio/wav")

        async def fake_wav_to_ogg(data=b"", *, path=None, timeout=60.0):
            return b"OggS-fake-opus"

        with patch("bridge.core.wav_to_ogg", side_effect=fake_wav_to_ogg):
            await self.event("vo_meta", "wxid_bob")

        sent = self.b.bot.sent[-1]
        self.assertTrue(sent.kwargs["voice_note"])
        attrs = sent.kwargs.get("attributes")
        self.assertEqual(len(attrs), 1)
        self.assertIsInstance(attrs[0], types.DocumentAttributeAudio)
        self.assertTrue(attrs[0].voice)
        self.assertEqual(attrs[0].duration, 3)
        self.assertEqual(attrs[0].waveform, wf_bytes)

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
        self.assertTrue(img.endswith("[图片，无法获取：企业微信发来的图片/文件需 agent-wechat 3.0 及以上版本才能下载，请升级或先在手机微信查看]"), img)
        self.assertTrue(f.endswith("[文件「报价单.xlsx」，无法获取：企业微信发来的图片/文件需 agent-wechat 3.0 及以上版本才能下载，请升级或先在手机微信查看]"), f)
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

        # Newly covered official WeChat emojis (e.g. [社会社会]), system pat notices, and quote truncation boundaries
        self.daemon.add_message("m3", "wxid_alice", "有的，最近我们找供应商把材料价格压了，正在核算新价格[社会社会]")
        await self.event("m3", "wxid_alice")
        self.assertEqual(self.b.bot.sent[-1].text, "有的，最近我们找供应商把材料价格压了，正在核算新价格🫡")

        self.daemon.add_message("m4", "wxid_alice", "Alice 拍了拍我[社会社会]", type=10000)
        await self.event("m4", "wxid_alice")
        self.assertEqual(self.b.bot.sent[-1].text, "<i>Alice 拍了拍我🫡</i>")

        # Quote preview truncates after emoji conversion so [社会社会] straddling index 200 isn't chopped in half
        long_quoted = "a" * 198 + "[社会社会]尾"
        self.daemon.add_message("m5", "wxid_alice", "同意", type=49, appMessageType=57,
                                quote={"senderName": "Alice", "embeddedText": long_quoted})
        await self.event("m5", "wxid_alice")
        self.assertIn("<blockquote>Alice：" + "a" * 198 + "🫡尾</blockquote>", self.b.bot.sent[-1].text)
        self.assertNotIn("[社会", self.b.bot.sent[-1].text)

    async def test_tg_dice_interactive_emojis_sent_as_text(self):
        from bridge.core import _describe_tg_msg, _tg_msg_fingerprint

        self.daemon.add_thread("wxid_alice", "Alice")
        self.daemon.add_message("m0", "wxid_alice", "hello", server="100")
        await self.event("m0", "wxid_alice")
        chat = self.b.store.chat_for_thread("wxid_alice")
        orig_tg_id = self.b.bot.sent[-1].id

        async def _fail_download(*args, **kwargs):
            return None

        def dice_event(msg_id, emoticon, value, reply_to=None):
            media = types.MessageMediaDice(value=value, emoticon=emoticon)
            msg = SimpleNamespace(
                id=msg_id, message="", action=None, via_bot_id=None, media=media,
                file=None, sticker=None, photo=None, voice=None, video=None, video_note=None, gif=None,
                download_media=_fail_download,
                reply_to=SimpleNamespace(reply_to_msg_id=reply_to) if reply_to else None,
            )
            return SimpleNamespace(message=msg, chat_id=chat)

        bot_before = len(self.b.bot.sent)
        cases = [
            (201, "🎲", 4, "🎲 4"),
            (202, "🎯", 6, "🎯"),
            (203, "🏀", 5, "🏀"),
            (204, "⚽", 3, "⚽"),
            (205, "🎳", 6, "🎳"),
            (206, "🎰", 64, "🎰"),
        ]
        for mid, emo, val, want in cases:
            ev = dice_event(mid, emo, val)
            self.assertEqual(_describe_tg_msg(ev.message), f"「{emo}」")
            await self.b.on_tg_message(ev)
            self.assertEqual(self.daemon.sends[-1][0]["text"], want)
            self.assertIsNotNone(self.b.store.wx_for_tg(chat, mid))

        # No error report posted to Telegram
        self.assertEqual(len(self.b.bot.sent), bot_before)

        # Distinct dice values produce distinct fingerprints for user/bot pairing
        self.assertNotEqual(
            _tg_msg_fingerprint(dice_event(207, "🎲", 1).message),
            _tg_msg_fingerprint(dice_event(208, "🎲", 6).message),
        )

        # Replying to a WeChat message with a dice emoji forwards it as a quoted text reply
        self.b.store.map_message(chat, "user", orig_tg_id, "wxid_alice", "m0", "100")
        await self.b.on_tg_message(dice_event(209, "🎲", 2, reply_to=orig_tg_id))
        last_body = self.daemon.sends[-1][0]
        self.assertEqual(last_body["text"], "🎲 2")
        self.assertEqual(last_body["quote"], {"messageId": "m0", "serverId": "100"})

    async def test_tg_emoji_to_wechat_optional_reverse_mapping(self):
        from bridge.config import BridgeConfig, load_config

        # 1) Config & env parsing (defaults to False)
        base_env = {"TG_API_ID": "1", "TG_API_HASH": "h", "TG_BOT_TOKEN": "b", "WECHAT_TOKEN": "t"}
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp) / "missing.toml", base_env)
            self.assertFalse(cfg.bridge.emoji_to_wechat)
            cfg = load_config(Path(tmp) / "missing.toml", {**base_env, "EMOJI_TO_WECHAT": "true"})
            self.assertTrue(cfg.bridge.emoji_to_wechat)
            with self.assertRaisesRegex(ValueError, "EMOJI_TO_WECHAT"):
                load_config(Path(tmp) / "missing.toml", {**base_env, "EMOJI_TO_WECHAT": "maybe"})
            toml_path = Path(tmp) / "config.toml"
            toml_path.write_text("[bridge]\nemoji_to_wechat = true\n", encoding="utf-8")
            self.assertTrue(load_config(toml_path, base_env).bridge.emoji_to_wechat)
            self.assertFalse(load_config(toml_path, {**base_env, "EMOJI_TO_WECHAT": "0"}).bridge.emoji_to_wechat)
            with self.assertRaisesRegex(ValueError, "emoji_to_wechat must be true or false"):
                BridgeConfig(emoji_to_wechat="true")  # type: ignore[arg-type]

        # 2) Direct chat: off by default, converts when enabled (text + media caption)
        self.daemon.add_thread("wxid_alice", "Alice")
        self.daemon.add_message("m0", "wxid_alice", "hi", server="100")
        await self.event("m0", "wxid_alice")
        chat = self.b.store.chat_for_thread("wxid_alice")

        self.assertFalse(self.b.cfg.bridge.emoji_to_wechat)
        await self.b.on_tg_message(tg_event(chat, 301, "好的😁🤦‍♂️🫡"))
        self.assertEqual(self.daemon.sends[-1][0]["text"], "好的😁🤦‍♂️🫡")

        self.b.cfg.bridge.emoji_to_wechat = True
        await self.b.on_tg_message(tg_event(chat, 302, "好的😁🤦‍♂️🫡❤️‍🔥"))
        self.assertEqual(self.daemon.sends[-1][0]["text"], "好的[呲牙][捂脸][社会社会]❤️‍🔥")

        async def _dl_photo(file=None):
            return b"\xff\xd8\xff\xe0jpg"

        photo_msg = SimpleNamespace(
            id=303, message="拍得好👍🏻❤️", action=None, via_bot_id=None,
            media=object(), photo=object(), sticker=None, voice=None, video=None, video_note=None, gif=None,
            file=SimpleNamespace(size=10, mime_type="image/jpeg", ext=".jpg", name="a.jpg"),
            download_media=_dl_photo, reply_to=None,
        )
        await self.b.on_tg_message(SimpleNamespace(message=photo_msg, chat_id=chat))
        self.assertEqual(self.daemon.sends[-1][0]["text"], "拍得好[强][爱心]")

        # 3) Group @mention still resolves member names containing Unicode emoji before emoji conversion
        room = "room_emoji@chatroom"
        self.daemon.add_thread(room, "Emoji Room", kind="group")
        self.daemon.group_members[room] = [{"id": "wxid_bob", "displayName": "Bob🐶"}]
        self.daemon.add_message("gm0", room, "yo", sender="wxid_bob", server="200")
        await self.event("gm0", room)
        gchat = self.b.store.chat_for_thread(room)

        await self.b.on_tg_message(tg_event(gchat, 304, "@Bob🐶 来看下😁"))
        sent_body = self.daemon.sends[-1][0]
        self.assertEqual(sent_body.get("mentions"), {"all": False, "userIds": ["wxid_bob"]})
        self.assertEqual(sent_body["text"], "@Bob🐶 来看下[呲牙]")

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
        self.b.wx._recall_timeout = 0.02
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

    async def test_latest_cursor_uses_newest_when_cursor_zero_expired(self):
        seen_params: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            seen_params.append(params)
            if params.get("newest") == "true":
                self.assertNotIn("after", params)
                return httpx.Response(200, json={"items": [{"cursor": "10500"}], "nextCursor": "10500"})
            return httpx.Response(410, json={"error": {"code": "event_cursor_expired", "retryable": False}})

        wx = WeChatClient("http://daemon", "secret-token", ACC, read_retry_delays=())
        await wx.http.aclose()
        wx.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            self.assertEqual(await wx.latest_cursor(), "10500")
            self.assertEqual(seen_params, [{"newest": "true", "limit": "1"}])
        finally:
            await wx.close()

        # Empty event log returns "0"
        wx2 = WeChatClient("http://daemon", "secret-token", ACC, read_retry_delays=())
        await wx2.http.aclose()
        wx2.http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"items": [], "nextCursor": "0"})))
        try:
            self.assertEqual(await wx2.latest_cursor(), "0")
        finally:
            await wx2.close()

    async def test_stream_raises_cursor_expired_on_http_410_and_preserves_error_codes(self):
        from bridge.wechat import WeChatError

        wx = WeChatClient("http://daemon", "secret-token", ACC)
        await wx.http.aclose()
        wx.http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(410, json={"error": {"code": "event_cursor_expired", "retryable": False}})))
        try:
            with self.assertRaises(CursorExpired):
                async for _ in wx.stream("5"):
                    pass
        finally:
            await wx.close()

        wx2 = WeChatClient("http://daemon", "secret-token", ACC)
        await wx2.http.aclose()
        wx2.http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(401, json={"error": {"code": "unauthorized", "retryable": False}})))
        try:
            with self.assertRaises(WeChatError) as ctx:
                async for _ in wx2.stream("5"):
                    pass
            self.assertEqual((ctx.exception.status, ctx.exception.code), (401, "unauthorized"))
        finally:
            await wx2.close()

    async def test_event_loop_recovers_from_http_410_cursor_expired_using_newest(self):
        from unittest.mock import patch

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(WeChatConfig(token="secret-token", account=ACC),
                     TelegramConfig(api_id=1, api_hash="x", bot_token="y"),
                     BridgeConfig(), Path(tmp.name) / "config.toml")
        b = Bridge(cfg, user_client=None, bot_client=None)
        b.store.set("cursor", "5")
        handled: list[dict] = []
        done = asyncio.Event()

        async def fake_handle(ev: dict):
            handled.append(ev)
            done.set()

        b.handle_event = fake_handle
        stream_cursors: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path.removeprefix("/v1/")
            params = dict(request.url.params)
            if path == f"accounts/{ACC}/events":
                if params.get("newest") == "true":
                    return httpx.Response(200, json={"items": [{"cursor": "10050"}], "nextCursor": "10050"})
                return httpx.Response(410, json={"error": {"code": "event_cursor_expired", "retryable": False}})
            if path == f"accounts/{ACC}/events/stream":
                after = params.get("after", "0")
                stream_cursors.append(after)
                if after == "5":
                    return httpx.Response(410, json={"error": {"code": "event_cursor_expired", "retryable": False}})
                body = (
                    "id: 10051\ndata: {\"kind\":\"message.committed\",\"data\":{\"messageId\":\"m1\"}}\n\n"
                    if after == "10050" else ": keepalive\n\n"
                )
                return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
            if path.startswith(f"accounts/{ACC}/consumers/"):
                return httpx.Response(200, json={})
            raise AssertionError(f"unexpected {request.method} {path}")

        await b.wx.http.aclose()
        b.wx.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        orig_sleep = asyncio.sleep

        async def fast_sleep(_delay: float):
            await orig_sleep(0)

        try:
            with patch("bridge.core.asyncio.sleep", side_effect=fast_sleep):
                task = asyncio.create_task(b.event_loop())
                await asyncio.wait_for(done.wait(), timeout=2.0)
                await orig_sleep(0.01)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(stream_cursors[:2], ["5", "10050"])
            self.assertEqual([e["cursor"] for e in handled], ["10051"])
            self.assertEqual(b.store.get("cursor"), "10051")
        finally:
            await b.close()
            b.store.db.close()
            tmp.cleanup()

    async def test_event_loop_retries_transient_error_during_initial_latest_cursor(self):
        from unittest.mock import patch

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(WeChatConfig(token="secret-token", account=ACC),
                     TelegramConfig(api_id=1, api_hash="x", bot_token="y"),
                     BridgeConfig(), Path(tmp.name) / "config.toml")
        b = Bridge(cfg, user_client=None, bot_client=None)
        b.wx._read_retry_delays = ()
        done = asyncio.Event()
        events_calls = 0
        stream_cursors: list[str] = []

        async def fake_handle(_ev: dict):
            done.set()

        b.handle_event = fake_handle

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal events_calls
            path = request.url.path.removeprefix("/v1/")
            if path == f"accounts/{ACC}/events":
                events_calls += 1
                if events_calls == 1:
                    return httpx.Response(503, json={"error": {"code": "unavailable", "retryable": True}})
                return httpx.Response(200, json={"items": [{"cursor": "200"}], "nextCursor": "200"})
            if path == f"accounts/{ACC}/events/stream":
                after = request.url.params.get("after", "0")
                stream_cursors.append(after)
                body = (
                    "id: 201\ndata: {\"kind\":\"message.committed\",\"data\":{\"messageId\":\"m1\"}}\n\n"
                    if after == "200" else ": keepalive\n\n"
                )
                return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
            if path.startswith(f"accounts/{ACC}/consumers/"):
                return httpx.Response(200, json={})
            raise AssertionError(f"unexpected {request.method} {path}")

        await b.wx.http.aclose()
        b.wx.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        orig_sleep = asyncio.sleep

        async def fast_sleep(_delay: float):
            await orig_sleep(0)

        try:
            with patch("bridge.core.asyncio.sleep", side_effect=fast_sleep):
                task = asyncio.create_task(b.event_loop())
                await asyncio.wait_for(done.wait(), timeout=2.0)
                await orig_sleep(0.01)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(events_calls, 2)
            self.assertEqual(stream_cursors[0], "200")
            self.assertEqual(b.store.get("cursor"), "201")
        finally:
            await b.close()
            b.store.db.close()
            tmp.cleanup()


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
            self.assertTrue(cfg.bridge.bridge_official_accounts)
            cfg = load_config(
                Path(tmp) / "missing.toml",
                {**env, "BRIDGE_OFFICIAL_ACCOUNTS": "false", "TG_OA_FORUM_CHAT": "-1008888"},
            )
            self.assertEqual((cfg.bridge.bridge_official_accounts, cfg.bridge.official_accounts_forum_chat),
                             (False, -1008888))
            with self.assertRaisesRegex(ValueError, "TG_OA_FORUM_CHAT"):
                load_config(Path(tmp) / "missing.toml", {**env, "TG_OA_FORUM_CHAT": "not-a-number"})
            with self.assertRaisesRegex(ValueError, "BRIDGE_OFFICIAL_ACCOUNTS"):
                load_config(Path(tmp) / "missing.toml", {**env, "BRIDGE_OFFICIAL_ACCOUNTS": "nope"})


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
