"""SYNC_WORKSPACES 限定資料夾比對測試。

之前是精確字串比對 (結尾斜線/大小寫/正反斜線/子資料夾全都對不上),
使用者一填就「什麼都不同步」。這裡鎖住正規化 + 子資料夾涵蓋的行為。
"""

import os
import unittest

import _paths  # noqa: F401  (設定 sys.path)
import run

CWD = r"D:\Programs\KiroTest"
SUB = r"D:\Programs\KiroTest\proj\src"


class TestInWorkspaces(unittest.TestCase):
    def test_empty_list_syncs_all(self):
        self.assertTrue(run._in_workspaces(CWD, []))
        self.assertTrue(run._in_workspaces("", []))

    def test_exact_match(self):
        self.assertTrue(run._in_workspaces(CWD, [r"D:\Programs\KiroTest"]))

    def test_trailing_separator(self):
        self.assertTrue(run._in_workspaces(CWD, [r"D:\Programs\KiroTest\\"]))

    def test_forward_slash(self):
        self.assertTrue(run._in_workspaces(CWD, ["D:/Programs/KiroTest"]))

    def test_subfolder_is_covered(self):
        self.assertTrue(run._in_workspaces(SUB, [r"D:\Programs\KiroTest"]))
        self.assertTrue(run._in_workspaces(CWD, [r"D:\Programs"]))

    def test_unrelated_is_blocked(self):
        self.assertFalse(run._in_workspaces(CWD, [r"D:\Other"]))

    def test_sibling_prefix_not_falsely_matched(self):
        # D:\Programs\Kiro 不該吃到 D:\Programs\KiroTest
        self.assertFalse(run._in_workspaces(CWD, [r"D:\Programs\Kiro"]))

    def test_blank_cwd_blocked_when_filtered(self):
        self.assertFalse(run._in_workspaces("", [r"D:\Programs\KiroTest"]))

    def test_multiple_workspaces_any_match(self):
        wl = [r"D:\Work\A", r"D:\Programs\KiroTest", r"D:\Work\B"]
        self.assertTrue(run._in_workspaces(SUB, wl))
        self.assertFalse(run._in_workspaces(r"D:\Elsewhere", wl))

    @unittest.skipUnless(os.name == "nt", "路徑大小寫不敏感只在 Windows")
    def test_case_insensitive_on_windows(self):
        self.assertTrue(run._in_workspaces(CWD, [r"d:\programs\kirotest"]))


if __name__ == "__main__":
    unittest.main()
