"""極簡 .env 讀寫 (零依賴)。環境變數優先於 .env 檔。"""

from __future__ import annotations

import os
import re
from pathlib import Path

# 使用者可能給值加引號; 中文 Windows 的輸入法還會打出全形/彎引號 (跟 ASCII 是不同字元)。
# 這些全部當外圍引號剝掉, 免得 URL 黏著怪引號而失效。
_QUOTES = "\"'“”‘’„‚＂＇「」『』"


def _dequote(v: str) -> str:
    """去掉值外圍的引號 (含全形/彎引號) 與空白; 內側殘留空白也一併清掉。"""
    return v.strip().strip(_QUOTES).strip()


def env_path(path: str | None = None) -> Path:
    """預設的 .env 位置 (跟這支 envcfg.py 同資料夾)。"""
    return Path(path) if path else Path(__file__).resolve().parent / ".env"


def _read_text(p: Path) -> str:
    """容錯解碼 .env。

    .env 可能被存成 UTF-8(含 BOM)或中文 Windows 記事本的 Big5/ANSI。
    寫死 utf-8 會在含中文路徑時 UnicodeDecodeError, 而且 load_env 在 import
    階段就跑, 一崩就「啟動前秒退、毫無輸出」。utf-8-sig 先吃(順帶去 BOM),
    再退到系統中文編碼, latin-1 保底 (任何位元組都能解, 絕不讓它掛掉)。
    """
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "cp950", "mbcs", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def load_env(path: str | None = None) -> dict:
    p = env_path(path)
    data: dict[str, str] = {}
    if not p.exists():
        return data
    for line in _read_text(p).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = _dequote(v)
    return data


def get(data: dict, key: str, default=None):
    return os.environ.get(key, data.get(key, default))


def set_env_value(key: str, value: str, path: str | None = None) -> Path:
    """把 KEY=value 寫回 .env (就地更新既有那行, 沒有就補在最後), 回寫入的檔案路徑。

    保留註解與其他設定; 一律以 UTF-8 寫出 (跟 _read_text 的容錯讀取搭配, 讓被存成
    Big5 的舊檔在第一次寫入後自動正規化成 UTF-8)。
    """
    p = env_path(path)
    lines = _read_text(p).splitlines() if p.exists() else []
    pat = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    for i, ln in enumerate(lines):
        if pat.match(ln) and not ln.lstrip().startswith("#"):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p
