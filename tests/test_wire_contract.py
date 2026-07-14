"""KSV1 wire format 合約測試: client uplink 產生 ↔ bot parse_ingest 解析。

這是全 repo 最重要的測試 — 格式定義橫跨兩個獨立部署 (client/uplink.py 產生,
bot/bot.py parse_ingest 解析), 改一邊沒改另一邊就靠這裡攔下來。
"""

import json
import unittest
from unittest import mock

import _paths  # noqa: F401
import _stub_discord

_stub_discord.install()  # 讓 import bot 不需要真的 discord.py

import uplink
from bot import parse_ingest


class TestParseIngest(unittest.TestCase):
    def test_non_ksv1_ignored(self):
        self.assertIsNone(parse_ingest(""))
        self.assertIsNone(parse_ingest("哈囉大家"))
        self.assertIsNone(parse_ingest("KSV2 {}"))  # 版本不符

    def test_bad_header_json_ignored(self):
        self.assertIsNone(parse_ingest("KSV1 {oops"))

    def test_header_without_newline(self):
        h = parse_ingest('KSV1 {"u":"a","k":"Snap"}')
        self.assertEqual(h["u"], "a")
        self.assertEqual(h["_text"], "")

    def test_multiline_text_preserved(self):
        h = parse_ingest('KSV1 {"u":"a"}\nline1\nline2')
        self.assertEqual(h["_text"], "line1\nline2")


class TestHelloRoundTrip(unittest.TestCase):
    def test_hello(self):
        sent = []
        with mock.patch.object(uplink, "_post", lambda url, content, log: sent.append(content)):
            uplink.post_hello("https://hook.example", "王小明")
        h = parse_ingest(sent[0])
        self.assertIsNotNone(h, "bot 解析不了 client 送的 Hello")
        self.assertEqual(h["k"], "Hello")
        self.assertEqual(h["u"], "王小明")
        self.assertEqual(h["s"], "")
        self.assertEqual(h["_text"], "")
        self.assertTrue(h.get("ts"))


class TestSnapRoundTrip(unittest.TestCase):
    def _roundtrip(self, **kwargs):
        sent = []

        def fake_mp(url, content, filename, part, log):
            sent.append((content, part))

        with mock.patch.object(uplink, "_post_multipart", fake_mp):
            uplink.post_snapshot("https://hook.example", "alice", "sid-42", **kwargs)
        return [(parse_ingest(c), part) for c, part in sent]

    def test_snap_fields_survive(self):
        got = self._roundtrip(zip_bytes=b"0123456789", title="修 bug", cwd="/proj",
                              chunk_bytes=4)
        self.assertEqual(len(got), 3)
        for i, (h, part) in enumerate(got):
            self.assertIsNotNone(h, "bot 解析不了 client 送的 Snap")
            self.assertEqual(h["k"], "Snap")
            self.assertEqual(h["u"], "alice")
            self.assertEqual(h["s"], "sid-42")
            self.assertEqual(h["p"], i)
            self.assertEqual(h["n"], 3)
            self.assertEqual(h["title"], "修 bug")
            self.assertEqual(h["cwd"], "/proj")
            self.assertEqual(h["_text"], "")
        # 三片的 g 一致, 且 bot 端 _on_snap 依 (s, g) 收集 → 不能是 None
        gens = {h["g"] for h, _ in got}
        self.assertEqual(len(gens), 1)
        self.assertTrue(all(g for g in gens))

    def test_tricky_title_survives_json_escaping(self):
        # title 含換行 / 引號 — json.dumps 會轉義, 不能破壞「header 只佔第一行」的約定
        title = '多行\n標題 "引號" \\ 反斜線'
        got = self._roundtrip(zip_bytes=b"x", title=title, cwd=None)
        h, _ = got[0]
        self.assertIsNotNone(h)
        self.assertEqual(h["title"], title)
        self.assertIsNone(h["cwd"])

    def test_gen_header_matches_wire_gen(self):
        # /link 靠 store 的 gen 對應分片; header 的 g 必須等於 post_snapshot 算的
        blob = b"hello"
        got = self._roundtrip(zip_bytes=blob)
        h, part = got[0]
        self.assertEqual(part, blob)
        size, _, crc = h["g"].partition("-")
        self.assertEqual(int(size), len(blob))
        self.assertEqual(len(crc), 8)
        int(crc, 16)  # 是合法 hex


if __name__ == "__main__":
    unittest.main()
