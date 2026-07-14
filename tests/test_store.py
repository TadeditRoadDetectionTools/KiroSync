"""bot/store.py 的測試: 對應關係持久化 + 舊 schema 遷移。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import _paths  # noqa: F401

from store import Store


class TestStore(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(":memory:"))

    def tearDown(self):
        self.store.close()

    def test_forum_roundtrip_and_upsert(self):
        self.assertIsNone(self.store.get_forum("alice"))
        self.store.set_forum("alice", 111)
        self.assertEqual(self.store.get_forum("alice"), 111)
        self.store.set_forum("alice", 222)  # 重建 forum 後覆蓋
        self.assertEqual(self.store.get_forum("alice"), 222)

    def test_thread_roundtrip(self):
        self.assertIsNone(self.store.get_thread("sid-1"))
        self.store.set_thread("sid-1", 333, 111)
        self.assertEqual(self.store.get_thread("sid-1"), 333)

    def test_rendered_default_zero_and_update(self):
        self.assertEqual(self.store.get_rendered("nope"), 0)
        self.store.set_thread("sid-1", 333, 111)
        self.assertEqual(self.store.get_rendered("sid-1"), 0)
        self.store.set_rendered("sid-1", 42)
        self.assertEqual(self.store.get_rendered("sid-1"), 42)
        # set_rendered 不該弄丟 thread 對應
        self.assertEqual(self.store.get_thread("sid-1"), 333)

    def test_rendered_upsert_without_existing_row(self):
        self.store.set_rendered("fresh", 5)
        self.assertEqual(self.store.get_rendered("fresh"), 5)
        self.assertIsNone(self.store.get_thread("fresh"))  # thread 尚未建立

    def test_snapshot_roundtrip(self):
        self.assertIsNone(self.store.get_snapshot("sid-1"))
        self.store.set_snapshot("sid-1", "100-abcd1234", 999, [1, 2, 3])
        snap = self.store.get_snapshot("sid-1")
        self.assertEqual(snap["gen"], "100-abcd1234")
        self.assertEqual(snap["channel_id"], 999)
        self.assertEqual(snap["msg_ids"], [1, 2, 3])
        # 新世代覆蓋
        self.store.set_snapshot("sid-1", "200-ffff0000", 999, [7])
        self.assertEqual(self.store.get_snapshot("sid-1")["msg_ids"], [7])

    def test_find_sessions_prefix_and_sentinel_excluded(self):
        self.store.set_thread("abc123", 1, 1)
        self.store.set_thread("abd456", 2, 1)
        self.store.set_thread("__info__:alice", 5, 1)  # info thread 哨兵列
        self.assertEqual(self.store.find_sessions("ab"), ["abc123", "abd456"])
        self.assertEqual(self.store.find_sessions("abc"), ["abc123"])
        # 哨兵列永遠不該被 /link 撈到 — 不論用什麼前綴
        self.assertEqual(self.store.find_sessions("__"), [])
        self.assertNotIn("__info__:alice", self.store.find_sessions(""))

    def test_find_sessions_escapes_like_wildcards(self):
        self.store.set_thread("a_b789", 3, 1)
        self.store.set_thread("axb000", 4, 1)
        self.assertEqual(self.store.find_sessions("a_"), ["a_b789"])  # _ 不是萬用字元
        self.assertEqual(self.store.find_sessions("%"), [])           # % 也不是
        self.assertEqual(self.store.find_sessions("a"), ["a_b789", "axb000"])

    def test_kv_roundtrip(self):
        self.assertIsNone(self.store.get_kv("webhook_url"))
        self.store.set_kv("webhook_url", "https://a")
        self.store.set_kv("webhook_url", "https://b")
        self.assertEqual(self.store.get_kv("webhook_url"), "https://b")


class TestMigration(unittest.TestCase):
    def test_old_sessions_table_gains_rendered_column(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "old.db"
            db = sqlite3.connect(str(db_path))
            db.execute(
                "CREATE TABLE sessions ("
                "session_id TEXT PRIMARY KEY, thread_id INTEGER, forum_id INTEGER)"
            )
            db.execute("INSERT INTO sessions VALUES ('s1', 10, 20)")
            db.commit()
            db.close()

            store = Store(db_path)
            try:
                self.assertEqual(store.get_thread("s1"), 10)      # 舊資料還在
                self.assertEqual(store.get_rendered("s1"), 0)      # 新欄位補 0
                store.set_rendered("s1", 9)
                self.assertEqual(store.get_rendered("s1"), 9)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
