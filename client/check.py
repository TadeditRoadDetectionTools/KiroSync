"""
KiroSync Client 設定自檢。

用法:
    cd client
    python check.py            # 檢查 .env 的 WEBHOOK_URL / USER_NAME 等有沒有填好
    python check.py --no-net   # 不連 Discord, 只做本機格式檢查
    (也可 python run.py check)

檢查項目:
  1. WEBHOOK_URL  有沒有填、格式對不對、(預設) 實際 GET 一次確認可用並顯示連到哪個頻道
  2. USER_NAME    有沒有填 (空的話 bot 會用系統帳號分流, 顯示實際會用的值)
  3. Kiro session 目錄  存不存在、有沒有 session 可同步

回傳碼: 有任何 FAIL -> 1, 否則 0 (方便接在啟動腳本前面把關)。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import routes as routes_mod
from envcfg import get, load_env

HERE = Path(__file__).resolve().parent
_UA = "DiscordBot (KiroSync, 1.0)"
# Discord webhook URL: .../api[/vN]/webhooks/<19+ 位 id>/<token>
WEBHOOK_RE = re.compile(
    r"^https://(?:discord|discordapp)\.com/api(?:/v\d+)?/webhooks/\d{17,20}/[\w-]+$"
)

# 統計各級結果, 決定 exit code
_counts = {"ok": 0, "warn": 0, "fail": 0}


def _mark(level: str, title: str, detail: str = "") -> None:
    icon = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}[level]
    _counts[level] += 1
    line = f"{icon} {title}"
    print(line if not detail else f"{line}\n     {detail}")


def _redact(url: str) -> str:
    """遮掉 webhook token, 只留 .../webhooks/<id>/****** 供顯示。"""
    return re.sub(r"(/webhooks/\d+/)[\w-]+", r"\1******", url)


def check_webhook(url: str | None, use_net: bool) -> None:
    if not url:
        _mark("fail", "WEBHOOK_URL 未填",
              "到 Discord 的 kiro-command 頻道看釘選訊息, 或打 /webhook 取得, 填進 .env")
        return
    if not WEBHOOK_RE.match(url):
        _mark("fail", "WEBHOOK_URL 格式不對",
              f"應長得像 https://discord.com/api/webhooks/<id>/<token>, 目前: {_redact(url)}")
        return
    if not use_net:
        _mark("ok", "WEBHOOK_URL 格式正確 (未連線驗證)", _redact(url))
        return

    # 實際 GET 一次: 唯讀, 回傳這個 webhook 自己的資訊
    req = urllib.request.Request(url, headers={"User-Agent": _UA}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            info = json.loads(r.read().decode("utf-8"))
        name = info.get("name") or "(無名稱)"
        chan = info.get("channel_id") or "?"
        _mark("ok", "WEBHOOK_URL 有效",
              f"webhook「{name}」→ 頻道 {chan}  (id {info.get('id')})")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            _mark("fail", f"WEBHOOK_URL 已失效 (HTTP {e.code})",
                  "webhook 可能被刪或 token 錯了, 重新從 /webhook 取得")
        else:
            _mark("warn", f"WEBHOOK_URL 驗證回應異常 (HTTP {e.code})",
                  "格式沒問題, 但 Discord 回了非預期狀態, 可稍後再試")
    except urllib.error.URLError as e:
        _mark("warn", "無法連線 Discord 驗證 webhook",
              f"格式正確, 但連不上 ({e.reason}); 離線環境可加 --no-net 略過")
    except Exception as e:
        _mark("warn", "webhook 驗證時發生非預期錯誤", str(e))


def check_user_name(raw: str | None) -> None:
    if raw and raw.strip():
        _mark("ok", "USER_NAME 已填", raw.strip())
        return
    _mark("fail", "USER_NAME 未填",
          "必填: bot 用它把你的對話分流到專屬 forum。請在 .env 設定 "
          "(多台機器要填一樣的, 否則會拆成多個 forum)")


def check_kiro_dir(watch_dir: str | None) -> None:
    wd = Path(watch_dir) if watch_dir else Path.home() / ".kiro" / "sessions" / "cli"
    if not wd.exists():
        _mark("warn", "找不到 Kiro session 目錄",
              f"{wd}  (Kiro CLI 用過嗎? 或用 WATCH_DIR 指定)")
        return
    n = sum(1 for _ in wd.glob("*.jsonl"))
    if n == 0:
        _mark("warn", "Kiro session 目錄是空的", f"{wd}  (還沒有任何對話, 開一場 Kiro 就會出現)")
    else:
        _mark("ok", f"Kiro session 目錄正常 ({n} 個 session)", str(wd))


def check_routes() -> None:
    rs = routes_mod.load_routes()
    if not rs:
        return  # 沒設路由 = 全進個人 forum, 不算問題, 不用提
    lines = "; ".join(
        f"{r.get('folder')}→{r.get('category_id')}" + (f"（{r.get('label')}）" if r.get("label") else "")
        for r in rs)
    _mark("ok", f"資料夾路由 {len(rs)} 條", lines)


def run(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="ks-check", description="KiroSync client 設定自檢")
    p.add_argument("--no-net", action="store_true",
                   help="不連 Discord, 只做本機格式檢查")
    args = p.parse_args(argv)

    env = load_env()
    env_path = HERE / ".env"
    print(f"[ks] 讀取設定: {env_path}{'' if env_path.exists() else '  (檔案不存在, 只看環境變數)'}\n")

    check_webhook(get(env, "WEBHOOK_URL"), use_net=not args.no_net)
    check_user_name(get(env, "USER_NAME"))
    check_kiro_dir(get(env, "WATCH_DIR"))
    check_routes()

    print()
    c = _counts
    summary = f"通過 {c['ok']} · 提醒 {c['warn']} · 失敗 {c['fail']}"
    if c["fail"]:
        print(f"[ks] {summary} — 有必填項未就緒, 修正後再跑 `python run.py sync`。")
        return 1
    if c["warn"]:
        print(f"[ks] {summary} — 可以跑 sync, 但上面的提醒建議看一下。")
        return 0
    print(f"[ks] {summary} — 一切就緒, 可以 `python run.py sync`。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(run())
