"""Douyin videos in the reader Mini App: link detection, share-link resolution and the embed player page."""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from bridge import article as A
from bridge import douyin
from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.core import Bridge

VID = "7708609042687627874"
SHARE = "https://v.douyin.com/VYZaWHad0gs/"
TRACKED = (f"https://www.iesdouyin.com/share/video/{VID}/?region=CN&mid=1&u_code=x&did=MS4w&iid=MS4w"
           "&share_sign=abc&utm_medium=android&app=aweme")


class UrlTests(unittest.TestCase):
    def test_detection(self):
        for good in (SHARE, "http://v.douyin.com/ikagBCYn", f"https://www.douyin.com/video/{VID}",
                     f"https://www.iesdouyin.com/share/video/{VID}/", f"https://www.douyin.com/jingxuan?modal_id={VID}"):
            self.assertTrue(douyin.is_video_url(good), good)
        for bad in ("https://v.douyin.com/", "https://www.douyin.com/", f"https://www.douyin.com/note/{VID}",
                    f"https://www.douyin.com/user/{VID}", f"https://evil.com/video/{VID}",
                    f"https://douyin.com.evil.com/video/{VID}", "https://v.douyin.com/a/b/", "javascript:1", None):
            self.assertFalse(douyin.is_video_url(bad), bad)
        self.assertEqual(douyin.video_id(TRACKED), VID)
        self.assertEqual(douyin.video_id(f"https://www.douyin.com/?modal_id={VID}&x=1"), VID)
        self.assertEqual(douyin.video_id("https://www.douyin.com/?modal_id=1;alert(1)"), "")

    def test_player_page(self):
        page = douyin.render_player(douyin.article_for(VID, SHARE), nonce="N0")
        self.assertIn(f'src="https://open.douyin.com/player/video?vid={VID}&amp;autoplay=0"', page)
        self.assertIn('referrerpolicy="no-referrer"', page)
        self.assertIn('<script nonce="N0">', page)
        self.assertIsNone(douyin.render_player(A.Article(url="https://mp.weixin.qq.com/s/x", title="t", account="",
                                                         author="", published=0, cover="", description="",
                                                         content="<p>x</p>")))
        with self.assertRaises(ValueError):
            douyin.player_url('1"><script>')


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_share_links(self):
        seen = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(str(req.url))
            path = req.url.path
            if req.url.host != "v.douyin.com":
                raise AssertionError(f"share pages must not be downloaded: {req.url}")
            target = {"/VYZaWHad0gs/": TRACKED,
                      "/hop1/": "https://v.douyin.com/VYZaWHad0gs/",
                      "/note/": f"https://www.iesdouyin.com/share/note/{VID}/?region=CN",
                      "/away/": "https://evil.example/",
                      "/gone/": "https://www.douyin.com/"}.get(path)
            return httpx.Response(302, headers={"location": target}) if target else httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            art = await A.fetch_article(SHARE, client)
            self.assertEqual(art.url, f"https://www.douyin.com/video/{VID}")  # tracking parameters dropped
            self.assertEqual(seen, [SHARE])
            art = await A.fetch_article("https://v.douyin.com/hop1/", client)
            self.assertEqual(douyin.video_id(art.url), VID)
            seen.clear()
            art = await A.fetch_article(f"https://www.douyin.com/video/{VID}", client)  # no request needed
            self.assertEqual((douyin.video_id(art.url), seen), (VID, []))
            for path, code in (("note", "douyin_not_video"), ("away", "douyin_off_host"),
                               ("gone", "douyin_not_video"), ("dead", "douyin_http_404")):
                with self.assertRaises(A.ArticleError) as cm:
                    await A.fetch_article(f"https://v.douyin.com/{path}/", client)
                self.assertEqual((cm.exception.code, cm.exception.fallback_original), (code, True), path)


class ReaderWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiohttp
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(WeChatConfig(token="t"), TelegramConfig(api_id=1, api_hash="x", bot_token="123:abc"),
                     BridgeConfig(create_interval_seconds=0, sync_avatar=False, call_web_app="app",
                                  call_web_url="http://127.0.0.1/wxcall", call_web_listen="127.0.0.1:0"),
                     Path(self.tmp.name) / "config.toml")
        self.b = Bridge(cfg)
        self.b.bot_me = SimpleNamespace(username="mybot")
        self.web = self.b.call_web
        await self.web.start()
        self.http = aiohttp.ClientSession()
        self.base = f"http://127.0.0.1:{self.web.bound_port}/wxcall"

    async def asyncTearDown(self):
        await self.http.close()
        await self.web.stop()
        await self.b.wx.http.aclose()
        self.b.store.db.close()
        self.tmp.cleanup()

    async def test_link_prefetches_and_serves_the_player(self):
        calls = []

        async def fake_fetch(url):
            calls.append(url)
            return douyin.article_for(VID, url)

        with mock.patch.object(A, "fetch_article", side_effect=fake_fetch):
            link = self.web.article_link(SHARE)
            m = re.fullmatch(r"https://t\.me/mybot/app\?startapp=r_([A-Za-z0-9]{16})", link or "")
            self.assertIsNotNone(m, link)
            for _ in range(100):
                if self.b.store.get_cached_article(SHARE):
                    break
                await asyncio.sleep(0.01)
            self.web.article_cache.delete(SHARE)  # served from the SQLite copy
            async with self.http.get(f"{self.base}/read/{m.group(1)}") as r:
                self.assertEqual(r.status, 200)
                page = await r.text()
                csp = r.headers["Content-Security-Policy"]
        self.assertEqual(calls, [SHARE])
        self.assertIn(f"https://open.douyin.com/player/video?vid={VID}", page)
        self.assertIn("frame-src https://open.douyin.com;", csp)

    async def test_other_reader_pages_keep_frames_blocked_and_notes_fall_back(self):
        with mock.patch.object(A, "fetch_article", side_effect=A.ArticleError(
                "x", code="douyin_not_video", fallback_original=True)):
            aid = self.web.article_link("https://v.douyin.com/note1/").rsplit("r_", 1)[1]
            async with self.http.get(f"{self.base}/read/{aid}") as r:
                page = await r.text()
                csp = r.headers["Content-Security-Policy"]
        self.assertIn("location.replace", page)
        self.assertIn(json.dumps("https://v.douyin.com/note1/"), page)
        self.assertIn("frame-src 'none';", csp)

    def test_toggle(self):
        self.b.cfg.bridge.douyin_reader = False  # no reader: the card still opens in place
        self.assertEqual(self.web.reader_kind(SHARE), "link")
        self.b.cfg.bridge.links_in_app = False
        self.assertIsNone(self.web.article_link(SHARE))
        self.assertIsNotNone(self.web.article_link("https://mp.weixin.qq.com/s/abcDEF123"))


if __name__ == "__main__":
    unittest.main()
