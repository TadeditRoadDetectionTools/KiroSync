"""
KiroSync Client 進入點 (擷取 + 上行)。

用法:
    cd client
    cp .env.example .env      # 填好 WEBHOOK_URL / USER_NAME 後
    python run.py sync        # 監看並上行到 Discord (預設只傳啟動後的新對話)
    python run.py sync --backfill   # 連既有內容也傳
    python run.py export <sid>      # 把某 session 打包上傳 Discord (供他機搬移)
    python run.py pull <url>        # 從 Discord 附件連結還原 session 到本機
    python run.py tail        # 只在本機即時印出+存 (不碰 Discord)
    python run.py once        # 掃一次
    python run.py sessions    # 列出已存 session
    python run.py events <sid>
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

from envcfg import get, load_env
from store import Store
from uplink import ATTACH_MAX_BYTES, fetch_bytes, post_event, post_hello, post_transfer
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


def include_tools() -> bool:
    return (_e("SYNC_TOOLS") or "1").strip().lower() not in ("0", "false", "no", "off")


def _fmt(evt: dict) -> str:
    label = {"Prompt": "user", "AssistantMessage": "response",
             "ToolResults": "tool"}.get(evt["kind"], evt["kind"])
    text = (evt.get("text") or "").replace("\n", " ")
    if len(text) > 200:
        text = text[:200] + "…"
    natt = len(evt.get("attachments") or [])
    tag = f" 🖼️x{natt}" if natt else ""
    return f"[{evt['session_id'][:8]}] {label}:{tag} {text}"


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
        # 跳過既無文字又無附件的事件 (例如純工具回合的空殼)
        if not (evt.get("text") or "").strip() and not evt.get("attachments"):
            return
        meta = _meta(store, evt["session_id"])
        if wl and (meta.get("cwd") or "") not in wl:  # 空清單=全部同步
            return
        post_event(webhook, user, evt, meta)
        print(_fmt(evt), flush=True)

    # 一連上就讓 bot 建好 forum + 資訊 thread (不用等第一則對話)
    post_hello(webhook, user)
    watcher = Watcher(watch_dir(), store, on_event=handle,
                      backfill=args.backfill, include_tools=include_tools())
    print(f"[ks] sync 中: {watch_dir()} -> Discord webhook  使用者={user}  工具記錄={include_tools()}")
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
        if not (evt.get("text") or "").strip() and not evt.get("attachments"):
            return
        meta = _meta(store, evt["session_id"])
        post_event(webhook, user, evt, meta)
        print(_fmt(evt), flush=True)

    w = Watcher(wd, store, on_event=handle, backfill=True, include_tools=include_tools())
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


def cmd_export(args) -> None:
    """把指定 session 的 .jsonl+.json 打包成 zip, 當單一附件上傳 Discord。
    接收端在 Discord「複製附件連結」, 到目標機器跑 `pull <連結>` 還原。"""
    webhook = _e("WEBHOOK_URL")
    if not webhook:
        sys.exit(".env 缺 WEBHOOK_URL")
    user = user_name()
    wd = watch_dir()
    matches = sorted(wd.glob(f"{args.session_id}*.jsonl"))
    if not matches:
        sys.exit(f"在 {wd} 找不到符合 '{args.session_id}' 的 session 檔")
    if len(matches) > 1 and not args.all:
        print("符合多個 session, 請給更完整的 id, 或加 --all 全部匯出:")
        for m in matches:
            print("   ", m.stem)
        return

    for jsonl_path in matches:
        sid = jsonl_path.stem
        json_path = wd / f"{sid}.json"
        title = cwd = None
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(jsonl_path, arcname=jsonl_path.name)
            if json_path.exists():
                z.write(json_path, arcname=json_path.name)
                try:
                    meta = json.loads(json_path.read_text(encoding="utf-8"))
                    if isinstance(meta, dict):
                        title, cwd = meta.get("title"), meta.get("cwd")
                except Exception:
                    pass
        blob = buf.getvalue()
        if len(blob) > ATTACH_MAX_BYTES:
            print(f"[ks] {sid} 打包後 {len(blob) / 1024 / 1024:.1f} MB > 8 MB 上限, 略過")
            continue
        caption = (
            "📦 **Session 匯出包** — 對此附件「複製連結」, 到目標機器跑 "
            "`python run.py pull <連結>` 還原\n"
            f"Session ID: `{sid}`\nTitle: {title or '(no title)'}"
        )
        post_transfer(webhook, user, sid, f"{sid}.zip", blob,
                      title=title, cwd=cwd, caption=caption)
        print(f"[ks] 已匯出 {sid} ({len(blob) / 1024:.0f} KB zip) → Discord thread")
    print("[ks] export 完成。到 Discord 對附件複製連結, "
          "目標機器跑: python run.py pull <連結>")


def _rewrite_cwd(raw: bytes, new_cwd: str) -> bytes:
    """把 session .json 的 cwd 改寫成 new_cwd, 並把 permissions 裡等於舊 cwd
    的可讀/可寫路徑一起換掉 (讓搬到本機後落在對的資料夾)。"""
    try:
        meta = json.loads(raw.decode("utf-8"))
    except Exception as e:
        print(f"[ks] .json 解析失敗, cwd 未改寫: {e}")
        return raw
    old = meta.get("cwd")
    meta["cwd"] = new_cwd
    try:
        fs = meta["session_state"]["permissions"]["filesystem"]
        for key in ("allowed_read_paths", "allowed_write_paths",
                    "denied_read_paths", "denied_write_paths"):
            lst = fs.get(key)
            if isinstance(lst, list) and old:
                fs[key] = [new_cwd if p == old else p for p in lst]
    except (KeyError, TypeError):
        pass
    print(f"[ks] cwd 改寫: {old!r} -> {new_cwd!r}")
    return json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")


def cmd_pull(args) -> None:
    """從 Discord 附件連結抓 zip, 解開寫回本機 session 目錄。零憑證, 純向外。
    加 --cwd 可在還原時把 session 的工作目錄改寫成本機路徑。"""
    wd = watch_dir()
    wd.mkdir(parents=True, exist_ok=True)
    for url in args.url:
        blob = fetch_bytes(url)
        if blob is None:
            continue
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile:
            print(f"[ks] 不是有效的 zip (連結對嗎?): {url[:60]}…")
            continue
        written = []
        for name in zf.namelist():
            base = os.path.basename(name)  # 只取檔名, 防 zip-slip 路徑穿越
            if not base or not (base.endswith(".jsonl") or base.endswith(".json")):
                continue
            data = zf.read(name)
            if base.endswith(".json") and args.cwd:
                data = _rewrite_cwd(data, args.cwd)
            (wd / base).write_bytes(data)
            written.append(base)
        if written:
            print(f"[ks] 已還原到 {wd}: {', '.join(written)}")
        else:
            print("[ks] zip 內沒有 .jsonl/.json 可還原")
    print("[ks] pull 完成。Kiro CLI 現在應該看得到這個 session 了。")


def cmd_tail(args) -> None:
    store = Store(db_path())
    watcher = Watcher(watch_dir(), store,
                      on_event=lambda e: print(_fmt(e), flush=True),
                      backfill=args.backfill, include_tools=include_tools())
    print(f"[ks] 監看 {watch_dir()}  (Ctrl-C 結束)  backfill={args.backfill}")
    _loop(watcher, store)


def cmd_once(args) -> None:
    store = Store(db_path())
    watcher = Watcher(watch_dir(), store,
                      on_event=lambda e: print(_fmt(e), flush=True),
                      backfill=args.backfill, include_tools=include_tools())
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

    px = sub.add_parser("export", help="把指定 session 打包上傳 Discord (供他機 pull 還原)")
    px.add_argument("session_id", help="session id (可只給前幾碼)")
    px.add_argument("--all", action="store_true", help="前綴符合多個時全部匯出")
    px.set_defaults(func=cmd_export)

    pp = sub.add_parser("pull", help="從 Discord 附件連結還原 session 到本機")
    pp.add_argument("url", nargs="+", help="Discord 附件連結 (可多個)")
    pp.add_argument("--cwd", help="還原時把 session 的 cwd 改寫成此路徑 "
                                  "(連 permissions 可讀/可寫路徑一起換), 讓它落在本機資料夾")
    pp.set_defaults(func=cmd_pull)

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
