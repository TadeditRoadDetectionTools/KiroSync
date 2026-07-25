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
    user_key    TEXT NOT NULL,
    category_id INTEGER NOT NULL DEFAULT 0,  -- 0 = 家用/預設分類的 forum; 其餘 = 路由到的 Discord 分類 id
    forum_id    INTEGER NOT NULL,
    PRIMARY KEY (user_key, category_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    thread_id  INTEGER,
    forum_id   INTEGER,
    rendered   INTEGER NOT NULL DEFAULT 0,  -- 已貼到 thread 的 .jsonl 行數 (架構 B: bot 端渲染進度)
    summarized INTEGER NOT NULL DEFAULT 0   -- 已被 /summary 總結到的 .jsonl 行數 (diff 游標)
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

# info thread 的哨兵列 (__info__:<user>) 不是真 session, 對外查詢一律排除
_NOT_SENTINEL = "session_id NOT LIKE '\\_\\_info\\_\\_:%' ESCAPE '\\'"


def _esc_like(s: str) -> str:
    """跳脫使用者輸入裡的 LIKE 萬用字元, 讓 %/_ 只當普通字元比對。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Store:
    def __init__(self, db_path: Path):
        self.db = sqlite3.connect(str(db_path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        # 舊 DB 的 sessions 表可能缺欄位; 補上 (CREATE IF NOT EXISTS 不會加欄位)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(sessions)").fetchall()}
        for col in ("rendered", "summarized"):
            if col not in cols:
                self.db.execute(
                    f"ALTER TABLE sessions ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        # 舊 DB 的 user_forums 是 user_key 單一主鍵; 改成 (user_key, category_id) 複合鍵。
        # 不能靠 ALTER 改主鍵, 重建表; 既有列一律標 category_id=0 (家用/預設分類)。
        uf = {r["name"] for r in self.db.execute("PRAGMA table_info(user_forums)").fetchall()}
        if "category_id" not in uf:
            self.db.executescript(
                "ALTER TABLE user_forums RENAME TO user_forums_legacy;"
                "CREATE TABLE user_forums ("
                "  user_key TEXT NOT NULL, category_id INTEGER NOT NULL DEFAULT 0,"
                "  forum_id INTEGER NOT NULL, PRIMARY KEY (user_key, category_id));"
                "INSERT INTO user_forums(user_key, category_id, forum_id) "
                "  SELECT user_key, 0, forum_id FROM user_forums_legacy;"
                "DROP TABLE user_forums_legacy;"
            )

    def get_forum(self, user_key: str, category_id: int = 0) -> Optional[int]:
        """category_id=0 = 家用/預設分類的 forum; 其餘 = 路由到的分類。"""
        row = self.db.execute(
            "SELECT forum_id FROM user_forums WHERE user_key = ? AND category_id = ?",
            (user_key, int(category_id)),
        ).fetchone()
        return int(row["forum_id"]) if row else None

    def set_forum(self, user_key: str, forum_id: int, category_id: int = 0) -> None:
        self.db.execute(
            "INSERT INTO user_forums(user_key, category_id, forum_id) VALUES(?, ?, ?) "
            "ON CONFLICT(user_key, category_id) DO UPDATE SET forum_id = excluded.forum_id",
            (user_key, int(category_id), forum_id),
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

    def list_sessions(self, *, user_key: str = None, prefix: str = None) -> list[dict]:
        """列出 session, 回 [{session_id, user_key}] (依 id 排序)。
        兩個條件都不給 = 全部。用來支撐 /summary 的三種範圍 (全部/某使用者/某 session)。
        一律排除 __info__: 哨兵列 (那是 info thread 的對應, 不是真 session)。"""
        where = [f"s.{_NOT_SENTINEL}"]
        params: list = []
        if user_key is not None:
            # 一個使用者現在可能有多個 forum (每個路由分類一個), 用 IN 涵蓋全部
            where.append("s.forum_id IN (SELECT forum_id FROM user_forums WHERE user_key = ?)")
            params.append(user_key)
        if prefix is not None:
            where.append("s.session_id LIKE ? ESCAPE '\\'")
            params.append(_esc_like(prefix) + "%")
        rows = self.db.execute(
            "SELECT s.session_id, u.user_key FROM sessions s "
            "LEFT JOIN user_forums u ON u.forum_id = s.forum_id "
            "WHERE " + " AND ".join(where) + " ORDER BY s.session_id",
            params,
        ).fetchall()
        return [{"session_id": r["session_id"], "user_key": r["user_key"]} for r in rows]

    def find_sessions(self, prefix: str) -> list[str]:
        """依 id 前綴找 session id。使用者輸入的 LIKE 萬用字元 (%/_) 會被跳脫。"""
        return [r["session_id"] for r in self.list_sessions(prefix=prefix)]

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

    def get_summarized(self, session_id: str) -> int:
        """已被 /summary 總結到的行數 (diff 游標); 沒總結過回 0。"""
        row = self.db.execute(
            "SELECT summarized FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["summarized"]) if row and row["summarized"] is not None else 0

    def set_summarized(self, session_id: str, n: int) -> None:
        self.db.execute(
            "INSERT INTO sessions(session_id, summarized) VALUES(?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET summarized = excluded.summarized",
            (session_id, n),
        )
        self.db.commit()

    def get_summary_roles(self) -> list[int]:
        """除了 Discord 原生管理權限外, 額外可用 /summary 的身分組 id。"""
        try:
            ids = json.loads(self.get_kv("summary_roles") or "[]")
        except Exception:
            return []
        return [int(i) for i in ids] if isinstance(ids, list) else []

    def set_summary_roles(self, role_ids) -> None:
        self.set_kv("summary_roles", json.dumps(sorted({int(i) for i in role_ids})))

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
