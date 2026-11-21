"""Bilibili videos in the built-in reader Mini App, through Bilibili's official embed player.

https://player.bilibili.com/player.html?bvid=<BV id> plays public videos without a login (at a modest
quality); the mobile site, by contrast, keeps asking to install the app. The server
only resolves b23.tv share links (Location headers, no page download). Live rooms, bangumi, articles and
profiles open the original link instead.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import embed
from .article import Article, ArticleError

SHARE_HOSTS = ("bilibili.com", "b23.tv", "bili2233.cn")
SHORT_HOSTS = ("b23.tv", "bili2233.cn")
PLAYER_ORIGIN = "https://player.bilibili.com"
_VIDEO_PATH_RE = re.compile(r"^/(?:s/)?video/(BV[0-9A-Za-z]{10}|av\d{1,20})/?$", re.I)
_SHORT_PATH_RE = re.compile(r"^/[0-9A-Za-z]{4,20}/?$")
_FALLBACK = "该哔哩哔哩链接暂不支持内嵌播放，正在打开原链接…"
normalize_url = embed.normalize_url


def video_ref(url: str | None) -> tuple[str, int]:
    """("BV…" or "av…", page) for a bilibili.com video URL, else ("", 0)."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return "", 0
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not embed.on_host(host, "bilibili.com"):
        return "", 0
    m = _VIDEO_PATH_RE.match(parts.path)
    if not m:
        return "", 0
    vid = m.group(1)
    vid = "BV" + vid[2:] if vid[:2].upper() == "BV" else "av" + vid[2:]
    try:
        page = int((parse_qs(parts.query).get("p") or ["1"])[0])
    except ValueError:
        page = 1
    return vid, page if 1 <= page <= 999 else 1


def _is_short_link(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (parts.scheme in ("http", "https") and (parts.hostname or "").lower() in SHORT_HOSTS
            and bool(_SHORT_PATH_RE.match(parts.path)))


def is_video_url(url: str | None) -> bool:
    url = (url or "").strip()
    return _is_short_link(url) or bool(video_ref(url)[0])


def player_url(vid: str, page: int = 1) -> str:
    if re.fullmatch(r"BV[0-9A-Za-z]{10}", vid or ""):
        ident = f"bvid={vid}"
    elif re.fullmatch(r"av\d{1,20}", vid or ""):
        ident = f"aid={vid[2:]}"
    else:
        raise ValueError(f"bad Bilibili video id {vid!r}")
    return f"{PLAYER_ORIGIN}/player.html?{ident}&p={int(page)}&autoplay=0&danmaku=0&high_quality=1"


def article_for(vid: str, page: int = 1) -> Article:
    url = f"https://www.bilibili.com/video/{vid}" + (f"?p={page}" if page > 1 else "")
    return Article(url=url, title="哔哩哔哩视频", account="", author="", published=0, cover="",
                   description="", content="")


async def fetch_video(url: str, session: Any, *, uses_curl: bool,
                      net_errors: tuple[type[BaseException], ...]) -> Article:
    if not is_video_url(url):
        raise ArticleError("不是哔哩哔哩视频链接", code="invalid_url")
    final = await embed.resolve(url, session, uses_curl=uses_curl, net_errors=net_errors, hosts=SHARE_HOSTS,
                                found=lambda u: bool(video_ref(u)[0]), hop=_is_short_link,
                                fallback=_FALLBACK, code="bili")
    return article_for(*video_ref(final))


def embed_src(article: Article) -> str:
    vid, page = video_ref(article.url)
    return player_url(vid, page) if vid else ""


def render_player(article: Article, *, nonce: str = "") -> str | None:
    src = embed_src(article)
    return embed.render_player(src, title="哔哩哔哩视频", nonce=nonce) if src else None
