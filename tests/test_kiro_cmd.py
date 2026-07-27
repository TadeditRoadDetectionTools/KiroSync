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


if __name__ == "__main__":
    unittest.main()
