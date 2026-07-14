"""bot/util.py chunk() 的測試。"""

import unittest

import _paths  # noqa: F401

from util import chunk


class TestChunk(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(chunk("", 10), [])
        self.assertEqual(chunk(None, 10), [])

    def test_short_text_single_piece(self):
        self.assertEqual(chunk("hello", 10), ["hello"])
        self.assertEqual(chunk("x" * 10, 10), ["x" * 10])

    def test_all_pieces_within_size(self):
        text = "\n".join(f"line-{i}" for i in range(200))
        for piece in chunk(text, 50):
            self.assertLessEqual(len(piece), 50)

    def test_prefers_newline_boundary(self):
        text = "aaaa\nbbbb\ncccc"
        pieces = chunk(text, 10)
        self.assertEqual(pieces[0], "aaaa\nbbbb")  # 在 size 內最後一個換行處切
        self.assertEqual(pieces[1], "cccc")

    def test_hard_cut_without_newline(self):
        text = "x" * 25
        self.assertEqual(chunk(text, 10), ["x" * 10, "x" * 10, "x" * 5])

    def test_content_preserved_ignoring_split_newlines(self):
        text = "aaaa\nbbbb\ncccc\ndddd"
        joined = "\n".join(chunk(text, 10))
        self.assertEqual(joined, text)

    def test_leading_newline_edge(self):
        # 第一個字元就是換行 (rfind 回 0) → 不能切出空片, 改成硬切
        pieces = chunk("\nabcdef", 3)
        self.assertTrue(all(pieces))
        self.assertTrue(all(len(p) <= 3 for p in pieces))


if __name__ == "__main__":
    unittest.main()
