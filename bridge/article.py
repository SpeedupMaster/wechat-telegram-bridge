"""Shared reader plumbing for the Mini App: one fetch session, caches, and the reader / handoff pages.

Site modules (xhs, douyin, bilibili) produce an `Article`; this module fetches through a reusable session and
renders it. WeChat Official Account articles are not fetched: their cards open the original page in place.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import html
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

try:
    from curl_cffi.requests import AsyncSession as _CurlAsyncSession
    from curl_cffi.requests.exceptions import CurlError as _CurlBaseError, RequestException as _CurlReqError
    _CURL_ERRORS: tuple[type[BaseException], ...] = (_CurlBaseError, _CurlReqError)
except ImportError:  # pragma: no cover
    _CurlAsyncSession = None
    _CURL_ERRORS = ()

MAX_PAGE_BYTES = 12 * 1024 * 1024  # note pages are mostly inline script; several MB is common
FETCH_TIMEOUT = 20.0
MAX_REDIRECTS = 5
MAX_FETCH_CONCURRENCY = 2
IMPERSONATE_BROWSER = "safari_ios"
USER_AGENT = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")


class ArticleError(Exception):
    """A user-facing reason why the article could not be shown."""

    def __init__(self, message: str, *, code: str = "error", fallback_original: bool = False):
        super().__init__(message)
        self.code = code
        self.fallback_original = fallback_original


@dataclass
class Article:
    url: str
    title: str
    account: str = ""
    author: str = ""
    published: int = 0
    cover: str = ""
    description: str = ""
    content: str = ""  # sanitized HTML of the article body ("" = show the description + cover only)


def normalize_url(url: str) -> str:
    """https, lowercase host, no fragment: the cache / article-id key for a reader URL."""
    parts = urlsplit(html.unescape(url.strip()))
    return urlunsplit(("https", parts.netloc.lower(), parts.path, parts.query, ""))


_CURRENT_FETCHER: contextvars.ContextVar[ArticleFetcher | None] = contextvars.ContextVar(
    "_CURRENT_FETCHER", default=None)


async def _fetch_with_session(url: str, session: Any, *, uses_curl: bool) -> Article:
    from . import bilibili, douyin, xhs  # they import this module; keep the dependency one-way at import time
    if xhs.is_note_url(url):
        return await xhs.fetch_note(url, session, uses_curl=uses_curl, net_errors=_CURL_ERRORS)
    for site in (douyin, bilibili):
        if site.is_video_url(url):
            return await site.fetch_video(url, session, uses_curl=uses_curl, net_errors=_CURL_ERRORS)
    raise ArticleError("此链接不支持阅读模式", code="invalid_url")


class ArticleFetcher:
    """Reusable session (curl_cffi TLS impersonation + cookies, or httpx fallback) with concurrency cap."""

    def __init__(self, *, proxy: str = "", max_concurrency: int = MAX_FETCH_CONCURRENCY,
                 impersonate: str = IMPERSONATE_BROWSER):
        self.proxy = (proxy or "").strip()
        self.impersonate = impersonate
        self._max_concurrency = max(1, int(max_concurrency))
        self._sem: asyncio.Semaphore | None = None
        self._session: Any = None
        self._uses_curl = False

    def _ensure_session(self) -> tuple[Any, bool]:
        if self._session is None:
            if _CurlAsyncSession is not None:
                self._session = _CurlAsyncSession(
                    impersonate=self.impersonate,
                    timeout=FETCH_TIMEOUT,
                    allow_redirects=False,
                    proxy=self.proxy or None,
                    default_headers=True,
                )
                self._uses_curl = True
            else:
                self._session = httpx.AsyncClient(
                    timeout=FETCH_TIMEOUT,
                    follow_redirects=False,
                    proxy=self.proxy or None,
                )
                self._uses_curl = False
        return self._session, self._uses_curl

    async def fetch(self, url: str) -> Article:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_concurrency)
        async with self._sem:
            session, uses_curl = self._ensure_session()
            return await _fetch_with_session(url, session, uses_curl=uses_curl)

    async def close(self) -> None:
        if self._session is None:
            return
        sess, self._session = self._session, None
        if hasattr(sess, "aclose"):
            await sess.aclose()
        elif hasattr(sess, "close"):
            res = sess.close()
            if asyncio.iscoroutine(res):
                await res


@contextlib.contextmanager
def use_fetcher(fetcher: ArticleFetcher | None):
    """Bind a reusable ArticleFetcher for calls to fetch_article() inside this context."""
    tok = _CURRENT_FETCHER.set(fetcher)
    try:
        yield
    finally:
        _CURRENT_FETCHER.reset(tok)


async def fetch_article(url: str, client: Any = None, *, proxy: str = "") -> Article:
    """Fetch a note / video page through the site module that handles its URL."""
    if isinstance(client, ArticleFetcher):
        return await client.fetch(url)
    if client is not None:
        return await _fetch_with_session(url, client, uses_curl=False)
    if (ctx_fetcher := _CURRENT_FETCHER.get()) is not None:
        return await ctx_fetcher.fetch(url)
    fetcher = ArticleFetcher(proxy=proxy)
    try:
        return await fetcher.fetch(url)
    finally:
        await fetcher.close()


class ArticleCache:
    """Small in-memory cache of parsed articles (and recent failures, briefly)."""

    def __init__(self, size: int = 32, ttl: float = 3600.0, fail_ttl: float = 60.0):
        self.size, self.ttl, self.fail_ttl = size, ttl, fail_ttl
        self._items: dict[str, tuple[float, Article | ArticleError]] = {}

    def get(self, url: str) -> Article | ArticleError | None:
        hit = self._items.get(url)
        if hit is None:
            return None
        ts, value = hit
        if time.monotonic() - ts > (self.fail_ttl if isinstance(value, ArticleError) else self.ttl):
            self._items.pop(url, None)
            return None
        self._items[url] = self._items.pop(url)  # LRU bump
        return value

    def put(self, url: str, value: Article | ArticleError) -> None:
        self._items.pop(url, None)
        self._items[url] = (time.monotonic(), value)
        while len(self._items) > self.size:
            self._items.pop(next(iter(self._items)))

    def delete(self, url: str) -> None:
        self._items.pop(url, None)


# ---------------------------------------------------------------------- reader page
_CHINA_TZ = timezone(timedelta(hours=8))  # the supported sites show publish times in Beijing time


def _fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, _CHINA_TZ).strftime("%Y-%m-%d %H:%M") if ts > 0 else ""


def _safe_js_literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")


def render_page(article: Article | None, *, error: str = "", source_url: str = "",
                nonce: str = "", retry_url: str = "") -> str:
    url = article.url if article else source_url
    if article is not None:
        meta = " · ".join(x for x in (article.account, article.author if article.author != article.account else "",
                                      _fmt_date(article.published)) if x)
        if article.content:
            body = article.content
        else:
            body = (f'<p>{html.escape(article.description)}</p>' if article.description else "") + \
                   (f'<p><img src="{html.escape(article.cover)}" referrerpolicy="no-referrer"></p>'
                    if article.cover else "") + \
                   '<p class="wx-media">［此类文章暂不支持阅读模式，请打开原文查看］</p>'
        head = (f'<h1 class="title">{html.escape(article.title)}</h1>'
                + (f'<div class="meta">{html.escape(meta)}</div>' if meta else ""))
        title = article.title
        retry_btn = ""
    else:
        title = "无法打开文章"
        head = '<h1 class="title">无法打开文章</h1>'
        body = f'<p class="err">{html.escape(error or "文章不存在或链接已失效")}</p>'
        retry_btn = (f'<a class="retry" data-inapp="1" href="{html.escape(retry_url)}">重试</a>'
                     if retry_url else "")
    open_btn = (f'<a class="open" id="open" data-inapp="1" href="{html.escape(url)}">打开原文</a>'
                if url else "")
    bar_inner = f'<div class="bar-inner">{retry_btn}{open_btn}</div>' if (retry_btn or open_btn) else ""
    return (READER_HTML.replace("__TITLE__", html.escape(title)).replace("__HEAD__", head)
            .replace("__BAR__", bar_inner).replace("__NONCE__", nonce).replace("__BODY__", body))


def render_redirect_page(source_url: str, *, reason: str = "", retry_url: str = "", nonce: str = "",
                         open_label: str = "立即打开原文") -> str:
    """Mini App handoff page: expands the Mini App and navigates in place to the original URL."""
    safe_url = html.escape(source_url, quote=True)
    safe_reason = html.escape(reason or "正在打开原文…")
    retry_btn = (f'<a class="retry" data-inapp="1" href="{html.escape(retry_url, quote=True)}">重试阅读模式</a>'
                 if retry_url else "")
    open_btn = (f'<a class="open" data-inapp="1" href="{safe_url}">{html.escape(open_label)}</a>'
                if source_url else "")
    return (REDIRECT_HTML
            .replace("__REASON__", safe_reason)
            .replace("__URL_ATTR__", safe_url)
            .replace("__URL_JS__", _safe_js_literal(source_url))
            .replace("__RETRY__", retry_btn)
            .replace("__OPEN__", open_btn)
            .replace("__NONCE__", nonce))


READER_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<title>__TITLE__</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #ffffff;
    --fg: #1f1f1f;
    --muted: #8a8a8a;
    --border: #eeeeee;
    --link: #576b95;
    --bar-bg: rgba(255,255,255,.94);
    --img-bg: #f3f3f3;
    --code-bg: #f6f8fa;
    --err: #c0392b;
    --btn-sec-bg: #f0f2f5;
    --btn-sec-fg: #1f1f1f;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #17181c;
      --fg: #e7e8ec;
      --muted: #989ba6;
      --border: #2b2d35;
      --link: #7aa7e0;
      --bar-bg: rgba(23,24,28,.94);
      --img-bg: #22242b;
      --code-bg: #1f222a;
      --err: #f07167;
      --btn-sec-bg: #262830;
      --btn-sec-fg: #e7e8ec;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--fg); }
  body { font: 17px/1.75 -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB",
         "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif; -webkit-text-size-adjust: 100%;
         overflow-wrap: break-word; }
  main { max-width: 680px; margin: 0 auto; padding: 20px 16px calc(88px + env(safe-area-inset-bottom)); }
  .title { font-size: 22px; line-height: 1.4; margin: 0 0 10px; font-weight: 700; }
  .meta { color: var(--muted); font-size: 14px; margin-bottom: 22px; }
  .content { overflow-x: hidden; }
  .content img { max-width: 100% !important; height: auto !important; background: var(--img-bg);
                 cursor: zoom-in; border-radius: 4px; }
  .content figure.wx-pic { margin: 14px 0; text-align: center; }
  .content .xhs-video { margin: 0 0 14px; }
  .content video { display: block; width: 100%; height: auto; max-height: 78vh; background: #000;
                   border-radius: 8px; }
  .content section, .content div, .content p, .content table { max-width: 100% !important; }
  .content table { border-collapse: collapse; display: block; overflow-x: auto; }
  .content pre { background: var(--code-bg); padding: 12px 14px; border-radius: 8px; overflow-x: auto;
                 font-size: 14px; line-height: 1.55; }
  .content code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .9em; }
  .content blockquote { margin: 12px 0; padding: 6px 12px; border-left: 3px solid var(--border); color: var(--muted); }
  .content a { color: var(--link); }
  .content p { margin: 0 0 8px; }
  .wx-media { color: var(--muted); font-size: 14px; text-align: center; margin: 12px 0 !important; }
  .err { color: var(--err); }
  .bar { position: fixed; left: 0; right: 0; bottom: 0; padding: 10px 16px calc(10px + env(safe-area-inset-bottom));
         background: var(--bar-bg); border-top: 1px solid var(--border); display: flex; justify-content: center;
         -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px); }
  .bar-inner { display: flex; gap: 10px; width: 100%; max-width: 680px; }
  .open, .retry { display: block; flex: 1; text-align: center; padding: 11px 14px; border-radius: 10px;
                  text-decoration: none; font-size: 16px; font-weight: 600; }
  .open { background: #07c160; color: #fff; }
  .retry { flex: 0 0 auto; min-width: 96px; background: var(--btn-sec-bg); color: var(--btn-sec-fg); }
  .wx-lightbox { position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,.92);
                 display: flex; align-items: center; justify-content: center; padding: 16px; cursor: zoom-out; }
  .wx-lightbox[hidden] { display: none; }
  .wx-lightbox img { max-width: 100%; max-height: 100%; object-fit: contain; border-radius: 4px; }
  @media (prefers-color-scheme: dark) {
    .content [style*="background-color: rgb(255, 255, 255)"],
    .content [style*="background-color:#fff"],
    .content [style*="background-color: #fff"] { background-color: transparent !important; }
  }
</style>
</head>
<body>
<main>
__HEAD__
<div class="content">__BODY__</div>
</main>
<div class="bar">__BAR__</div>
<div class="wx-lightbox" id="lightbox" hidden><img alt=""></div>
<script nonce="__NONCE__">
(function () {
  var hp = {};
  try {
    location.hash.slice(1).split('&').forEach(function (kv) {
      var i = kv.indexOf('=');
      if (i > 0) hp[decodeURIComponent(kv.slice(0, i))] = decodeURIComponent(kv.slice(i + 1));
    });
  } catch (e) {}
  var inTg = !!(hp.tgWebAppData || hp.tgWebAppVersion);
  try { if (!inTg) inTg = !!sessionStorage.getItem('tgWebAppData'); } catch (e) {}
  var theme = null;
  try {
    var rawTheme = hp.tgWebAppThemeParams || sessionStorage.getItem('tgWebAppThemeParams');
    if (rawTheme) {
      sessionStorage.setItem('tgWebAppThemeParams', rawTheme);
      theme = JSON.parse(rawTheme);
    }
  } catch (e) {}
  var root = document.documentElement;
  function okColor(c) { return typeof c === 'string' && /^#[0-9a-fA-F]{6}$/.test(c); }
  if (theme) {
    if (okColor(theme.bg_color)) root.style.setProperty('--bg', theme.bg_color);
    if (okColor(theme.text_color)) root.style.setProperty('--fg', theme.text_color);
    if (okColor(theme.hint_color)) root.style.setProperty('--muted', theme.hint_color);
    if (okColor(theme.link_color)) root.style.setProperty('--link', theme.link_color);
    if (okColor(theme.secondary_bg_color)) {
      root.style.setProperty('--btn-sec-bg', theme.secondary_bg_color);
      root.style.setProperty('--code-bg', theme.secondary_bg_color);
    }
  }
  function post(type, data) {
    try {
      if (window.TelegramWebviewProxy && window.TelegramWebviewProxy.postEvent) {
        window.TelegramWebviewProxy.postEvent(type, JSON.stringify(data || ''));
      } else if (window.external && 'notify' in window.external) {
        window.external.notify(JSON.stringify({eventType: type, eventData: data || ''}));
      } else if (window.parent !== window) {
        window.parent.postMessage(JSON.stringify({eventType: type, eventData: data || ''}), '*');
      } else { return false; }
      return true;
    } catch (e) { return false; }
  }
  var bg = (theme && okColor(theme.bg_color)) ? theme.bg_color : (
    window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? '#17181c' : '#ffffff'
  );
  if (inTg) {
    post('web_app_set_header_color', {color: bg});
    post('web_app_set_background_color', {color: bg});
    post('web_app_set_bottom_bar_color', {color: bg});
    post('web_app_expand');
    post('web_app_ready');
  }
  var lb = document.getElementById('lightbox');
  var lbImg = lb ? lb.querySelector('img') : null;
  if (lb && lbImg) {
    lb.addEventListener('click', function () { lb.hidden = true; lbImg.removeAttribute('src'); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !lb.hidden) { lb.hidden = true; lbImg.removeAttribute('src'); }
    });
  }
  // External links open in Telegram's browser; internal [data-inapp] links stay inside the Mini App.
  document.addEventListener('click', function (e) {
    var img = e.target && e.target.closest ? e.target.closest('.content img') : null;
    if (img && lb && lbImg && !img.closest('a[href]')) {
      lbImg.src = img.currentSrc || img.src;
      lb.hidden = false;
      return;
    }
    var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a || a.hasAttribute('data-inapp') || !/^https?:/i.test(a.href)) return;
    e.preventDefault();
    if (!(inTg && post('web_app_open_link', {url: a.href}))) window.open(a.href, '_blank', 'noopener');
  });
})();
</script>
</body>
</html>
"""

REDIRECT_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<noscript><meta http-equiv="refresh" content="0;url=__URL_ATTR__"></noscript>
<title>正在打开原文…</title>
<style>
  :root { color-scheme: light dark; --bg: #fff; --fg: #1f1f1f; --muted: #666; --btn-sec: #f0f2f5; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #17181c; --fg: #e7e8ec; --muted: #989ba6; --btn-sec: #262830; }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; min-height: 100%; background: var(--bg); color: var(--fg); }
  body { font: 16px/1.6 -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
         display: flex; align-items: center; justify-content: center; padding: 24px; }
  .box { width: 100%; max-width: 420px; text-align: center; }
  .msg { color: var(--muted); margin: 0 0 20px; }
  .open, .retry { display: block; width: 100%; padding: 12px 16px; border-radius: 10px;
                  text-decoration: none; font-weight: 600; margin-top: 10px; }
  .open { background: #07c160; color: #fff; }
  .retry { background: var(--btn-sec); color: var(--fg); }
</style>
</head>
<body>
<div class="box">
  <p class="msg">__REASON__</p>
  __OPEN__
  __RETRY__
</div>
<script nonce="__NONCE__">
(function () {
  function post(type, data) {
    try {
      if (window.TelegramWebviewProxy && window.TelegramWebviewProxy.postEvent) {
        window.TelegramWebviewProxy.postEvent(type, JSON.stringify(data || ''));
      } else if (window.external && 'notify' in window.external) {
        window.external.notify(JSON.stringify({eventType: type, eventData: data || ''}));
      } else if (window.parent !== window) {
        window.parent.postMessage(JSON.stringify({eventType: type, eventData: data || ''}), '*');
      }
    } catch (e) {}
  }
  post('web_app_expand');
  post('web_app_ready');
  var target = __URL_JS__;
  if (target && /^https?:\/\//i.test(target)) {
    window.location.replace(target);
  }
})();
</script>
</body>
</html>
"""
