"""共用小工具。"""

from __future__ import annotations

from typing import List


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
