"""Xiaohongshu notes in the built-in reader: URL detection, note parsing, fetching and the Mini App wiring."""
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
from bridge import xhs
from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.core import Bridge

NOTE_ID = "6aff09f8000000002b03eb5b"
NOTE_URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=TOK=&xsec_source=app_share"
SHARE_URL = "http://xhslink.com/o/AbC123xyz"


def _page(note: dict | None, *, desktop: bool = False) -> str:
    if note is None:
        state = {"noteData": {"data": {}}}
    elif desktop:
        state = {"note": {"noteDetailMap": {NOTE_ID: {"note": note}}}}
    else:
        state = {"noteData": {"data": {"noteData": note}}}
    raw = json.dumps(state, ensure_ascii=False).replace('"__UNDEF__"', "undefined")
    return f"<html><head><script>window.__INITIAL_STATE__={raw}</script></head><body></body></html>"


VIDEO_NOTE = {
    "noteId": NOTE_ID, "type": "video", "title": "", "time": 1795099128000,
    "desc": "多多夸奖男朋友#一二布布[话题]# #情侣日常[话题]# 好看[笑哭R]\n第二行 <b>",
    "user": {"nickName": "一二遇布"},
    "imageList": [{"fileId": "1040g008cover", "width": 1920, "height": 1080,
                   "url": "http://sns-webpic-qc.xhscdn.com/202611212045/sig/1040g008cover!h5_1080jpg"}],
    "video": {
        "image": {"thumbnailFileid": "frame/110/0/sprite.webp"},
        "media": {"stream": {
            "h264": [{"width": 1280, "height": 720,
                      "masterUrl": "http://sns-video-v2.xhscdn.com/stream/v.mp4?sign=abc&t=1",
                      "backupUrls": ["http://sns-bak-v1.xhscdn.com/stream/v.mp4",
                                     "http://evil.example/stream/v.mp4"]}],
            "h265": [{"masterUrl": "http://sns-video-v2.xhscdn.com/stream/v265.mp4?sign=x",
                      "backupUrls": ["http://sns-bak-v1.xhscdn.com/stream/v.mp4"]}],
        }},
    },
    "interactInfo": {"likedCount": "99", "collectedCount": "31", "commentCount": "0"},
    "ipLocation": "__UNDEF__",
}

IMAGE_NOTE = {
    "noteId": NOTE_ID, "type": "normal", "title": "周末探店", "time": "0",
    "desc": "人均50", "user": {"nickname": "小王"},
    "imageList": [{"fileId": "1040g00a", "width": 1080, "height": 1440},
                  {"traceId": "spectrum/1040g00b", "width": "800", "height": "600"},
                  {"fileId": "../../etc/passwd"}],
}


class UrlTests(unittest.TestCase):
    def test_is_note_url(self):
        for good in (NOTE_URL, SHARE_URL, "https://xhslink.cn/a/sT7omKb6ijX6",
                     f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}?xsec_token=t",
                     f"https://www.rednote.com/explore/{NOTE_ID}?xsec_token=t",
                     f"https://www.xiaohongshu.com/user/profile/67fbbf04000000000e01f7a6/{NOTE_ID}"):
            self.assertTrue(xhs.is_note_url(good), good)
        for bad in ("https://www.xiaohongshu.com/explore", "https://www.xiaohongshu.com/login",
                    "https://xhslink.com/", "https://evil.com/explore/" + NOTE_ID,
                    "https://xiaohongshu.com.evil.com/explore/" + NOTE_ID, "javascript:alert(1)", None):
            self.assertFalse(xhs.is_note_url(bad), bad)

    def test_clean_text(self):
        self.assertEqual(xhs.clean_text("夸奖#一二布布[话题]# #情侣日常[话题]#  好[笑哭R]看"),
                         "夸奖#一二布布 #情侣日常 好看")


class ParseTests(unittest.TestCase):
    def test_video_note(self):
        art = xhs.parse_note(_page(VIDEO_NOTE), NOTE_URL)
        self.assertEqual(art.title, "多多夸奖男朋友 好看")
        self.assertEqual(art.account, "一二遇布")
        self.assertEqual(art.published, 1795099128)
        self.assertEqual(art.cover, "https://sns-na-i2.xhscdn.com/1040g008cover?imageView2/2/w/1080/format/jpg")
        srcs = re.findall(r'<source src="([^"]+)"', art.content)
        # unsigned backups first, signed master later, h265 after h264, no duplicates or foreign hosts
        self.assertEqual(srcs, ["https://sns-bak-v1.xhscdn.com/stream/v.mp4",
                                "https://sns-video-v2.xhscdn.com/stream/v.mp4?sign=abc&amp;t=1",
                                "https://sns-video-v2.xhscdn.com/stream/v265.mp4?sign=x"])
        self.assertIn('width="1280" height="720"', art.content)
        self.assertIn(f'poster="{art.cover}"', art.content)
        self.assertIn("#一二布布 #情侣日常 好看<br>第二行 &lt;b&gt;", art.content)
        self.assertIn("❤️ 99 · ⭐ 31", art.content)
        self.assertNotIn("💬", art.content)  # zero counts are hidden
        self.assertNotIn("sprite", art.content)
        self.assertNotIn("evil", art.content)

    def test_video_streams_outside_media(self):
        # Some page variants (seen from overseas servers) leave media.stream empty: the streams are then only
        # in mediaV2, a JSON string with snake_case keys, and the original upload in consumer.originVideoKey.
        media_v2 = {"video": {"width": 1920}, "stream": {
            "h265": [{"master_url": "http://sns-video-v2.xhscdn.com/stream/b265.mp4?sign=y"}],
            "h264": [{"width": 1920, "height": 1080, "master_url": "http://sns-video-v2.xhscdn.com/stream/b.mp4?sign=z",
                      "backup_urls": ["http://sns-bak-v6.xhscdn.com/stream/b.mp4", "http://evil.example/b.mp4"]}]}}
        video = {"media": {"stream": {"h264": [], "h265": [], "h266": [], "av1": []}},
                 "mediaV2": json.dumps(media_v2), "consumer": {"originVideoKey": "pre_post/1040g0cg327nh03m"}}
        art = xhs.parse_note(_page({**VIDEO_NOTE, "video": video}), NOTE_URL)
        self.assertEqual(re.findall(r'<source src="([^"]+)"', art.content),
                         ["https://sns-bak-v6.xhscdn.com/stream/b.mp4",
                          "https://sns-video-v2.xhscdn.com/stream/b.mp4?sign=z",
                          "https://sns-video-v2.xhscdn.com/stream/b265.mp4?sign=y",
                          "https://sns-video-bd.xhscdn.com/pre_post/1040g0cg327nh03m"])
        self.assertIn('width="1920" height="1080"', art.content)
        self.assertNotIn("evil", art.content)
        # the original upload alone is enough; a malformed key or mediaV2 is ignored
        for consumer, ok in (({"originVideoKey": "pre_post/abc"}, True), ({"originVideoKey": "../x"}, False),
                             ({"originVideoKey": "a b"}, False), (None, False)):
            note = {**VIDEO_NOTE, "video": {"media": {"stream": {}}, "mediaV2": "{bad", "consumer": consumer}}
            if ok:
                self.assertIn('<source src="https://sns-video-bd.xhscdn.com/pre_post/abc"',
                              xhs.parse_note(_page(note), NOTE_URL).content)
            else:
                with self.assertRaises(A.ArticleError) as cm, self.assertLogs("bridge.xhs", "INFO") as logs:
                    xhs.parse_note(_page(note), NOTE_URL)
                self.assertEqual(cm.exception.code, "xhs_no_video")
                self.assertIn("stream={}", logs.output[0])

    def test_image_note_and_desktop_state(self):
        for page in (_page(IMAGE_NOTE), _page(IMAGE_NOTE, desktop=True)):
            art = xhs.parse_note(page, NOTE_URL)
            self.assertEqual((art.title, art.account, art.published), ("周末探店", "小王", 0))
            imgs = re.findall(r'<img src="([^"]+)"', art.content)
            self.assertEqual(imgs, ["https://sns-na-i2.xhscdn.com/1040g00a?imageView2/2/w/1080/format/jpg",
                                    "https://sns-na-i2.xhscdn.com/spectrum/1040g00b?imageView2/2/w/1080/format/jpg"])
            self.assertIn('width="1080" height="1440"', art.content)
            self.assertIn('referrerpolicy="no-referrer"', art.content)
            self.assertEqual(art.cover, imgs[0])

    def test_unusable_pages_fall_back_to_the_original(self):
        for page in ("<html>登录</html>", _page(None), _page({**VIDEO_NOTE, "video": {"media": {}}})):
            with self.assertRaises(A.ArticleError) as cm:
                xhs.parse_note(page, NOTE_URL)
            self.assertTrue(cm.exception.fallback_original)


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_share_link_redirects_and_blocks(self):
        seen = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append((str(req.url), req.headers.get("user-agent", "")))
            if req.url.host == "xhslink.com" and req.url.path == "/o/AbC123xyz":
                return httpx.Response(302, headers={
                    "location": f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}?xsec_token=T&xsec_source=app_share"})
            if req.url.host == "xhslink.com" and req.url.path == "/o/expired":
                return httpx.Response(302, headers={"location": "https://www.xiaohongshu.com/"})
            if req.url.host == "xhslink.com" and req.url.path == "/o/intl":
                return httpx.Response(302, headers={
                    "location": f"https://www.rednote.com/discovery/item/{NOTE_ID}?xsec_token=T"})
            if req.url.host == "xhslink.com" and req.url.path == "/o/away":
                return httpx.Response(302, headers={"location": "https://evil.example/"})
            if req.url.path.startswith("/discovery/item/"):
                return httpx.Response(200, content=_page(VIDEO_NOTE).encode(),
                                      headers={"content-type": "text/html; charset=utf-8"})
            if req.url.path.startswith("/explore/"):
                return httpx.Response(302, headers={"location": "/login?redirectPath=x"})
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            art = await A.fetch_article(SHARE_URL, client)
            self.assertEqual(art.title, "多多夸奖男朋友 好看")
            self.assertEqual(art.url,
                             f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}?xsec_token=T&xsec_source=app_share")
            self.assertEqual(seen[0][0], "https://xhslink.com/o/AbC123xyz")
            self.assertIn("iPhone", seen[0][1])  # the mobile page carries the note without a login
            art = await A.fetch_article("https://xhslink.com/o/intl", client)  # overseas: international domain
            self.assertEqual(art.url, f"https://www.rednote.com/discovery/item/{NOTE_ID}?xsec_token=T")
            with self.assertLogs("bridge.xhs", "INFO") as logs, self.assertRaises(A.ArticleError):
                await A.fetch_article("https://xhslink.com/o/away", client)
            self.assertIn("another site: evil.example/", logs.output[0])
            for url, code in (("https://xhslink.com/o/expired", "xhs_blocked"),
                              ("https://xhslink.com/o/away", "xhs_off_host"),
                              (NOTE_URL, "xhs_blocked")):
                with self.assertRaises(A.ArticleError) as cm:
                    await A.fetch_article(url, client)
                self.assertEqual((cm.exception.code, cm.exception.fallback_original), (code, True), url)


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

    async def test_link_prefetches_and_reader_plays_video(self):
        calls = []

        async def fake_fetch(url):
            calls.append(url)
            return xhs.parse_note(_page(VIDEO_NOTE), url)

        with mock.patch.object(A, "fetch_article", side_effect=fake_fetch):
            link = self.web.article_link(SHARE_URL)
            m = re.fullmatch(r"https://t\.me/mybot/app\?startapp=r_([A-Za-z0-9]{16})", link or "")
            self.assertIsNotNone(m, link)
            norm = "https://xhslink.com/o/AbC123xyz"
            self.assertEqual(self.b.store.article_url(m.group(1)), norm)
            for _ in range(100):  # prefetched in the background while the share token is fresh
                if self.b.store.get_cached_article(norm):
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(calls, [norm])
            self.web.article_cache.delete(norm)  # the page must also work from the SQLite copy
            async with self.http.get(f"{self.base}/read/{m.group(1)}") as r:
                self.assertEqual(r.status, 200)
                page = await r.text()
                csp = r.headers["Content-Security-Policy"]
            self.assertEqual(calls, [norm])
            self.assertIn('<video controls playsinline', page)
            self.assertIn("https://sns-bak-v1.xhscdn.com/stream/v.mp4", page)
            self.assertIn("media-src https:", csp)
            self.assertIn('<meta name="referrer" content="no-referrer">', page)

    async def test_blocked_note_opens_the_original_in_place(self):
        with mock.patch.object(A, "fetch_article",
                               side_effect=A.ArticleError("blocked", code="xhs_blocked", fallback_original=True)):
            link = self.web.article_link(NOTE_URL)
            aid = link.rsplit("r_", 1)[1]
            async with self.http.get(f"{self.base}/read/{aid}") as r:
                self.assertEqual(r.status, 200)
                page = await r.text()
        self.assertIn("location.replace", page)
        self.assertIn(json.dumps(NOTE_URL), page)

    def test_toggle(self):
        self.b.cfg.bridge.xhs_reader = False  # no reader: the card still opens in place
        self.assertEqual(self.web.reader_kind(NOTE_URL), "link")
        self.b.cfg.bridge.links_in_app = False
        self.assertIsNone(self.web.article_link(NOTE_URL))
        self.assertIsNotNone(self.web.article_link("https://mp.weixin.qq.com/s/abcDEF123"))


if __name__ == "__main__":
    unittest.main()
