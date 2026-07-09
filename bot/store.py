"""
Bot 端儲存 (SQLite): 只存「對應關係」。

  user_key -> forum 頻道 id
  session_id -> thread id (+ 所屬 forum)

這是唯一無法重算、必須持久化的狀態; 對話內容本身不存在這 (真相在各 client 的
.jsonl 與 Discord thread 裡)。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_forums (
    user_key TEXT PRIMARY KEY,
    forum_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    thread_id  INTEGER,
    forum_id   INTEGER,
    rendered   INTEGER NOT NULL DEFAULT 0  -- 已貼到 thread 的 .jsonl 行數 (架構 B: bot 端渲染進度)
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    session_id TEXT PRIMARY KEY,  -- 每個 session 只留「最新一代」快照的片訊息, 供離線拉取
    gen        TEXT,
    channel_id INTEGER,
    msg_ids    TEXT               -- JSON list, 依片序 (part 0..n-1) 的 message id
);
"""


class Store:
    def __init__(self, db_path: Path):
        self.db = sqlite3.connect(str(db_path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        # 舊 DB 的 sessions 表可能沒有 rendered 欄位; 補上 (CREATE IF NOT EXISTS 不會加欄位)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(sessions)").fetchall()}
        if "rendered" not in cols:
            self.db.execute("ALTER TABLE sessions ADD COLUMN rendered INTEGER NOT NULL DEFAULT 0")

    def get_forum(self, user_key: str) -> Optional[int]:
        row = self.db.execute(
            "SELECT forum_id FROM user_forums WHERE user_key = ?", (user_key,)
        ).fetchone()
        return int(row["forum_id"]) if row else None

    def set_forum(self, user_key: str, forum_id: int) -> None:
        self.db.execute(
            "INSERT INTO user_forums(user_key, forum_id) VALUES(?, ?) "
            "ON CONFLICT(user_key) DO UPDATE SET forum_id = excluded.forum_id",
            (user_key, forum_id),
        )
        self.db.commit()

    def get_thread(self, session_id: str) -> Optional[int]:
        row = self.db.execute(
            "SELECT thread_id FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["thread_id"]) if row and row["thread_id"] is not None else None

    def set_thread(self, session_id: str, thread_id: int, forum_id: int) -> None:
        self.db.execute(
            "INSERT INTO sessions(session_id, thread_id, forum_id) VALUES(?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET thread_id = excluded.thread_id, "
            "forum_id = excluded.forum_id",
            (session_id, thread_id, forum_id),
        )
        self.db.commit()

    def get_rendered(self, session_id: str) -> int:
        row = self.db.execute(
            "SELECT rendered FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["rendered"]) if row and row["rendered"] is not None else 0

    def set_rendered(self, session_id: str, n: int) -> None:
        self.db.execute(
            "INSERT INTO sessions(session_id, rendered) VALUES(?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET rendered = excluded.rendered",
            (session_id, n),
        )
        self.db.commit()

    def get_snapshot(self, session_id: str) -> Optional[dict]:
        row = self.db.execute(
            "SELECT gen, channel_id, msg_ids FROM snapshots WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        try:
            ids = json.loads(row["msg_ids"] or "[]")
        except Exception:
            ids = []
        return {"gen": row["gen"], "channel_id": row["channel_id"], "msg_ids": ids}

    def set_snapshot(self, session_id: str, gen: str, channel_id: int, msg_ids: list) -> None:
        self.db.execute(
            "INSERT INTO snapshots(session_id, gen, channel_id, msg_ids) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET gen = excluded.gen, "
            "channel_id = excluded.channel_id, msg_ids = excluded.msg_ids",
            (session_id, gen, channel_id, json.dumps(list(msg_ids))),
        )
        self.db.commit()

    def get_kv(self, k: str) -> Optional[str]:
        row = self.db.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
        return row["v"] if row else None

    def set_kv(self, k: str, v: str) -> None:
        self.db.execute(
            "INSERT INTO kv(k, v) VALUES(?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, v),
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()
