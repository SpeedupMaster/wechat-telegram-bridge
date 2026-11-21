"""Douyin videos in the built-in reader Mini App, through Douyin's official embed player.

Douyin's share pages no longer carry the video in their HTML (the page script fetches it from signed APIs),
but the official embed player at https://open.douyin.com/player/video?vid=<id> plays any public video
without a login, a signature or a Referer, and allows being framed. So the server only resolves a share
link's redirect to learn the video id (no page download), and the Mini App shows the player full screen.
Anything that is not a plain video (图文 notes, profiles, deleted posts) opens the original link instead.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import embed
from .article import Article, ArticleError

SHARE_HOSTS = ("douyin.com", "iesdouyin.com")
PLAYER_URL = "https://open.douyin.com/player/video?vid={vid}&autoplay=0"
PLAYER_ORIGIN = "https://open.douyin.com"
_VIDEO_PATH_RE = re.compile(r"^/(?:share/)?video/(\d{8,25})/?$")
_ID_RE = re.compile(r"^\d{8,25}$")
_SHORT_PATH_RE = re.compile(r"^/[0-9A-Za-z_-]{4,40}/?$")
_FALLBACK = "抖音视频暂时无法在阅读页播放，正在打开原链接…"
normalize_url = embed.normalize_url


def video_id(url: str | None) -> str:
    """The video id in a full Douyin URL (…/video/<id>, …/share/video/<id>, ?modal_id=<id>), else ""."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not embed.on_host((parts.hostname or "").lower(), *SHARE_HOSTS):
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


def player_url(vid: str) -> str:
    if not _ID_RE.match(vid or ""):
        raise ValueError(f"bad Douyin video id {vid!r}")
    return PLAYER_URL.format(vid=vid)


def article_for(vid: str, source_url: str = "") -> Article:
    """What the reader caches for a video: the id travels in the canonical URL (tracking parameters dropped)."""
    return Article(url=f"https://www.douyin.com/video/{vid}", title="抖音视频", account="", author="",
                   published=0, cover="", description="", content="")


async def fetch_video(url: str, session: Any, *, uses_curl: bool,
                      net_errors: tuple[type[BaseException], ...]) -> Article:
    if not is_video_url(url):
        raise ArticleError("不是抖音视频链接", code="invalid_url")
    final = await embed.resolve(url, session, uses_curl=uses_curl, net_errors=net_errors, hosts=SHARE_HOSTS,
                                found=lambda u: bool(video_id(u)), hop=_is_short_link,
                                fallback=_FALLBACK, code="douyin")
    return article_for(video_id(final), url)


def embed_src(article: Article) -> str:
    vid = video_id(article.url)
    return player_url(vid) if vid else ""


def render_player(article: Article, *, nonce: str = "") -> str | None:
    """Full-screen embed player page for a cached Douyin video, or None if the article is not one."""
    src = embed_src(article)
    return embed.render_player(src, title="抖音视频", nonce=nonce) if src else None
