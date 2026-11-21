"""Douyin videos in the built-in reader Mini App, through Douyin's official embed player.

Douyin's share pages no longer carry the video in their HTML (the page script fetches it from signed APIs),
but the official embed player at https://open.douyin.com/player/video?vid=<id> plays any public video
without a login, a signature or a Referer, and allows being framed. So the server only resolves a share
link's redirect to learn the video id (no page download), and the Mini App shows the player full screen.
Anything that is not a plain video (图文 notes, profiles, deleted posts) opens the original link instead.
"""
from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import httpx

from .article import USER_AGENT, Article, ArticleError, MAX_REDIRECTS

SHARE_HOSTS = ("douyin.com", "iesdouyin.com")
PLAYER_URL = "https://open.douyin.com/player/video?vid={vid}&autoplay=0"
PLAYER_ORIGIN = "https://open.douyin.com"
_VIDEO_PATH_RE = re.compile(r"^/(?:share/)?video/(\d{8,25})/?$")
_ID_RE = re.compile(r"^\d{8,25}$")
_SHORT_PATH_RE = re.compile(r"^/[0-9A-Za-z_-]{4,40}/?$")
_FALLBACK = "抖音视频暂时无法在阅读页播放，正在打开原链接…"


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _on_host(host: str, *domains: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def video_id(url: str | None) -> str:
    """The video id in a full Douyin URL (…/video/<id>, …/share/video/<id>, ?modal_id=<id>), else ""."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not _on_host((parts.hostname or "").lower(), *SHARE_HOSTS):
        return ""
    if m := _VIDEO_PATH_RE.match(parts.path):
        return m.group(1)
    modal = (parse_qs(parts.query).get("modal_id") or [""])[0]
    return modal if _ID_RE.match(modal) else ""


def _is_short_link(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (parts.scheme in ("http", "https") and (parts.hostname or "").lower() == "v.douyin.com"
            and bool(_SHORT_PATH_RE.match(parts.path)))


def is_video_url(url: str | None) -> bool:
    """v.douyin.com share links (videos, as far as we can tell before resolving) and direct video URLs."""
    url = (url or "").strip()
    return _is_short_link(url) or bool(video_id(url))


def normalize_url(url: str) -> str:
    parts = urlsplit(html.unescape(url.strip()))
    return urlunsplit(("https", parts.netloc.lower(), parts.path, parts.query, ""))


def player_url(vid: str) -> str:
    if not _ID_RE.match(vid or ""):
        raise ValueError(f"bad Douyin video id {vid!r}")
    return PLAYER_URL.format(vid=vid)


def article_for(vid: str, source_url: str) -> Article:
    """What the reader caches for a video: the id travels in the canonical URL."""
    canonical = f"https://www.douyin.com/video/{vid}"
    return Article(url=canonical, title="抖音视频", account="", author="", published=0, cover="",
                   description="", content="")


async def fetch_video(url: str, session: Any, *, uses_curl: bool,
                      net_errors: tuple[type[BaseException], ...]) -> Article:
    """Resolve a share link to its video id by following redirects (Location headers only, no page body)."""
    if not is_video_url(url):
        raise ArticleError("不是抖音视频链接", code="invalid_url")
    current = normalize_url(url)
    if vid := video_id(current):
        return article_for(vid, current)
    headers = ({"Accept-Language": "zh-CN,zh-Hans;q=0.9"} if uses_curl else {
        "User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh-Hans;q=0.9"})
    try:
        for _ in range(MAX_REDIRECTS + 1):
            async with session.stream("GET", current, headers=headers) as resp:
                if not resp.is_redirect:
                    # 404 for a dead short link, or a page we would have to run scripts to understand
                    raise ArticleError(_FALLBACK, code=f"douyin_http_{resp.status_code}", fallback_original=True)
                nxt = urljoin(current, resp.headers.get("location", ""))
            if not _on_host(_host(nxt), *SHARE_HOSTS):
                raise ArticleError(_FALLBACK, code="douyin_off_host", fallback_original=True)
            if vid := video_id(nxt):
                return article_for(vid, normalize_url(url))
            if not _is_short_link(nxt):
                # /share/note/ (图文), /share/user/ (profile), the home page for a deleted video, …
                raise ArticleError(_FALLBACK, code="douyin_not_video", fallback_original=True)
            current = normalize_url(nxt)
        raise ArticleError(_FALLBACK, code="too_many_redirects", fallback_original=True)
    except (httpx.HTTPError, *net_errors) as e:
        raise ArticleError(_FALLBACK, code="network", fallback_original=True) from e


def render_player(article: Article, *, nonce: str = "") -> str | None:
    """Full-screen embed player page for a cached Douyin video, or None if the article is not one."""
    vid = video_id(article.url)
    if not vid:
        return None
    return PLAYER_HTML.replace("__SRC__", html.escape(player_url(vid))).replace("__NONCE__", nonce)


PLAYER_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<title>抖音视频</title>
<style>
  html, body { margin: 0; height: 100%; background: #000; overflow: hidden; }
  iframe { display: block; width: 100vw; height: 100vh; height: 100dvh; border: 0; background: #000; }
</style>
</head>
<body>
<iframe src="__SRC__" referrerpolicy="no-referrer" allow="autoplay; fullscreen; picture-in-picture"
        allowfullscreen title="抖音视频"></iframe>
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
  post('web_app_set_header_color', {color: '#000000'});
  post('web_app_set_background_color', {color: '#000000'});
  post('web_app_set_bottom_bar_color', {color: '#000000'});
  post('web_app_expand');
  post('web_app_ready');
})();
</script>
</body>
</html>
"""
