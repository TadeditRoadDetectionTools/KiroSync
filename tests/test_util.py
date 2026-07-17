"""bot/util.py 的測試: chunk() 與 read_snapshot_zip()。"""

import io
import json
import unittest
import zipfile

import _paths  # noqa: F401

from util import chunk, read_snapshot_zip


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


def _zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


class TestReadSnapshotZip(unittest.TestCase):
    def test_jsonl_and_meta(self):
        blob = _zip({"s.jsonl": '{"kind":"Prompt"}\n',
                     "s.json": json.dumps({"title": "標題", "cwd": "/p"})})
        jsonl, meta = read_snapshot_zip(blob)
        self.assertEqual(jsonl, '{"kind":"Prompt"}\n')
        self.assertEqual(meta["title"], "標題")

    def test_missing_meta_is_normal(self):
        jsonl, meta = read_snapshot_zip(_zip({"s.jsonl": "x\n"}))
        self.assertEqual(jsonl, "x\n")
        self.assertEqual(meta, {})

    def test_broken_meta_tolerated(self):
        jsonl, meta = read_snapshot_zip(_zip({"s.jsonl": "x\n", "s.json": "{壞掉"}))
        self.assertEqual(meta, {})

    def test_non_dict_meta_tolerated(self):
        _, meta = read_snapshot_zip(_zip({"s.jsonl": "x\n", "s.json": "[1,2]"}))
        self.assertEqual(meta, {})

    def test_nested_paths(self):
        jsonl, _ = read_snapshot_zip(_zip({"sub/dir/s.jsonl": "y\n"}))
        self.assertEqual(jsonl, "y\n")

    def test_bad_zip_returns_none(self):
        self.assertIsNone(read_snapshot_zip(b"not a zip"))

    def test_undecodable_bytes_replaced_not_raised(self):
        jsonl, _ = read_snapshot_zip(_zip({"s.jsonl": b"\xff\xfe bad\n"}))
        self.assertIn("bad", jsonl)


if __name__ == "__main__":
    unittest.main()
