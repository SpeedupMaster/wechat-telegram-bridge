"""Tests for runtime memory optimizations: lazy imports, idle cleanup, disk spill, and bounded caches."""
from __future__ import annotations

import asyncio
import gzip
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from PIL import Image
from telethon import types

from bridge import article as A
from bridge import cards, core, mem, stickers
from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.store import Store
from bridge.wechat import Media, WeChatClient


class LazyImportTests(unittest.TestCase):
    def test_importing_core_skips_callweb_aiohttp_web_article_curl_cffi_and_rlottie(self):
        code = (
            "import sys, bridge.core; "
            "bad = [m for m in ('bridge.callweb', 'aiohttp.web', 'bridge.article', 'curl_cffi', 'rlottie_python') "
            "if m in sys.modules]; "
            "assert not bad, f'eagerly imported: {bad}'"
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_importing_article_skips_curl_cffi(self):
        code = (
            "import sys, bridge.article; "
            "assert 'curl_cffi' not in sys.modules, 'curl_cffi should be lazy-loaded'"
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)


class ClientAndStoreMemoryTests(unittest.TestCase):
    def test_topics_mode_skips_user_client_and_caps_entity_cache(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg_topics = Config(
                WeChatConfig(token="tok"),
                TelegramConfig(api_id=1, api_hash="h", bot_token="1:a"),
                BridgeConfig(mode="topics"),
                Path(tmp) / "config.toml",
            )
            b_topics = core.Bridge(cfg_topics)
            try:
                self.assertIsNone(b_topics.user)
                self.assertFalse((Path(tmp) / "data" / "user.session").exists())
                self.assertEqual(b_topics.bot._entity_cache_limit, core.TG_ENTITY_CACHE_LIMIT)
                self.assertIsNone(b_topics.call_web)
            finally:
                b_topics.store.db.close()
                b_topics.bot.session.close()

            cfg_groups = Config(
                WeChatConfig(token="tok"),
                TelegramConfig(api_id=1, api_hash="h", bot_token="1:a"),
                BridgeConfig(mode="groups"),
                Path(tmp) / "config.toml",
            )
            b_groups = core.Bridge(cfg_groups)
            try:
                self.assertIsNotNone(b_groups.user)
                self.assertEqual(b_groups.user._entity_cache_limit, core.TG_ENTITY_CACHE_LIMIT)
                self.assertEqual(b_groups.bot._entity_cache_limit, core.TG_ENTITY_CACHE_LIMIT)
            finally:
                b_groups.store.db.close()
                b_groups.user.session.close()
                b_groups.bot.session.close()

    def test_store_sets_bounded_sqlite_cache_and_journal_limit(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = Store(Path(tmp) / "s.db")
            try:
                cache_size = store.db.execute("PRAGMA cache_size").fetchone()[0]
                journal_limit = store.db.execute("PRAGMA journal_size_limit").fetchone()[0]
                self.assertEqual(cache_size, -1024)
                self.assertEqual(journal_limit, 4194304)
            finally:
                store.db.close()

    def test_docker_and_compose_configure_glibc_malloc_limits(self):
        root = Path(__file__).resolve().parent.parent
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        compose = (root / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("MALLOC_ARENA_MAX=2", dockerfile)
        self.assertIn("MALLOC_TRIM_THRESHOLD_=65536", dockerfile)
        self.assertIn("MALLOC_ARENA_MAX", compose)
        self.assertIn("MALLOC_TRIM_THRESHOLD_", compose)


class IdleCleanupAndBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_article_fetcher_closes_idle_session_and_recreates_on_next_fetch(self):
        created: list[object] = []

        class FakeCurlSession:
            def __init__(self, **kw):
                self.closed = False
                created.append(self)

            async def close(self):
                self.closed = True

        async def fake_fetch(url, session, *, uses_curl):
            return A.Article(url=url, title="ok")

        with mock.patch.object(A, "_CurlAsyncSession", FakeCurlSession), \
             mock.patch.object(A, "_fetch_with_session", side_effect=fake_fetch):
            fetcher = A.ArticleFetcher(idle_timeout=0.05)
            await fetcher.fetch("https://www.xiaohongshu.com/explore/0123456789abcdef01234567")
            self.assertEqual(len(created), 1)
            self.assertFalse(created[0].closed)
            await asyncio.sleep(0.12)
            self.assertTrue(created[0].closed)
            self.assertIsNone(fetcher._session)
            # Subsequent fetch transparently opens a fresh session.
            await fetcher.fetch("https://www.xiaohongshu.com/explore/0123456789abcdef01234567")
            self.assertEqual(len(created), 2)
            self.assertFalse(created[1].closed)
            await fetcher.close()
            self.assertTrue(created[1].closed)

    async def test_cards_clear_caches_and_idle_timer(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg = Config(
                WeChatConfig(token="tok"),
                TelegramConfig(api_id=1, api_hash="h", bot_token="1:a"),
                BridgeConfig(mode="topics"),
                Path(tmp) / "config.toml",
            )
            b = core.Bridge(cfg)
            try:
                cards._load_font(None, 24)
                self.assertGreater(cards._load_font.cache_info().currsize, 0)
                b._card_cache_idle_seconds = 0.04
                b._schedule_card_cache_clear()
                await asyncio.sleep(0.1)
                self.assertEqual(cards._load_font.cache_info().currsize, 0)
            finally:
                await b.close()
                b.store.db.close()
                b.bot.session.close()

    async def test_sender_names_cache_is_bounded(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg = Config(
                WeChatConfig(token="tok"),
                TelegramConfig(api_id=1, api_hash="h", bot_token="1:a"),
                BridgeConfig(mode="topics"),
                Path(tmp) / "config.toml",
            )
            b = core.Bridge(cfg)
            try:
                async def fake_contact_names(wx_id, *, fresh=False):
                    return f"R_{wx_id}", f"N_{wx_id}"

                b._contact_names = fake_contact_names
                with mock.patch.object(core, "NAMES_CACHE_MAX", 5):
                    for i in range(8):
                        await b._sender_name({"threadId": "t1", "senderId": f"u{i}"})
                    self.assertEqual(len(b.names), 5)
                    self.assertNotIn(("t1", "u0"), b.names)
                    self.assertIn(("t1", "u7"), b.names)
            finally:
                await b.close()
                b.store.db.close()
                b.bot.session.close()


class TelegramMediaSpillTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_telegram_video_spills_to_disk_streams_upload_and_cleans_up(self):
        payload = b"\x00\x00\x00\x18ftypmp42" + (b"V" * 200)
        uploaded: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/uploads"):
                uploaded.append(request.read())
                return httpx.Response(200, json={"id": "src-disk-1"})
            if request.url.path.endswith("/sends"):
                return httpx.Response(200, json={"id": "job1", "state": "completed"})
            return httpx.Response(404, json={"error": {"code": "not_found"}})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg = Config(
                WeChatConfig(token="tok"),
                TelegramConfig(api_id=1, api_hash="h", bot_token="1:a"),
                BridgeConfig(mode="topics"),
                Path(tmp) / "config.toml",
            )
            b = core.Bridge(cfg)
            try:
                await b.wx.http.aclose()
                b.wx.http = httpx.AsyncClient(
                    transport=httpx.MockTransport(handler),
                    headers={"Authorization": "Bearer tok"},
                )
                b.wx.account = "acc1"

                seen_targets: list[object] = []

                async def fake_download(file=None):
                    seen_targets.append(file)
                    self.assertIsInstance(file, str)
                    with open(file, "wb") as fp:
                        fp.write(payload)
                    return file

                msg = SimpleNamespace(
                    file=SimpleNamespace(size=len(payload), mime_type="video/mp4", ext=".mp4", name="big.mp4"),
                    photo=None, sticker=None, voice=None, gif=None, video=True, video_note=None,
                    download_media=fake_download,
                )

                with mock.patch("bridge.wechat.MEDIA_SPILL_BYTES", 64):
                    kind, media = await b._tg_media(msg)
                self.assertEqual(kind, "video")
                self.assertIsInstance(media, Media)
                self.assertEqual(media.data, b"")
                self.assertIsNotNone(media.path)
                temp_dir = os.path.dirname(media.path)
                self.assertTrue(os.path.isfile(media.path))

                try:
                    res = await b.wx.send_media("wxid_bob", kind, media, "k-spill")
                    self.assertEqual(res["state"], "completed")
                finally:
                    media.close()

                self.assertEqual(len(uploaded), 1)
                self.assertIn(payload, uploaded[0])
                self.assertFalse(os.path.exists(temp_dir))
            finally:
                await b.close()
                b.store.db.close()
                b.bot.session.close()

    async def test_large_telegram_image_document_spills_and_validates_without_copy(self):
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (30, 90, 180)).save(buf, "JPEG", quality=95)
        jpg_bytes = buf.getvalue()

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg = Config(
                WeChatConfig(token="tok"),
                TelegramConfig(api_id=1, api_hash="h", bot_token="1:a"),
                BridgeConfig(mode="topics"),
                Path(tmp) / "config.toml",
            )
            b = core.Bridge(cfg)
            try:
                async def fake_download(file=None):
                    self.assertIsInstance(file, str)
                    with open(file, "wb") as fp:
                        fp.write(jpg_bytes)
                    return file

                msg = SimpleNamespace(
                    file=SimpleNamespace(size=len(jpg_bytes), mime_type="image/jpeg", ext=".jpg", name="orig.jpg"),
                    photo=None, sticker=None, voice=None, gif=None, video=None, video_note=None,
                    download_media=fake_download,
                )

                with mock.patch("bridge.wechat.MEDIA_SPILL_BYTES", 64):
                    kind, media = await b._tg_media(msg)
                self.assertEqual(kind, "image")
                self.assertIsInstance(media, Media)
                self.assertEqual(media.data, b"")
                self.assertIsNotNone(media.path)
                temp_dir = os.path.dirname(media.path)
                self.assertEqual(media.read(), jpg_bytes)
                media.close()
                self.assertFalse(os.path.exists(temp_dir))
            finally:
                await b.close()
                b.store.db.close()
                b.bot.session.close()


class StickerAndMemTests(unittest.TestCase):
    def test_streaming_pam_reader_handles_rgba_and_rgb_frames(self):
        p1 = bytes([255, 0, 0, 128] * 4)
        p2 = bytes([0, 255, 0] * 4)
        stream = io.BytesIO(
            b"P7\nWIDTH 2\nHEIGHT 2\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n" + p1
            + b"P7\nWIDTH 2\nHEIGHT 2\nDEPTH 3\nMAXVAL 255\nTUPLTYPE RGB\nENDHDR\n" + p2
        )
        frames, w, h = stickers._read_pam_rgba_frames(stream)
        self.assertEqual((len(frames), w, h), (2, 2, 2))
        self.assertEqual(frames[0].mode, "RGBA")
        self.assertEqual(frames[1].mode, "RGBA")
        self.assertEqual(frames[0].getpixel((0, 0)), (255, 0, 0, 128))
        self.assertEqual(frames[1].getpixel((0, 0)), (0, 255, 0, 255))

    def test_rgba_frames_have_alpha_supports_pil_and_bytes(self):
        opaque = Image.new("RGBA", (4, 4), (10, 20, 30, 255))
        transparent = Image.new("RGBA", (4, 4), (10, 20, 30, 0))
        self.assertFalse(stickers._rgba_frames_have_alpha([opaque]))
        self.assertTrue(stickers._rgba_frames_have_alpha([opaque, transparent]))
        self.assertFalse(stickers._rgba_frames_have_alpha([opaque.tobytes()]))
        self.assertTrue(stickers._rgba_frames_have_alpha([transparent.tobytes()]))

    def test_tgs_disables_rlottie_model_cache_and_destroys_animation(self):
        lottie = {"v": "5.5.2", "fr": 30, "ip": 0, "op": 1, "w": 64, "h": 64, "layers": []}
        tgs = gzip.compress(json.dumps(lottie).encode())
        events: list[tuple] = []

        class FakeAnim:
            def lottie_configure_model_cache_size(self, n):
                events.append(("cache", n))

            def lottie_animation_get_totalframe(self):
                return 1

            def lottie_animation_get_framerate(self):
                return 30

            def lottie_animation_get_size(self):
                return 64, 64

            def lottie_animation_render(self, frame_num=0, width=64, height=64):
                return bytes([0, 0, 255, 255] * (width * height))

            def lottie_animation_destroy(self):
                events.append(("destroy",))

        fake_mod = SimpleNamespace(LottieAnimation=SimpleNamespace(from_data=lambda _s: FakeAnim()))
        with mock.patch.dict(sys.modules, {"rlottie_python": fake_mod}):
            out = stickers.tgs_to_wechat_gif(tgs, None)
        self.assertTrue(out.startswith((b"GIF87a", b"GIF89a")))
        self.assertEqual(events, [("cache", 0), ("destroy",)])

    def test_mem_trim_rate_limiting(self):
        calls: list[int] = []
        with mock.patch.object(mem, "_get_malloc_trim", return_value=lambda pad: (calls.append(pad) or 1)), \
             mock.patch.object(mem, "_LAST_TRIM", 0.0):
            self.assertTrue(mem.maybe_trim_memory(min_interval=60.0, collect=False))
            self.assertFalse(mem.maybe_trim_memory(min_interval=60.0, collect=False))
            self.assertEqual(calls, [0])


if __name__ == "__main__":
    unittest.main()
