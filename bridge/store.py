"""SQLite state: event cursor, WeChat thread <-> Telegram group or forum topic, message id mapping."""
from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS threads (
    wx_thread  TEXT PRIMARY KEY,
    tg_chat    INTEGER NOT NULL UNIQUE,
    title      TEXT,
    created_at INTEGER NOT NULL
);
-- Topics mode: WeChat thread <-> topic in a forum supergroup (or an on-demand standalone group
-- with tg_topic = 0), kept apart from `threads`, so switching modes keeps both sets of bindings.
-- tg_topic = id of the topic's first message, or 0 for a standalone group linked in topics mode.
CREATE TABLE IF NOT EXISTS topics (
    wx_thread  TEXT PRIMARY KEY,
    tg_chat    INTEGER NOT NULL,
    tg_topic   INTEGER NOT NULL,
    title      TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (tg_chat, tg_topic)
);
-- Message ids in Telegram basic groups are per account: the same message has one id for the bot
-- and another for your account. side = 'bot' (ids the bot uses, e.g. for reply_to) or
-- 'user' (ids your account sees, e.g. the message you replied to).
DROP TABLE IF EXISTS messages;
CREATE TABLE IF NOT EXISTS msgmap (
    tg_chat   INTEGER NOT NULL,
    side      TEXT NOT NULL,
    tg_msg    INTEGER NOT NULL,
    wx_thread TEXT NOT NULL,
    wx_msg    TEXT,
    wx_server TEXT,
    PRIMARY KEY (tg_chat, side, tg_msg)
);
CREATE TABLE IF NOT EXISTS sent (
    wx_thread TEXT NOT NULL,
    wx_server TEXT NOT NULL,
    PRIMARY KEY (wx_thread, wx_server)
);
-- Official Account articles opened through the reader Mini App: id is the start parameter in the card button.
CREATE TABLE IF NOT EXISTS articles (
    id         TEXT PRIMARY KEY,
    url        TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);
-- Persistent cache of fetched & sanitized Official Account articles (LRU by accessed_at).
CREATE TABLE IF NOT EXISTS article_cache (
    url         TEXT PRIMARY KEY,
    final_url   TEXT NOT NULL,
    title       TEXT NOT NULL,
    account     TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    published   INTEGER NOT NULL DEFAULT 0,
    cover       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',
    fetched_at  INTEGER NOT NULL,
    accessed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS article_cache_accessed ON article_cache (accessed_at);
CREATE INDEX IF NOT EXISTS msgmap_wx_msg ON msgmap (wx_msg);
CREATE INDEX IF NOT EXISTS msgmap_wx_server ON msgmap (wx_thread, wx_server);
"""


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._cache_seq = 0

    # --- kv ---------------------------------------------------------------
    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value))

    # --- threads ----------------------------------------------------------
    def chat_for_thread(self, wx_thread: str) -> int | None:
        row = self.db.execute("SELECT tg_chat FROM threads WHERE wx_thread=?", (wx_thread,)).fetchone()
        return row[0] if row else None

    def thread_for_chat(self, tg_chat: int) -> str | None:
        row = self.db.execute("SELECT wx_thread FROM threads WHERE tg_chat=?", (tg_chat,)).fetchone()
        return row[0] if row else None

    def link(self, wx_thread: str, tg_chat: int, title: str) -> None:
        self.db.execute("DELETE FROM threads WHERE wx_thread=? OR tg_chat=?", (wx_thread, tg_chat))
        self.db.execute("INSERT INTO threads(wx_thread,tg_chat,title,created_at) VALUES(?,?,?,?)",
                        (wx_thread, tg_chat, title, int(time.time())))

    def unlink(self, wx_thread: str) -> None:
        self.db.execute("DELETE FROM threads WHERE wx_thread=?", (wx_thread,))

    def migrate_chat(self, old_chat: int, new_chat: int) -> None:
        self.db.execute("UPDATE threads SET tg_chat=? WHERE tg_chat=?", (new_chat, old_chat))
        self.db.execute("UPDATE topics SET tg_chat=? WHERE tg_chat=?", (new_chat, old_chat))
        self.db.execute("UPDATE msgmap SET tg_chat=? WHERE tg_chat=?", (new_chat, old_chat))

    def linked_chats(self) -> set[int]:
        return {r[0] for r in self.db.execute("SELECT tg_chat FROM threads")}

    def list_threads(self) -> list[tuple[str, int, str, int]]:
        return list(self.db.execute(
            "SELECT wx_thread, tg_chat, COALESCE(title, ''), created_at FROM threads ORDER BY created_at DESC"
        ))

    def update_title(self, wx_thread: str, title: str) -> None:
        self.db.execute("UPDATE threads SET title=? WHERE wx_thread=?", (title, wx_thread))

    # --- forum topics -----------------------------------------------------
    def topic_for_thread(self, wx_thread: str) -> tuple[int, int] | None:
        row = self.db.execute("SELECT tg_chat, tg_topic FROM topics WHERE wx_thread=?", (wx_thread,)).fetchone()
        return (row[0], row[1]) if row else None

    def thread_for_topic(self, tg_chat: int, tg_topic: int) -> str | None:
        row = self.db.execute("SELECT wx_thread FROM topics WHERE tg_chat=? AND tg_topic=?",
                              (tg_chat, tg_topic)).fetchone()
        return row[0] if row else None

    def thread_for_topic_group(self, tg_chat: int) -> str | None:
        return self.thread_for_topic(tg_chat, 0)

    def link_topic(self, wx_thread: str, tg_chat: int, tg_topic: int, title: str) -> None:
        self.db.execute("DELETE FROM topics WHERE wx_thread=? OR (tg_chat=? AND tg_topic=?)",
                        (wx_thread, tg_chat, tg_topic))
        self.db.execute("INSERT INTO topics(wx_thread,tg_chat,tg_topic,title,created_at) VALUES(?,?,?,?,?)",
                        (wx_thread, tg_chat, tg_topic, title, int(time.time())))

    def link_topic_group(self, wx_thread: str, tg_chat: int, title: str) -> None:
        self.link_topic(wx_thread, tg_chat, 0, title)

    def unlink_topic(self, wx_thread: str) -> None:
        self.db.execute("DELETE FROM topics WHERE wx_thread=?", (wx_thread,))

    def list_topics(self, tg_chat: int) -> list[tuple[str, int, str, int]]:
        return list(self.db.execute(
            "SELECT wx_thread, tg_topic, COALESCE(title, ''), created_at FROM topics WHERE tg_chat=? AND tg_topic > 0 "
            "ORDER BY created_at DESC", (tg_chat,)))

    def list_topic_groups(self) -> list[tuple[str, int, str, int]]:
        return list(self.db.execute(
            "SELECT wx_thread, tg_chat, COALESCE(title, ''), created_at FROM topics WHERE tg_topic = 0 "
            "ORDER BY created_at DESC"))

    def update_topic_title(self, wx_thread: str, title: str) -> None:
        self.db.execute("UPDATE topics SET title=? WHERE wx_thread=?", (title, wx_thread))

    def forget_messages(self, tg_chat: int, wx_thread: str) -> None:
        """Drop message mappings of a thread in a chat (its topic was deleted: those messages are gone)."""
        self.db.execute("DELETE FROM msgmap WHERE tg_chat=? AND wx_thread=?", (tg_chat, wx_thread))

    # --- messages ---------------------------------------------------------
    @staticmethod
    def _norm_id(value: str | int | None) -> str | None:
        if value is None:
            return None
        s = str(value).strip()
        return None if s in ("", "0") else s

    def map_message(self, tg_chat: int, side: str, tg_msg: int, wx_thread: str, wx_msg: str | None,
                    wx_server: str | None) -> None:
        wx_msg = self._norm_id(wx_msg)
        wx_server = self._norm_id(wx_server)
        self.db.execute(
            "INSERT INTO msgmap(tg_chat,side,tg_msg,wx_thread,wx_msg,wx_server) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(tg_chat,side,tg_msg) DO UPDATE SET wx_msg=COALESCE(excluded.wx_msg,wx_msg), "
            "wx_server=COALESCE(excluded.wx_server,wx_server)",
            (tg_chat, side, tg_msg, wx_thread, wx_msg, wx_server))

    def wx_entry_for_tg(self, tg_chat: int, tg_msg: int, side: str = "user") -> tuple[str, str | None, str | None] | None:
        sides = ("user", "bot") if side == "user" and tg_chat < -1000000000000 else (side,)
        for s in sides:
            row = self.db.execute(
                "SELECT wx_thread, wx_msg, wx_server FROM msgmap WHERE tg_chat=? AND side=? AND tg_msg=?",
                (tg_chat, s, tg_msg)).fetchone()
            if row:
                return row[0], row[1], row[2]
        return None

    def wx_for_tg(self, tg_chat: int, tg_msg: int, side: str = "user") -> tuple[str | None, str | None] | None:
        entry = self.wx_entry_for_tg(tg_chat, tg_msg, side)
        return (entry[1], entry[2]) if entry else None

    def tg_for_wx(self, wx_thread: str, wx_msg: str | None, wx_server: str | None,
                  side: str = "bot", tg_chat: int | None = None) -> tuple[int, int] | None:
        """Telegram copy of a WeChat message; `tg_chat` limits it to one chat (e.g. after switching modes)."""
        in_chat, chat_args = ("", ()) if tg_chat is None else (" AND tg_chat=?", (tg_chat,))
        for col, val in (("wx_msg", self._norm_id(wx_msg)), ("wx_server", self._norm_id(wx_server))):
            if not val:
                continue
            row = self.db.execute(
                f"SELECT tg_chat, tg_msg FROM msgmap WHERE wx_thread=? AND {col}=? AND side=?{in_chat} "
                "ORDER BY tg_msg LIMIT 1",
                (wx_thread, val, side, *chat_args)).fetchone()
            if row:
                return row[0], row[1]
            # Supergroups share one message-ID space across user and bot sessions; also match legacy user-only rows.
            if side == "bot":
                row = self.db.execute(
                    f"SELECT tg_chat, tg_msg FROM msgmap WHERE wx_thread=? AND {col}=? AND side='user' "
                    f"AND tg_chat < -1000000000000{in_chat} ORDER BY tg_msg LIMIT 1",
                    (wx_thread, val, *chat_args)).fetchone()
                if row:
                    return row[0], row[1]
        return None

    def add_sent(self, wx_thread: str, wx_server: str) -> None:
        wx_server = self._norm_id(wx_server)
        if not wx_server:
            return
        self.db.execute("INSERT OR IGNORE INTO sent(wx_thread,wx_server) VALUES(?,?)", (wx_thread, wx_server))

    def is_sent(self, wx_thread: str, wx_server: str) -> bool:
        wx_server = self._norm_id(wx_server)
        if not wx_server:
            return False
        return self.db.execute("SELECT 1 FROM sent WHERE wx_thread=? AND wx_server=?",
                               (wx_thread, wx_server)).fetchone() is not None

    def lookup_tg_delete(self, tg_chat: int | None, tg_msg: int,
                         side: str = "user") -> tuple[int, str, str | None, str | None] | None:
        """Find (tg_chat, wx_thread, wx_msg, wx_server) for a deleted Telegram message."""
        if tg_chat is not None:
            return self.db.execute(
                "SELECT tg_chat, wx_thread, wx_msg, wx_server FROM msgmap WHERE tg_chat=? AND side=? AND tg_msg=?",
                (tg_chat, side, tg_msg)).fetchone()
        rows = self.db.execute(
            "SELECT tg_chat, wx_thread, wx_msg, wx_server FROM msgmap "
            "WHERE side=? AND tg_msg=? AND tg_chat < 0 AND tg_chat > -1000000000000",
            (side, tg_msg)).fetchall()
        return rows[0] if len(rows) == 1 else None

    def claim_echo(self, wx_thread: str, wx_server: str, wx_msg: str) -> bool:
        """True if this outgoing WeChat message is one we sent from Telegram (and links its saved id)."""
        wx_server = self._norm_id(wx_server)
        if not wx_server:
            return False
        if not self.db.execute("SELECT 1 FROM sent WHERE wx_thread=? AND wx_server=?",
                               (wx_thread, wx_server)).fetchone():
            return False
        self.db.execute("UPDATE msgmap SET wx_msg=? WHERE wx_thread=? AND wx_server=? AND wx_msg IS NULL",
                        (wx_msg, wx_thread, wx_server))
        return True

    def seen_wx_msg(self, wx_msg: str) -> bool:
        return self.db.execute("SELECT 1 FROM msgmap WHERE wx_msg=? LIMIT 1", (wx_msg,)).fetchone() is not None

    # --- reader articles --------------------------------------------------
    def article_id(self, url: str) -> str:
        """Stable short id for an article URL (created on first use)."""
        row = self.db.execute("SELECT id FROM articles WHERE url=?", (url,)).fetchone()
        if row:
            return row[0]
        while True:
            # [A-Za-z0-9] only: it travels in a Mini App start parameter and must not clash with call tokens
            aid = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
                          for _ in range(16))
            try:
                self.db.execute("INSERT INTO articles(id,url,created_at) VALUES(?,?,?)", (aid, url, int(time.time())))
                return aid
            except sqlite3.IntegrityError:
                row = self.db.execute("SELECT id FROM articles WHERE url=?", (url,)).fetchone()
                if row:
                    return row[0]

    def article_url(self, aid: str) -> str | None:
        row = self.db.execute("SELECT url FROM articles WHERE id=?", (aid,)).fetchone()
        return row[0] if row else None

    def _next_cache_ts(self) -> int:
        self._cache_seq = max(self._cache_seq + 1, int(time.time() * 1_000_000))
        return self._cache_seq

    def get_cached_article(self, url: str) -> dict | None:
        row = self.db.execute(
            "SELECT final_url, title, account, author, published, cover, description, content "
            "FROM article_cache WHERE url=?",
            (url,),
        ).fetchone()
        if not row:
            return None
        self.db.execute("UPDATE article_cache SET accessed_at=? WHERE url=?", (self._next_cache_ts(), url))
        return {
            "url": row[0],
            "title": row[1],
            "account": row[2],
            "author": row[3],
            "published": row[4],
            "cover": row[5],
            "description": row[6],
            "content": row[7],
        }

    def save_cached_article(self, url: str, data: dict, *, max_entries: int = 500) -> None:
        now_s = int(time.time())
        now_seq = self._next_cache_ts()
        self.db.execute(
            "INSERT INTO article_cache(url, final_url, title, account, author, published, cover, description, content, "
            "fetched_at, accessed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET final_url=excluded.final_url, title=excluded.title, "
            "account=excluded.account, author=excluded.author, published=excluded.published, cover=excluded.cover, "
            "description=excluded.description, content=excluded.content, fetched_at=excluded.fetched_at, "
            "accessed_at=excluded.accessed_at",
            (
                url,
                data.get("url") or url,
                data.get("title") or "",
                data.get("account") or "",
                data.get("author") or "",
                int(data.get("published") or 0),
                data.get("cover") or "",
                data.get("description") or "",
                data.get("content") or "",
                now_s,
                now_seq,
            ),
        )
        if max_entries > 0:
            self.db.execute(
                "DELETE FROM article_cache WHERE url IN ("
                "SELECT url FROM article_cache ORDER BY accessed_at DESC LIMIT -1 OFFSET ?)",
                (max_entries,),
            )

    def delete_cached_article(self, url: str) -> None:
        self.db.execute("DELETE FROM article_cache WHERE url=?", (url,))
