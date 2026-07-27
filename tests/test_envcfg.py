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

    def test_curly_and_fullwidth_quotes_stripped(self):
        # 中文輸入法打出的全形/彎引號跟 ASCII 不同字元, 也要剝掉否則 URL 失效
        url = "https://discord.com/api/webhooks/1/a"
        self.assertEqual(self._load(f"A=“{url}”\n")["A"], url)   # 彎雙引號
        self.assertEqual(self._load(f"B=＂{url}＂\n")["B"], url)  # 全形雙引號
        self.assertEqual(self._load(f"C=「{url}」\n")["C"], url)  # 角括號

    def test_trailing_space_inside_quotes(self):
        url = "https://discord.com/api/webhooks/1/a"
        self.assertEqual(self._load(f'D="{url} "\n')["D"], url)

    def test_unmatched_leading_quote(self):
        url = "https://discord.com/api/webhooks/1/a"
        self.assertEqual(self._load(f'E="{url}\n')["E"], url)

    def test_missing_file_returns_empty(self):
        self.assertEqual(envcfg.load_env("/nonexistent/.env"), {})


class TestSetEnvValue(unittest.TestCase):
    """寫回 .env: 就地更新既有行、保留註解、缺就補、Big5 舊檔正規化成 UTF-8。"""

    def _write(self, td, text, encoding="utf-8"):
        p = Path(td) / ".env"
        p.write_bytes(text.encode(encoding))
        return p

    def test_updates_existing_key_in_place(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "# 註解\nWEBHOOK_URL=\nUSER_NAME=bob\n")
            envcfg.set_env_value("WEBHOOK_URL", "https://x/y", str(p))
            out = p.read_text(encoding="utf-8")
            self.assertIn("WEBHOOK_URL=https://x/y", out)
            self.assertIn("# 註解", out)          # 註解保留
            self.assertIn("USER_NAME=bob", out)   # 其他設定不動
            self.assertEqual(envcfg.load_env(str(p))["WEBHOOK_URL"], "https://x/y")

    def test_appends_when_key_absent(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "USER_NAME=bob\n")
            envcfg.set_env_value("WEBHOOK_URL", "https://x/y", str(p))
            data = envcfg.load_env(str(p))
            self.assertEqual(data["WEBHOOK_URL"], "https://x/y")
            self.assertEqual(data["USER_NAME"], "bob")

    def test_creates_file_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ".env"
            envcfg.set_env_value("USER_NAME", "alice", str(p))
            self.assertTrue(p.exists())
            self.assertEqual(envcfg.load_env(str(p))["USER_NAME"], "alice")

    def test_does_not_touch_commented_key(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "# USER_NAME=範例\n")
            envcfg.set_env_value("USER_NAME", "alice", str(p))
            out = p.read_text(encoding="utf-8")
            self.assertIn("# USER_NAME=範例", out)  # 註解那行不能被當成設定改掉
            self.assertEqual(envcfg.load_env(str(p))["USER_NAME"], "alice")

    def test_big5_file_survives_and_becomes_utf8(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "SYNC_WORKSPACES=D:\\IGS\\AI測試\nUSER_NAME=\n", "cp950")
            envcfg.set_env_value("USER_NAME", "alice", str(p))
            data = envcfg.load_env(str(p))
            self.assertEqual(data["USER_NAME"], "alice")
            self.assertEqual(data["SYNC_WORKSPACES"], "D:\\IGS\\AI測試")  # 中文沒壞
            p.read_text(encoding="utf-8")  # 已是 UTF-8, 不該丟例外


class TestEncodingTolerance(unittest.TestCase):
    """.env 存成非 UTF-8 (中文 Windows Big5/ANSI) 或含 BOM 都不該讓 load_env 崩。"""

    def _load_bytes(self, text: str, encoding: str) -> dict:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ".env"
            p.write_bytes((text + "\n").encode(encoding))
            return envcfg.load_env(str(p))

    def test_big5_does_not_crash(self):
        # 舊版寫死 utf-8 讀取, 這行含中文會 UnicodeDecodeError 而秒退
        data = self._load_bytes("SYNC_WORKSPACES=D:\\IGS\\AI測試", "cp950")
        self.assertEqual(data.get("SYNC_WORKSPACES"), "D:\\IGS\\AI測試")

    def test_utf8_bom_first_key_not_polluted(self):
        # UTF-8 with BOM: 第一個 key 不該被 ﻿ 汙染而讀不到
        data = self._load_bytes("WEBHOOK_URL=https://x/y", "utf-8-sig")
        self.assertEqual(data.get("WEBHOOK_URL"), "https://x/y")


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
