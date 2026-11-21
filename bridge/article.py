"""Read WeChat Official Account articles (mp.weixin.qq.com) inside Telegram.

The bridge fetches the article page server-side, keeps only the article body (`#js_content`) through a tag /
attribute allowlist, and serves it as a lightweight reader page from the call web server, so a link card button
can open it as a Mini App (t.me links skip Telegram's "Open this link?" prompt).

WeChat image hosts refuse foreign Referers but serve requests without one, so the reader page sends no Referer
instead of proxying images.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import html
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

try:
    from curl_cffi.requests import AsyncSession as _CurlAsyncSession
    from curl_cffi.requests.exceptions import CurlError as _CurlBaseError, RequestException as _CurlReqError
    _CURL_ERRORS: tuple[type[BaseException], ...] = (_CurlBaseError, _CurlReqError)
except ImportError:  # pragma: no cover
    _CurlAsyncSession = None
    _CURL_ERRORS = ()

ARTICLE_HOST = "mp.weixin.qq.com"
MAX_PAGE_BYTES = 12 * 1024 * 1024  # article pages are mostly inline script; 3-4 MB is common
FETCH_TIMEOUT = 20.0
MAX_REDIRECTS = 5
MAX_FETCH_CONCURRENCY = 2
IMPERSONATE_BROWSER = "safari_ios"
USER_AGENT = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")

# Hosts whose plain-http image URLs we upgrade to https (the reader page is https; mixed content is blocked).
_HTTPS_IMAGE_HOSTS = ("qpic.cn", "qlogo.cn", "weixin.qq.com", "qq.com")

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
         "track", "wbr"}
_ALLOWED = {
    "section", "div", "p", "span", "strong", "b", "em", "i", "u", "s", "strike", "del", "ins", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "li", "a", "img", "figure", "figcaption",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "colgroup", "col", "pre", "code", "sup",
    "sub", "small", "big", "font", "center", "mark", "abbr", "cite", "q", "dl", "dt", "dd",
}
# Dropped together with everything inside them.
_DROP = {"script", "style", "svg", "noscript", "template", "object", "embed", "form", "input", "button",
         "textarea", "select", "canvas", "head", "title", "math", "link", "meta"}
# Embedded media the reader cannot play: replaced by a hint pointing at the original article.
_MEDIA = {"iframe": "视频", "video": "视频", "audio": "音频", "mpvoice": "音频", "mp-common-mpaudio": "音频",
          "mp-common-videosnap": "视频号内容", "mp-miniprogram": "小程序", "mp-common-product": "商品卡片",
          "qqmusic": "音乐"}
_STYLE_BLOCK = re.compile(r"expression\s*\(|javascript:|vbscript:|behavior\s*:|-moz-binding|"
                          r"position\s*:\s*(fixed|sticky)", re.I)
_CSS_URL = re.compile(r"url\(\s*(['\"]?)\s*([^'\")\s]+)\s*\1\s*\)", re.I)


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


def is_article_url(url: str | None) -> bool:
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    return (parts.scheme in ("http", "https") and (parts.hostname or "").lower() == ARTICLE_HOST
            and (parts.path == "/s" or parts.path.startswith("/s/") or parts.path.startswith("/s?")))


def normalize_url(url: str) -> str:
    """https, no fragment (WeChat appends #rd / #wechat_redirect)."""
    parts = urlsplit(html.unescape(url.strip()))
    return urlunsplit(("https", parts.netloc.lower(), parts.path, parts.query, ""))


def _https_image(url: str) -> str:
    url = html.unescape(url.strip())
    if url.startswith("//"):
        url = "https:" + url
    parts = urlsplit(url)
    if parts.scheme == "http" and (parts.hostname or "").endswith(_HTTPS_IMAGE_HOSTS):
        url = urlunsplit(("https",) + tuple(parts)[1:])
    return url if url.startswith("https://") else ""


def _safe_style(style: str) -> str:
    out = []
    for decl in style.split(";"):
        decl = decl.strip()
        if not decl or _STYLE_BLOCK.search(decl):
            continue
        if "url(" in decl.lower():
            bad = False

            def fix(m: re.Match) -> str:
                nonlocal bad
                u = _https_image(m.group(2))
                if not u:
                    bad = True
                return f'url("{u}")'
            decl = _CSS_URL.sub(fix, decl)
            if bad or "url(" in decl.lower() and not _CSS_URL.search(decl):
                continue
        out.append(decl)
    return "; ".join(out)


class _BodyCleaner(HTMLParser):
    """Copies the subtree of the element with id="js_content" through the allowlist."""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.out: list[str] = []
        self.found = False
        self.done = False
        self.stack: list[tuple[str, bool]] = []  # open elements inside the body: (tag, emitted)
        self.skip = 0  # >0 while inside a dropped subtree (depth of the stack where it started)
        self.text_len = 0
        self.images = 0

    # -- helpers ---------------------------------------------------------
    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        a = {k.lower(): (v or "") for k, v in attrs}
        keep: list[tuple[str, str]] = []
        if style := _safe_style(a.get("style", "")):
            keep.append(("style", style))
        if tag == "img":
            src = _https_image(a.get("data-src") or a.get("data-original") or a.get("data-croporisrc")
                               or a.get("src") or "")
            if not src:
                return ""
            keep.append(("src", src))
            keep.append(("loading", "lazy"))
            keep.append(("referrerpolicy", "no-referrer"))
            if alt := a.get("alt"):
                keep.append(("alt", alt))
            try:
                w = int(float(a.get("data-w") or 0))
                ratio = float(a.get("data-ratio") or 0)
            except ValueError:
                w, ratio = 0, 0.0
            if w > 0 and 0 < ratio < 50:
                keep += [("width", str(w)), ("height", str(max(1, round(w * ratio))))]
        elif tag == "a":
            href = html.unescape(a.get("href", "")).strip()
            if href:
                href = urljoin(self.base_url, href)
                if href.startswith(("https://", "http://")):
                    keep.append(("href", href))
        elif tag in ("td", "th"):
            for k in ("colspan", "rowspan"):
                if a.get(k, "").isdigit():
                    keep.append((k, a[k]))
        elif tag == "font" and a.get("color"):
            keep.append(("color", a["color"]))
        if tag in ("p", "div", "td", "th", "section", "h1", "h2", "h3", "h4", "h5", "h6") and \
                a.get("align", "").lower() in ("left", "right", "center", "justify"):
            keep.append(("align", a["align"].lower()))
        return "".join(f' {k}="{html.escape(v, quote=True)}"' for k, v in keep)

    # -- parser callbacks ------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        if self.done:
            return
        tag = tag.lower()
        if not self.found:
            if any(k == "id" and v == "js_content" for k, v in attrs):
                self.found = True
                self.stack = [(tag, False)]
            return
        void = tag in _VOID
        if self.skip:
            if not void:
                self.stack.append((tag, False))
            return
        if tag in _DROP or tag in _MEDIA:
            if tag in _MEDIA:
                self.out.append(f'<p class="wx-media">［{_MEDIA[tag]}，请打开原文查看］</p>')
            if not void:
                self.stack.append((tag, False))
                self.skip = len(self.stack)
            return
        emitted = False
        if tag in _ALLOWED:
            attr_s = self._attrs(tag, attrs)
            if tag == "img" and not attr_s:
                return
            self.out.append(f"<{tag}{attr_s}>")
            emitted = True
            if tag == "img":
                self.images += 1
        if not void:
            self.stack.append((tag, emitted))

    def handle_startendtag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        self.handle_starttag(tag, attrs)
        if self.found and not self.done and tag not in _VOID and self.stack and self.stack[-1][0] == tag:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.found or self.done:
            return
        tag = tag.lower()
        if tag in _VOID:
            return
        # Like a browser: the end tag closes the nearest open element with that name (and anything left open in
        # it); one that matches only #js_content itself ends the body.
        names = [t for t, _ in self.stack]
        if tag not in names:
            return
        if len(names) - 1 - names[::-1].index(tag) == 0:
            while len(self.stack) > 1:
                t, emitted = self.stack.pop()
                if emitted and not self.skip:
                    self.out.append(f"</{t}>")
                if self.skip and len(self.stack) < self.skip:
                    self.skip = 0
            self.done = True
            return
        while len(self.stack) > 1:
            t, emitted = self.stack.pop()
            if emitted and not self.skip:
                self.out.append(f"</{t}>")
            if self.skip and len(self.stack) < self.skip:
                self.skip = 0
            if t == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.found and not self.done and not self.skip:
            self.text_len += len(data.strip())
            self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while len(self.stack) > 1:
            t, emitted = self.stack.pop()
            if emitted and not self.skip:
                self.out.append(f"</{t}>")
        return "".join(self.out).strip()


def clean_body(page: str, base_url: str) -> str:
    """Sanitized HTML of the article body, or "" if the page has no (non-empty) #js_content."""
    i = page.find('id="js_content"')
    if i < 0:
        return ""
    start = page.rfind("<", 0, i)
    parser = _BodyCleaner(base_url)
    step = 64 * 1024
    for pos in range(start, len(page), step):  # stop early: the rest of the page is megabytes of script
        parser.feed(page[pos:pos + step])
        if parser.done:
            break
    else:
        parser.close()
    body = parser.result()
    return body if (parser.text_len or parser.images) else ""


_META_RE = re.compile(r'<meta\s+[^>]*?property="(og:[a-z:]+)"\s+content="([^"]*)"', re.I)


def _unescape_js(raw: str) -> str:
    try:
        raw = re.sub(r"\\x([0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})",
                     lambda x: chr(int(x.group(1) or x.group(2), 16)), raw)
    except ValueError:
        pass
    raw = (raw.replace("\\/", "/").replace("\\n", "\n").replace("\\r", "\r")
           .replace("\\t", "\t").replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\"))
    return html.unescape(raw)


def _js_string(page: str, pattern: str) -> str:
    m = re.search(pattern, page)
    if not m:
        return ""
    return _unescape_js(m.group(1)).strip()


def _scan_js_bracket(text: str, start: int, open_ch: str, close_ch: str) -> str:
    """Return the balanced JS slice starting at `start` (respecting quotes/escapes), or ""."""
    if start < 0 or start >= len(text) or text[start] != open_ch:
        return ""
    depth = 0
    quote = ""
    esc = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = ""
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return ""


def _top_level_js_objects(arr_src: str) -> list[str]:
    """Extract top-level `{...}` object literals from a JS array literal."""
    out: list[str] = []
    if len(arr_src) < 2:
        return out
    i, n = 1, len(arr_src) - 1
    quote = ""
    esc = False
    while i < n:
        ch = arr_src[i]
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if ch == "{":
            obj = _scan_js_bracket(arr_src, i, "{", "}")
            if not obj:
                break
            out.append(obj)
            i += len(obj)
            continue
        i += 1
    return out


def _strip_nested_js_blocks(obj_src: str) -> str:
    """Keep only top-level properties of a JS `{...}` literal by blanking nested `{...}`/`[...]`."""
    if len(obj_src) < 2:
        return ""
    out: list[str] = []
    depth = 0
    quote = ""
    esc = False
    for ch in obj_src[1:-1]:
        if quote:
            if depth == 0:
                out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = ""
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            if depth == 0:
                out.append(ch)
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _extract_js_prop(flat_obj: str, key: str) -> str:
    m = re.search(rf'(?:["\']?{re.escape(key)}["\']?)\s*:\s*(?:JsDecode\(\s*)?([\'"])((?:[^\\]|\\.)*?)\1', flat_obj)
    if m:
        return _unescape_js(m.group(2)).strip()
    m_num = re.search(rf'(?:["\']?{re.escape(key)}["\']?)\s*:\s*(\d+)', flat_obj)
    return m_num.group(1) if m_num else ""


_PIC_LIST_RE = re.compile(r"window\.picture_page_info_list\s*=\s*\[")


def _extract_picture_post(page: str, description: str) -> str:
    """Render WeChat picture posts ('小绿书', item_show_type=8) from window.picture_page_info_list."""
    m = _PIC_LIST_RE.search(page)
    if not m:
        return ""
    arr_src = _scan_js_bracket(page, m.end() - 1, "[", "]")
    if not arr_src:
        return ""
    figures: list[str] = []
    seen: set[str] = set()
    for obj in _top_level_js_objects(arr_src):
        flat = _strip_nested_js_blocks(obj)
        src = _https_image(_extract_js_prop(flat, "cdn_url"))
        if not src or src in seen:
            continue
        seen.add(src)
        attrs = [f'src="{html.escape(src, quote=True)}"', 'loading="lazy"', 'referrerpolicy="no-referrer"']
        try:
            w = int(float(_extract_js_prop(flat, "width") or 0))
            h = int(float(_extract_js_prop(flat, "height") or 0))
        except ValueError:
            w, h = 0, 0
        if w > 0 and h > 0:
            attrs += [f'width="{w}"', f'height="{h}"']
        figures.append(f'<figure class="wx-pic"><img {" ".join(attrs)}></figure>')
    if not figures:
        return ""
    caption = (
        _js_string(page, r"content_noencode\s*[:=]\s*JsDecode\('((?:[^'\\]|\\.)*)'\)")
        or _js_string(page, r'content_noencode\s*[:=]\s*JsDecode\("((?:[^"\\]|\\.)*)"\)')
        or _js_string(page, r"content_noencode\s*[:=]\s*'((?:[^'\\]|\\.)*)'")
        or _js_string(page, r'content_noencode\s*[:=]\s*"((?:[^"\\]|\\.)*)"')
        or description
    ).strip()
    parts: list[str] = []
    if caption:
        for para in re.split(r"\r?\n+", caption):
            para = para.strip()
            if para:
                parts.append(f"<p>{html.escape(para, quote=False)}</p>")
    parts.extend(figures)
    return "".join(parts)


_BLOCKED_MARKERS = (
    ("环境异常", "微信要求验证访问环境，正在打开原文…", "captcha", True),
    ("wappoc_appmsgcaptcha", "微信要求验证访问环境，正在打开原文…", "captcha", True),
    ("完成验证后即可继续访问", "微信要求验证访问环境，正在打开原文…", "captcha", True),
    ("已被发布者删除", "该内容已被发布者删除", "deleted", False),
    ("此内容因违规无法查看", "此内容因违规无法查看", "removed", False),
    ("此内容被投诉且经审核涉嫌侵权", "此内容因涉嫌侵权无法查看", "removed", False),
    ("该公众号已迁移", "该公众号已迁移，请打开原文查看", "migrated", False),
)


def parse_article(page: str, url: str) -> Article:
    metas: dict[str, str] = {}
    for k, v in _META_RE.findall(page):
        metas.setdefault(k.lower(), html.unescape(v).strip())
    description = metas.get("og:description", "")
    content = clean_body(page, url) or _extract_picture_post(page, description)
    title = (metas.get("og:title")
             or _js_string(page, r"var msg_title = '((?:[^'\\]|\\.)*)'")
             or _js_string(page, r'var msg_title = "((?:[^"\\]|\\.)*)"'))
    if not content and not title:
        for marker, why, code, fallback in _BLOCKED_MARKERS:
            if marker in page:
                raise ArticleError(why, code=code, fallback_original=fallback)
        raise ArticleError("无法解析文章内容，请打开原文查看", code="parse_failed")
    try:
        published = int(_js_string(page, r'var ct = "(\d+)"') or _js_string(page, r"create_time\s*[:=]\s*JsDecode\('(\d+)'\)") or 0)
    except ValueError:
        published = 0
    return Article(
        url=url,
        title=title or "微信文章",
        account=_js_string(page, r'var nickname = htmlDecode\("((?:[^"\\]|\\.)*)"\)')
        or _js_string(page, r'var nickname = "((?:[^"\\]|\\.)*)"')
        or _js_string(page, r"var nickname = '((?:[^'\\]|\\.)*)'"),
        author=metas.get("og:article:author", ""),
        published=published,
        cover=_https_image(metas.get("og:image", "") or _js_string(page, r'var msg_cdn_url = "((?:[^"\\]|\\.)*)"')),
        description=description,
        content=content,
    )


_CAPTCHA_PATH_MARKERS = ("wappoc_appmsgcaptcha", "antispider")
_CURRENT_FETCHER: contextvars.ContextVar[ArticleFetcher | None] = contextvars.ContextVar(
    "_CURRENT_FETCHER", default=None)


def _is_captcha_url(url: str) -> bool:
    low = (urlsplit(url).path or "").lower()
    return any(m in low for m in _CAPTCHA_PATH_MARKERS)


async def _fetch_with_session(url: str, session: Any, *, uses_curl: bool) -> Article:
    from . import douyin, xhs  # they import this module; keep the dependency one-way at import time
    if xhs.is_note_url(url):
        return await xhs.fetch_note(url, session, uses_curl=uses_curl, net_errors=_CURL_ERRORS)
    if douyin.is_video_url(url):
        return await douyin.fetch_video(url, session, uses_curl=uses_curl, net_errors=_CURL_ERRORS)
    if not is_article_url(url):
        raise ArticleError("不是微信公众号文章链接", code="invalid_url")
    current = normalize_url(url)
    headers = (
        {"Accept-Language": "zh-CN,zh-Hans;q=0.9"}
        if uses_curl else {
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    net_errors: tuple[type[BaseException], ...] = (httpx.HTTPError, *_CURL_ERRORS)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            async with session.stream("GET", current, headers=headers) as resp:
                if resp.is_redirect:
                    nxt = urljoin(current, resp.headers.get("location", ""))
                    if (urlsplit(nxt).hostname or "").lower() != ARTICLE_HOST:
                        raise ArticleError("文章跳转到了其他网站，请打开原文查看", code="off_host")
                    if _is_captcha_url(nxt):
                        raise ArticleError("微信要求验证访问环境，正在打开原文…", code="captcha", fallback_original=True)
                    current = normalize_url(nxt)
                    continue
                if resp.status_code in (403, 429):
                    raise ArticleError(f"微信限制了服务器访问（HTTP {resp.status_code}）",
                                       code="rate_limit", fallback_original=True)
                if resp.status_code != 200:
                    raise ArticleError(f"微信返回 HTTP {resp.status_code}", code=f"http_{resp.status_code}")
                buf = bytearray()
                aiter = resp.aiter_content() if hasattr(resp, "aiter_content") else resp.aiter_bytes()
                async for chunk in aiter:
                    buf += chunk
                    if len(buf) > MAX_PAGE_BYTES:
                        raise ArticleError("文章页面过大", code="too_large")
                page = buf.decode(getattr(resp, "encoding", None) or "utf-8", errors="replace")
                return parse_article(page, current)
        raise ArticleError("文章跳转次数过多", code="too_many_redirects")
    except net_errors as e:
        raise ArticleError(f"无法连接微信服务器（{type(e).__name__}）", code="network") from e


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
    """Download and parse an article; redirects must stay on mp.weixin.qq.com."""
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
_CHINA_TZ = timezone(timedelta(hours=8))  # WeChat shows publish times in Beijing time


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
    open_btn = (f'<a class="open" id="open" href="{html.escape(url)}" target="_blank" rel="noopener">打开原文</a>'
                if url else "")
    bar_inner = f'<div class="bar-inner">{retry_btn}{open_btn}</div>' if (retry_btn or open_btn) else ""
    return (READER_HTML.replace("__TITLE__", html.escape(title)).replace("__HEAD__", head)
            .replace("__BAR__", bar_inner).replace("__NONCE__", nonce).replace("__BODY__", body))


def render_redirect_page(source_url: str, *, reason: str = "", retry_url: str = "", nonce: str = "") -> str:
    """Mini App handoff page: expands Mini App and navigates in-place to the original WeChat URL."""
    safe_url = html.escape(source_url, quote=True)
    safe_reason = html.escape(reason or "微信要求验证访问环境，正在打开原文…")
    retry_btn = (f'<a class="retry" data-inapp="1" href="{html.escape(retry_url, quote=True)}">重试阅读模式</a>'
                 if retry_url else "")
    open_btn = f'<a class="open" data-inapp="1" href="{safe_url}">立即打开原文</a>' if source_url else ""
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
