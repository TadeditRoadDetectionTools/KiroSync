"""極簡 .env 讀取 (零依賴)。環境變數優先於 .env 檔。"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | None = None) -> dict:
    p = Path(path) if path else Path(__file__).resolve().parent / ".env"
    data: dict[str, str] = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def get(data: dict, key: str, default=None):
    return os.environ.get(key, data.get(key, default))
