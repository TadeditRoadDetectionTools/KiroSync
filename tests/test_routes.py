"""client 資料夾→分類 ID 路由對應測試。"""

import os
import tempfile
import unittest

import _paths  # noqa: F401  (設定 sys.path)
import routes


class TestCategoryFor(unittest.TestCase):
    RS = [
        {"folder": r"D:\Work\ProjectA", "category_id": "111", "label": "A"},
        {"folder": r"D:\Work\ProjectA\sub", "category_id": "222", "label": "A-sub"},
        {"folder": r"D:\Other", "category_id": "333", "label": "O"},
    ]

    def test_exact(self):
        self.assertEqual(routes.category_for(r"D:\Work\ProjectA", self.RS), "111")

    def test_subfolder_covered(self):
        self.assertEqual(routes.category_for(r"D:\Work\ProjectA\x\y", self.RS), "111")

    def test_longest_prefix_wins(self):
        # 巢狀: sub 比 ProjectA 更長, 內層贏
        self.assertEqual(routes.category_for(r"D:\Work\ProjectA\sub\z", self.RS), "222")

    def test_no_match_returns_none(self):
        self.assertIsNone(routes.category_for(r"D:\Elsewhere", self.RS))

    def test_blank_cwd(self):
        self.assertIsNone(routes.category_for("", self.RS))

    @unittest.skipUnless(os.name == "nt", "路徑大小寫不敏感只在 Windows")
    def test_case_and_slash_insensitive(self):
        self.assertEqual(routes.category_for("d:/work/projecta/x", self.RS), "111")

    def test_route_missing_id_ignored(self):
        rs = [{"folder": r"D:\X", "category_id": "", "label": ""}]
        self.assertIsNone(routes.category_for(r"D:\X", rs))


class TestAddRemoveLoad(unittest.TestCase):
    def setUp(self):
        fd, self.p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.p)  # 讓它一開始不存在

    def tearDown(self):
        if os.path.exists(self.p):
            os.remove(self.p)

    def test_load_missing_is_empty(self):
        self.assertEqual(routes.load_routes(self.p), [])

    def test_add_then_load(self):
        routes.add_route(r"D:\A", "999", "labelA", path=self.p)
        rs = routes.load_routes(self.p)
        self.assertEqual(len(rs), 1)
        self.assertEqual(rs[0]["category_id"], "999")
        self.assertEqual(rs[0]["label"], "labelA")

    def test_add_same_folder_overwrites(self):
        routes.add_route(r"D:\A", "111", path=self.p)
        routes.add_route(r"D:\A\\", "222", path=self.p)  # 正規化後同資料夾
        rs = routes.load_routes(self.p)
        self.assertEqual(len(rs), 1)
        self.assertEqual(rs[0]["category_id"], "222")

    def test_remove(self):
        routes.add_route(r"D:\A", "111", path=self.p)
        self.assertTrue(routes.remove_route(r"D:\A", path=self.p))
        self.assertEqual(routes.load_routes(self.p), [])
        self.assertFalse(routes.remove_route(r"D:\A", path=self.p))

    def test_category_id_coerced_to_str(self):
        routes.add_route(r"D:\A", 12345, path=self.p)  # 傳數字也存成字串
        self.assertEqual(routes.load_routes(self.p)[0]["category_id"], "12345")


if __name__ == "__main__":
    unittest.main()
