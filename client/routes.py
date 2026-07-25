"""client 端「資料夾 → Discord 分類 ID」路由對應 (零依賴, 純標準庫)。

只有 client 知道本機資料夾路徑, 所以這份對應存在本機 routes.json, 由 `route` 指令維護。
這是**設定**(像 .env), 不是對話/進度狀態 —— 架構 B 的「client 無狀態」指的是不存
對話與同步進度 (那些 bot 端才有), 這份純設定不違反該原則。

分類一律用 **ID** 指名 (跟 .env 的 GUILD_ID/CATEGORY_ID 一致): 名稱會重複、改名即失效,
而且 client 沒有讀 Discord 的憑證、無法把名稱解析成 ID。label 只是給人看的顯示字串。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent


def routes_path() -> Path:
    return HERE / "routes.json"


def norm_path(p: str) -> str:
    """正規化路徑供比對: 統一分隔線、去結尾斜線、Windows 不分大小寫。"""
    if not p:
        return ""
    return os.path.normcase(os.path.normpath(p.strip()))


def load_routes(path=None) -> list:
    p = Path(path) if path else routes_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def save_routes(routes: list, path=None) -> None:
    p = Path(path) if path else routes_path()
    p.write_text(json.dumps(routes, ensure_ascii=False, indent=2), encoding="utf-8")


def add_route(folder: str, category_id: str, label: str = "", path=None) -> list:
    """新增一條路由; 同一資料夾 (正規化後相同) 視為覆蓋。"""
    routes = load_routes(path)
    nf = norm_path(folder)
    routes = [r for r in routes if norm_path(r.get("folder", "")) != nf]
    routes.append({"folder": folder, "category_id": str(category_id), "label": label or ""})
    save_routes(routes, path)
    return routes


def remove_route(folder: str, path=None) -> bool:
    """移除符合資料夾的路由; 回傳是否真的移除了。"""
    routes = load_routes(path)
    nf = norm_path(folder)
    kept = [r for r in routes if norm_path(r.get("folder", "")) != nf]
    save_routes(kept, path)
    return len(kept) != len(routes)


def category_for(cwd: str, routes: list) -> Optional[str]:
    """cwd 落在哪條路由資料夾內 (含子資料夾) -> 該路由的 category_id。
    最長前綴優先 (巢狀時內層贏); 無命中回 None (bot 端 fallback 個人 forum)。"""
    c = norm_path(cwd)
    if not c:
        return None
    best_id: Optional[str] = None
    best_len = -1
    for r in routes:
        f = norm_path(r.get("folder", ""))
        cid = str(r.get("category_id") or "").strip()
        if not f or not cid:
            continue
        if (c == f or c.startswith(f + os.sep)) and len(f) > best_len:
            best_len = len(f)
            best_id = cid
    return best_id
