"""client/uplink.py 的測試: 快照切片、世代 (gen) 標識。不真的打網路。"""

import json
import unittest
import zlib
from unittest import mock

import _paths  # noqa: F401

import uplink


def _capture_snapshot(**kwargs):
    """跑 post_snapshot 但攔下 _post_multipart, 回傳 (nparts, [(header, filename, part)])。"""
    sent = []

    def fake_mp(url, content, filename, part, log):
        self_header = json.loads(content[len("KSV1 "):])
        sent.append((self_header, filename, part))

    with mock.patch.object(uplink, "_post_multipart", fake_mp):
        n = uplink.post_snapshot("https://hook.example", "alice", "sid-1", **kwargs)
    return n, sent


class TestPostSnapshot(unittest.TestCase):
    def test_chunking_and_headers(self):
        blob = b"0123456789"
        n, sent = _capture_snapshot(zip_bytes=blob, title="標題", cwd="/w", chunk_bytes=4)
        self.assertEqual(n, 3)
        self.assertEqual([part for _, _, part in sent], [b"0123", b"4567", b"89"])
        gens = {h["g"] for h, _, _ in sent}
        self.assertEqual(len(gens), 1)  # 同一世代
        for i, (h, filename, _) in enumerate(sent):
            self.assertEqual(h["k"], "Snap")
            self.assertEqual(h["u"], "alice")
            self.assertEqual(h["s"], "sid-1")
            self.assertEqual(h["p"], i)
            self.assertEqual(h["n"], 3)
            self.assertEqual(h["title"], "標題")
            self.assertEqual(h["cwd"], "/w")
            self.assertIn(f".p{i}.zip", filename)

    def test_gen_is_len_dash_crc32(self):
        blob = b"hello world"
        n, sent = _capture_snapshot(zip_bytes=blob)
        expected = f"{len(blob)}-{zlib.crc32(blob) & 0xffffffff:08x}"
        self.assertEqual(sent[0][0]["g"], expected)

    def test_same_size_different_content_different_gen(self):
        _, a = _capture_snapshot(zip_bytes=b"aaaa")
        _, b = _capture_snapshot(zip_bytes=b"bbbb")
        self.assertNotEqual(a[0][0]["g"], b[0][0]["g"])

    def test_empty_blob_still_sends_one_part(self):
        n, sent = _capture_snapshot(zip_bytes=b"")
        self.assertEqual(n, 1)
        self.assertEqual(sent[0][2], b"")
        self.assertEqual(sent[0][0]["n"], 1)

    def test_invalid_chunk_bytes_falls_back(self):
        n, sent = _capture_snapshot(zip_bytes=b"abc", chunk_bytes=0)
        self.assertEqual(n, 1)  # 0 → 用預設上限, 小 blob 一片


class TestPostHello(unittest.TestCase):
    def test_hello_wire_shape(self):
        sent = []
        with mock.patch.object(uplink, "_post", lambda url, content, log: sent.append(content)):
            uplink.post_hello("https://hook.example", "alice")
        self.assertEqual(len(sent), 1)
        content = sent[0]
        self.assertTrue(content.startswith("KSV1 "))
        self.assertTrue(content.endswith("\n"))
        h = json.loads(content[len("KSV1 "):-1])
        self.assertEqual(h["k"], "Hello")
        self.assertEqual(h["u"], "alice")
        self.assertEqual(h["s"], "")
        self.assertIn("ts", h)


if __name__ == "__main__":
    unittest.main()
