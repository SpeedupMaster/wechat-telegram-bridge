"""Xiaohongshu (RedNote) notes for the built-in reader.

The mobile note page (what an iPhone gets for a share link with its xsec_token) embeds the whole note in
`window.__INITIAL_STATE__`: text, image file ids and MP4 streams. We turn that into the same `Article` the
WeChat reader renders, using URL forms that do not expire, so a cached note keeps working after the share
link's token has:

* images / video cover: https://sns-na-i2.xhscdn.com/<fileId> (refuses foreign Referers, fine without one;
  the reader page sends none),
* video: the unsigned `sns-bak-v*` backup streams (no Referer check), then the signed master URL.
"""
from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from .article import USER_AGENT, Article, ArticleError, MAX_PAGE_BYTES, MAX_REDIRECTS

import httpx

NOTE_HOSTS = ("xiaohongshu.com", "xhslink.com", "xhslink.cn")
IMAGE_BASE = "https://sns-na-i2.xhscdn.com/"
_NOTE_PATH_RE = re.compile(r"^/(?:explore|discovery/item|user/profile/[0-9a-zA-Z]+)/([0-9a-f]{24})/?$")
_FILE_ID_RE = re.compile(r"^[0-9A-Za-z_./-]{8,200}$")
_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*")
_UNDEFINED_RE = re.compile(r"([:\[,]\s*)undefined(?=\s*[,\]}])")
_TOPIC_RE = re.compile(r"#([^#\[\]\n]{1,40})\[话题\]#")
_EMOJI_CODE_RE = re.compile(r"\[[^\[\]\n]{1,10}R\]")
_SPACES_RE = re.compile(r"[ \t　]{2,}")
_HASHTAG_RE = re.compile(r"#[^\s#]+")
_FALLBACK = "小红书限制了服务器访问，正在打开原笔记…"


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _on_host(host: str, *domains: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_note_url(url: str | None) -> bool:
    """xhslink.com / xhslink.cn share links and xiaohongshu.com note pages."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https"):
        return False
    if _on_host(host, "xhslink.com", "xhslink.cn"):
        return len(parts.path.strip("/")) >= 3
    return _on_host(host, "xiaohongshu.com") and bool(_NOTE_PATH_RE.match(parts.path))


def normalize_url(url: str) -> str:
    """https, no fragment; the query (xsec_token) is kept because the note page needs it."""
    parts = urlsplit(html.unescape(url.strip()))
    return urlunsplit(("https", parts.netloc.lower(), parts.path, parts.query, ""))


def clean_text(text: str) -> str:
    """`#话题[话题]#` -> `#话题`, and drop `[笑哭R]`-style sticker codes the web cannot show."""
    text = _TOPIC_RE.sub(lambda m: f"#{m.group(1)} ", text or "")
    text = _EMOJI_CODE_RE.sub("", text)
    lines = (_SPACES_RE.sub(" ", line).strip() for line in text.replace("\r\n", "\n").split("\n"))
    return "\n".join(lines).strip()


def _image_url(file_id: str, width: int = 1080) -> str:
    file_id = (file_id or "").strip().lstrip("/")
    if not _FILE_ID_RE.match(file_id) or ".." in file_id:
        return ""
    return f"{IMAGE_BASE}{file_id}?imageView2/2/w/{width}/format/jpg"


def _https_media(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url if url.startswith("https://") and _on_host(_host(url), "xhscdn.com") else ""


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def extract_state(page: str) -> dict | None:
    m = _STATE_RE.search(page)
    if not m:
        return None
    end = page.find("</script>", m.end())
    raw = page[m.end():end if end > 0 else None].strip().rstrip(";")
    try:
        state = json.loads(_UNDEFINED_RE.sub(r"\1null", raw))
    except ValueError:
        return None
    return state if isinstance(state, dict) else None


def _find_note(state: dict) -> dict | None:
    # Mobile page: noteData.data.noteData; desktop page: note.noteDetailMap[<id>].note
    data = ((state.get("noteData") or {}).get("data") or {}).get("noteData")
    if isinstance(data, dict) and data.get("noteId"):
        return data
    for entry in ((state.get("note") or {}).get("noteDetailMap") or {}).values():
        note = (entry or {}).get("note") if isinstance(entry, dict) else None
        if isinstance(note, dict) and (note.get("noteId") or note.get("desc") or note.get("imageList")):
            return note
    return None


def _video_sources(video: dict) -> list[str]:
    streams = ((video.get("media") or {}).get("stream") or {})
    urls: list[str] = []
    for codec in ("h264", "h265", "av1"):  # h264 plays everywhere; others only as later fallbacks
        for s in streams.get(codec) or []:
            if not isinstance(s, dict):
                continue
            for u in [*(s.get("backupUrls") or []), s.get("masterUrl") or ""]:
                if (u := _https_media(u)) and u not in urls:
                    urls.append(u)
    return urls


def _video_size(video: dict) -> tuple[int, int]:
    for s in ((video.get("media") or {}).get("stream") or {}).get("h264") or []:
        if isinstance(s, dict) and _int(s.get("width")) and _int(s.get("height")):
            return _int(s["width"]), _int(s["height"])
    return 0, 0


def parse_note(page: str, url: str) -> Article:
    state = extract_state(page)
    note = _find_note(state) if state else None
    if not note:
        raise ArticleError(_FALLBACK, code="xhs_no_note", fallback_original=True)
    desc = clean_text(str(note.get("desc") or ""))
    title = clean_text(str(note.get("title") or ""))
    if not title:  # untitled (typical for videos): first line of the text, without its hashtags
        first = next((ln for ln in desc.split("\n") if ln.strip()), "")
        title = _SPACES_RE.sub(" ", _HASHTAG_RE.sub("", first)).strip() or first
    title = title.strip()[:80] or "小红书笔记"
    images = [img for img in note.get("imageList") or [] if isinstance(img, dict)]
    parts: list[str] = []
    cover = ""
    video = note.get("video") if isinstance(note.get("video"), dict) else None
    if str(note.get("type") or "") == "video" and video:
        sources = _video_sources(video)
        # imageList[0] is the cover; video.image.thumbnailFileid is often a sprite sheet of frames
        thumb = ((video.get("image") or {}).get("thumbnailFileid") or "")
        cover = (_image_url(images[0].get("fileId") or "") if images else "") or _image_url(thumb)
        if not sources:
            raise ArticleError(_FALLBACK, code="xhs_no_video", fallback_original=True)
        w, h = _video_size(video)
        size = f' width="{w}" height="{h}"' if w and h else ""
        poster = f' poster="{html.escape(cover)}"' if cover else ""
        srcs = "".join(f'<source src="{html.escape(u)}" type="video/mp4">' for u in sources)
        parts.append(f'<div class="xhs-video"><video controls playsinline preload="metadata"{size}{poster}>'
                     f'{srcs}</video></div>')
    else:
        for img in images:
            src = _image_url(img.get("fileId") or img.get("traceId") or "")
            if not src:
                continue
            cover = cover or src
            w, h = _int(img.get("width")), _int(img.get("height"))
            size = f' width="{w}" height="{h}"' if w and h else ""
            parts.append(f'<figure class="wx-pic"><img src="{html.escape(src)}" loading="lazy" '
                         f'referrerpolicy="no-referrer"{size}></figure>')
        if not parts and not desc:
            raise ArticleError(_FALLBACK, code="xhs_empty", fallback_original=True)
    if desc:
        parts.append("<p>" + html.escape(desc).replace("\n", "<br>") + "</p>")
    info = note.get("interactInfo") or {}
    stats = " · ".join(f"{icon} {info.get(key)}" for icon, key in (("❤️", "likedCount"), ("⭐", "collectedCount"),
                                                                    ("💬", "commentCount"))
                       if str(info.get(key) or "").strip() not in ("", "0"))
    if stats:
        parts.append(f'<p class="wx-media">{html.escape(stats)}</p>')
    user = note.get("user") or {}
    return Article(
        url=url,
        title=title,
        account=str(user.get("nickName") or user.get("nickname") or "").strip(),
        published=_int(note.get("time")) // 1000,
        cover=cover,
        description=desc,
        content="".join(parts),
    )


async def fetch_note(url: str, session: Any, *, uses_curl: bool, net_errors: tuple[type[BaseException], ...]) -> Article:
    """Resolve a share link and parse the mobile note page; redirects must stay on Xiaohongshu hosts."""
    if not is_note_url(url):
        raise ArticleError("不是小红书笔记链接", code="invalid_url")
    current = normalize_url(url)
    headers = ({"Accept-Language": "zh-CN,zh-Hans;q=0.9"} if uses_curl else {
        "User-Agent": USER_AGENT,  # the mobile page carries the note data without a login
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    try:
        for _ in range(MAX_REDIRECTS + 1):
            async with session.stream("GET", current, headers=headers) as resp:
                if resp.is_redirect:
                    nxt = urljoin(current, resp.headers.get("location", ""))
                    host = _host(nxt)
                    if not _on_host(host, *NOTE_HOSTS):
                        raise ArticleError(_FALLBACK, code="xhs_off_host", fallback_original=True)
                    path = urlsplit(nxt).path
                    if _on_host(host, "xiaohongshu.com") and not _NOTE_PATH_RE.match(path):
                        # login wall, 404 page or the home page (expired token)
                        raise ArticleError(_FALLBACK, code="xhs_blocked", fallback_original=True)
                    current = normalize_url(nxt)
                    continue
                if resp.status_code != 200:
                    raise ArticleError(_FALLBACK, code=f"xhs_http_{resp.status_code}", fallback_original=True)
                buf = bytearray()
                aiter = resp.aiter_content() if hasattr(resp, "aiter_content") else resp.aiter_bytes()
                async for chunk in aiter:
                    buf += chunk
                    if len(buf) > MAX_PAGE_BYTES:
                        raise ArticleError("笔记页面过大", code="too_large", fallback_original=True)
                page = buf.decode(getattr(resp, "encoding", None) or "utf-8", errors="replace")
                return parse_note(page, current)
        raise ArticleError(_FALLBACK, code="too_many_redirects", fallback_original=True)
    except (httpx.HTTPError, *net_errors) as e:
        raise ArticleError(f"无法连接小红书（{type(e).__name__}），正在打开原笔记…", code="network",
                           fallback_original=True) from e
