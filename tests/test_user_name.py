"""USER_NAME 必填測試: 留空要拒絕啟動 (不再退回系統帳號)。"""

import os
import unittest
from unittest import mock

import _paths  # noqa: F401  (設定 sys.path)
import run


class TestUserNameRequired(unittest.TestCase):
    def test_missing_exits(self):
        # .env 與環境變數都沒有 USER_NAME -> 應 sys.exit, 不再 fallback 系統名
        env_no_user = {k: v for k, v in os.environ.items() if k != "USER_NAME"}
        with mock.patch.dict(run.ENV, {}, clear=True), \
             mock.patch.dict(os.environ, env_no_user, clear=True):
            with self.assertRaises(SystemExit):
                run.user_name()

    def test_blank_exits(self):
        with mock.patch.dict(os.environ, {"USER_NAME": "   "}):
            with self.assertRaises(SystemExit):
                run.user_name()

    def test_present_is_stripped(self):
        with mock.patch.dict(os.environ, {"USER_NAME": "  alice  "}):
            self.assertEqual(run.user_name(), "alice")


if __name__ == "__main__":
    unittest.main()
