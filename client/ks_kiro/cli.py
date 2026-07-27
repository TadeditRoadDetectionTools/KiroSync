"""`ks` / `ks-kiro` 兩個安裝後指令的進入點。

client/ 的模組彼此用裸名 import (`from uplink import ...`), 所以這裡先把 client/
放進 sys.path 再載入 run。用 editable 安裝 (pip install -e ./client) 時, 這個檔案
就在 client/ks_kiro/ 底下, 往上一層即 client/ —— .env、routes.json 都仍在那裡。
"""

from __future__ import annotations

import sys
from pathlib import Path

CLIENT_DIR = Path(__file__).resolve().parent.parent


def _load_run():
    if str(CLIENT_DIR) not in sys.path:
        sys.path.insert(0, str(CLIENT_DIR))
    try:
        import run  # noqa: PLC0415  (要先設好 sys.path 才能 import)
    except ImportError as e:  # 只可能發生在 client/ 被移走/刪掉時
        raise SystemExit(
            f"找不到 KiroSync client 程式碼 ({CLIENT_DIR})。\n"
            f"若你移動過專案資料夾, 重新安裝即可: pip install -e <新路徑>/client\n"
            f"原始錯誤: {e}"
        )
    return run


def _utf8_stdout() -> None:
    try:  # Windows 主控台預設 cp950, 中文輸出會炸
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main(argv=None) -> None:
    """`ks <子指令>` — 完整 CLI。"""
    _utf8_stdout()
    _load_run().main(argv)


def kiro_main(argv=None) -> None:
    """`ks-kiro` — 等同 `ks kiro`, 額外參數原樣往後帶。"""
    _utf8_stdout()
    args = list(sys.argv[1:] if argv is None else argv)
    _load_run().main(["kiro"] + args)
