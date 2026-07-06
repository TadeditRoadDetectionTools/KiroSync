"""
監看 ~/.kiro/sessions/cli/ 並把新事件落地。

用 polling (每 interval 掃一次): 標準庫零依賴; 對「檔案被寫一半」天然免疫,
因為只處理「有換行結尾的完整行」, 半行下一輪才讀。

解析 (Kiro CLI 2.10.0 的 .jsonl): 每行 {"version","kind","data"}:
  kind == "Prompt"           -> 使用者輸入
  kind == "AssistantMessage" -> AI 回覆
  其他 kind                   -> 原封不動存 raw, 不當掉
  文字在 data.content[] 裡 kind=="text" 的 data; timestamp 在 data.meta.timestamp。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

from store import Store

ATTACHED_MAX = 4000    # @ 文字檔內容截斷
RAW_DB_MAX = 20000     # 存進 DB 的 raw_json 上限 (圖片那行可達數百 KB)
ATTACHED_RE = re.compile(r'<attached_file path="([^"]*)">(.*?)</attached_file>', re.DOTALL)


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


TOOL_RESULT_MAX = 1000  # 單個工具結果截斷長度 (工具回傳可能很大)


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


class Watcher:
    def __init__(
        self, watch_dir: Path, store: Store, *,
        on_event: Optional[Callable[[dict], None]] = None,
        backfill: bool = False,
        include_tools: bool = True,
    ):
        self.dir = watch_dir
        self.store = store
        self.on_event = on_event
        self.backfill = backfill
        self.include_tools = include_tools
        self._initialized: set[str] = set()
        # 啟動當下就已存在的 session 檔 = 舊 session (不回填時跳過其既有內容);
        # 啟動後才「冒出來」的檔 = 你新開的 session, 從頭完整擷取。
        self._preexisting: set[str] = set()
        try:
            if self.dir.exists():
                self._preexisting = {str(p) for p in self.dir.glob("*.jsonl")}
        except Exception:
            pass

    def _read_metadata(self, session_id: str) -> None:
        meta_path = self.dir / f"{session_id}.json"
        if not meta_path.exists():
            return
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(meta, dict):
            return

        def dig(d, *keys):
            for k in keys:
                d = d.get(k) if isinstance(d, dict) else None
            return d

        self.store.upsert_session(
            session_id,
            cwd=meta.get("cwd"),
            title=meta.get("title"),
            model=dig(meta, "session_state", "rts_model_state", "model_info", "model_name"),
            created_at=meta.get("created_at"),
            updated_at=meta.get("updated_at"),
        )

    def _process_line(self, session_id: str, seq: int, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            evt = json.loads(line)
        except Exception:
            self.store.add_event(session_id, seq, "ParseError", None, None, line[:4000])
            return
        if not isinstance(evt, dict):
            self.store.add_event(session_id, seq, "NonObject", None, None, line[:4000])
            return

        kind = evt.get("kind", "Unknown")
        data = evt.get("data", {}) if isinstance(evt.get("data"), dict) else {}
        text = _render_content(data, self.include_tools)
        attachments = _extract_attachments(data, seq)
        ts = _extract_ts(data)

        raw_db = line if len(line) <= RAW_DB_MAX else line[:RAW_DB_MAX] + "…(truncated)"
        is_new = self.store.add_event(session_id, seq, kind, ts, text, raw_db)
        if is_new and self.on_event is not None:
            self.on_event({
                "session_id": session_id, "seq": seq, "kind": kind,
                "ts": ts, "text": text, "attachments": attachments,
            })

    def _process_file(self, path: Path) -> None:
        session_id = path.stem
        key = str(path)

        if key not in self._initialized:
            self._initialized.add(key)
            if not self.store.known_session(session_id):
                self.store.upsert_session(session_id)
            self._read_metadata(session_id)
            # 只有「啟動前就存在的舊 session」才跳過既有內容; 啟動後新開的 session 從頭抓。
            skip_old = (
                not self.backfill
                and key in self._preexisting
                and self.store.get_offset(key) == 0
            )
            if skip_old:
                try:
                    size = path.stat().st_size
                except OSError:
                    return
                if size > 0:
                    self.store.set_offset(key, size)
                    return

        offset = self.store.get_offset(key)
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size <= offset:
            return

        with path.open("rb") as f:
            f.seek(offset)
            chunk = f.read()

        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            return  # 還沒有完整的一行
        complete = chunk[: last_nl + 1]
        new_offset = offset + len(complete)

        base_seq = self._current_seq(session_id)
        text = complete.decode("utf-8", errors="replace")
        for i, ln in enumerate(text.splitlines()):
            self._process_line(session_id, base_seq + i, ln)

        self.store.set_offset(key, new_offset)
        self._read_metadata(session_id)

    def _current_seq(self, session_id: str) -> int:
        row = self.store.db.execute(
            "SELECT COALESCE(MAX(seq), -1) AS m FROM events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["m"]) + 1

    def scan_once(self) -> None:
        if not self.dir.exists():
            return
        for path in sorted(self.dir.glob("*.jsonl")):
            try:
                self._process_file(path)
            except Exception as e:
                print(f"[watcher] error on {path.name}: {e}")
