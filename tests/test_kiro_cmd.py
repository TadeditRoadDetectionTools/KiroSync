"""ks-kiro 啟動器的純邏輯測試 (詢問分類的判定 + 互動詢問更新 routes)。

subprocess 啟動 sync/kiro 的部分靠實機驗, 這裡只鎖住可純測的決策與路由持久化。
"""

import os
import tempfile
import unittest
from unittest import mock

import _paths  # noqa: F401
import routes
import run


class TestDecideCat(unittest.TestCase):
    def test_blank_keeps_existing(self):
        self.assertEqual(run._decide_cat("", "111"), ("keep", "111"))
        self.assertEqual(run._decide_cat("   ", None), ("keep", None))

    def test_dash_means_default(self):
        self.assertEqual(run._decide_cat("-", "111"), ("default", None))

    def test_digits_set(self):
        self.assertEqual(run._decide_cat("152938", None), ("set", "152938"))

    def test_non_digit_invalid_keeps(self):
        self.assertEqual(run._decide_cat("abc", "111"), ("invalid", "111"))


class TestPromptCategory(unittest.TestCase):
    def setUp(self):
        fd, self.p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.p)
        # 讓 routes 模組讀寫這個臨時檔
        self._patch = mock.patch.object(routes, "routes_path", lambda: __import__("pathlib").Path(self.p))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        if os.path.exists(self.p):
            os.remove(self.p)

    def test_set_new_route(self):
        with mock.patch("builtins.input", return_value="999"):
            cat = run._prompt_category(r"D:\Work\A", [])
        self.assertEqual(cat, "999")
        self.assertEqual(routes.category_for(r"D:\Work\A", routes.load_routes()), "999")

    def test_blank_keeps_default_when_none(self):
        with mock.patch("builtins.input", return_value=""):
            cat = run._prompt_category(r"D:\Work\A", [])
        self.assertIsNone(cat)
        self.assertEqual(routes.load_routes(), [])  # 沒寫入任何路由

    def test_dash_clears_existing(self):
        routes.add_route(r"D:\Work\A", "111", path=self.p)
        rl = routes.load_routes()
        with mock.patch("builtins.input", return_value="-"):
            cat = run._prompt_category(r"D:\Work\A", rl)
        self.assertIsNone(cat)
        self.assertIsNone(routes.category_for(r"D:\Work\A", routes.load_routes()))

    def test_eof_treated_as_blank(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            cat = run._prompt_category(r"D:\Work\A", [])
        self.assertIsNone(cat)


class TestEnsureEnvInteractive(unittest.TestCase):
    """.env 有空值時當場問使用者並寫回; 非互動環境則明確報錯不卡住。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.here = __import__("pathlib").Path(self.td.name)
        self._p = mock.patch.object(run, "HERE", self.here)
        self._p.start()
        # 讓 _e() 只看得到我們給的 ENV, 且環境變數不干擾
        self._env = mock.patch.dict(run.ENV, {}, clear=True)
        self._env.start()
        self._os = mock.patch.dict(
            os.environ, {k: v for k, v in os.environ.items()
                         if k not in ("WEBHOOK_URL", "USER_NAME")}, clear=True)
        self._os.start()

    def tearDown(self):
        self._os.stop()
        self._env.stop()
        self._p.stop()
        self.td.cleanup()

    def test_noop_when_all_present(self):
        run.ENV.update({"WEBHOOK_URL": "https://discord.com/api/webhooks/1/a",
                        "USER_NAME": "alice"})
        with mock.patch("builtins.input", side_effect=AssertionError("不該問")):
            run.ensure_env_interactive()

    def test_non_interactive_exits_instead_of_hanging(self):
        with mock.patch.object(run.sys.stdin, "isatty", return_value=False):
            with self.assertRaises(SystemExit):
                run.ensure_env_interactive()

    def test_prompts_and_writes_env(self):
        url = "https://discord.com/api/webhooks/1529410298883211425/abcDEF-123"
        with mock.patch.object(run.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", side_effect=[url, "alice"]):
            run.ensure_env_interactive()
        written = (self.here / ".env").read_text(encoding="utf-8")
        self.assertIn(f"WEBHOOK_URL={url}", written)
        self.assertIn("USER_NAME=alice", written)
        # 同一行程立刻讀得到 (背景 sync 子行程也才拿得到)
        self.assertEqual(run._e("WEBHOOK_URL"), url)
        self.assertEqual(run.user_name(), "alice")

    def test_rejects_bad_webhook_then_accepts(self):
        url = "https://discord.com/api/webhooks/1529410298883211425/abcDEF-123"
        with mock.patch.object(run.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", side_effect=["不是網址", url, "alice"]):
            run.ensure_env_interactive()
        self.assertEqual(run._e("WEBHOOK_URL"), url)

    def test_strips_quotes_user_pasted(self):
        url = "https://discord.com/api/webhooks/1529410298883211425/abcDEF-123"
        with mock.patch.object(run.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", side_effect=[f'"{url}"', "alice"]):
            run.ensure_env_interactive()
        self.assertEqual(run._e("WEBHOOK_URL"), url)  # 引號要被剝掉

    def test_eof_exits_immediately(self):
        with mock.patch.object(run.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(SystemExit):
                run.ensure_env_interactive()


if __name__ == "__main__":
    unittest.main()
