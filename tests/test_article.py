"""Shared Mini App reader: fetch session, caches, reader / handoff pages, and the CallWeb reader routes.

WeChat Official Account articles are not fetched any more: their card buttons open the original page in place.
"""
from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bridge import article as A
from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.core import Bridge
from bridge.store import Store

ART_URL = "https://mp.weixin.qq.com/s/abcDEF123"
# A Xiaohongshu note: still rendered by the reader, so it exercises the shared fetch / cache / page plumbing.
NOTE_URL = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"


def _btn_url(btn):
    return btn.type.url if hasattr(btn, "type") and hasattr(btn.type, "url") else btn.url


class ReaderPageTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(A.normalize_url("http://WWW.Xiaohongshu.com/explore/1?a=1&amp;b=2#frag"),
                         "https://www.xiaohongshu.com/explore/1?a=1&b=2")

    def test_render_escapes(self):
        art = A.Article(url=NOTE_URL, title="<b>x</b>", account="号", content="<p>正文</p>")
        page = A.render_page(art, nonce="abc")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)
        self.assertIn("<p>正文</p>", page)
        # 打开原文 stays inside the Mini App (no web_app_open_link hand-off to an external browser)
        self.assertIn(f'<a class="open" id="open" data-inapp="1" href="{NOTE_URL}">打开原文</a>', page)
        self.assertNotIn('target="_blank"', page)
        self.assertIn('<meta name="referrer" content="no-referrer">', page)
        dated = A.render_page(A.Article(url=NOTE_URL, title="t", account="号", published=1607438787), nonce="")
        self.assertIn("号 · 2020-12-08 22:46", dated)  # Beijing time, whatever the server's timezone
        bare = A.render_page(A.Article(url=NOTE_URL, title="t", description="摘要"), nonce="n1")
        self.assertIn("暂不支持阅读模式", bare)
        self.assertIn('<script nonce="n1">', bare)
        err = A.render_page(None, error="<坏>", source_url=NOTE_URL)
        self.assertIn("&lt;坏&gt;", err)
        self.assertIn("打开原文", err)

    def test_cache_lru_and_failure_ttl(self):
        cache = A.ArticleCache(size=2, fail_ttl=0.0)
        a1, a2, a3 = (A.Article(url=str(i), title=str(i)) for i in range(3))
        cache.put("1", a1)
        cache.put("2", a2)
        self.assertIs(cache.get("1"), a1)  # bump 1, so 2 is evicted next
        cache.put("3", a3)
        self.assertIsNone(cache.get("2"))
        self.assertIs(cache.get("1"), a1)
        cache.put("bad", A.ArticleError("x"))
        self.assertIsNone(cache.get("bad"))  # failures expire (fail_ttl = 0)


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_account_articles_are_not_fetched(self):
        session = mock.Mock()
        for url in (ART_URL, "https://example.com/s/1"):
            with self.assertRaises(A.ArticleError) as cm:
                await A.fetch_article(url, session)
            self.assertEqual(cm.exception.code, "invalid_url")
        session.stream.assert_not_called()

    async def test_fetcher_concurrency_and_curl_cffi_session(self):
        active = 0
        peak = 0
        created_kwargs = {}
        seen_sessions = []

        class FakeCurlSession:
            def __init__(self, **kw):
                created_kwargs.update(kw)
                self.closed = False

            async def close(self):
                self.closed = True

        async def fake_fetch(url, session, *, uses_curl):
            nonlocal active, peak
            seen_sessions.append((session, uses_curl))
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1
            return A.Article(url=url, title="笔记")

        with mock.patch.object(A, "_CurlAsyncSession", FakeCurlSession),                 mock.patch.object(A, "_fetch_with_session", side_effect=fake_fetch):
            fetcher = A.ArticleFetcher(proxy="socks5://127.0.0.1:7890", max_concurrency=1)
            with A.use_fetcher(fetcher):
                a1, a2 = await asyncio.gather(A.fetch_article(NOTE_URL), A.fetch_article(NOTE_URL + "2"))
            self.assertEqual((a1.title, a2.title), ("笔记", "笔记"))
            self.assertEqual(peak, 1)
            self.assertIs(seen_sessions[0][0], seen_sessions[1][0])  # one reusable session
            self.assertTrue(seen_sessions[0][1])
            self.assertEqual(created_kwargs.get("impersonate"), A.IMPERSONATE_BROWSER)
            self.assertEqual(created_kwargs.get("proxy"), "socks5://127.0.0.1:7890")
            self.assertFalse(created_kwargs.get("allow_redirects"))
            await fetcher.close()

    def test_article_proxy_config_validation(self):
        from bridge.config import load_config
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg = load_config(Path(tmp) / "missing.toml", environ={
                "TG_API_ID": "1", "TG_API_HASH": "h", "TG_BOT_TOKEN": "1:a", "WECHAT_TOKEN": "t",
                "ARTICLE_PROXY": "http://127.0.0.1:8080",
            })
            self.assertEqual(cfg.bridge.article_proxy, "http://127.0.0.1:8080")
            with self.assertRaises(ValueError):
                BridgeConfig(article_proxy="ftp://bad.example:21")


class StoreTests(unittest.TestCase):
    def test_article_ids_are_stable_and_alphanumeric(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = Store(Path(tmp) / "s.db")
            aid = store.article_id(ART_URL)
            self.assertRegex(aid, r"^[A-Za-z0-9]{16}$")
            self.assertEqual(store.article_id(ART_URL), aid)
            self.assertNotEqual(store.article_id(ART_URL + "x"), aid)
            self.assertEqual(store.article_url(aid), ART_URL)
            self.assertIsNone(store.article_url("nope"))
            store.db.close()

    def test_persistent_article_cache_and_eviction(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = Store(Path(tmp) / "s.db")
            aid1 = store.article_id("https://www.xiaohongshu.com/explore/a1")
            store.save_cached_article("https://www.xiaohongshu.com/explore/a1", {
                "url": "https://www.xiaohongshu.com/explore/a1", "title": "第一篇", "account": "号1",
                "author": "甲", "published": 1700000001, "cover": "", "description": "摘1", "content": "<p>正文1</p>",
            }, max_entries=2)
            store.save_cached_article("https://www.xiaohongshu.com/explore/a2", {
                "url": "https://www.xiaohongshu.com/explore/a2", "title": "第二篇", "account": "号2",
                "author": "", "published": 1700000002, "cover": "", "description": "", "content": "<p>正文2</p>",
            }, max_entries=2)
            # Touch a1 so a2 becomes oldest, then insert a3 -> a2 evicted, a1 & a3 kept.
            hit1 = store.get_cached_article("https://www.xiaohongshu.com/explore/a1")
            self.assertIsNotNone(hit1)
            self.assertEqual(hit1["title"], "第一篇")
            self.assertEqual(hit1["content"], "<p>正文1</p>")
            store.save_cached_article("https://www.xiaohongshu.com/explore/a3", {
                "url": "https://www.xiaohongshu.com/explore/a3", "title": "第三篇", "account": "号3",
                "author": "", "published": 1700000003, "cover": "", "description": "", "content": "<p>正文3</p>",
            }, max_entries=2)
            self.assertIsNotNone(store.get_cached_article("https://www.xiaohongshu.com/explore/a1"))
            self.assertIsNone(store.get_cached_article("https://www.xiaohongshu.com/explore/a2"))
            self.assertIsNotNone(store.get_cached_article("https://www.xiaohongshu.com/explore/a3"))
            # Short ID mapping remains intact even after cache deletion.
            store.delete_cached_article("https://www.xiaohongshu.com/explore/a1")
            self.assertIsNone(store.get_cached_article("https://www.xiaohongshu.com/explore/a1"))
            self.assertEqual(store.article_url(aid1), "https://www.xiaohongshu.com/explore/a1")
            store.db.close()


class ReaderWebTests(unittest.IsolatedAsyncioTestCase):
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

    def test_article_link(self):
        # Official Account articles open the original in place, like any other page: the exact URL is kept and
        # the raw link rides along as a second button.
        wx = "http://mp.weixin.qq.com/s/abcDEF123#rd"
        self.assertEqual(self.web.reader_kind(wx), "link")
        row = self.b._card_buttons({"url": wx}, 5)[0]
        m = re.fullmatch(r"https://t\.me/mybot/app\?startapp=r_([A-Za-z0-9]{16})", _btn_url(row[0]))
        self.assertIsNotNone(m, _btn_url(row[0]))
        self.assertEqual(self.b.store.article_url(m.group(1)), wx)  # exact URL: scheme and fragment kept
        self.assertEqual((row[1].text, _btn_url(row[1])), ("🔗 原链接", wx))
        self.b.cfg.bridge.article_reader = False  # deprecated: no effect any more
        self.assertEqual(self.web.reader_kind(wx), "link")
        # Notes still go through the reader: canonical URL, one button (the reader page has its own 打开原文).
        with mock.patch.object(self.web, "_prefetch"):
            link = self.web.article_link(NOTE_URL + "#frag")
            row = self.b._card_buttons({"url": NOTE_URL}, 5)[0]
        m = re.fullmatch(r"https://t\.me/mybot/app\?startapp=r_([A-Za-z0-9]{16})", link or "")
        self.assertIsNotNone(m, link)
        self.assertEqual(self.b.store.article_url(m.group(1)), NOTE_URL)
        self.assertEqual([_btn_url(b) for b in row], [link])
        self.assertIsNone(self.web.article_link("https://t.me/somechannel/12"))  # Telegram opens these natively
        self.b.cfg.bridge.links_in_app = False
        self.assertIsNone(self.web.article_link(ART_URL))
        self.assertEqual(_btn_url(self.b._card_buttons({"url": ART_URL}, 5)[0][0]), ART_URL)
        self.b.cfg.bridge.links_in_app = True
        self.web.app_name = ""
        self.assertIsNone(self.web.article_link(ART_URL))

    async def test_old_article_reader_links_open_the_original(self):
        # Cards sent before the change carry r_<id> links to an mp.weixin URL: hand them over, never fetch.
        aid = self.b.store.article_id(ART_URL)
        with mock.patch.object(A, "fetch_article", side_effect=AssertionError("must not fetch")):
            async with self.http.get(f"{self.base}/read/{aid}") as r:
                self.assertEqual(r.status, 200)
                text = await r.text()
        self.assertIn("location.replace", text)
        self.assertIn(ART_URL, text)
        self.assertNotIn("retry=1", text)

    def test_call_tokens_never_look_like_reader_links(self):
        with mock.patch("bridge.callweb.secrets.token_urlsafe", side_effect=["r_abc", "r_def", "callTok"]):
            self.assertEqual(self.web._new_token("call", "c1"), "callTok")

    async def test_reader_page(self):
        aid = self.b.store.article_id(NOTE_URL)
        calls = []

        async def fake_fetch(url):
            calls.append(url)
            await asyncio.sleep(0.05)
            return A.Article(url=url, title="测试 & 标题", account="某博主", content="<p>第一段</p>")

        with mock.patch.object(A, "fetch_article", side_effect=fake_fetch):
            async def get(path):
                async with self.http.get(self.base + path) as r:
                    return r.status, r.headers, await r.text()
            (s1, h1, t1), (s2, _, _) = await asyncio.gather(get(f"/read/{aid}"), get(f"/read/{aid}"))
            self.assertEqual((s1, s2), (200, 200))
            self.assertEqual(calls, [NOTE_URL])  # concurrent taps share one download
            self.assertIn("测试 &amp; 标题", t1)
            self.assertIn("第一段", t1)
            self.assertEqual(h1["Referrer-Policy"], "no-referrer")
            nonce = re.search(r"'nonce-([^']+)'", h1["Content-Security-Policy"]).group(1)
            self.assertIn(f'<script nonce="{nonce}">', t1)
            # The Mini App's root URL serves the reader when the start parameter says so (cached now).
            s3, _, t3 = await get(f"/?tgWebAppStartParam=r_{aid}")
            self.assertEqual((s3, len(calls)), (200, 1))
            self.assertIn("第一段", t3)
            # The call page itself redirects r_ start parameters in the browser.
            _, _, call_page = await get("/")
            self.assertIn("'/read/'", call_page)
            # ... from <head>, before the call page's styles or UI exist, so the call screen never flashes.
            head_redirect = call_page.index("window.__wxReader = true")
            self.assertLess(head_redirect, call_page.index("<style>"))
            self.assertLess(head_redirect, call_page.index("<body"))
            self.assertIn('(window.__wxReader) return;', call_page)
            self.assertNotIn("__CFG__", call_page)
            s4, _, t4 = await get("/read/unknownid123")
            self.assertEqual(s4, 404)
            self.assertIn("链接已失效", t4)
            s5, _, _ = await get("/read/..%2Fetc")
            self.assertEqual(s5, 404)

    async def test_reader_page_fetch_failure(self):
        aid = self.b.store.article_id(NOTE_URL)
        with mock.patch.object(A, "fetch_article", side_effect=A.ArticleError("笔记已被删除")):
            async with self.http.get(f"{self.base}/read/{aid}") as r:
                self.assertEqual(r.status, 502)
                text = await r.text()
        self.assertIn("笔记已被删除", text)
        self.assertIn(f'href="{NOTE_URL}"', text)
        self.assertIn(f'href="/wxcall/read/{aid}?retry=1"', text)
        self.assertNotIn("location.replace", text)
        self.assertIsNone(self.b.store.get_cached_article(NOTE_URL))

    async def test_reader_page_auto_redirects_in_mini_app_when_blocked(self):
        aid = self.b.store.article_id(NOTE_URL)
        err = A.ArticleError("小红书要求登录，正在打开原笔记…", code="login_wall", fallback_original=True)
        with mock.patch.object(A, "fetch_article", side_effect=err):
            async with self.http.get(f"{self.base}/read/{aid}") as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")
                nonce = re.search(r"'nonce-([^']+)'", r.headers["Content-Security-Policy"]).group(1)
                text = await r.text()
        self.assertIn(f'<script nonce="{nonce}">', text)
        self.assertIn("location.replace", text)
        self.assertIn(NOTE_URL, text)
        self.assertIn("web_app_expand", text)
        self.assertIn("web_app_ready", text)
        self.assertIn(f'href="/wxcall/read/{aid}?retry=1"', text)
        self.assertIsNone(self.b.store.get_cached_article(NOTE_URL))

    def test_reader_html_supports_dark_mode_and_lightbox(self):
        page = A.render_page(A.Article(url=NOTE_URL, title="t", content='<p><img src="https://sns-img.xhscdn.com/a"></p>'),
                             nonce="n2", retry_url="/read/abc?retry=1")
        self.assertIn("prefers-color-scheme: dark", page)
        self.assertIn("tgWebAppThemeParams", page)
        self.assertIn('id="lightbox"', page)
        self.assertIn("data-inapp", page)

    async def test_reader_persists_cache_across_memory_clear_and_supports_retry(self):
        aid = self.b.store.article_id(NOTE_URL)
        calls = 0

        async def fake_fetch(url):
            nonlocal calls
            calls += 1
            return A.Article(url=url, title=f"版本 {calls}", account="某号", content=f"<p>第 {calls} 次抓取</p>")

        with mock.patch.object(A, "fetch_article", side_effect=fake_fetch):
            async with self.http.get(f"{self.base}/read/{aid}") as r1:
                self.assertEqual(r1.status, 200)
                t1 = await r1.text()
            self.assertIn("第 1 次抓取", t1)
            self.assertEqual(calls, 1)

            # Simulate restart by replacing in-memory cache; SQLite still serves it without refetching.
            self.web.article_cache = A.ArticleCache()
            async with self.http.get(f"{self.base}/read/{aid}") as r2:
                self.assertEqual(r2.status, 200)
                t2 = await r2.text()
            self.assertIn("第 1 次抓取", t2)
            self.assertEqual(calls, 1)

            # Explicit retry bypasses both memory and SQLite caches.
            async with self.http.get(f"{self.base}/read/{aid}?retry=1") as r3:
                self.assertEqual(r3.status, 200)
                t3 = await r3.text()
            self.assertIn("第 2 次抓取", t3)
            self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
