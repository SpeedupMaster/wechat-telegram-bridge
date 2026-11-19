"""Telethon's HTML parse mode plus Telegram's date entity.

Telethon (1.x) only knows the classic tags. Telegram also has a date/time entity
(`MessageEntityFormattedDate`; Bot API `<tg-time unix="…" format="…">`): the client shows
the time in the reader's own timezone and language, and `format="r"` keeps a relative
time ("3 minutes ago") up to date. The text inside the tag is what older clients show.

Use `HTML` as `parse_mode=`; `strip()` turns the markup back into plain Telethon HTML for
the rare server that refuses the entity.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from telethon.extensions import html as tl_html
from telethon.helpers import add_surrogate, del_surrogate, strip_text
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
        return tl_html.unparse(text, [e for e in entities or [] if not isinstance(e, MessageEntityFormattedDate)])


HTML = _HtmlPlus()
