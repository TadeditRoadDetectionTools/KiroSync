"""bot/kiroparse.py 的測試 — 全專案唯一一份 .jsonl 解析邏輯。"""

import json
import unittest

import _paths  # noqa: F401

import kiroparse
from kiroparse import parse_line


def _line(kind: str, content: list, meta: dict | None = None) -> str:
    data = {"content": content}
    if meta is not None:
        data["meta"] = meta
    return json.dumps({"version": 1, "kind": kind, "data": data}, ensure_ascii=False)


class TestParseLine(unittest.TestCase):
    def test_blank_line_is_none(self):
        self.assertIsNone(parse_line("", 0))
        self.assertIsNone(parse_line("   \t ", 0))

    def test_bad_json_becomes_parse_error_not_crash(self):
        ev = parse_line("{not json", 0)
        self.assertEqual(ev["kind"], "ParseError")
        self.assertEqual(ev["text"], "{not json")

    def test_non_object_json(self):
        ev = parse_line("[1, 2, 3]", 0)
        self.assertEqual(ev["kind"], "NonObject")

    def test_prompt_text_and_timestamp(self):
        ev = parse_line(
            _line("Prompt", [{"kind": "text", "data": "哈囉"}],
                  meta={"timestamp": 1700000000}),
            0,
        )
        self.assertEqual(ev["kind"], "Prompt")
        self.assertEqual(ev["text"], "哈囉")
        self.assertEqual(ev["ts"], 1700000000)
        self.assertEqual(ev["attachments"], [])

    def test_unknown_kind_tolerated(self):
        ev = parse_line(json.dumps({"kind": "SomethingNew", "data": {"x": 1}}), 0)
        self.assertEqual(ev["kind"], "SomethingNew")
        self.assertIsNone(ev["text"])  # content 不是 list → 沒文字, 但不當掉

    def test_missing_data_tolerated(self):
        ev = parse_line(json.dumps({"kind": "Prompt"}), 0)
        self.assertEqual(ev["kind"], "Prompt")
        self.assertIsNone(ev["text"])

    def test_tool_use_rendering_and_truncation(self):
        long_input = {"arg": "y" * 500}
        ev = parse_line(
            _line("AssistantMessage", [
                {"kind": "text", "data": "先說明"},
                {"kind": "toolUse", "data": {"name": "fs_read", "input": long_input}},
            ]),
            0,
        )
        self.assertIn("先說明", ev["text"])
        self.assertIn("🔧 呼叫工具 `fs_read`", ev["text"])
        self.assertIn("…", ev["text"])  # input JSON > 300 字要截斷

    def test_include_tools_false_hides_tool_items(self):
        ev = parse_line(
            _line("AssistantMessage", [
                {"kind": "toolUse", "data": {"name": "t", "input": {}}},
            ]),
            0,
            include_tools=False,
        )
        self.assertIsNone(ev["text"])

    def test_tool_result_flatten_and_truncation(self):
        ev = parse_line(
            _line("ToolResults", [{
                "kind": "toolResult",
                "data": {"content": [
                    {"kind": "text", "data": "ok"},
                    {"kind": "json", "data": {"a": 1}},
                ]},
            }]),
            0,
        )
        self.assertIn("↩️ 工具結果", ev["text"])
        self.assertIn("ok", ev["text"])
        self.assertIn('{"a": 1}', ev["text"])

        big = parse_line(
            _line("ToolResults", [{
                "kind": "toolResult",
                "data": {"content": [{"kind": "text", "data": "z" * 3000}]},
            }]),
            0,
        )
        self.assertIn("…(截斷", big["text"])
        # 截斷後長度應在上限附近, 不會把 3000 字全倒出來
        self.assertLess(len(big["text"]), kiroparse.TOOL_RESULT_MAX + 100)


class TestAttachedFile(unittest.TestCase):
    def test_text_attached_file_rendered(self):
        t = '看這個 <attached_file path="a.txt">hello world</attached_file> 結束'
        ev = parse_line(_line("Prompt", [{"kind": "text", "data": t}]), 0)
        self.assertIn("📎 附加檔案 `a.txt`", ev["text"])
        self.assertIn("hello world", ev["text"])
        self.assertNotIn("<attached_file", ev["text"])

    def test_long_attached_file_truncated(self):
        t = f'<attached_file path="big.log">{"x" * 6000}</attached_file>'
        ev = parse_line(_line("Prompt", [{"kind": "text", "data": t}]), 0)
        self.assertIn("…(截斷, 共 6000 字)", ev["text"])

    def test_binary_attached_file_skipped(self):
        t = f'<attached_file path="img.png">{chr(0) * 100}</attached_file>'
        ev = parse_line(_line("Prompt", [{"kind": "text", "data": t}]), 0)
        self.assertIn("附加二進位檔 `img.png`", ev["text"])
        self.assertNotIn(chr(0), ev["text"])


class TestImageAttachments(unittest.TestCase):
    def test_bytes_image_extracted(self):
        png = [137, 80, 78, 71]
        ev = parse_line(
            _line("Prompt", [
                {"kind": "text", "data": "圖來了"},
                {"kind": "image", "data": {
                    "format": "PNG",
                    "source": {"kind": "bytes", "data": png},
                }},
            ]),
            seq=7,
        )
        self.assertEqual(len(ev["attachments"]), 1)
        att = ev["attachments"][0]
        self.assertEqual(att["filename"], "paste_7_1.png")  # seq_idx, 副檔名小寫
        self.assertEqual(att["data"], bytes(png))

    def test_non_bytes_source_ignored(self):
        ev = parse_line(
            _line("Prompt", [{"kind": "image", "data": {
                "format": "png", "source": {"kind": "url", "data": "http://x"},
            }}]),
            0,
        )
        self.assertEqual(ev["attachments"], [])


if __name__ == "__main__":
    unittest.main()
