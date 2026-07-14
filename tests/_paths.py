"""測試共用的 sys.path 設定。

bot/ 與 client/ 都用裸名 import (`from uplink import ...`), 所以把兩個資料夾都放進
sys.path。順序刻意讓 client/ 排在 bot/ 前面:
  - `run` / `envcfg` 兩邊同名 → 解析到 client 的 (envcfg 兩份內容相同)
  - `kiroparse` / `store` / `util` / `bot` 只存在 bot/ → 不受順序影響
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for sub in ("bot", "client"):  # 後插的在前 → client 優先
    p = str(ROOT / sub)
    if p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)
