"""SQLite state: event cursor, WeChat thread <-> Telegram group, message id mapping."""
from __future__ import annotations

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
CREATE INDEX IF NOT EXISTS msgmap_wx_msg ON msgmap (wx_msg);
CREATE INDEX IF NOT EXISTS msgmap_wx_server ON msgmap (wx_thread, wx_server);
"""


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

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
        self.db.execute("UPDATE msgmap SET tg_chat=? WHERE tg_chat=?", (new_chat, old_chat))

    def linked_chats(self) -> set[int]:
        return {r[0] for r in self.db.execute("SELECT tg_chat FROM threads")}

    # --- messages ---------------------------------------------------------
    def map_message(self, tg_chat: int, side: str, tg_msg: int, wx_thread: str, wx_msg: str | None,
                    wx_server: str | None) -> None:
        self.db.execute(
            "INSERT INTO msgmap(tg_chat,side,tg_msg,wx_thread,wx_msg,wx_server) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(tg_chat,side,tg_msg) DO UPDATE SET wx_msg=COALESCE(excluded.wx_msg,wx_msg), "
            "wx_server=COALESCE(excluded.wx_server,wx_server)",
            (tg_chat, side, tg_msg, wx_thread, wx_msg, wx_server))

    def wx_for_tg(self, tg_chat: int, tg_msg: int, side: str = "user") -> tuple[str | None, str | None] | None:
        row = self.db.execute("SELECT wx_msg, wx_server FROM msgmap WHERE tg_chat=? AND side=? AND tg_msg=?",
                              (tg_chat, side, tg_msg)).fetchone()
        return (row[0], row[1]) if row else None

    def tg_for_wx(self, wx_thread: str, wx_msg: str | None, wx_server: str | None,
                  side: str = "bot") -> tuple[int, int] | None:
        if wx_msg:
            row = self.db.execute("SELECT tg_chat, tg_msg FROM msgmap WHERE wx_msg=? AND side=? ORDER BY tg_msg LIMIT 1",
                                  (wx_msg, side)).fetchone()
            if row:
                return row[0], row[1]
        if wx_server:
            row = self.db.execute(
                "SELECT tg_chat, tg_msg FROM msgmap WHERE wx_thread=? AND wx_server=? AND side=? ORDER BY tg_msg LIMIT 1",
                (wx_thread, wx_server, side)).fetchone()
            if row:
                return row[0], row[1]
        return None

    def add_sent(self, wx_thread: str, wx_server: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO sent(wx_thread,wx_server) VALUES(?,?)", (wx_thread, wx_server))

    def claim_echo(self, wx_thread: str, wx_server: str, wx_msg: str) -> bool:
        """True if this outgoing WeChat message is one we sent from Telegram (and links its saved id)."""
        if not self.db.execute("SELECT 1 FROM sent WHERE wx_thread=? AND wx_server=?",
                               (wx_thread, wx_server)).fetchone():
            return False
        self.db.execute("UPDATE msgmap SET wx_msg=? WHERE wx_thread=? AND wx_server=? AND wx_msg IS NULL",
                        (wx_msg, wx_thread, wx_server))
        return True

    def seen_wx_msg(self, wx_msg: str) -> bool:
        return self.db.execute("SELECT 1 FROM msgmap WHERE wx_msg=? LIMIT 1", (wx_msg,)).fetchone() is not None
