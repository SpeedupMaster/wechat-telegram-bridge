"""Read WeChat Official Account articles (mp.weixin.qq.com) inside Telegram.

The bridge fetches the article page server-side, keeps only the article body (`#js_content`) through a tag /
attribute allowlist, and serves it as a lightweight reader page from the call web server, so a link card button
can open it as a Mini App (t.me links skip Telegram's "Open this link?" prompt).

WeChat image hosts refuse foreign Referers but serve requests without one, so the reader page sends no Referer
instead of proxying images.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

ARTICLE_HOST = "mp.weixin.qq.com"
MAX_PAGE_BYTES = 12 * 1024 * 1024  # article pages are mostly inline script; 3-4 MB is common
FETCH_TIMEOUT = 20.0
MAX_REDIRECTS = 5
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
            src = _https_image(a.get("data-src") or a.get("src") or "")
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


def _js_string(page: str, pattern: str) -> str:
    m = re.search(pattern, page)
    if not m:
        return ""
    raw = m.group(1)
    try:
        raw = re.sub(r"\\x([0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})",
                     lambda x: chr(int(x.group(1) or x.group(2), 16)), raw)
    except ValueError:
        pass
    return html.unescape(raw.replace("\\/", "/")).strip()


_BLOCKED_MARKERS = (
    ("环境异常", "微信要求验证访问环境，请打开原文查看"),
    ("已被发布者删除", "该内容已被发布者删除"),
    ("此内容因违规无法查看", "此内容因违规无法查看"),
    ("此内容被投诉且经审核涉嫌侵权", "此内容因涉嫌侵权无法查看"),
    ("该公众号已迁移", "该公众号已迁移，请打开原文查看"),
)


def parse_article(page: str, url: str) -> Article:
    metas: dict[str, str] = {}
    for k, v in _META_RE.findall(page):
        metas.setdefault(k.lower(), html.unescape(v).strip())
    content = clean_body(page, url)
    title = metas.get("og:title") or _js_string(page, r"var msg_title = '((?:[^'\\]|\\.)*)'")
    if not content and not title:
        for marker, why in _BLOCKED_MARKERS:
            if marker in page:
                raise ArticleError(why)
        raise ArticleError("无法解析文章内容，请打开原文查看")
    try:
        published = int(_js_string(page, r'var ct = "(\d+)"') or 0)
    except ValueError:
        published = 0
    return Article(
        url=url,
        title=title or "微信文章",
        account=_js_string(page, r'var nickname = htmlDecode\("((?:[^"\\]|\\.)*)"\)')
        or _js_string(page, r'var nickname = "((?:[^"\\]|\\.)*)"'),
        author=metas.get("og:article:author", ""),
        published=published,
        cover=_https_image(metas.get("og:image", "")),
        description=metas.get("og:description", ""),
        content=content,
    )


async def fetch_article(url: str, client: httpx.AsyncClient | None = None) -> Article:
    """Download and parse an article; redirects must stay on mp.weixin.qq.com."""
    if not is_article_url(url):
        raise ArticleError("不是微信公众号文章链接")
    url = normalize_url(url)
    own = client is None
    client = client or httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=False)
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", current, headers={
                "User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            }) as resp:
                if resp.is_redirect:
                    nxt = urljoin(current, resp.headers.get("location", ""))
                    if (urlsplit(nxt).hostname or "").lower() != ARTICLE_HOST:
                        raise ArticleError("文章跳转到了其他网站，请打开原文查看")
                    current = normalize_url(nxt)
                    continue
                if resp.status_code != 200:
                    raise ArticleError(f"微信返回 HTTP {resp.status_code}")
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    if len(buf) > MAX_PAGE_BYTES:
                        raise ArticleError("文章页面过大")
                page = buf.decode(resp.encoding or "utf-8", errors="replace")
                return parse_article(page, current)
        raise ArticleError("文章跳转次数过多")
    except httpx.HTTPError as e:
        raise ArticleError(f"无法连接微信服务器（{type(e).__name__}）") from e
    finally:
        if own:
            await client.aclose()


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


# ---------------------------------------------------------------------- reader page
_CHINA_TZ = timezone(timedelta(hours=8))  # WeChat shows publish times in Beijing time


def _fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, _CHINA_TZ).strftime("%Y-%m-%d %H:%M") if ts > 0 else ""


def render_page(article: Article | None, *, error: str = "", source_url: str = "", nonce: str = "") -> str:
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
    else:
        title = "无法打开文章"
        head = '<h1 class="title">无法打开文章</h1>'
        body = f'<p class="err">{html.escape(error or "文章不存在或链接已失效")}</p>'
    open_btn = (f'<a class="open" id="open" href="{html.escape(url)}" target="_blank" rel="noopener">打开原文</a>'
                if url else "")
    return (READER_HTML.replace("__TITLE__", html.escape(title)).replace("__HEAD__", head)
            .replace("__OPEN__", open_btn).replace("__NONCE__", nonce).replace("__BODY__", body))


READER_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<title>__TITLE__</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: #fff; color: #1f1f1f; }
  body { font: 17px/1.75 -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB",
         "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif; -webkit-text-size-adjust: 100%;
         overflow-wrap: break-word; }
  main { max-width: 680px; margin: 0 auto; padding: 20px 16px calc(88px + env(safe-area-inset-bottom)); }
  .title { font-size: 22px; line-height: 1.4; margin: 0 0 10px; font-weight: 700; }
  .meta { color: #8a8a8a; font-size: 14px; margin-bottom: 22px; }
  .content { overflow-x: hidden; }
  .content img { max-width: 100% !important; height: auto !important; background: #f3f3f3; }
  .content section, .content div, .content p, .content table { max-width: 100% !important; }
  .content table { border-collapse: collapse; display: block; overflow-x: auto; }
  .content a { color: #576b95; }
  .content p { margin: 0; }
  .wx-media { color: #8a8a8a; font-size: 14px; text-align: center; margin: 12px 0 !important; }
  .err { color: #c0392b; }
  .bar { position: fixed; left: 0; right: 0; bottom: 0; padding: 10px 16px calc(10px + env(safe-area-inset-bottom));
         background: rgba(255,255,255,.94); border-top: 1px solid #eee; display: flex; justify-content: center;
         -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px); }
  .open { display: block; width: 100%; max-width: 680px; text-align: center; padding: 11px 0; border-radius: 10px;
          background: #07c160; color: #fff; text-decoration: none; font-size: 16px; font-weight: 600; }
</style>
</head>
<body>
<main>
__HEAD__
<div class="content">__BODY__</div>
</main>
<div class="bar">__OPEN__</div>
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
  if (inTg) {
    post('web_app_set_header_color', {color: '#ffffff'});
    post('web_app_set_background_color', {color: '#ffffff'});
    post('web_app_set_bottom_bar_color', {color: '#ffffff'});
    post('web_app_expand');
    post('web_app_ready');
  }
  // Links must not navigate the Mini App itself away: hand them to Telegram's browser.
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a || !/^https?:/i.test(a.href)) return;
    e.preventDefault();
    if (!(inTg && post('web_app_open_link', {url: a.href}))) window.open(a.href, '_blank', 'noopener');
  });
})();
</script>
</body>
</html>
"""
