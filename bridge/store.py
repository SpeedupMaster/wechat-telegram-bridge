"""SQLite state: event cursor, WeChat thread <-> Telegram group or forum topic, message id mapping."""
from __future__ import annotations

import contextlib
import secrets
import sqlite3
import time
from pathlib import Path

SENT_MAX_ROWS = 5_000
MSGMAP_MAX_ROWS = 100_000
ARTICLES_MAX_ROWS = 20_000
PRUNE_EVERY_WRITES = 200

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
    def __init__(self, path: Path, *, max_sent: int = SENT_MAX_ROWS,
                 max_msgmap: int = MSGMAP_MAX_ROWS, max_articles: int = ARTICLES_MAX_ROWS,
                 prune_interval: int = PRUNE_EVERY_WRITES):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA cache_size=-1024")
        self.db.execute("PRAGMA journal_size_limit=4194304")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.executescript(SCHEMA)
        self._cache_seq = 0
        self._closed = False
        self._max_sent = max(0, int(max_sent))
        self._max_msgmap = max(0, int(max_msgmap))
        self._max_articles = max(0, int(max_articles))
        self._prune_interval = max(0, int(prune_interval))
        self._write_count = 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.db.close()
        except Exception:
            pass

    @contextlib.contextmanager
    def _tx(self):
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        else:
            if self.db.in_transaction:
                self.db.execute("COMMIT")

    def _table_may_exceed(self, table: str, keep: int) -> bool:
        if keep <= 0:
            return False
        bounds = self.db.execute(f"SELECT MIN(rowid), MAX(rowid) FROM {table}").fetchone()
        return bool(bounds and bounds[0] is not None and bounds[1] - bounds[0] >= keep)

    def _prune_table(self, table: str, keep: int) -> int:
        if not self._table_may_exceed(table, keep):
            return 0
        row = self.db.execute(
            f"SELECT rowid FROM {table} ORDER BY rowid DESC LIMIT 1 OFFSET ?",
            (keep - 1,),
        ).fetchone()
        if not row:
            return 0
        cur = self.db.execute(f"DELETE FROM {table} WHERE rowid < ?", (row[0],))
        return max(0, cur.rowcount)

    def prune(self, *, max_sent: int | None = None, max_msgmap: int | None = None,
              max_articles: int | None = None) -> dict[str, int]:
        """Trim unbounded history tables down to their latest `rowid` high-water marks."""
        limits = (
            ("sent", self._max_sent if max_sent is None else max(0, int(max_sent))),
            ("msgmap", self._max_msgmap if max_msgmap is None else max(0, int(max_msgmap))),
            ("articles", self._max_articles if max_articles is None else max(0, int(max_articles))),
        )
        candidates = [(t, k) for t, k in limits if self._table_may_exceed(t, k)]
        removed = {t: 0 for t, _ in limits}
        if not candidates:
            return removed
        with self._tx():
            for table, keep in candidates:
                removed[table] = self._prune_table(table, keep)
        return removed

    def _maybe_prune(self) -> None:
        self._write_count += 1
        if self._prune_interval > 0 and self._write_count >= self._prune_interval:
            self._write_count = 0
            self.prune()

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
        with self._tx():
            self.db.execute("DELETE FROM threads WHERE wx_thread=? OR tg_chat=?", (wx_thread, tg_chat))
            self.db.execute("INSERT INTO threads(wx_thread,tg_chat,title,created_at) VALUES(?,?,?,?)",
                            (wx_thread, tg_chat, title, int(time.time())))

    def unlink(self, wx_thread: str) -> None:
        self.db.execute("DELETE FROM threads WHERE wx_thread=?", (wx_thread,))

    def migrate_chat(self, old_chat: int, new_chat: int) -> None:
        with self._tx():
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
        with self._tx():
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

    def unmap_messages(self, tg_chat: int, tg_msgs: list[int]) -> None:
        """Drop specific Telegram message mappings after their placeholder messages were deleted."""
        if not tg_msgs:
            return
        marks = ",".join("?" for _ in tg_msgs)
        self.db.execute(f"DELETE FROM msgmap WHERE tg_chat=? AND tg_msg IN ({marks})", (tg_chat, *tg_msgs))

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
        self._maybe_prune()

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

    def tg_msgs_for_wx(self, wx_thread: str, wx_msg: str | None, wx_server: str | None,
                       side: str = "bot", tg_chat: int | None = None) -> tuple[int, list[int]] | None:
        """All Telegram message IDs (ordered ascending) mapped to a WeChat message in one chat."""
        in_chat, chat_args = ("", ()) if tg_chat is None else (" AND tg_chat=?", (tg_chat,))
        for col, val in (("wx_msg", self._norm_id(wx_msg)), ("wx_server", self._norm_id(wx_server))):
            if not val:
                continue
            rows = self.db.execute(
                f"SELECT tg_chat, tg_msg FROM msgmap WHERE wx_thread=? AND {col}=? AND side=?{in_chat} "
                "ORDER BY tg_msg",
                (wx_thread, val, side, *chat_args)).fetchall()
            if rows:
                target_chat = rows[0][0]
                return target_chat, [r[1] for r in rows if r[0] == target_chat]
            if side == "bot":
                rows = self.db.execute(
                    f"SELECT tg_chat, tg_msg FROM msgmap WHERE wx_thread=? AND {col}=? AND side='user' "
                    f"AND tg_chat < -1000000000000{in_chat} ORDER BY tg_msg",
                    (wx_thread, val, *chat_args)).fetchall()
                if rows:
                    target_chat = rows[0][0]
                    return target_chat, [r[1] for r in rows if r[0] == target_chat]
        return None

    def tg_for_wx(self, wx_thread: str, wx_msg: str | None, wx_server: str | None,
                  side: str = "bot", tg_chat: int | None = None) -> tuple[int, int] | None:
        """Telegram copy of a WeChat message; `tg_chat` limits it to one chat (e.g. after switching modes)."""
        hit = self.tg_msgs_for_wx(wx_thread, wx_msg, wx_server, side=side, tg_chat=tg_chat)
        return (hit[0], hit[1][0]) if hit else None

    def add_sent(self, wx_thread: str, wx_server: str) -> None:
        wx_server = self._norm_id(wx_server)
        if not wx_server:
            return
        self.db.execute("INSERT OR IGNORE INTO sent(wx_thread,wx_server) VALUES(?,?)", (wx_thread, wx_server))
        self._maybe_prune()

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
                self._maybe_prune()
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
        with self._tx():
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
