"""envcfg (bot/client 兩份相同) 的測試: .env 解析 + 環境變數優先。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _paths  # noqa: F401

import envcfg


class TestLoadEnv(unittest.TestCase):
    def _load(self, text: str) -> dict:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ".env"
            p.write_text(text, encoding="utf-8")
            return envcfg.load_env(str(p))

    def test_basic_parse(self):
        data = self._load(
            "# 註解要跳過\n"
            "\n"
            "FOO=bar\n"
            "QUOTED=\"hello world\"\n"
            "SINGLE='x'\n"
            "SPACED =  padded  \n"
            "no_equals_line\n"
            "EMPTY=\n"
        )
        self.assertEqual(data["FOO"], "bar")
        self.assertEqual(data["QUOTED"], "hello world")  # 雙引號剝掉
        self.assertEqual(data["SINGLE"], "x")            # 單引號剝掉
        self.assertEqual(data["SPACED"], "padded")       # 前後空白剝掉
        self.assertEqual(data["EMPTY"], "")
        self.assertNotIn("no_equals_line", data)
        self.assertNotIn("# 註解要跳過", data)

    def test_value_with_equals_sign(self):
        # webhook URL 常含 '=' — 只在第一個 '=' 切
        data = self._load("URL=https://x.example/?a=1&b=2\n")
        self.assertEqual(data["URL"], "https://x.example/?a=1&b=2")

    def test_missing_file_returns_empty(self):
        self.assertEqual(envcfg.load_env("/nonexistent/.env"), {})


class TestGet(unittest.TestCase):
    def test_env_var_overrides_dotenv(self):
        data = {"KEY": "from-dotenv"}
        with mock.patch.dict(os.environ, {"KEY": "from-env"}):
            self.assertEqual(envcfg.get(data, "KEY"), "from-env")

    def test_dotenv_fallback_and_default(self):
        data = {"KEY": "from-dotenv"}
        env = {k: v for k, v in os.environ.items() if k != "KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(envcfg.get(data, "KEY"), "from-dotenv")
            self.assertIsNone(envcfg.get(data, "MISSING"))
            self.assertEqual(envcfg.get(data, "MISSING", "dflt"), "dflt")


if __name__ == "__main__":
    unittest.main()
