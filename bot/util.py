"""共用小工具。"""

from __future__ import annotations

import io
import json
import zipfile
from typing import List, Optional, Tuple


def read_snapshot_zip(blob: bytes) -> Optional[Tuple[str, dict]]:
    """把快照 zip 讀成 (jsonl 文字, meta dict); 不是有效 zip 回 None。

    `.json` 缺席或壞掉是正常情況 (session 剛開、或寫到一半) -> meta 給 {}。
    _apply_snapshot 與 summary 共用這一份, 避免兩處各自解 zip。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return None
    jsonl_text, meta = "", {}
    for name in zf.namelist():
        base = name.rsplit("/", 1)[-1]
        if base.endswith(".jsonl"):
            jsonl_text = zf.read(name).decode("utf-8", "replace")
        elif base.endswith(".json"):
            try:
                loaded = json.loads(zf.read(name).decode("utf-8", "replace"))
            except Exception:
                loaded = {}
            meta = loaded if isinstance(loaded, dict) else {}
    return jsonl_text, meta


def chunk(text: str, size: int) -> List[str]:
    """把長文字切成 <= size 的段, 盡量在換行處切。空字串回傳 []。"""
    text = text or ""
    out: List[str] = []
    while text:
        if len(text) <= size:
            out.append(text)
            break
        cut = text.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out
