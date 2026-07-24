"""bot.py `_run_summary` 的端到端邏輯測試 (用假 discord + 假快照, 不連線、不打 LLM)。

驗的是「範圍解析 → 取新增行 → 統計/摘要 → 推進游標 → 出報告」這條線的接線。
"""

import asyncio
import io
import json
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401
import _stub_discord

_stub_discord.install()

import gemini
import summary
from bot import KiroBot
from store import Store

DAY1 = 1_752_000_000  # 某個固定時間, 測試不依賴真實時鐘


def _line(kind="Prompt", text="hi", ts=None):
    data = {"content": [{"kind": "text", "data": text}]}
    if ts is not None:
        data["meta"] = {"timestamp": ts}
    return json.dumps({"version": 1, "kind": kind, "data": data}, ensure_ascii=False)


def _zip(sid, lines, meta=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{sid}.jsonl", "".join(ln + "\n" for ln in lines))
        if meta is not None:
            z.writestr(f"{sid}.json", json.dumps(meta, ensure_ascii=False))
    return buf.getvalue()


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, file=None, **kw):
        self.sent.append({"content": content, "file": file})


class FakeInteraction:
    def __init__(self):
        self.followup = FakeFollowup()


class SummaryTestBase(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(":memory:"))
        self.blobs = {}  # sid -> zip bytes (缺席 = 分片已不可用)
        self._forums = {}
        self.bot = KiroBot(
            {"guild_id": "1", "category_id": "2", "gemini_api_key": ""}, self.store)

        async def fake_blob(sid):
            return self.blobs.get(sid)

        self.bot._snapshot_blob = fake_blob

    def tearDown(self):
        self.store.close()

    def _forum_id(self, user: str) -> int:
        # 固定編號; 不用 hash() —— 字串 hash 每個 process 都不同, 測試不該依賴它
        return self._forums.setdefault(user, 100 + len(self._forums))

    def _run(self, scope="all", target=None, since=None):
        inter = FakeInteraction()
        asyncio.run(self.bot._run_summary(inter, scope, target, since))
        text = "\n".join(s["content"] or "" for s in inter.followup.sent)
        return inter, text

    def _add_session(self, sid, user, lines, title=None):
        """一個「同步過」的 session: forum/thread 對應 + snapshots 紀錄 + 可抓回的分片。"""
        fid = self._forum_id(user)
        self.store.set_forum(user, fid)
        self.store.set_thread(sid, 1, fid)
        self.store.set_snapshot(sid, "g1", 42, [1])
        self.blobs[sid] = _zip(sid, lines, {"title": title or sid, "cwd": "/p"})


class TestScopes(SummaryTestBase):
    def test_all_scope_groups_users(self):
        self._add_session("a1111111", "alice", [_line(text="一")], title="A 的任務")
        self._add_session("b2222222", "bob", [_line(text="二")], title="B 的任務")
        _, text = self._run()
        self.assertIn("alice", text)
        self.assertIn("bob", text)
        self.assertIn("A 的任務", text)
        self.assertIn("B 的任務", text)
        self.assertIn("全部使用者", text)

    def test_user_scope_filters(self):
        self._add_session("a1111111", "alice", [_line(text="一")], title="A 的任務")
        self._add_session("b2222222", "bob", [_line(text="二")], title="B 的任務")
        _, text = self._run(scope="user", target="alice")
        self.assertIn("A 的任務", text)
        self.assertNotIn("B 的任務", text)

    def test_session_scope_by_prefix(self):
        self._add_session("a1111111", "alice", [_line(text="一")], title="A 的任務")
        self._add_session("b2222222", "bob", [_line(text="二")], title="B 的任務")
        _, text = self._run(scope="session", target="b222")
        self.assertIn("B 的任務", text)
        self.assertNotIn("A 的任務", text)

    def test_scope_needs_target(self):
        self._add_session("a1111111", "alice", [_line()])
        for scope in ("user", "session"):
            _, text = self._run(scope=scope)
            self.assertIn("指定", text)

    def test_no_matching_sessions(self):
        _, text = self._run(scope="user", target="nobody")
        self.assertIn("找不到符合的 session", text)


class TestCursorAndDate(SummaryTestBase):
    def test_cursor_mode_only_new_lines_then_advances(self):
        lines = [_line(text="一"), _line(text="二")]
        self._add_session("a1111111", "alice", lines)
        _, text = self._run()
        self.assertIn("新增 2 行", text)
        self.assertEqual(self.store.get_summarized("a1111111"), 2)  # 游標推到檔尾

        # 沒有新內容 -> 這個 session 不出現在報告裡
        _, text2 = self._run()
        self.assertIn("沒有新的對話內容", text2)

        # 追加一行 -> 只算新增的那行
        self.blobs["a1111111"] = _zip("a1111111", lines + [_line(text="三")],
                                      {"title": "a1111111"})
        _, text3 = self._run()
        self.assertIn("新增 1 行", text3)
        self.assertEqual(self.store.get_summarized("a1111111"), 3)

    def test_date_mode_selects_by_timestamp(self):
        lines = [_line(text="舊", ts=DAY1), _line(text="新", ts=DAY1 + 86400 * 3)]
        self._add_session("a1111111", "alice", lines)
        since = summary.fmt_ts(DAY1 + 86400 * 3)[:10]  # 新那行的日期 (YYYY-MM-DD)
        _, text = self._run(since=since)
        self.assertIn("新增 1 行", text)

    def test_bad_date_aborts_without_touching_cursor(self):
        self._add_session("a1111111", "alice", [_line()])
        _, text = self._run(since="2026/07/16")
        self.assertIn("日期格式看不懂", text)
        self.assertEqual(self.store.get_summarized("a1111111"), 0)

    def test_cursor_is_per_session(self):
        self._add_session("a1111111", "alice", [_line(text="一")])
        self._add_session("b2222222", "bob", [_line(text="二"), _line(text="三")])
        self._run()
        self.assertEqual(self.store.get_summarized("a1111111"), 1)
        self.assertEqual(self.store.get_summarized("b2222222"), 2)


class TestSnapshotFailures(SummaryTestBase):
    def test_unavailable_snapshot_listed_as_skipped(self):
        self._add_session("a1111111", "alice", [_line(text="一")])
        self.store.set_forum("bob", 200)
        self.store.set_thread("dead1234", 1, 200)  # 有 session 但沒有可用分片
        _, text = self._run()
        self.assertIn("略過 1 個 session", text)
        self.assertIn("dead1234", text)
        self.assertIn("alice", text)  # 其他 session 照常出

    def test_bad_zip_skipped_and_cursor_untouched(self):
        self.store.set_forum("alice", 100)
        self.store.set_thread("a1111111", 1, 100)
        self.blobs["a1111111"] = b"not a zip"
        _, text = self._run()
        self.assertIn("略過 1 個 session", text)
        self.assertEqual(self.store.get_summarized("a1111111"), 0)


class LLMTestBase(SummaryTestBase):
    def _fake_calls(self, outcomes):
        """攔 gemini.call (不是 call_chain) —— 讓真正的退場鏈邏輯也一起被驗到。
        outcomes: {model: (text, err)}; 沒列到的 model 一律當 404。"""
        seen = []

        async def fake_call(prompt, *, api_key, model):
            seen.append({"model": model, "prompt": prompt, "key": api_key})
            return outcomes.get(model, (None, "HTTP 404 — model not found"))

        return mock.patch.object(gemini, "call", fake_call), seen


class TestLLMWiring(LLMTestBase):
    def test_without_key_stats_only_and_no_llm_call(self):
        self._add_session("a1111111", "alice", [_line(text="一")])
        patch, seen = self._fake_calls({})
        with patch:
            _, text = self._run()
        self.assertEqual(seen, [])  # 沒 key 就不該打
        self.assertIn("未設定 GEMINI_API_KEY", text)
        self.assertIn("新增 1 行", text)

    def test_with_key_calls_llm_and_includes_summary(self):
        self.bot.gemini_key = "fake-key"
        self.bot.gemini_models = ["m1"]
        self._add_session("a1111111", "alice", [_line(text="幫我修 bug")],
                          title="修 bug")
        patch, seen = self._fake_calls({"m1": ("**完成事項** 修好了", None)})
        with patch:
            _, text = self._run()
        self.assertIn("幫我修 bug", seen[0]["prompt"])   # 文字稿有進 prompt
        self.assertIn("修 bug", seen[0]["prompt"])       # 標題也有
        self.assertEqual(seen[0]["key"], "fake-key")
        self.assertIn("**完成事項** 修好了", text)
        self.assertNotIn("未設定 GEMINI_API_KEY", text)

    def test_llm_failure_falls_back_to_stats(self):
        self.bot.gemini_key = "fake-key"
        self.bot.gemini_models = ["m1", "m2"]
        self._add_session("a1111111", "alice", [_line(text="一")])
        patch, _ = self._fake_calls({})  # 全部 model 都失敗

        with patch:
            _, text = self._run()
        self.assertIn("新增 1 行", text)  # 統計還在, 功能不因 LLM 掛掉而不可用
        self.assertIn("摘要引擎全部不可用", text)
        self.assertIn("/summary-check", text)  # 要指路
        self.assertEqual(self.store.get_summarized("a1111111"), 1)


class TestModelChain(LLMTestBase):
    """多 model 依序退場, 並在報告裡標明實際用的是哪一個。"""

    def setUp(self):
        super().setUp()
        self.bot.gemini_key = "fake-key"
        self.bot.gemini_models = ["dead-1", "good-2", "good-3"]

    def test_report_names_the_model_used(self):
        self._add_session("a1111111", "alice", [_line(text="一")])
        patch, seen = self._fake_calls({"good-2": ("摘要", None)})
        with patch:
            _, text = self._run()
        self.assertIn("摘要引擎: `good-2`", text)   # 指出實際用了哪個
        self.assertNotIn("`dead-1`", text)          # 沒用到的不該被寫成用了
        self.assertEqual([c["model"] for c in seen], ["dead-1", "good-2"])

    def test_first_model_used_when_healthy(self):
        self.bot.gemini_models = ["good-1", "good-2"]
        self._add_session("a1111111", "alice", [_line(text="一")])
        patch, seen = self._fake_calls({"good-1": ("摘要", None)})
        with patch:
            _, text = self._run()
        self.assertIn("摘要引擎: `good-1`", text)
        self.assertEqual([c["model"] for c in seen], ["good-1"])  # 不該多試後面的

    def test_dead_model_not_retried_for_every_session(self):
        # 4 個 session 不該對已知不通的 model 白打 4 次 404
        for i in range(4):
            self._add_session(f"s{i}" + "0" * 6, "alice", [_line(text=str(i))])
        patch, seen = self._fake_calls({"good-2": ("摘要", None)})
        with patch:
            _, text = self._run()
        tried = [c["model"] for c in seen]
        self.assertEqual(tried.count("dead-1"), 1)   # 只在第一個 session 試過一次
        self.assertEqual(tried.count("good-2"), 4)   # 之後直接用它
        self.assertIn("摘要引擎: `good-2`", text)

    def test_mid_report_fallback_lists_both_models(self):
        # good-2 中途額度用完 -> 退到 good-3; 報告要把兩個都列出來
        state = {"n": 0}

        async def fake_call(prompt, *, api_key, model):
            if model == "good-2":
                state["n"] += 1
                if state["n"] > 1:
                    return None, "HTTP 429 — quota exceeded"
                return "摘要", None
            if model == "good-3":
                return "摘要", None
            return None, "HTTP 404 — model not found"

        for i in range(3):
            self._add_session(f"s{i}" + "0" * 6, "alice", [_line(text=str(i))])
        with mock.patch.object(gemini, "call", fake_call):
            _, text = self._run()
        self.assertIn("`good-2`", text)
        self.assertIn("`good-3`", text)


class TestSummaryCheck(SummaryTestBase):
    """/summary-check: 三個前提各自的狀態, 失敗時必須講得出原因。"""

    def _check(self):
        inter = FakeInteraction()
        asyncio.run(self.bot._run_summary_check(inter))
        return "\n".join(s["content"] or "" for s in inter.followup.sent)

    def test_healthy(self):
        self.bot.gemini_key = "fake-key"
        self.bot.gemini_models = ["good-1"]
        self._add_session("a1111111", "alice", [_line()])

        async def ok(prompt, *, api_key, model):
            return "OK", None

        with mock.patch.object(gemini, "call", ok):
            text = self._check()
        self.assertIn("服務正常", text)
        self.assertIn("model=`good-1`", text)  # 指出用的是哪個
        self.assertIn("1/1 個 session 有快照", text)
        self.assertNotIn("[失敗]", text)

    def test_engine_failure_reports_reason(self):
        self.bot.gemini_key = "bad-key"
        self.bot.gemini_models = ["m1"]
        self._add_session("a1111111", "alice", [_line()])

        async def fail(prompt, *, api_key, model):
            return None, "HTTP 400 — API key not valid"

        with mock.patch.object(gemini, "call", fail):
            text = self._check()
        self.assertIn("有項目不可用", text)
        self.assertIn("API key not valid", text)  # 真正的原因要出現
        self.assertIn("降級成只出統計卡", text)   # 以及影響是什麼

    def test_all_models_dead_lists_every_one(self):
        self.bot.gemini_key = "fake-key"
        self.bot.gemini_models = ["dead-1", "dead-2", "dead-3"]
        self._add_session("a1111111", "alice", [_line()])

        async def fail(prompt, *, api_key, model):
            return None, f"HTTP 404 — {model} not found"

        with mock.patch.object(gemini, "call", fail):
            text = self._check()
        self.assertIn("3 個 model 全部不可用", text)
        for m in ("dead-1", "dead-2", "dead-3"):
            self.assertIn(m, text)  # 每一個的原因都要講

    def test_fallback_is_warn_and_names_the_dead_model(self):
        # 能用但退過 = 前面那個每次都會白試一輪, 值得提醒使用者移掉
        self.bot.gemini_key = "fake-key"
        self.bot.gemini_models = ["dead-1", "good-2"]
        self._add_session("a1111111", "alice", [_line()])

        async def chain(prompt, *, api_key, model):
            if model == "good-2":
                return "OK", None
            return None, "HTTP 404 — model not found"

        with mock.patch.object(gemini, "call", chain):
            text = self._check()
        self.assertIn("可用, 但有降級", text)
        self.assertIn("退到第 2 個 model=`good-2`", text)
        self.assertIn("跳過 `dead-1`", text)
        self.assertIn("HTTP 404", text)
        self.assertNotIn("[失敗]", text)  # 不是壞掉, 只是有浪費

    def test_missing_key_is_warn_not_fail(self):
        # 沒填 key 是「降級」不是「壞掉」—— /summary 仍然可用
        self._add_session("a1111111", "alice", [_line()])
        text = self._check()
        self.assertIn("可用, 但有降級", text)
        self.assertIn("GEMINI_API_KEY", text)
        self.assertNotIn("[失敗]", text)

    def test_no_sessions_warns(self):
        text = self._check()
        self.assertIn("還沒有任何 session", text)

    def test_snapshot_recorded_but_parts_gone_is_fail(self):
        # DB 有記錄 != 分片還在; 實抓才驗得出來
        self.store.set_forum("alice", 100)
        self.store.set_thread("dead1234", 1, 100)
        self.store.set_snapshot("dead1234", "g1", 42, [1])
        text = self._check()
        self.assertIn("[失敗]", text)
        self.assertIn("抓不回", text)
        self.assertIn("dead1234", text)

    def test_partial_snapshots_warn(self):
        self._add_session("a1111111", "alice", [_line()])
        self.store.set_thread("b2222222", 2, self._forum_id("alice"))  # 沒有快照
        text = self._check()
        self.assertIn("1/2 個 session 有快照", text)
        self.assertIn("其餘會被略過", text)

    def test_access_lists_roles(self):
        self._add_session("a1111111", "alice", [_line()])
        self.store.set_summary_roles([555])
        text = self._check()
        self.assertIn("<@&555>", text)

    def test_probe_crash_is_reported_not_swallowed(self):
        # 檢查本身壞掉也要有回應, 不能讓指令靜默失敗
        self._add_session("a1111111", "alice", [_line()])

        async def boom(sid):
            raise RuntimeError("discord 掛了")

        self.bot._snapshot_blob = boom
        text = self._check()
        self.assertIn("檢查過程出錯", text)
        self.assertIn("discord 掛了", text)


class TestReportDelivery(LLMTestBase):
    def test_long_report_becomes_file_attachment(self):
        self.bot.gemini_key = "fake-key"
        self.bot.gemini_models = ["m1"]
        for i in range(12):
            self._add_session(f"s{i}" + "0" * 6, f"user{i}", [_line(text="x")])
        patch, _ = self._fake_calls({"m1": ("摘要 " * 300, None)})
        with patch:
            inter, _ = self._run()
        files = [s["file"] for s in inter.followup.sent if s["file"]]
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].filename.endswith(".md"))

    def test_short_report_sent_inline_within_limit(self):
        self._add_session("a1111111", "alice", [_line(text="一")])
        inter, _ = self._run()
        self.assertTrue(all(s["file"] is None for s in inter.followup.sent))
        for s in inter.followup.sent:
            self.assertLessEqual(len(s["content"] or ""), 2000)


if __name__ == "__main__":
    unittest.main()
