"""Shared pieces for videos shown through a site's official embed player (Douyin, Bilibili).

The server never downloads the video page: it follows a share link's redirects (Location headers only) until
the URL names a video, and the Mini App then navigates in place to the official player.

The player is opened as the page itself rather than framed: iOS WebKit blocks third-party cookies in frames
(the players need theirs to fetch the video, so a framed player shows "加载失败") and does not reliably size
an iframe to the Mini App view.
"""
from __future__ import annotations

import html
import json
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .article import USER_AGENT, ArticleError, MAX_REDIRECTS


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def on_host(host: str, *domains: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def normalize_url(url: str) -> str:
    parts = urlsplit(html.unescape(url.strip()))
    return urlunsplit(("https", parts.netloc.lower(), parts.path, parts.query, ""))


async def resolve(url: str, session: Any, *, uses_curl: bool, net_errors: tuple[type[BaseException], ...],
                  hosts: tuple[str, ...], found: Callable[[str], bool], hop: Callable[[str], bool],
                  fallback: str, code: str) -> str:
    """Follow redirects from a share link until `found(url)`; anything else opens the original link instead."""
    current = normalize_url(url)
    if found(current):
        return current
    headers = ({"Accept-Language": "zh-CN,zh-Hans;q=0.9"} if uses_curl else {
        "User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh-Hans;q=0.9"})
    try:
        for _ in range(MAX_REDIRECTS + 1):
            async with session.stream("GET", current, headers=headers) as resp:
                if not resp.is_redirect:
                    # a dead short link, or a page we would have to run scripts to understand
                    raise ArticleError(fallback, code=f"{code}_http_{resp.status_code}", fallback_original=True)
                nxt = urljoin(current, resp.headers.get("location", ""))
            if not on_host(host_of(nxt), *hosts):
                raise ArticleError(fallback, code=f"{code}_off_host", fallback_original=True)
            if found(nxt):
                return nxt
            if not hop(nxt):  # 图文 notes, profiles, live rooms, the home page for a deleted video, …
                raise ArticleError(fallback, code=f"{code}_not_video", fallback_original=True)
            current = normalize_url(nxt)
        raise ArticleError(fallback, code="too_many_redirects", fallback_original=True)
    except (httpx.HTTPError, *net_errors) as e:
        raise ArticleError(fallback, code="network", fallback_original=True) from e


def render_player(src: str, *, title: str, nonce: str = "") -> str:
    """Black handoff page: paints Telegram's header black, then replaces itself with the player."""
    return (PLAYER_HTML.replace("__SRC_ATTR__", html.escape(src, quote=True))
            .replace("__SRC_JS__", json.dumps(src).replace("<", "\\u003c").replace(">", "\\u003e"))
            .replace("__TITLE__", html.escape(title)).replace("__NONCE__", nonce))


PLAYER_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<noscript><meta http-equiv="refresh" content="0;url=__SRC_ATTR__"></noscript>
<title>__TITLE__</title>
<style>
  html, body { margin: 0; height: 100%; background: #000; color: #8a8a8a; }
  body { display: flex; align-items: center; justify-content: center;
         font: 15px/1.6 -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; }
  a { color: #8a8a8a; }
</style>
</head>
<body>
<a href="__SRC_ATTR__">正在打开__TITLE__…</a>
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
  // The player pages are dark; set Telegram's chrome before leaving, since they cannot.
  post('web_app_set_header_color', {color: '#000000'});
  post('web_app_set_background_color', {color: '#000000'});
  post('web_app_set_bottom_bar_color', {color: '#000000'});
  post('web_app_expand');
  post('web_app_ready');
  var target = __SRC_JS__;
  if (/^https:\/\//.test(target)) window.location.replace(target);
})();
</script>
</body>
</html>
"""
