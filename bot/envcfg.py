"""極簡 .env 讀取 (零依賴)。環境變數優先於 .env 檔。"""

from __future__ import annotations

import os
from pathlib import Path

# 使用者可能給值加引號; 中文 Windows 的輸入法還會打出全形/彎引號 (跟 ASCII 是不同字元)。
# 這些全部當外圍引號剝掉, 免得 URL 黏著怪引號而失效。
_QUOTES = "\"'“”‘’„‚＂＇「」『』"


def _dequote(v: str) -> str:
    """去掉值外圍的引號 (含全形/彎引號) 與空白; 內側殘留空白也一併清掉。"""
    return v.strip().strip(_QUOTES).strip()


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
        data[k.strip()] = _dequote(v)
    return data


def get(data: dict, key: str, default=None):
    return os.environ.get(key, data.get(key, default))
