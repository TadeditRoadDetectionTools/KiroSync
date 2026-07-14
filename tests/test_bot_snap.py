"""bot/bot.py 快照處理的測試 (用假 discord, 不連線)。

TestOnSnap:        _apply_snapshot 換成記錄器, 驗「收集 → 收齊 → 依序併回 → 清緩衝」。
TestApplySnapshot: ensure_forum/ensure_thread/_delete_msgs 換成假件, 驗「渲染進度 +
                   舊分片清理」的簿記邏輯。
"""

import asyncio
import io
import json
import unittest
import zipfile
from pathlib import Path

import _paths  # noqa: F401
import _stub_discord

_stub_discord.install()

from bot import KiroBot
from store import Store


class FakeAttachment:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self) -> bytes:
        return self._data


class FakeChannel:
    def __init__(self, cid: int):
        self.id = cid


class FakeMessage:
    def __init__(self, mid: int, data: bytes, channel_id: int = 555):
        self.id = mid
        self.attachments = [FakeAttachment(data)]
        self.channel = FakeChannel(channel_id)


def _header(sid="sid-1", gen="g1", p=0, n=1, user="alice"):
    return {"u": user, "s": sid, "k": "Snap", "g": gen, "p": p, "n": n,
            "title": "t", "cwd": "/w"}


class TestOnSnap(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(":memory:"))
        self.bot = KiroBot({"guild_id": "1", "category_id": "2"}, self.store)
        self.applied = []

        async def record_apply(h, s, gen, blob, msg_ids, channel_id):
            self.applied.append(
                {"s": s, "gen": gen, "blob": blob,
                 "msg_ids": msg_ids, "channel_id": channel_id})

        self.bot._apply_snapshot = record_apply

    def tearDown(self):
        self.store.close()

    def _snap(self, h, msg):
        return asyncio.run(self.bot._on_snap(h, msg))

    def test_single_part_applies_immediately(self):
        keep = self._snap(_header(n=1), FakeMessage(100, b"zipdata"))
        self.assertTrue(keep)  # 分片訊息要保留當離線 pull 來源
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.applied[0]["blob"], b"zipdata")
        self.assertEqual(self.applied[0]["msg_ids"], [100])
        self.assertEqual(self.applied[0]["channel_id"], 555)
        self.assertEqual(self.bot._snap_buf, {})  # 緩衝清乾淨

    def test_out_of_order_parts_reassemble_in_order(self):
        keep1 = self._snap(_header(p=1, n=2), FakeMessage(201, b"BBB"))
        self.assertTrue(keep1)
        self.assertEqual(self.applied, [])  # 還沒收齊
        keep0 = self._snap(_header(p=0, n=2), FakeMessage(200, b"AAA"))
        self.assertTrue(keep0)
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.applied[0]["blob"], b"AAABBB")       # 依片序併回
        self.assertEqual(self.applied[0]["msg_ids"], [200, 201])  # id 也依片序
        self.assertEqual(self.bot._snap_buf, {})

    def test_incomplete_generation_waits(self):
        self._snap(_header(p=0, n=3), FakeMessage(1, b"a"))
        self._snap(_header(p=2, n=3), FakeMessage(3, b"c"))
        self.assertEqual(self.applied, [])
        self.assertIn(("sid-1", "g1"), self.bot._snap_buf)

    def test_stale_generation_purged_when_new_gen_arrives(self):
        # g1 有片上傳失敗, 永遠收不齊; g2 的片開始到達時要把 g1 殘片清掉 (緩衝有界)
        self._snap(_header(gen="g1", p=0, n=2), FakeMessage(1, b"old0"))
        self.assertIn(("sid-1", "g1"), self.bot._snap_buf)
        self._snap(_header(gen="g2", p=0, n=2), FakeMessage(2, b"new0"))
        self.assertNotIn(("sid-1", "g1"), self.bot._snap_buf)
        self.assertNotIn(("sid-1", "g1"), self.bot._snap_msgs)
        # g2 自己已收的片不受清理影響, 照常收齊套用
        keep = self._snap(_header(gen="g2", p=1, n=2), FakeMessage(3, b"new1"))
        self.assertTrue(keep)
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.applied[0]["gen"], "g2")
        self.assertEqual(self.applied[0]["blob"], b"new0new1")
        self.assertEqual(self.bot._snap_buf, {})

    def test_purge_only_touches_same_session(self):
        # 清舊世代只針對同一個 session, 別的 session 的等待中殘片不能被誤刪
        self._snap(_header(sid="sid-A", gen="g1", p=0, n=2), FakeMessage(1, b"a"))
        self._snap(_header(sid="sid-B", gen="g9", p=0, n=2), FakeMessage(2, b"b"))
        self.assertIn(("sid-A", "g1"), self.bot._snap_buf)
        self.assertIn(("sid-B", "g9"), self.bot._snap_buf)

    def test_missing_attachment_or_sid_rejected(self):
        msg = FakeMessage(1, b"x")
        msg.attachments = []
        self.assertFalse(self._snap(_header(), msg))
        self.assertFalse(self._snap(_header(sid=""), FakeMessage(2, b"x")))
        self.assertEqual(self.applied, [])

    def test_bad_part_numbers_rejected(self):
        h = _header()
        h["p"], h["n"] = "abc", "def"
        self.assertFalse(self._snap(h, FakeMessage(1, b"x")))
        self.assertEqual(self.applied, [])

    def test_route_dispatches_snap(self):
        keep = asyncio.run(self.bot.route(_header(n=1), FakeMessage(9, b"z")))
        self.assertTrue(keep)
        self.assertEqual(len(self.applied), 1)

    def test_route_ignores_unknown_kind(self):
        keep = asyncio.run(self.bot.route({"u": "alice", "k": "Mystery"}, None))
        self.assertFalse(keep)
        self.assertEqual(self.applied, [])


class FakeThread:
    def __init__(self):
        self.name = "原本的名字"
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)

    async def edit(self, name=None, **kwargs):
        if name:
            self.name = name


def _zip_blob(jsonl_lines, meta=None, sid="sid-1") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{sid}.jsonl",
                   "".join(line + "\n" for line in jsonl_lines))
        if meta is not None:
            z.writestr(f"{sid}.json", json.dumps(meta, ensure_ascii=False))
    return buf.getvalue()


def _prompt_line(text: str) -> str:
    return json.dumps(
        {"kind": "Prompt", "data": {"content": [{"kind": "text", "data": text}]}},
        ensure_ascii=False)


class TestApplySnapshot(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(":memory:"))
        self.bot = KiroBot({"guild_id": "1", "category_id": "2"}, self.store)
        self.thread = FakeThread()
        self.deleted = []

        async def fake_forum(user):
            return object()

        async def fake_thread(forum, sid, title, cwd, user):
            return self.thread

        async def fake_delete(channel_id, msg_ids):
            self.deleted.append((channel_id, list(msg_ids)))

        self.bot.ensure_forum = fake_forum
        self.bot.ensure_thread = fake_thread
        self.bot._delete_msgs = fake_delete

    def tearDown(self):
        self.store.close()

    def _apply(self, blob, gen="g1", msg_ids=(1,), channel_id=42, title=None):
        h = {"u": "alice", "s": "sid-1", "k": "Snap", "title": title, "cwd": "/w"}
        asyncio.run(self.bot._apply_snapshot(
            h, "sid-1", gen, blob, list(msg_ids), channel_id))

    def test_renders_only_appended_lines(self):
        self._apply(_zip_blob([_prompt_line("一"), _prompt_line("二")]),
                    gen="g1", msg_ids=[1])
        self.assertEqual(len(self.thread.sent), 2)
        self.assertEqual(self.store.get_rendered("sid-1"), 2)
        # 下一世代多一行 → 只貼新增的那行
        self._apply(_zip_blob([_prompt_line("一"), _prompt_line("二"),
                               _prompt_line("三")]),
                    gen="g2", msg_ids=[2])
        self.assertEqual(len(self.thread.sent), 3)
        self.assertIn("三", self.thread.sent[-1])
        self.assertEqual(self.store.get_rendered("sid-1"), 3)

    def test_line_count_regression_resets_progress(self):
        # session 檔被重寫變短 (append-only 假設被打破): 不重複貼, 但進度要跟上,
        # 之後的新行才會繼續渲染 (修掉「永遠卡死」的行為)
        self.store.set_rendered("sid-1", 10)
        self._apply(_zip_blob([_prompt_line("一"), _prompt_line("二")]),
                    gen="g1", msg_ids=[1])
        self.assertEqual(self.thread.sent, [])                      # 不重複貼舊行
        self.assertEqual(self.store.get_rendered("sid-1"), 2)       # 進度重設到檔尾
        self._apply(_zip_blob([_prompt_line("一"), _prompt_line("二"),
                               _prompt_line("三")]),
                    gen="g2", msg_ids=[2])
        self.assertEqual(len(self.thread.sent), 1)                  # 新行恢復渲染
        self.assertIn("三", self.thread.sent[0])

    def test_new_generation_deletes_old_parts(self):
        blob = _zip_blob([_prompt_line("一")])
        self._apply(blob, gen="g1", msg_ids=[10, 11])
        self._apply(blob, gen="g2", msg_ids=[20])
        self.assertEqual(self.deleted, [(42, [10, 11])])
        self.assertEqual(self.store.get_snapshot("sid-1")["msg_ids"], [20])

    def test_same_generation_resend_deletes_orphan_parts(self):
        # client 重啟後同世代重送: 訊息 id 是新的, 舊分片一樣是孤兒, 要刪
        blob = _zip_blob([_prompt_line("一")])
        self._apply(blob, gen="g1", msg_ids=[10, 11])
        self._apply(blob, gen="g1", msg_ids=[30, 31])
        self.assertEqual(self.deleted, [(42, [10, 11])])
        self.assertEqual(self.store.get_snapshot("sid-1")["msg_ids"], [30, 31])

    def test_overlapping_msg_ids_not_deleted(self):
        # 交集的 id 還是最新快照的一部分, 不能誤刪
        blob = _zip_blob([_prompt_line("一")])
        self._apply(blob, gen="g1", msg_ids=[10, 11])
        self._apply(blob, gen="g1", msg_ids=[11, 12])
        self.assertEqual(self.deleted, [(42, [10])])

    def test_bad_zip_skipped_without_side_effects(self):
        self._apply(b"not a zip", gen="g1", msg_ids=[1])
        self.assertEqual(self.thread.sent, [])
        self.assertEqual(self.deleted, [])
        self.assertIsNone(self.store.get_snapshot("sid-1"))

    def test_title_rename(self):
        self._apply(_zip_blob([_prompt_line("一")]), title="新標題")
        self.assertEqual(self.thread.name, "新標題")


if __name__ == "__main__":
    unittest.main()
