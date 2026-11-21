"""Official Account article reader: parsing / sanitizing, fetching, and the CallWeb reader page."""
from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from bridge import article as A
from bridge.config import BridgeConfig, Config, TelegramConfig, WeChatConfig
from bridge.core import Bridge, _link_buttons
from bridge.store import Store

ART_URL = "https://mp.weixin.qq.com/s/abcDEF123"

PAGE = """<!doctype html><html><head>
<meta property="og:title" content="测试 &amp; 标题" />
<meta property="og:image" content="http://mmbiz.qpic.cn/cover/0?wx_fmt=jpeg" />
<meta property="og:description" content="摘要" />
<meta property="og:article:author" content="作者甲" />
<script>var nickname = htmlDecode("某公众号"); var ct = "1700000000";</script>
</head><body>
<h1 id="activity-name">测试标题</h1>
<div class="rich_media_content" id="js_content" style="visibility: hidden; opacity: 0;">
  <section style="color: red; position: fixed; width: expression(alert(1))"><p onclick="evil()">第一段<strong>加粗</strong>
  <p>没闭合的段落
  <img class="rich_pages" data-src="http://mmbiz.qpic.cn/a/640?wx_fmt=png" data-w="600" data-ratio="0.5" src="data:x" />
  <a href="javascript:alert(1)">坏链接</a><a href="https://example.com/x?a=1&amp;b=2">好链接</a>
  <script>alert('x')</script><style>body{display:none}</style>
  <svg viewBox="0 0 1 1"><text>svg 文字</text></svg>
  <iframe class="video_iframe" data-src="https://v.qq.com/x"></iframe>
  <mp-custom>保留的子内容</mp-custom>
  <section style="background-image: url(http://mmbiz.qpic.cn/bg/0)">背景</section>
  <section style="background-image: url(javascript:alert(1)); color: blue">坏背景</section>
  </section>
</div>
<div id="js_tags">不属于正文</div>
<script>var tail = "</div>";</script>
</body></html>"""


def _btn_url(btn):
    return btn.type.url if hasattr(btn, "type") and hasattr(btn.type, "url") else btn.url


class ParseTests(unittest.TestCase):
    def test_is_article_url_and_normalize(self):
        self.assertTrue(A.is_article_url("http://mp.weixin.qq.com/s?__biz=MzA&mid=1&idx=1&sn=x#rd"))
        self.assertTrue(A.is_article_url(ART_URL))
        self.assertFalse(A.is_article_url("https://mp.weixin.qq.com/cgi-bin/home"))
        self.assertFalse(A.is_article_url("https://evil.com/s/abc"))
        self.assertFalse(A.is_article_url("https://mp.weixin.qq.com.evil.com/s/abc"))
        self.assertFalse(A.is_article_url(None))
        self.assertEqual(A.normalize_url("http://mp.weixin.qq.com/s?__biz=MzA&amp;mid=1#rd"),
                         "https://mp.weixin.qq.com/s?__biz=MzA&mid=1")

    def test_parse_and_sanitize(self):
        art = A.parse_article(PAGE, ART_URL)
        self.assertEqual(art.title, "测试 & 标题")
        self.assertEqual(art.account, "某公众号")
        self.assertEqual(art.author, "作者甲")
        self.assertEqual(art.published, 1700000000)
        self.assertEqual(art.cover, "https://mmbiz.qpic.cn/cover/0?wx_fmt=jpeg")
        c = art.content
        self.assertIn("第一段<strong>加粗</strong>", c)
        self.assertIn("没闭合的段落", c)
        self.assertIn('src="https://mmbiz.qpic.cn/a/640?wx_fmt=png"', c)
        self.assertIn('width="600" height="300"', c)
        self.assertIn('referrerpolicy="no-referrer"', c)
        self.assertIn('href="https://example.com/x?a=1&amp;b=2"', c)
        self.assertIn("保留的子内容", c)
        self.assertIn("［视频，请打开原文查看］", c)
        self.assertIn("url(&quot;https://mmbiz.qpic.cn/bg/0&quot;)", c)
        self.assertIn("color: blue", c)
        self.assertIn("color: red", c)
        for bad in ("onclick", "javascript", "alert", "<script", "<style", "display:none", "svg", "expression",
                    "position", "data:x", "不属于正文", "mp-custom", "<iframe"):
            self.assertNotIn(bad, c)
        # Every emitted element is closed again.
        for tag in ("section", "p", "a", "strong"):
            self.assertEqual(c.count(f"<{tag}>") + c.count(f"<{tag} "), c.count(f"</{tag}>"), tag)

    def test_blocked_and_empty_pages(self):
        with self.assertRaisesRegex(A.ArticleError, "已被发布者删除") as cm_del:
            A.parse_article("<html><body>该内容已被发布者删除</body></html>", ART_URL)
        self.assertFalse(cm_del.exception.fallback_original)
        with self.assertRaisesRegex(A.ArticleError, "验证") as cm_cap:
            A.parse_article("<html><body>当前环境异常，完成验证后即可继续访问</body></html>", ART_URL)
        self.assertTrue(cm_cap.exception.fallback_original)
        self.assertEqual(cm_cap.exception.code, "captcha")
        # Picture-style posts without a #js_content body still get title + summary + cover.
        art = A.parse_article('<meta property="og:title" content="图片消息" />'
                              '<meta property="og:description" content="说明" />', ART_URL)
        self.assertEqual((art.title, art.description, art.content), ("图片消息", "说明", ""))
        page = A.render_page(art, nonce="n1")
        self.assertIn("暂不支持阅读模式", page)
        self.assertIn('<script nonce="n1">', page)

    def test_parse_picture_post_xiaolvshu(self):
        pic_page = """<!doctype html><html><head>
        <meta property="og:title" content="春日随拍" />
        <meta property="og:description" content="备用摘要" />
        <script>
        var nickname = "摄影日记";
        var ct = "1710000000";
        var content_noencode = JsDecode('今天天气真好\\x0a第二行 <b>转义</b>');
        window.picture_page_info_list = [
          {
            width: '1080',
            height: '1440',
            cdn_url: 'http://mmecoa.qpic.cn/mmecoa_jpg/pic1/0?wx_fmt=jpeg\\x26amp;from=appmsg',
            watermark_info: {
              cdn_url: 'http://mmecoa.qpic.cn/mmecoa_jpg/watermark_ignore/0',
            },
          },
          {
            "width": 1200,
            "height": 800,
            "cdn_url": "https://mmbiz.qpic.cn/sz_mmbiz_png/pic2/0?wx_fmt=png",
          },
        ].slice(0, 20);
        </script>
        </head><body></body></html>"""
        art = A.parse_article(pic_page, ART_URL)
        self.assertEqual(art.title, "春日随拍")
        self.assertEqual(art.account, "摄影日记")
        self.assertEqual(art.published, 1710000000)
        self.assertIn("今天天气真好", art.content)
        self.assertIn("第二行 &lt;b&gt;转义&lt;/b&gt;", art.content)
        self.assertIn('src="https://mmecoa.qpic.cn/mmecoa_jpg/pic1/0?wx_fmt=jpeg&amp;from=appmsg"', art.content)
        self.assertIn('width="1080" height="1440"', art.content)
        self.assertIn('src="https://mmbiz.qpic.cn/sz_mmbiz_png/pic2/0?wx_fmt=png"', art.content)
        self.assertNotIn("watermark_ignore", art.content)
        self.assertNotIn("暂不支持阅读模式", A.render_page(art, nonce="n1"))

    def test_parse_croporisrc_and_code_blocks(self):
        code_page = """<html><head><meta property="og:title" content="代码文" /></head><body>
        <div id="js_content">
          <img data-croporisrc="http://mmbiz.qpic.cn/crop/0?wx_fmt=jpeg" />
          <pre><code>x = 1 &lt; 2</code></pre>
        </div></body></html>"""
        art = A.parse_article(code_page, ART_URL)
        self.assertIn('src="https://mmbiz.qpic.cn/crop/0?wx_fmt=jpeg"', art.content)
        self.assertIn("<pre><code>x = 1 &lt; 2</code></pre>", art.content)

    def test_render_escapes(self):
        art = A.Article(url=ART_URL, title="<b>x</b>", account="号", content="<p>正文</p>")
        page = A.render_page(art, nonce="abc")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)
        self.assertIn("<p>正文</p>", page)
        # 打开原文 stays inside the Mini App (no web_app_open_link hand-off to an external browser)
        self.assertIn(f'<a class="open" id="open" data-inapp="1" href="{ART_URL}">打开原文</a>', page)
        self.assertNotIn('target="_blank"', page)
        self.assertIn('<meta name="referrer" content="no-referrer">', page)
        dated = A.render_page(A.Article(url=ART_URL, title="t", account="号", published=1607438787), nonce="")
        self.assertIn("号 · 2020-12-08 22:46", dated)  # Beijing time, whatever the server's timezone
        err = A.render_page(None, error="<坏>", source_url=ART_URL)
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
    async def test_fetch_follows_same_host_redirects_only(self):
        seen = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(str(req.url))
            if req.url.path == "/s/abcDEF123":
                return httpx.Response(302, headers={"location": "/s?__biz=X&mid=1#wechat_redirect"})
            if req.url.path == "/s/away":
                return httpx.Response(302, headers={"location": "https://evil.example/s/1"})
            if req.url.path == "/s/gone":
                return httpx.Response(404)
            return httpx.Response(200, content=PAGE.encode(), headers={"content-type": "text/html; charset=utf-8"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            art = await A.fetch_article(ART_URL, client)
            self.assertEqual(art.title, "测试 & 标题")
            self.assertEqual(art.url, "https://mp.weixin.qq.com/s?__biz=X&mid=1")
            self.assertEqual(seen, [ART_URL, "https://mp.weixin.qq.com/s?__biz=X&mid=1"])
            with self.assertRaisesRegex(A.ArticleError, "其他网站"):
                await A.fetch_article("https://mp.weixin.qq.com/s/away", client)
            with self.assertRaisesRegex(A.ArticleError, "404") as cm_404:
                await A.fetch_article("https://mp.weixin.qq.com/s/gone", client)
            self.assertFalse(cm_404.exception.fallback_original)
            with self.assertRaisesRegex(A.ArticleError, "不是"):
                await A.fetch_article("https://example.com/s/1", client)
            with mock.patch.object(A, "MAX_PAGE_BYTES", 100):
                with self.assertRaisesRegex(A.ArticleError, "过大"):
                    await A.fetch_article("https://mp.weixin.qq.com/s/big", client)

    async def test_fetch_captcha_redirect_and_rate_limit_mark_fallback(self):
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/s/cap":
                return httpx.Response(302, headers={"location": "/mp/wappoc_appmsgcaptcha?poc_token=abc"})
            if req.url.path == "/s/r403":
                return httpx.Response(403)
            if req.url.path == "/s/r429":
                return httpx.Response(429)
            return httpx.Response(200, content=PAGE.encode())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(A.ArticleError) as cm_cap:
                await A.fetch_article("https://mp.weixin.qq.com/s/cap", client)
            self.assertTrue(cm_cap.exception.fallback_original)
            self.assertEqual(cm_cap.exception.code, "captcha")
            for path in ("/s/r403", "/s/r429"):
                with self.assertRaises(A.ArticleError) as cm:
                    await A.fetch_article("https://mp.weixin.qq.com" + path, client)
                self.assertTrue(cm.exception.fallback_original)
                self.assertEqual(cm.exception.code, "rate_limit")

    async def test_fetcher_concurrency_and_curl_cffi_session(self):
        active = 0
        peak = 0
        created_kwargs = {}

        class FakeResp:
            is_redirect = False
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            encoding = "utf-8"

            async def aiter_content(self):
                yield PAGE.encode()

        class FakeStream:
            async def __aenter__(self):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.03)
                return FakeResp()

            async def __aexit__(self, *exc):
                nonlocal active
                active -= 1

        class FakeCurlSession:
            def __init__(self, **kw):
                created_kwargs.update(kw)
                self.closed = False

            def stream(self, method, url, **kw):
                return FakeStream()

            async def close(self):
                self.closed = True

        with mock.patch.object(A, "_CurlAsyncSession", FakeCurlSession):
            fetcher = A.ArticleFetcher(proxy="socks5://127.0.0.1:7890", max_concurrency=1)
            with A.use_fetcher(fetcher):
                a1, a2 = await asyncio.gather(
                    A.fetch_article(ART_URL),
                    A.fetch_article(ART_URL + "2"),
                )
            self.assertEqual((a1.title, a2.title), ("测试 & 标题", "测试 & 标题"))
            self.assertEqual(peak, 1)
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
            aid1 = store.article_id("https://mp.weixin.qq.com/s/a1")
            store.save_cached_article("https://mp.weixin.qq.com/s/a1", {
                "url": "https://mp.weixin.qq.com/s/a1", "title": "第一篇", "account": "号1",
                "author": "甲", "published": 1700000001, "cover": "", "description": "摘1", "content": "<p>正文1</p>",
            }, max_entries=2)
            store.save_cached_article("https://mp.weixin.qq.com/s/a2", {
                "url": "https://mp.weixin.qq.com/s/a2", "title": "第二篇", "account": "号2",
                "author": "", "published": 1700000002, "cover": "", "description": "", "content": "<p>正文2</p>",
            }, max_entries=2)
            # Touch a1 so a2 becomes oldest, then insert a3 -> a2 evicted, a1 & a3 kept.
            hit1 = store.get_cached_article("https://mp.weixin.qq.com/s/a1")
            self.assertIsNotNone(hit1)
            self.assertEqual(hit1["title"], "第一篇")
            self.assertEqual(hit1["content"], "<p>正文1</p>")
            store.save_cached_article("https://mp.weixin.qq.com/s/a3", {
                "url": "https://mp.weixin.qq.com/s/a3", "title": "第三篇", "account": "号3",
                "author": "", "published": 1700000003, "cover": "", "description": "", "content": "<p>正文3</p>",
            }, max_entries=2)
            self.assertIsNotNone(store.get_cached_article("https://mp.weixin.qq.com/s/a1"))
            self.assertIsNone(store.get_cached_article("https://mp.weixin.qq.com/s/a2"))
            self.assertIsNotNone(store.get_cached_article("https://mp.weixin.qq.com/s/a3"))
            # Short ID mapping remains intact even after cache deletion.
            store.delete_cached_article("https://mp.weixin.qq.com/s/a1")
            self.assertIsNone(store.get_cached_article("https://mp.weixin.qq.com/s/a1"))
            self.assertEqual(store.article_url(aid1), "https://mp.weixin.qq.com/s/a1")
            store.db.close()


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

    def test_article_link(self):
        link = self.web.article_link("http://mp.weixin.qq.com/s/abcDEF123#rd")
        m = re.fullmatch(r"https://t\.me/mybot/app\?startapp=r_([A-Za-z0-9]{16})", link or "")
        self.assertIsNotNone(m, link)
        self.assertEqual(self.b.store.article_url(m.group(1)), ART_URL)
        self.assertIsNone(self.web.article_link("https://www.xiaohongshu.com/discovery/item/1"))
        # The card button uses it; other links keep their own URL.
        btn = _link_buttons({"url": ART_URL}, 5, self.b._reader_link({"url": ART_URL}))[0][0]
        self.assertEqual(_btn_url(btn), link)
        xhs = "https://www.xiaohongshu.com/x"
        self.assertEqual(_btn_url(_link_buttons({"url": xhs}, 5, self.b._reader_link({"url": xhs}))[0][0]), xhs)
        self.b.cfg.bridge.article_reader = False
        self.assertIsNone(self.web.article_link(ART_URL))
        self.b.cfg.bridge.article_reader = True
        self.web.app_name = ""
        self.assertIsNone(self.web.article_link(ART_URL))

    def test_call_tokens_never_look_like_reader_links(self):
        with mock.patch("bridge.callweb.secrets.token_urlsafe", side_effect=["r_abc", "r_def", "callTok"]):
            self.assertEqual(self.web._new_token("call", "c1"), "callTok")

    async def test_reader_page(self):
        aid = self.b.store.article_id(ART_URL)
        calls = []

        async def fake_fetch(url):
            calls.append(url)
            await asyncio.sleep(0.05)
            return A.parse_article(PAGE, url)

        with mock.patch.object(A, "fetch_article", side_effect=fake_fetch):
            async def get(path):
                async with self.http.get(self.base + path) as r:
                    return r.status, r.headers, await r.text()
            (s1, h1, t1), (s2, _, _) = await asyncio.gather(get(f"/read/{aid}"), get(f"/read/{aid}"))
            self.assertEqual((s1, s2), (200, 200))
            self.assertEqual(calls, [ART_URL])  # concurrent taps share one download
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
        aid = self.b.store.article_id(ART_URL)
        with mock.patch.object(A, "fetch_article", side_effect=A.ArticleError("该内容已被发布者删除")):
            async with self.http.get(f"{self.base}/read/{aid}") as r:
                self.assertEqual(r.status, 502)
                text = await r.text()
        self.assertIn("该内容已被发布者删除", text)
        self.assertIn(f'href="{ART_URL}"', text)
        self.assertIn(f'href="/wxcall/read/{aid}?retry=1"', text)
        self.assertNotIn("location.replace", text)
        self.assertIsNone(self.b.store.get_cached_article(ART_URL))

    async def test_reader_page_auto_redirects_in_mini_app_on_captcha(self):
        aid = self.b.store.article_id(ART_URL)
        err = A.ArticleError("微信要求验证访问环境，正在打开原文…", code="captcha", fallback_original=True)
        with mock.patch.object(A, "fetch_article", side_effect=err):
            async with self.http.get(f"{self.base}/read/{aid}") as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")
                nonce = re.search(r"'nonce-([^']+)'", r.headers["Content-Security-Policy"]).group(1)
                text = await r.text()
        self.assertIn(f'<script nonce="{nonce}">', text)
        self.assertIn("location.replace", text)
        self.assertIn(ART_URL, text)
        self.assertIn("web_app_expand", text)
        self.assertIn("web_app_ready", text)
        self.assertIn(f'href="/wxcall/read/{aid}?retry=1"', text)
        self.assertIsNone(self.b.store.get_cached_article(ART_URL))

    def test_reader_html_supports_dark_mode_and_lightbox(self):
        page = A.render_page(A.Article(url=ART_URL, title="t", content='<p><img src="https://mmbiz.qpic.cn/a/0"></p>'),
                             nonce="n2", retry_url="/read/abc?retry=1")
        self.assertIn("prefers-color-scheme: dark", page)
        self.assertIn("tgWebAppThemeParams", page)
        self.assertIn('id="lightbox"', page)
        self.assertIn("data-inapp", page)

    async def test_reader_persists_cache_across_memory_clear_and_supports_retry(self):
        aid = self.b.store.article_id(ART_URL)
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
