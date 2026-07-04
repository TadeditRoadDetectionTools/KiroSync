"""
Client 端儲存 (SQLite)。

  1. file_state: 每個 .jsonl 檔已讀到第幾個 byte -> 斷點續讀, 重啟不重複。
  2. sessions:   session metadata (title / cwd / model)。
  3. events:     逐則事件, 並用 UNIQUE(session_id, seq) 去重。

只用標準庫 sqlite3, 零依賴。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS file_state (
    path   TEXT PRIMARY KEY,
    offset INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    cwd        TEXT,
    title      TEXT,
    model      TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    ts         INTEGER,
    text       TEXT,
    raw_json   TEXT NOT NULL,
    UNIQUE(session_id, seq)
);
"""


class Store:
    def __init__(self, db_path: Path):
        self.db = sqlite3.connect(str(db_path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---- 斷點續讀 ----
    def get_offset(self, path: str) -> int:
        row = self.db.execute(
            "SELECT offset FROM file_state WHERE path = ?", (path,)
        ).fetchone()
        return row["offset"] if row else 0

    def set_offset(self, path: str, offset: int) -> None:
        self.db.execute(
            "INSERT INTO file_state(path, offset) VALUES(?, ?) "
            "ON CONFLICT(path) DO UPDATE SET offset = excluded.offset",
            (path, offset),
        )
        self.db.commit()

    # ---- session ----
    def upsert_session(
        self, session_id: str, *,
        cwd: Optional[str] = None, title: Optional[str] = None,
        model: Optional[str] = None, created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> None:
        existing = self.db.execute(
            "SELECT session_id FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if existing is None:
            self.db.execute(
                "INSERT INTO sessions(session_id, cwd, title, model, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (session_id, cwd, title, model, created_at, updated_at),
            )
        else:
            fields = {"cwd": cwd, "title": title, "model": model,
                      "created_at": created_at, "updated_at": updated_at}
            sets, vals = [], []
            for k, v in fields.items():
                if v is not None:
                    sets.append(f"{k} = ?")
                    vals.append(v)
            if sets:
                vals.append(session_id)
                self.db.execute(
                    f"UPDATE sessions SET {', '.join(sets)} WHERE session_id = ?", vals
                )
        self.db.commit()

    def known_session(self, session_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone() is not None

    # ---- events ----
    def add_event(
        self, session_id: str, seq: int, kind: str, ts: Optional[int],
        text: Optional[str], raw_json: str,
    ) -> bool:
        """回傳 True = 新事件 (成功插入); False = 重複。"""
        try:
            self.db.execute(
                "INSERT INTO events(session_id, seq, kind, ts, text, raw_json) "
                "VALUES(?,?,?,?,?,?)",
                (session_id, seq, kind, ts, text, raw_json),
            )
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def close(self) -> None:
        self.db.close()
