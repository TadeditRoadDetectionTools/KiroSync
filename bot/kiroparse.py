"""
Kiro CLI .jsonl 事件解析。

架構 B: client 只上傳 raw session, 由 bot 端解析+渲染。所以這套解析邏輯集中在
bot, 改格式只需動這裡, 不用重佈所有 client (全專案只有這一份解析邏輯)。

Kiro CLI 2.10.0 的 .jsonl: 每行 {"version","kind","data"}:
  kind == "Prompt"           -> 使用者輸入
  kind == "AssistantMessage" -> AI 回覆
  其他 kind                   -> 原封不動 (不當掉)
  文字在 data.content[] 裡 kind=="text" 的 data; timestamp 在 data.meta.timestamp。
"""

from __future__ import annotations

import json
import re
from typing import Optional

ATTACHED_MAX = 4000    # @ 文字檔內容截斷
ATTACHED_RE = re.compile(r'<attached_file path="([^"]*)">(.*?)</attached_file>', re.DOTALL)
TOOL_RESULT_MAX = 1000  # 單個工具結果截斷長度


def _looks_binary(s: str) -> bool:
    if not s:
        return False
    sample = s[:2000]
    bad = sum(1 for ch in sample if ch == "�" or ord(ch) < 9)
    return bad / max(1, len(sample)) > 0.10


def _sub_attached(m: "re.Match") -> str:
    fname, body = m.group(1), m.group(2)
    if _looks_binary(body):  # @ 圖片/二進位檔 -> 只留標記, 不倒亂碼
        return f"\n📎 附加二進位檔 `{fname}`（略過原始位元組）\n"
    b = body.strip()
    if len(b) > ATTACHED_MAX:
        b = b[:ATTACHED_MAX] + f" …(截斷, 共 {len(b)} 字)"
    return f"\n📎 附加檔案 `{fname}`：\n{b}\n"


def _render_text_item(t: str) -> str:
    """把 @ 附加檔案的 <attached_file> 區塊整理成可讀標記 (二進位則跳過)。"""
    return ATTACHED_RE.sub(_sub_attached, t)


def _flatten_toolresult(content) -> str:
    """把 toolResult.data.content ([{kind:text/json,...}]) 攤平成字串。"""
    if not isinstance(content, list):
        return str(content)
    out = []
    for c in content:
        if not isinstance(c, dict):
            continue
        if c.get("kind") == "json":
            try:
                out.append(json.dumps(c.get("data"), ensure_ascii=False))
            except Exception:
                out.append(str(c.get("data")))
        else:
            out.append(str(c.get("data", "")))
    return " ".join(p for p in out if p).strip()


def _render_content(data: dict, include_tools: bool = True) -> Optional[str]:
    """把一個事件的 content[] render 成文字。
    text -> 原文; toolUse -> 🔧 呼叫; toolResult -> ↩️ 結果 (截斷)。"""
    content = data.get("content")
    if not isinstance(content, list):
        return None
    parts = []
    for c in content:
        if not isinstance(c, dict):
            continue
        k = c.get("kind")
        if k == "text":
            t = c.get("data", "")
            if t:
                parts.append(_render_text_item(t))
        elif k == "toolUse" and include_tools:
            d = c.get("data", {}) if isinstance(c.get("data"), dict) else {}
            name = d.get("name", "?")
            try:
                inp = json.dumps(d.get("input"), ensure_ascii=False)
            except Exception:
                inp = str(d.get("input"))
            if len(inp) > 300:
                inp = inp[:300] + "…"
            parts.append(f"🔧 呼叫工具 `{name}` {inp}")
        elif k == "toolResult" and include_tools:
            d = c.get("data", {}) if isinstance(c.get("data"), dict) else {}
            res = _flatten_toolresult(d.get("content"))
            if len(res) > TOOL_RESULT_MAX:
                res = res[:TOOL_RESULT_MAX] + f" …(截斷, 共 {len(res)} 字)"
            parts.append(f"↩️ 工具結果 {res}")
    text = "\n".join(p for p in parts if p).strip()
    return text or None


def _extract_attachments(data: dict, seq: int) -> list:
    """從 content 抽出 image (多模態貼圖) -> [{filename, data:bytes}]。"""
    content = data.get("content")
    if not isinstance(content, list):
        return []
    out = []
    for idx, c in enumerate(content):
        if not isinstance(c, dict) or c.get("kind") != "image":
            continue
        cd = c.get("data", {}) if isinstance(c.get("data"), dict) else {}
        fmt = str(cd.get("format") or "png").lower()
        src = cd.get("source", {}) if isinstance(cd.get("source"), dict) else {}
        if src.get("kind") == "bytes" and isinstance(src.get("data"), list):
            try:
                b = bytes(src["data"])
            except Exception:
                continue
            out.append({"filename": f"paste_{seq}_{idx}.{fmt}", "data": b})
    return out


def _extract_ts(data: dict) -> Optional[int]:
    meta = data.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("timestamp"), (int, float)):
        return int(meta["timestamp"])
    return None


def _load_line(line: str) -> Optional[dict]:
    """把一行讀成 dict; 空行/壞行/非物件回 None。"""
    line = line.strip()
    if not line:
        return None
    try:
        evt = json.loads(line)
    except Exception:
        return None
    return evt if isinstance(evt, dict) else None


def line_timestamp(line: str) -> Optional[int]:
    """只取一行的 timestamp, 不做完整渲染 (掃描用, 不必為了讀時間去解圖片位元組)。
    取不到回 None。單位由呼叫端正規化 — 見 summary.normalize_ts。"""
    evt = _load_line(line)
    if evt is None:
        return None
    data = evt.get("data")
    return _extract_ts(data) if isinstance(data, dict) else None


def line_kinds(line: str) -> tuple:
    """回 (事件 kind, content 各項的 kind 清單); 空行/壞行回 (None, [])。
    給統計用 — 讓 JSON 結構的知識維持只有這個模組知道。"""
    evt = _load_line(line)
    if evt is None:
        return None, []
    data = evt.get("data")
    content = data.get("content") if isinstance(data, dict) else None
    kinds = [c.get("kind") for c in content
             if isinstance(c, dict)] if isinstance(content, list) else []
    return evt.get("kind", "Unknown"), kinds


def parse_line(line: str, seq: int, include_tools: bool = True) -> Optional[dict]:
    """把 .jsonl 的一行解析成 {kind, text, attachments, ts}; 空行回 None。
    壞行不當掉: 回一個 kind=ParseError 的 dict。"""
    line = line.strip()
    if not line:
        return None
    try:
        evt = json.loads(line)
    except Exception:
        return {"kind": "ParseError", "text": line[:4000], "attachments": [], "ts": None}
    if not isinstance(evt, dict):
        return {"kind": "NonObject", "text": line[:4000], "attachments": [], "ts": None}
    kind = evt.get("kind", "Unknown")
    data = evt.get("data", {}) if isinstance(evt.get("data"), dict) else {}
    return {
        "kind": kind,
        "text": _render_content(data, include_tools),
        "attachments": _extract_attachments(data, seq),
        "ts": _extract_ts(data),
    }
