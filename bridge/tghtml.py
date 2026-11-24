"""Telethon's HTML parse mode plus Telegram's date entity and UTF-16 safe HTML splitting.

Telethon (1.x) only knows the classic tags. Telegram also has a date/time entity
(`MessageEntityFormattedDate`; Bot API `<tg-time unix="…" format="…">`): the client shows
the time in the reader's own timezone and language, and `format="r"` keeps a relative
time ("3 minutes ago") up to date. The text inside the tag is what older clients show.

Use `HTML` as `parse_mode=`; `strip()` turns the markup back into plain Telethon HTML for
the rare server that refuses the entity.
"""
from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime, timezone
from typing import Iterable

from telethon.extensions import html as tl_html
from telethon.helpers import add_surrogate, del_surrogate, strip_text
from telethon.tl import types
from telethon.tl.types import MessageEntityFormattedDate

# Bot API `date_time_format`: "r" (relative) or "w?[dD]?[tT]?".
_FLAGS = {"r": "relative", "w": "day_of_week", "d": "short_date", "D": "long_date",
          "t": "short_time", "T": "long_time"}
_TIME_TAG = re.compile(r"</?tg-time\b[^>]*>")


def tg_time(when: float, fallback: str, fmt: str = "t") -> str:
    """Markup for a moment shown in the reader's own timezone; `fallback` must already be escaped."""
    return f'<tg-time unix="{int(when)}" format="{fmt}">{fallback}</tg-time>'


def strip(text: str) -> str:
    """The same message without date entities (their fallback text stays)."""
    return _TIME_TAG.sub("", text)


def utf16_len(text: str) -> int:
    """Length of plain text in Telegram UTF-16 code units."""
    return len(add_surrogate(text))


def _format_date_flags(entity: MessageEntityFormattedDate) -> str:
    if getattr(entity, "relative", False):
        return "r"
    return "".join(k for k, attr in _FLAGS.items() if k != "r" and getattr(entity, attr, False))


def _entity_tags(entity, slice_text: str) -> tuple[str, str] | None:
    if isinstance(entity, types.MessageEntityBold):
        return "<b>", "</b>"
    if isinstance(entity, types.MessageEntityItalic):
        return "<i>", "</i>"
    if isinstance(entity, types.MessageEntityUnderline):
        return "<u>", "</u>"
    if isinstance(entity, types.MessageEntityStrike):
        return "<s>", "</s>"
    if isinstance(entity, types.MessageEntitySpoiler):
        return "<tg-spoiler>", "</tg-spoiler>"
    if isinstance(entity, types.MessageEntityCode):
        return "<code>", "</code>"
    if isinstance(entity, types.MessageEntityPre):
        lang = getattr(entity, "language", None) or ""
        if lang:
            return f'<pre><code class="language-{html.escape(lang, quote=True)}">', "</code></pre>"
        return "<pre>", "</pre>"
    if isinstance(entity, types.MessageEntityTextUrl):
        return f'<a href="{html.escape(entity.url or "", quote=True)}">', "</a>"
    if isinstance(entity, types.MessageEntityUrl):
        return f'<a href="{html.escape(slice_text, quote=True)}">', "</a>"
    if isinstance(entity, types.MessageEntityEmail):
        return f'<a href="mailto:{html.escape(slice_text, quote=True)}">', "</a>"
    if isinstance(entity, types.MessageEntityBlockquote):
        if getattr(entity, "collapsed", False):
            return "<blockquote expandable>", "</blockquote>"
        return "<blockquote>", "</blockquote>"
    if isinstance(entity, MessageEntityFormattedDate):
        when = getattr(entity, "date", None)
        unix = int(when.timestamp()) if isinstance(when, datetime) else int(when or 0)
        fmt = _format_date_flags(entity)
        return f'<tg-time unix="{unix}" format="{fmt}">', "</tg-time>"
    if isinstance(entity, types.MessageEntityCustomEmoji):
        return f'<tg-emoji emoji-id="{int(entity.document_id)}">', "</tg-emoji>"
    if isinstance(entity, types.MessageEntityMentionName):
        return f'<a href="tg://user?id={int(entity.user_id)}">', "</a>"
    return None


def _clone_entity(entity, offset: int, length: int):
    cls = type(entity)
    if isinstance(entity, types.MessageEntityTextUrl):
        return cls(offset=offset, length=length, url=entity.url)
    if isinstance(entity, types.MessageEntityPre):
        return cls(offset=offset, length=length, language=getattr(entity, "language", "") or "")
    if isinstance(entity, types.MessageEntityBlockquote):
        return cls(offset=offset, length=length, collapsed=getattr(entity, "collapsed", None))
    if isinstance(entity, MessageEntityFormattedDate):
        kwargs = {attr: True for attr in _FLAGS.values() if getattr(entity, attr, False)}
        return cls(offset=offset, length=length, date=entity.date, **kwargs)
    if isinstance(entity, types.MessageEntityCustomEmoji):
        return cls(offset=offset, length=length, document_id=entity.document_id)
    if isinstance(entity, types.MessageEntityMentionName):
        return cls(offset=offset, length=length, user_id=entity.user_id)
    return cls(offset=offset, length=length)


def _render_html(surr_text: str, entities: Iterable) -> str:
    """Serialize surrogate-indexed plain text + Telegram entities into properly nested HTML."""
    n = len(surr_text)
    if n == 0:
        return ""
    spans: list[tuple[int, int, int, str, str]] = []
    for idx, ent in enumerate(entities or ()):
        s = max(0, min(n, ent.offset))
        e = max(0, min(n, ent.offset + ent.length))
        if e <= s:
            continue
        tags = _entity_tags(ent, del_surrogate(surr_text[s:e]))
        if tags is None:
            continue
        spans.append((s, e, idx, tags[0], tags[1]))
    if not spans:
        return html.escape(del_surrogate(surr_text))

    starts: dict[int, list[tuple[int, int, int, str, str]]] = {}
    ends: dict[int, list[int]] = {}
    points = {0, n}
    for sp in spans:
        s, e, idx, _, _ = sp
        starts.setdefault(s, []).append(sp)
        ends.setdefault(e, []).append(idx)
        points.add(s)
        points.add(e)

    ordered_points = sorted(points)
    out: list[str] = []
    # Active stack entries: (idx, open_tag, close_tag)
    active: list[tuple[int, str, str]] = []

    for pi, pos in enumerate(ordered_points):
        if closing := ends.get(pos):
            closing_set = set(closing)
            reopen: list[tuple[int, str, str]] = []
            while active and closing_set:
                top = active.pop()
                out.append(top[2])
                if top[0] in closing_set:
                    closing_set.remove(top[0])
                else:
                    reopen.append(top)
            for item in reversed(reopen):
                out.append(item[1])
                active.append(item)

        if opening := starts.get(pos):
            # Longer spans wrap shorter spans at the same start offset.
            for s, e, idx, open_tag, close_tag in sorted(opening, key=lambda x: (-(x[1] - x[0]), x[2])):
                out.append(open_tag)
                active.append((idx, open_tag, close_tag))

        if pi + 1 < len(ordered_points):
            nxt = ordered_points[pi + 1]
            if nxt > pos:
                out.append(html.escape(del_surrogate(surr_text[pos:nxt])))

    while active:
        out.append(active.pop()[2])
    return "".join(out)


def _cp_at(surr: str, idx: int) -> tuple[int, int]:
    """Return (codepoint, utf16_units) starting at `idx` in a surrogate-encoded string."""
    c = ord(surr[idx])
    if 0xD800 <= c <= 0xDBFF and idx + 1 < len(surr):
        lo = ord(surr[idx + 1])
        if 0xDC00 <= lo <= 0xDFFF:
            return 0x10000 + ((c - 0xD800) << 10) + (lo - 0xDC00), 2
    return c, 1


def _cp_before(surr: str, idx: int) -> tuple[int, int]:
    """Return (codepoint, utf16_units) ending at `idx` in a surrogate-encoded string."""
    c = ord(surr[idx - 1])
    if 0xDC00 <= c <= 0xDFFF and idx >= 2:
        hi = ord(surr[idx - 2])
        if 0xD800 <= hi <= 0xDBFF:
            return 0x10000 + ((hi - 0xD800) << 10) + (c - 0xDC00), 2
    return c, 1


def _is_regional_indicator(cp: int) -> bool:
    return 0x1F1E6 <= cp <= 0x1F1FF


def _at_safe_boundary(surr: str, idx: int) -> bool:
    """True when `idx` does not split a UTF-16 surrogate pair, ZWJ emoji cluster, modifier, or flag."""
    if idx <= 0 or idx >= len(surr):
        return True
    if 0xDC00 <= ord(surr[idx]) <= 0xDFFF:
        return False
    cp_next, _ = _cp_at(surr, idx)
    cp_prev, _ = _cp_before(surr, idx)
    if cp_prev == 0x200D:
        return False
    if cp_next in (0x200D, 0xFE0F, 0xFE0E, 0x20E3):
        return False
    if 0x1F3FB <= cp_next <= 0x1F3FF:
        return False
    if unicodedata.combining(chr(cp_next)) != 0:
        return False
    if _is_regional_indicator(cp_prev) and _is_regional_indicator(cp_next):
        count = 0
        p = idx
        while p > 0:
            cp, step = _cp_before(surr, p)
            if not _is_regional_indicator(cp):
                break
            count += 1
            p -= step
        if count % 2 == 1:
            return False
    return True


def _pick_split(surr: str, start: int, limit: int) -> int:
    n = len(surr)
    end = min(n, start + limit)
    if end >= n:
        return n

    # Prefer breaking on a newline in the second half of the window so lines stay intact.
    nl = surr.rfind("\n", start + max(1, limit // 2), end)
    if nl >= start:
        cut = nl + 1
        if cut > start and _at_safe_boundary(surr, cut):
            return cut

    cut = end
    if cut < n and 0xDC00 <= ord(surr[cut]) <= 0xDFFF:
        cut -= 1
    while cut > start and not _at_safe_boundary(surr, cut):
        _, step = _cp_before(surr, cut)
        cut -= step
    if cut > start:
        return cut

    # Pathological single cluster exceeding `limit`: break without splitting a surrogate pair.
    cut = end
    if cut < n and 0xDC00 <= ord(surr[cut]) <= 0xDFFF:
        cut -= 1
    return max(start + 1, cut)


def split_html(text: str, limit: int = 4000, *, rest_limit: int | None = None) -> list[str]:
    """Split Telegram HTML safely by rendered UTF-16 code units without breaking tags, entities, or emoji.

    Fast path: returns `[text]` verbatim whenever the rendered UTF-16 length is `<= limit`.
    """
    if not text:
        return [""]
    plain, entities = HTML.parse(text)
    surr = add_surrogate(plain)
    if len(surr) <= limit:
        return [text]

    next_limit = rest_limit if rest_limit is not None else limit
    chunks: list[str] = []
    start = 0
    n = len(surr)
    cur_limit = limit
    while start < n:
        cut = _pick_split(surr, start, cur_limit)
        seg = surr[start:cut]
        seg_entities = []
        for ent in entities:
            es = ent.offset
            ee = ent.offset + ent.length
            if ee <= start or es >= cut:
                continue
            os = max(es, start)
            oe = min(ee, cut)
            if oe > os:
                seg_entities.append(_clone_entity(ent, os - start, oe - os))
        rendered = _render_html(seg, seg_entities)
        if rendered and del_surrogate(seg).strip():
            chunks.append(rendered)
        start = cut
        cur_limit = next_limit
    return chunks or [""]


class _Parser(tl_html.HTMLToTelegramParser):
    def handle_starttag(self, tag, attrs):
        if tag != "tg-time":
            return super().handle_starttag(tag, attrs)
        self._open_tags.appendleft(tag)
        self._open_tags_meta.appendleft(None)
        a = dict(attrs)
        try:
            when = datetime.fromtimestamp(int(a.get("unix") or ""), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return
        flags = {_FLAGS[c]: True for c in (a.get("format") or "") if c in _FLAGS}
        if flags.get("relative"):
            flags = {"relative": True}
        if tag not in self._building_entities:
            self._building_entities[tag] = MessageEntityFormattedDate(
                offset=len(self.text), length=0, date=when, **flags)


class _HtmlPlus:
    @staticmethod
    def parse(text: str):
        if not text:
            return text, []
        p = _Parser()
        p.feed(add_surrogate(text))
        out = strip_text(p.text, p.entities)
        p.entities.reverse()
        p.entities.sort(key=lambda e: e.offset)
        return del_surrogate(out), p.entities

    @staticmethod
    def unparse(text, entities):
        if not text:
            return text
        return _render_html(add_surrogate(text), entities or ())


HTML = _HtmlPlus()
