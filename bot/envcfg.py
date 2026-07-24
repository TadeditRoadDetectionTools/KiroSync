"""極簡 .env 讀取 (零依賴)。環境變數優先於 .env 檔。"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | None = None) -> dict:
    p = Path(path) if path else Path(__file__).resolve().parent / ".env"
    data: dict[str, str] = {}
    if not p.exists():
        return data
    # .env 可能被存成 UTF-8(含 BOM)或中文 Windows 記事本的 Big5/ANSI。
    # 寫死 utf-8 會在含中文路徑時 UnicodeDecodeError, 而且 load_env 在 import
    # 階段就跑, 一崩就「啟動前秒退、毫無輸出」。utf-8-sig 先吃(順帶去 BOM),
    # 再退到系統中文編碼, latin-1 保底 (任何位元組都能解, 絕不讓它掛掉)。
    raw = p.read_bytes()
    text = None
    for enc in ("utf-8-sig", "cp950", "mbcs", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def get(data: dict, key: str, default=None):
    return os.environ.get(key, data.get(key, default))
