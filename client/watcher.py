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
from pathlib import Path
from typing import Callable, Optional

from store import Store


def _extract_text(data: dict) -> Optional[str]:
    content = data.get("content")
    if not isinstance(content, list):
        return None
    parts = [c.get("data", "") for c in content if isinstance(c, dict) and c.get("kind") == "text"]
    text = "".join(parts).strip()
    return text or None


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
    ):
        self.dir = watch_dir
        self.store = store
        self.on_event = on_event
        self.backfill = backfill
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
        text = _extract_text(data)
        ts = _extract_ts(data)

        is_new = self.store.add_event(session_id, seq, kind, ts, text, line)
        if is_new and self.on_event is not None:
            self.on_event({
                "session_id": session_id, "seq": seq, "kind": kind,
                "ts": ts, "text": text,
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
