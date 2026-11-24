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

from .mem import maybe_trim_memory
from .web import load_text

_UNSET = object()
_CurlAsyncSession: Any = _UNSET
_CURL_ERRORS: tuple[type[BaseException], ...] = ()


def _load_curl_cffi() -> tuple[Any, tuple[type[BaseException], ...]]:
    """Lazily import curl_cffi (libcurl-impersonate + BoringSSL) on first reader fetch."""
    global _CurlAsyncSession, _CURL_ERRORS
    if _CurlAsyncSession is _UNSET:
        try:
            from curl_cffi.requests import AsyncSession as _Session
            from curl_cffi.requests.exceptions import CurlError as _CurlBaseError, RequestException as _CurlReqError
            _CurlAsyncSession = _Session
            _CURL_ERRORS = (_CurlBaseError, _CurlReqError)
        except ImportError:  # pragma: no cover
            _CurlAsyncSession = None
            _CURL_ERRORS = ()
    return _CurlAsyncSession, _CURL_ERRORS


MAX_PAGE_BYTES = 12 * 1024 * 1024  # note pages are mostly inline script; several MB is common
FETCH_TIMEOUT = 20.0
MAX_REDIRECTS = 5
MAX_FETCH_CONCURRENCY = 2
SESSION_IDLE_TIMEOUT = 300.0  # close idle curl_cffi / httpx session after 5 min to free TLS/pool buffers
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
    net_errors = _load_curl_cffi()[1] if uses_curl else _CURL_ERRORS
    if xhs.is_note_url(url):
        return await xhs.fetch_note(url, session, uses_curl=uses_curl, net_errors=net_errors)
    for site in (douyin, bilibili):
        if site.is_video_url(url):
            return await site.fetch_video(url, session, uses_curl=uses_curl, net_errors=net_errors)
    raise ArticleError("此链接不支持阅读模式", code="invalid_url")


class ArticleFetcher:
    """Reusable session (curl_cffi TLS impersonation + cookies, or httpx fallback) with concurrency cap."""

    def __init__(self, *, proxy: str = "", max_concurrency: int = MAX_FETCH_CONCURRENCY,
                 impersonate: str = IMPERSONATE_BROWSER, idle_timeout: float = SESSION_IDLE_TIMEOUT):
        self.proxy = (proxy or "").strip()
        self.impersonate = impersonate
        self._max_concurrency = max(1, int(max_concurrency))
        self._idle_timeout = max(0.0, float(idle_timeout))
        self._sem: asyncio.Semaphore | None = None
        self._session: Any = None
        self._uses_curl = False
        self._active_fetches = 0
        self._idle_handle: asyncio.TimerHandle | None = None
        self._idle_close_task: asyncio.Task | None = None

    def _cancel_idle_close(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _schedule_idle_close(self) -> None:
        self._cancel_idle_close()
        if self._idle_timeout <= 0 or self._session is None or self._active_fetches > 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._idle_handle = loop.call_later(self._idle_timeout, self._on_idle_timeout)

    def _on_idle_timeout(self) -> None:
        self._idle_handle = None
        if self._active_fetches > 0 or self._session is None:
            return
        self._idle_close_task = asyncio.create_task(self._close_on_idle())

    async def _close_on_idle(self) -> None:
        try:
            await self._close_session()
        finally:
            self._idle_close_task = None
            maybe_trim_memory(min_interval=0.0)

    def _ensure_session(self) -> tuple[Any, bool]:
        if self._session is None:
            curl_cls, _ = _load_curl_cffi()
            if curl_cls is not None:
                self._session = curl_cls(
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
                    trust_env=False,
                )
                self._uses_curl = False
        return self._session, self._uses_curl

    async def fetch(self, url: str) -> Article:
        self._cancel_idle_close()
        self._active_fetches += 1
        try:
            if self._idle_close_task is not None and not self._idle_close_task.done():
                await self._idle_close_task
            if self._sem is None:
                self._sem = asyncio.Semaphore(self._max_concurrency)
            async with self._sem:
                session, uses_curl = self._ensure_session()
                return await _fetch_with_session(url, session, uses_curl=uses_curl)
        finally:
            self._active_fetches -= 1
            if self._active_fetches == 0:
                self._schedule_idle_close()

    async def _close_session(self) -> None:
        if self._session is None:
            return
        sess, self._session = self._session, None
        if hasattr(sess, "aclose"):
            await sess.aclose()
        elif hasattr(sess, "close"):
            res = sess.close()
            if asyncio.iscoroutine(res):
                await res

    async def close(self) -> None:
        self._cancel_idle_close()
        task = self._idle_close_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            await task
        await self._close_session()


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

    def __init__(self, size: int = 16, ttl: float = 1800.0, fail_ttl: float = 60.0):
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


READER_HTML = load_text("reader.html")
REDIRECT_HTML = load_text("redirect.html")
