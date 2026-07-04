"""
KiroSync Client 進入點 (擷取 + 上行)。

用法:
    cd client
    cp .env.example .env      # 填好 WEBHOOK_URL / USER_NAME 後
    python run.py sync        # 監看並上行到 Discord (預設只傳啟動後的新對話)
    python run.py sync --backfill   # 連既有內容也傳
    python run.py tail        # 只在本機即時印出+存 (不碰 Discord)
    python run.py once        # 掃一次
    python run.py sessions    # 列出已存 session
    python run.py events <sid>
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from envcfg import get, load_env
from store import Store
from uplink import post_event, post_hello
from watcher import Watcher

HERE = Path(__file__).resolve().parent
ENV = load_env()


def _e(key, default=None):
    return get(ENV, key, default)


def watch_dir() -> Path:
    wd = _e("WATCH_DIR") or ""
    return Path(wd) if wd else Path.home() / ".kiro" / "sessions" / "cli"


def db_path() -> Path:
    return HERE / (_e("DB_PATH") or "ks_client.db")


def interval() -> float:
    try:
        return float(_e("POLL_INTERVAL") or 0.7)
    except ValueError:
        return 0.7


def workspaces() -> list[str]:
    raw = _e("SYNC_WORKSPACES") or ""
    return [w.strip() for w in raw.split(",") if w.strip()]


def user_name() -> str:
    return _e("USER_NAME") or os.environ.get("USERNAME") or "kiro-user"


def _fmt(evt: dict) -> str:
    label = {"Prompt": "user", "AssistantMessage": "response"}.get(evt["kind"], evt["kind"])
    text = (evt["text"] or "").replace("\n", " ")
    if len(text) > 200:
        text = text[:200] + "…"
    return f"[{evt['session_id'][:8]}] {label}: {text}"


def _meta(store: Store, session_id: str) -> dict:
    row = store.db.execute(
        "SELECT title, cwd FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return {"title": row["title"] if row else None, "cwd": row["cwd"] if row else None}


def _loop(watcher: Watcher, store: Store) -> None:
    try:
        while True:
            watcher.scan_once()
            time.sleep(interval())
    except KeyboardInterrupt:
        print("\n[ks] 結束。")
    finally:
        store.close()


def cmd_sync(args) -> None:
    webhook = _e("WEBHOOK_URL")
    if not webhook:
        sys.exit(".env 缺 WEBHOOK_URL")
    user = user_name()
    wl = workspaces()
    store = Store(db_path())

    def handle(evt: dict) -> None:
        if not (evt.get("text") or "").strip():  # 跳過無文字的工具回合, thread 只留真正對話
            return
        meta = _meta(store, evt["session_id"])
        if wl and (meta.get("cwd") or "") not in wl:  # 空清單=全部同步
            return
        post_event(webhook, user, evt, meta)
        print(_fmt(evt), flush=True)

    # 一連上就讓 bot 建好 forum + 資訊 thread (不用等第一則對話)
    post_hello(webhook, user)
    watcher = Watcher(watch_dir(), store, on_event=handle, backfill=args.backfill)
    print(f"[ks] sync 中: {watch_dir()} -> Discord webhook  使用者={user}")
    _loop(watcher, store)


def cmd_import(args) -> None:
    """朔及既往: 把指定的舊 session 從頭重讀並上傳 (清掉該 session 的既有記錄再重送)。"""
    webhook = _e("WEBHOOK_URL")
    if not webhook:
        sys.exit(".env 缺 WEBHOOK_URL")
    user = user_name()
    wd = watch_dir()
    matches = sorted(wd.glob(f"{args.session_id}*.jsonl"))
    if not matches:
        sys.exit(f"在 {wd} 找不到符合 '{args.session_id}' 的 session 檔")
    if len(matches) > 1 and not args.all:
        print("符合多個 session, 請給更完整的 id, 或加 --all 全部匯入:")
        for m in matches:
            print("   ", m.stem)
        return

    store = Store(db_path())

    def handle(evt: dict) -> None:
        if not (evt.get("text") or "").strip():
            return
        meta = _meta(store, evt["session_id"])
        post_event(webhook, user, evt, meta)
        print(_fmt(evt), flush=True)

    w = Watcher(wd, store, on_event=handle, backfill=True)
    for path in matches:
        sid = path.stem
        # 清掉這個 session 的 offset + events, 讓它重讀重送 (繞過去重)
        store.db.execute("DELETE FROM events WHERE session_id = ?", (sid,))
        store.db.execute("DELETE FROM file_state WHERE path = ?", (str(path),))
        store.db.commit()
        print(f"[ks] import {sid} …")
        w._process_file(path)
    store.close()
    print("[ks] import 完成。")


def cmd_tail(args) -> None:
    store = Store(db_path())
    watcher = Watcher(watch_dir(), store,
                      on_event=lambda e: print(_fmt(e), flush=True),
                      backfill=args.backfill)
    print(f"[ks] 監看 {watch_dir()}  (Ctrl-C 結束)  backfill={args.backfill}")
    _loop(watcher, store)


def cmd_once(args) -> None:
    store = Store(db_path())
    watcher = Watcher(watch_dir(), store,
                      on_event=lambda e: print(_fmt(e), flush=True),
                      backfill=args.backfill)
    watcher.scan_once()
    store.close()


def cmd_sessions(args) -> None:
    store = Store(db_path())
    rows = store.db.execute(
        "SELECT session_id, title, cwd, model, "
        "(SELECT COUNT(*) FROM events e WHERE e.session_id = s.session_id) AS n "
        "FROM sessions s ORDER BY updated_at DESC"
    ).fetchall()
    for r in rows:
        print(f"{r['session_id'][:8]}  {r['n']:>3} 則  {r['model'] or '?':<8}  "
              f"{r['title'] or '(無標題)'}  [{r['cwd'] or ''}]")
    store.close()


def cmd_events(args) -> None:
    store = Store(db_path())
    rows = store.db.execute(
        "SELECT seq, kind, text FROM events WHERE session_id LIKE ? ORDER BY seq ASC",
        (args.session_id + "%",),
    ).fetchall()
    for r in rows:
        print(_fmt({"session_id": args.session_id, "kind": r["kind"], "text": r["text"]}))
    store.close()


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="ks-client", description="KiroSync client — 擷取+上行")
    sub = p.add_subparsers(dest="cmd", required=True)

    psy = sub.add_parser("sync", help="監看並上行到 Discord")
    psy.add_argument("--backfill", action="store_true")
    psy.set_defaults(func=cmd_sync)

    pt = sub.add_parser("tail", help="只在本機即時印出+存")
    pt.add_argument("--backfill", action="store_true")
    pt.set_defaults(func=cmd_tail)

    po = sub.add_parser("once", help="掃一次")
    po.add_argument("--backfill", action="store_true")
    po.set_defaults(func=cmd_once)

    pi = sub.add_parser("import", help="朔及既往: 把指定舊 session 重讀並上傳")
    pi.add_argument("session_id", help="session id (可只給前幾碼)")
    pi.add_argument("--all", action="store_true", help="前綴符合多個時全部匯入")
    pi.set_defaults(func=cmd_import)

    ps = sub.add_parser("sessions", help="列出 session")
    ps.set_defaults(func=cmd_sessions)

    pe = sub.add_parser("events", help="印某 session 事件")
    pe.add_argument("session_id")
    pe.set_defaults(func=cmd_events)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
