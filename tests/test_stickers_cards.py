"""Tests for sticker conversion/forwarding (WebP/TGS/WebM/GIF/WXGF) and link card rendering."""
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
from tests.fakes import (
    ACC,
    AVATAR,
    BridgeTestMixin,
    FakeBot,
    FakeDaemon,
    FakeUser,
    _png,
    _wxgf_sample,
    tg_event,
)


class StickerForwardingTests(BridgeTestMixin, unittest.IsolatedAsyncioTestCase):
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

    async def test_telegram_stickers_use_high_res_limits(self):
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

        # 512px static WebP, 384px animated TGS
        await self.b.on_tg_message(make_ev(8201, webp, "image/webp", ".webp"))
        self.assertEqual(extract_gif(*self.daemon.uploads[-1]).size, (512, 512))
        await self.b.on_tg_message(make_ev(8202, tgs, "application/x-tgsticker", ".tgs"))
        self.assertEqual(extract_gif(*self.daemon.uploads[-1]).size, (384, 384))

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

    def assertColor(self, got, want):
        self.assertTrue(all(abs(a - b) <= 12 for a, b in zip(got, want)), f"{got} != {want}")

    async def test_bridge_sends_sticker_as_native_gif(self):
        b = Bridge.__new__(Bridge)
        b.max_media = 50 * 1048576
        data = self.webp()

        async def download_media(file=None):
            return data
        msg = SimpleNamespace(file=SimpleNamespace(size=len(data), mime_type="image/webp", name=None, ext=".webp"),
                              sticker=True, photo=None, download_media=download_media)
        kind, media = await b._tg_media(msg)
        self.assertEqual((kind, media.mime, media.filename), ("sticker", "image/gif", "sticker.gif"))
        self.assertTrue(media.data.startswith(b"GIF8"))

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

    async def test_transparent_animation_with_full_bleed_flash_frame_preserves_transparency(self):
        import io
        from PIL import Image, ImageDraw, ImageSequence

        # Frame 0: full-bleed white flash (all alpha=255); Frame 1: transparent bg; Frame 2: full-bleed black flash;
        # Frame 3: transparent bg. Without per-frame translucency normalization, Frame 0/2 leave white/black stuck.
        f0 = Image.new("RGBA", (128, 128), (255, 255, 255, 255))
        f1 = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        ImageDraw.Draw(f1).ellipse((32, 32, 96, 96), fill=(255, 30, 30, 255))
        f2 = Image.new("RGBA", (128, 128), (0, 0, 0, 255))
        f3 = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        ImageDraw.Draw(f3).rectangle((40, 40, 88, 88), fill=(30, 120, 255, 255))

        gif_bytes = self.stickers._rgba_to_transparent_gif_pillow(
            [(f0, 60), (f1, 60), (f2, 60), (f3, 60)], max_edge=128,
        )
        # Caller's source frames must not be mutated in place
        self.assertEqual(f0.getpixel((0, 0)), (255, 255, 255, 255))
        self.assertEqual(f2.getpixel((0, 0)), (0, 0, 0, 255))

        with Image.open(io.BytesIO(gif_bytes)) as gim:
            decoded = [fr.convert("RGBA") for fr in ImageSequence.Iterator(gim)]
        self.assertEqual(len(decoded), 4)
        # Frames 1 and 3 must keep transparent backgrounds rather than inheriting white/black from Frame 0/2
        self.assertEqual(decoded[1].getpixel((10, 10))[3], 0)
        self.assertColor(decoded[1].getpixel((64, 64))[:3], (255, 30, 30))
        self.assertEqual(decoded[3].getpixel((10, 10))[3], 0)
        self.assertColor(decoded[3].getpixel((64, 64))[:3], (30, 120, 255))

        # Raw RGBA bytes path used by _ffmpeg_rgba_to_gif also marks opaque flash frames translucent
        raw_opaque = bytes([255, 255, 255, 255] * 16)
        patched_raw = self.stickers._ensure_frame_translucent(raw_opaque, 4, 4)
        self.assertTrue(self.stickers._frame_has_transparency(patched_raw))
        self.assertEqual(patched_raw[4:], raw_opaque[4:])

    async def test_webm_codec_detection_ebml_sniffing_and_vp8_support(self):
        import subprocess
        from unittest import mock

        ebml_vp8 = b"\x1a\x45\xdf\xa3" + b"\x00" * 8 + b"webm" + b"\x00" * 16 + b"\x86\x85V_VP8" + b"\x00" * 16
        ebml_vp9 = b"\x1a\x45\xdf\xa3" + b"\x00" * 8 + b"webm" + b"\x00" * 16 + b"\x86\x85V_VP9" + b"\x00" * 16
        ebml_av1 = b"\x1a\x45\xdf\xa3" + b"\x00" * 8 + b"webm" + b"\x00" * 16 + b"\x86\x85V_AV1" + b"\x00" * 16

        self.assertTrue(self.stickers._is_webm(ebml_vp8, "application/octet-stream"))
        self.assertFalse(self.stickers._is_webm(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32, "video/mp4"))
        self.assertEqual(self.stickers._webm_decoder_args(ebml_vp8), ("-c:v", "libvpx"))
        self.assertEqual(self.stickers._webm_decoder_args(ebml_vp9), ("-c:v", "libvpx-vp9"))
        self.assertEqual(self.stickers._webm_decoder_args(ebml_av1), ())

        seen_args: list[tuple[tuple[str, ...], bool]] = []

        def fake_transcode(data, ffmpeg, *, ext, input_args=(), preserve_alpha=True, **kw):
            seen_args.append((input_args, preserve_alpha))
            return b"GIF89a-ok"

        with mock.patch.object(self.stickers, "find_ffmpeg", return_value="ffmpeg"), \
             mock.patch.object(self.stickers, "_transcode_video_to_wechat_gif", side_effect=fake_transcode):
            out_gif = self.stickers.gif_media_to_wechat_gif(ebml_vp8, "application/octet-stream", "ffmpeg")
            res_st = await self.stickers.to_wechat_sticker(ebml_vp8, "application/octet-stream")
        self.assertEqual(out_gif, b"GIF89a-ok")
        self.assertEqual(res_st, ("sticker", b"GIF89a-ok"))
        self.assertEqual(seen_args, [(("-c:v", "libvpx"), True), (("-c:v", "libvpx"), True)])

        # Fallback when local ffmpeg build lacks libvpx decoder
        calls = 0

        def fail_first_unknown_decoder(data, ffmpeg, *, ext, input_args=(), preserve_alpha=True, **kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise subprocess.CalledProcessError(1, ["ffmpeg"], stderr=b"Unknown decoder 'libvpx'")
            return b"GIF89a-fallback"

        with mock.patch.object(self.stickers, "_transcode_video_to_wechat_gif", side_effect=fail_first_unknown_decoder):
            self.assertEqual(self.stickers.webm_to_wechat_gif(ebml_vp8, "ffmpeg"), b"GIF89a-fallback")
        self.assertEqual(calls, 2)

        dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("libvpx_vp8,libvpx_vp9", dockerfile)

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
        self.b = Bridge(cfg, user_client=FakeUser(), bot_client=FakeBot())
        await self.b.wx.http.aclose()
        self.b.wx.http = httpx.AsyncClient(
            transport=httpx.MockTransport(self.daemon.handler),
            headers={"Authorization": "Bearer secret-token"},
        )
        self.b.wx._poll_initial_delay = 0.0
        self.b.wx._avatar_poll_interval = 0.0
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

        douyin = cards.identify_link_source({"url": "https://v.douyin.com/VYZaWHad0gs/"}, None)
        self.assertEqual((douyin.label, douyin.brand, douyin.button_label), ("抖音", "douyin", "🎬 打开抖音"))

        douyin_lite = cards.identify_link_source(
            {"url": "https://v.douyin.com/VYZaWHad0gs/", "sourceName": "抖音极速版"}, None
        )
        self.assertEqual(
            (douyin_lite.label, douyin_lite.brand, douyin_lite.button_label),
            ("抖音极速版", "douyin", "🎬 打开抖音极速版"),
        )

        kuaishou_lite = cards.identify_link_source(
            {"url": "https://v.kuaishou.com/abc123", "sourceName": "快手极速版"}, None
        )
        self.assertEqual(
            (kuaishou_lite.label, kuaishou_lite.brand, kuaishou_lite.button_label),
            ("快手极速版", "kuaishou", "🎬 打开快手极速版"),
        )

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

            # Douyin Lite & Kuaishou Lite badges render cleanly without a thumbnail.
            dy_png = cards.render_link_card(
                title="记得你叫王老八 记得你叫我忘了吧 不好意思波忘不了...",
                description="乐呵又一天的作品",
                url="https://v.douyin.com/VYZaWHad0gs/",
                source_name="抖音极速版",
            )
            self.assertIsNotNone(dy_png)
            im_dy = Image.open(io.BytesIO(dy_png))
            self.assertEqual((im_dy.size[0], im_dy.mode), (cards.CARD_WIDTH, "RGB"))
            ks_png = cards.render_link_card(
                title="今天做了一道硬菜",
                description="",
                url="https://v.kuaishou.com/ks123",
                source_name="快手极速版",
            )
            self.assertIsNotNone(ks_png)

            # Single-weight Regular-only setup (as in the container image without NotoSansSC-Bold.otf)
            # synthesizes a visibly heavier Bold title and renders CJK brand badges cleanly.
            reg_only, _, emoji_p, _ = cards._font_paths()
            assert reg_only is not None
            with mock.patch("bridge.cards._font_paths", return_value=(reg_only, reg_only, emoji_p, True)):
                faux_png = cards.render_link_card(
                    title="周末探店🌟 伪粗体标题与小红书角标测试",
                    description="单字重环境自动超采样加粗",
                    url="https://www.xiaohongshu.com/discovery/item/66f2",
                    app_type=5,
                )
                zhihu_png = cards.render_link_card(
                    title="如何评价单字重超采样加粗？",
                    description="知乎角标同样走超采样描边",
                    url="https://www.zhihu.com/question/12345",
                    app_type=5,
                )
            self.assertIsNotNone(faux_png)
            self.assertIsNotNone(zhihu_png)
            im_faux = Image.open(io.BytesIO(faux_png))
            self.assertEqual((im_faux.size[0], im_faux.mode), (cards.CARD_WIDTH, "RGB"))

            from PIL import ImageDraw
            c_reg = Image.new("RGB", (320, 64), (255, 255, 255))
            c_faux = Image.new("RGB", (320, 64), (255, 255, 255))
            f_reg = cards._load_font(reg_only, cards.TITLE_SIZE)
            cards._draw_mixed_text(c_reg, ImageDraw.Draw(c_reg), (8, 8), "露营装备指南",
                                   f_reg, cards.TITLE_SIZE, cards.TITLE_COLOR)
            cards._draw_mixed_text(c_faux, ImageDraw.Draw(c_faux), (8, 8), "露营装备指南",
                                   f_reg, cards.TITLE_SIZE, cards.TITLE_COLOR, faux_bold_path=reg_only)
            ink_reg = sum(1 for px in c_reg.getdata() if px != (255, 255, 255))
            ink_faux = sum(1 for px in c_faux.getdata() if px != (255, 255, 255))
            self.assertGreater(ink_faux, ink_reg)

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

    def test_extract_text_link_douyin_and_other_platforms(self):
        from bridge import cards

        # 1) Exact Douyin Lite share text with obfuscated prefix tokens
        got = cards.extract_text_link(
            "0.00 09/26 :1pm E@h.OK IiC:/ 复制打开抖音极速版，看看【乐呵又一天的作品】"
            "记得你叫王老八 记得你叫我忘了吧 不好意思波忘不了... https://v.douyin.com/VYZaWHad0gs/"
        )
        self.assertEqual(got, {
            "title": "记得你叫王老八 记得你叫我忘了吧 不好意思波忘不了...",
            "description": "乐呵又一天的作品",
            "sourceName": "抖音极速版",
            "url": "https://v.douyin.com/VYZaWHad0gs/",
        })

        # 2) Regular Douyin with trailing obfuscation tokens
        got2 = cards.extract_text_link(
            "4.82 复制打开抖音，看看【白浪源烟薯种植基地的作品】一旦找对了原产地，就很难再换其他家了... "
            "https://v.douyin.com/ikagBCYn/ P@k.cN 01/23 ytr:/"
        )
        self.assertEqual(got2, {
            "title": "一旦找对了原产地，就很难再换其他家了...",
            "description": "白浪源烟薯种植基地的作品",
            "sourceName": "抖音",
            "url": "https://v.douyin.com/ikagBCYn/",
        })

        # 3) Douyin Lite with trailing CTA after Chinese comma (keeps 抖音极速版 over generic trailing 抖音)
        got3 = cards.extract_text_link(
            "7.15 复制打开抖音极速版，看看【XX的日常】周末逛公园 https://v.douyin.com/xxxxxx/，"
            "长按复制此条消息，打开抖音查看..."
        )
        self.assertEqual(got3, {
            "title": "周末逛公园",
            "description": "XX的日常",
            "sourceName": "抖音极速版",
            "url": "https://v.douyin.com/xxxxxx/",
        })

        # 4) Douyin without space after `:/`, with zero-width space, nested 【】 in caption, and user note
        got4 = cards.extract_text_link(
            "笑死我了\n0.00 09/26 :1pm E@h.OK IiC:/\u200b复制打开抖音极速版，看看【作者的作品】【合集】第一集 "
            "https://v.douyin.com/abc123/"
        )
        self.assertEqual(got4, {
            "title": "【合集】第一集",
            "description": "作者的作品",
            "sourceName": "抖音极速版",
            "url": "https://v.douyin.com/abc123/",
            "note": "笑死我了",
        })

        # 5) Douyin profile share boilerplate and reverse-order Dou音 CTA
        got5 = cards.extract_text_link(
            "长按复制此条消息，打开抖音极速版搜索，查看TA的更多作品。 https://v.douyin.com/prof1/"
        )
        self.assertEqual(got5, {
            "title": "抖音极速版主页",
            "description": "查看TA的更多作品",
            "sourceName": "抖音极速版",
            "url": "https://v.douyin.com/prof1/",
        })
        got6 = cards.extract_text_link(
            "【乐呵又一天的作品】记得你叫王老八 https://v.douyin.com/rev1/ 复制此链接，打开Dou音搜索，直接观看视频！"
        )
        self.assertEqual(got6, {
            "title": "记得你叫王老八",
            "description": "乐呵又一天的作品",
            "sourceName": "抖音",
            "url": "https://v.douyin.com/rev1/",
        })
        got7 = cards.extract_text_link(
            "复制打开抖音极速版，看看【乐呵又一天的主页】 https://v.douyin.com/home1/"
        )
        self.assertEqual(got7, {
            "title": "乐呵又一天的主页",
            "description": "",
            "sourceName": "抖音极速版",
            "url": "https://v.douyin.com/home1/",
        })

        # 6) Kuaishou Lite, Xiaohongshu copy-link, and Bilibili copy-link share texts
        ks = cards.extract_text_link(
            "今天做了一道硬菜 #美食 https://v.kuaishou.com/ks123 复制此消息，打开【快手极速版】直接观看！"
        )
        self.assertEqual(ks, {
            "title": "今天做了一道硬菜 #美食",
            "description": "",
            "sourceName": "快手极速版",
            "url": "https://v.kuaishou.com/ks123",
        })
        xhs = cards.extract_text_link(
            "87 小明发布了一篇小红书笔记，快来看吧！ 😆 abcd1234 😆 http://xhslink.com/a/xyz，"
            "复制本条信息，打开【小红书】App查看精彩内容！"
        )
        self.assertEqual(xhs, {
            "title": "小明的小红书笔记",
            "description": "",
            "sourceName": "小红书",
            "url": "http://xhslink.com/a/xyz",
        })
        bili = cards.extract_text_link(
            "【【4K修复】感受一下什么叫真正的神仙颜值！-哔哩哔哩】【视频标记点 01:23】 https://b23.tv/BV1xx411c7m9"
        )
        self.assertEqual(bili, {
            "title": "【4K修复】感受一下什么叫真正的神仙颜值！",
            "description": "视频标记点 01:23",
            "sourceName": "哔哩哔哩",
            "url": "https://b23.tv/BV1xx411c7m9",
        })

        # 7) Ordinary chat messages mentioning URLs must NOT be hijacked
        for ordinary in (
            "我今天打开抖音刷到了这个 https://v.douyin.com/abc/",
            "你看这个视频 https://v.douyin.com/abc/ 太好笑了",
            "0.00 09/26 :1pm E@h.OK IiC:/ 复制打开抖音极速版，看看【作者的作品】标题 https://v.douyin.com/a/ 靠谱吗",
            "两个链接 https://v.douyin.com/a/ 和 https://v.douyin.com/b/",
            "看看这篇笔记 https://www.xiaohongshu.com/explore/123",
            "这个UP主太强了 https://b23.tv/abc",
            "看看#日常##搞笑#还挺好[破涕为笑] https://a.b/#x#1 <tag>",
        ):
            self.assertIsNone(cards.extract_text_link(ordinary), ordinary)

    async def test_douyin_lite_text_share_end_to_end_and_text_mode(self):
        from unittest import mock

        dy_url = "https://v.douyin.com/VYZaWHad0gs/"
        dy_text = (
            "0.00 09/26 :1pm E@h.OK IiC:/ 复制打开抖音极速版，看看【乐呵又一天的作品】"
            f"记得你叫王老八 记得你叫我忘了吧 不好意思波忘不了... {dy_url}"
        )
        self.daemon.add_thread("wxid_alice", "Alice")
        self.daemon.add_message("dy1", "wxid_alice", dy_text, type_=1)
        self.daemon.add_message("plain1", "wxid_alice", f"刚才刷到 {dy_url}", type_=1)

        def _btn_url(btn):
            return btn.type.url if hasattr(btn, "type") and hasattr(btn.type, "url") else btn.url

        # 1) Plain-text Douyin Lite share renders as a card image + CTA button without calling media download
        fake_card = _png(800, 220, (255, 255, 255))
        with mock.patch("bridge.cards.render_link_card", return_value=fake_card) as rmock:
            await self.event("dy1", "wxid_alice")
        rmock.assert_called_once_with(
            title="记得你叫王老八 记得你叫我忘了吧 不好意思波忘不了...",
            description="乐呵又一天的作品",
            url=dy_url,
            source_name="抖音极速版",
            app_type=None,
            thumbnail_bytes=None,
        )
        self.assertEqual(self.daemon.content_requests, [])
        sent = self.b.bot.sent[-1]
        self.assertEqual(sent.file.name, "link-card.png")
        self.assertIsNone(sent.kwargs["caption"])
        self.assertEqual(sent.kwargs["buttons"][0][0].text, "🎬 打开抖音极速版")
        self.assertEqual(_btn_url(sent.kwargs["buttons"][0][0]), dy_url)

        # 2) Ordinary text containing the same URL stays plain text
        await self.event("plain1", "wxid_alice")
        plain_sent = self.b.bot.sent[-1]
        self.assertIsNone(plain_sent.file)
        self.assertEqual(plain_sent.text, f"刚才刷到 {dy_url}")

        # 3) Group share with leading user note preserves sender + note in photo caption
        self.daemon.add_thread("123@chatroom", "Friends", kind="group")
        self.daemon.add_contact("wxid_bob", "Bob")
        self.daemon.add_message("dy2", "123@chatroom", f"笑死我了\n{dy_text}", type_=1, sender="wxid_bob")
        with mock.patch("bridge.cards.render_link_card", return_value=fake_card):
            await self.event("dy2", "123@chatroom")
        grp_sent = self.b.bot.sent[-1]
        self.assertEqual(grp_sent.file.name, "link-card.png")
        self.assertEqual(grp_sent.kwargs["caption"], "<b>Alice in group</b>：\n笑死我了")
        self.assertEqual(grp_sent.kwargs["buttons"][0][0].text, "🎬 打开抖音极速版")

        # 4) Text-only mode (link_card_images = False) produces clean HTML + CTA button without noise tokens
        self.b.cfg.bridge.link_card_images = False
        self.daemon.add_message("dy3", "wxid_alice", dy_text, type_=1)
        await self.event("dy3", "wxid_alice")
        txt_sent = self.b.bot.sent[-1]
        self.assertIsNone(txt_sent.file)
        self.assertEqual(
            txt_sent.text,
            "🎬 <b>记得你叫王老八 记得你叫我忘了吧 不好意思波忘不了...</b>\n乐呵又一天的作品\n<i>来自 抖音极速版</i>",
        )
        self.assertNotIn("E@h.OK", txt_sent.text)
        self.assertNotIn("IiC:/", txt_sent.text)
        self.assertEqual(txt_sent.kwargs["buttons"][0][0].text, "🎬 打开抖音极速版")
        self.assertEqual(_btn_url(txt_sent.kwargs["buttons"][0][0]), dy_url)

        # 5) Quoted reply containing a Douyin Lite share text also renders cleanly with quote prefix
        self.daemon.add_message(
            "dy4", "wxid_alice", dy_text, type_=49, appMessageType=57,
            quote={"senderName": "Alice", "embeddedText": "发个搞笑视频"},
        )
        await self.event("dy4", "wxid_alice")
        q_sent = self.b.bot.sent[-1]
        self.assertIn("<blockquote>Alice：发个搞笑视频</blockquote>", q_sent.text)
        self.assertIn("🎬 <b>记得你叫王老八 记得你叫我忘了吧 不好意思波忘不了...</b>", q_sent.text)
        self.assertEqual(q_sent.kwargs["buttons"][0][0].text, "🎬 打开抖音极速版")
