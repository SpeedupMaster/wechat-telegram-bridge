"""Bilibili's embed player and every other link card opening in place inside the Mini App."""
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
from bridge import bilibili
from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.core import Bridge

BV = "BV1GJ411x7h7"


class BilibiliUrlTests(unittest.TestCase):
    def test_detection(self):
        for good in ("https://b23.tv/AbCd123", "http://bili2233.cn/xYz9", f"https://www.bilibili.com/video/{BV}/",
                     f"https://m.bilibili.com/video/{BV}?p=2", "https://www.bilibili.com/video/av80433022",
                     f"https://www.bilibili.com/s/video/{BV}"):
            self.assertTrue(bilibili.is_video_url(good), good)
        for bad in ("https://b23.tv/", "https://live.bilibili.com/123", "https://space.bilibili.com/1",
                    "https://www.bilibili.com/bangumi/play/ep1", f"https://evil.com/video/{BV}",
                    f"https://bilibili.com.evil.com/video/{BV}", "https://b23.tv/a/b", None):
            self.assertFalse(bilibili.is_video_url(bad), bad)
        self.assertEqual(bilibili.video_ref(f"https://m.bilibili.com/video/{BV}?p=3&share_source=x"), (BV, 3))
        self.assertEqual(bilibili.video_ref(f"https://www.bilibili.com/video/{BV}?p=abc"), (BV, 1))
        self.assertEqual(bilibili.video_ref("https://www.bilibili.com/video/AV80433022"), ("av80433022", 1))

    def test_player(self):
        self.assertEqual(bilibili.player_url(BV, 2), "https://player.bilibili.com/player.html?bvid="
                         f"{BV}&p=2&autoplay=0&danmaku=0&high_quality=1")
        self.assertIn("?aid=80433022&", bilibili.player_url("av80433022"))
        with self.assertRaises(ValueError):
            bilibili.player_url('BV1"><script>x')
        page = bilibili.render_player(bilibili.article_for(BV, 2), nonce="N")
        self.assertIn(f'var target = "https://player.bilibili.com/player.html?bvid={BV}&p=2&', page)
        self.assertNotIn("<iframe", page)
        self.assertIn("<title>哔哩哔哩视频</title>", page)


class BilibiliFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_links(self):
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.host != "b23.tv":
                raise AssertionError(f"video pages must not be downloaded: {req.url}")
            target = {"/vid1": f"https://www.bilibili.com/video/{BV}?p=2&share_source=copy&vd_source=abc",
                      "/live1": "https://live.bilibili.com/21452505?broadcast_type=0",
                      "/away1": "https://evil.example/"}.get(req.url.path)
            return httpx.Response(302, headers={"location": target}) if target else httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            art = await A.fetch_article("https://b23.tv/vid1", client)
            self.assertEqual(art.url, f"https://www.bilibili.com/video/{BV}?p=2")  # tracking dropped
            for path, code in (("live1", "bili_not_video"), ("away1", "bili_off_host"), ("dead1", "bili_http_404")):
                with self.assertRaises(A.ArticleError) as cm:
                    await A.fetch_article(f"https://b23.tv/{path}", client)
                self.assertEqual((cm.exception.code, cm.exception.fallback_original), (code, True), path)


class MiniAppWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiohttp
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config(WeChatConfig(token="t"), TelegramConfig(api_id=1, api_hash="x", bot_token="123:abc"),
                     BridgeConfig(create_interval_seconds=0, sync_avatar=False, call_web_app="app",
                                  call_web_url="http://127.0.0.1/wxcall", call_web_listen="127.0.0.1:0"),
                     Path(self.tmp.name) / "config.toml")
        self.b = Bridge(cfg, user_client=None, bot_client=None)
        self.b.bot_me = SimpleNamespace(username="mybot")
        self.web = self.b.call_web
        await self.web.start()
        self.http = aiohttp.ClientSession()
        self.base = f"http://127.0.0.1:{self.web.bound_port}/wxcall"

    async def asyncTearDown(self):
        await self.http.close()
        await self.b.close()
        self.tmp.cleanup()

    async def _get(self, link: str) -> tuple[str, str]:
        aid = link.rsplit("r_", 1)[1]
        async with self.http.get(f"{self.base}/read/{aid}") as r:
            self.assertEqual(r.status, 200)
            return await r.text(), r.headers["Content-Security-Policy"]

    async def test_bilibili_player(self):
        calls = []

        async def fake_fetch(url):
            calls.append(url)
            return bilibili.article_for(BV)

        with mock.patch.object(A, "fetch_article", side_effect=fake_fetch):
            link = self.web.article_link("https://b23.tv/vid1")
            for _ in range(100):  # resolved ahead of time, like the other share links
                if calls:
                    break
                await asyncio.sleep(0.01)
            page, csp = await self._get(link)
        self.assertEqual(calls, ["https://b23.tv/vid1"])
        self.assertIn(f"https://player.bilibili.com/player.html?bvid={BV}", page)
        self.assertIn("frame-src 'none';", csp)

    async def test_other_links_open_in_place_without_fetching(self):
        url = "http://example.com/a?b=1&c=<x>#top"
        with mock.patch.object(A, "fetch_article", side_effect=AssertionError("must not fetch")):
            link = self.web.article_link(url)
            await asyncio.sleep(0.05)
            page, csp = await self._get(link)
        self.assertIn("location.replace", page)
        self.assertIn(json.dumps(url).replace("<", "\\u003c").replace(">", "\\u003e"), page)
        self.assertNotIn("<x>", page)
        self.assertIn(">立即打开</a>", page)
        self.assertNotIn("重试", page)
        self.assertIn("frame-src 'none';", csp)

    def test_what_stays_out(self):
        for url in ("https://t.me/c/1/2", "https://telegram.org/blog", "ftp://x.com/", "https://a.com/x y",
                    "https://a.com/" + "x" * 5000, None):
            self.assertIsNone(self.web.article_link(url), url)
        self.b.cfg.bridge.bilibili_reader = False
        self.assertEqual(self.web.reader_kind("https://b23.tv/vid1"), "link")
        self.assertTrue(re.fullmatch(r"https://t\.me/mybot/app\?startapp=r_\w{16}",
                                     self.web.article_link("https://github.com/x/y") or ""))


if __name__ == "__main__":
    unittest.main()
