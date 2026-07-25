"""
KiroSync Client 進入點 (擷取 + 上行)。

架構 B: client 只把 raw session 快照 (zip) 上傳; 解析/格式化/貼圖/離線拉取全由 bot 端做。
client 因此是**無狀態的** — 不解析、不存 DB, 重啟後靠比對檔案 signature 決定要不要上傳。

用法:
    cd client
    cp .env.example .env      # 填好 WEBHOOK_URL / USER_NAME 後
    python run.py check       # 自檢 .env (WEBHOOK_URL / USER_NAME 有沒有填好)
    python run.py sync        # 監看並把 raw 快照上傳 Discord (bot 端渲染)
    python run.py export <sid>      # 一次性把某 session 快照上傳 (供他機搬移)
    python run.py pull <url...>     # 從 /link 給的連結還原 session 到本機 (多片依序併接)
    python run.py sessions    # 列出本機 session (直接讀 session 目錄)
    python run.py route add <資料夾> <分類ID>   # 把某資料夾的 session 送到指定 Discord 分類
    python run.py route list / route remove <資料夾>
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
from typing import Optional

import routes as routes_mod
from envcfg import get, load_env
from uplink import SNAP_CHUNK_BYTES, fetch_bytes, post_hello, post_snapshot

HERE = Path(__file__).resolve().parent
ENV = load_env()


def _e(key, default=None):
    return get(ENV, key, default)


def watch_dir() -> Path:
    wd = _e("WATCH_DIR") or ""
    return Path(wd) if wd else Path.home() / ".kiro" / "sessions" / "cli"


def interval() -> float:
    try:
        return float(_e("POLL_INTERVAL") or 0.7)
    except ValueError:
        return 0.7


def workspaces() -> list[str]:
    raw = _e("SYNC_WORKSPACES") or ""
    return [w.strip() for w in raw.split(",") if w.strip()]


def _norm_path(p: str) -> str:
    """正規化路徑供比對: 統一分隔線、去結尾斜線、Windows 上不分大小寫。
    這樣 D:/Programs/X、D:\\Programs\\X\\、d:\\programs\\x 都會對上同一個。"""
    if not p:
        return ""
    return os.path.normcase(os.path.normpath(p.strip()))


def _in_workspaces(cwd: str, wl: list[str]) -> bool:
    """cwd 是否落在任一限定資料夾內 (含子資料夾)。空清單 = 全部同步。
    在 D:\\Work 底下任何子專案開的 session 都算 D:\\Work 的一部分。"""
    if not wl:
        return True
    c = _norm_path(cwd)
    if not c:
        return False
    for w in wl:
        w = _norm_path(w)
        if w and (c == w or c.startswith(w + os.sep)):
            return True
    return False


def user_name() -> str:
    # 必填: USER_NAME 是 bot 分流到你專屬 forum 的鍵。以前留空會退回系統帳號,
    # 但那會讓多台機器各自用不同的系統名 -> 拆成多個 forum、也可能撞名。
    name = (_e("USER_NAME") or "").strip()
    if not name:
        sys.exit(".env 缺 USER_NAME —— 請在 client/.env 設定你的識別名 "
                 "(bot 用它把你的對話分流到專屬 forum; 多台機器要填一樣的)")
    return name


def _read_meta(meta_path: Path) -> dict:
    if not meta_path.exists():
        return {}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return meta if isinstance(meta, dict) else {}


def _read_cwd(meta_path: Path) -> Optional[str]:
    return _read_meta(meta_path).get("cwd")


def _build_session_zip(wd: Path, sid: str):
    """把 session 的 .jsonl(+.json) 打成 zip, 回傳 (zip_bytes, title, cwd)。"""
    jsonl_path = wd / f"{sid}.jsonl"
    json_path = wd / f"{sid}.json"
    title = cwd = None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(jsonl_path, arcname=jsonl_path.name)
        if json_path.exists():
            z.write(json_path, arcname=json_path.name)
            meta = _read_meta(json_path)
            title, cwd = meta.get("title"), meta.get("cwd")
    return buf.getvalue(), title, cwd


def _snapshot_session(webhook: str, user: str, wd: Path, sid: str,
                      chunk: int, cat: Optional[str] = None) -> Optional[tuple[int, int]]:
    """打包並上傳一個 session 快照, 回傳 (片數, zip 大小)。
    cat = 目標 Discord 分類 ID (資料夾路由算出來的); None = bot 端進個人 forum。
    session 檔在 glob 之後被刪 (Kiro 清理) 等 OSError 不往外丟, 回 None 讓呼叫端略過,
    不能讓它炸掉 sync 的監看迴圈。"""
    try:
        blob, title, cwd = _build_session_zip(wd, sid)
    except OSError as e:
        print(f"[ks] 略過 {sid[:8]}: 讀 session 檔失敗 ({e})", flush=True)
        return None
    nparts = post_snapshot(webhook, user, sid, blob, title=title, cwd=cwd,
                           cat=cat, chunk_bytes=chunk)
    return nparts, len(blob)


def _snap_params() -> tuple[int, float, float, float]:
    def f(key, default):
        try:
            return float(_e(key) or default)
        except ValueError:
            return default
    chunk = int(f("SNAP_CHUNK_MB", 24) * 1024 * 1024) or SNAP_CHUNK_BYTES
    return chunk, f("SNAP_DEBOUNCE", 2.0), f("SNAP_MIN_INTERVAL", 5.0), f("SNAP_MAX_WAIT", 15.0)


def cmd_sync(args) -> None:
    """架構 B: 只把 raw session 快照上傳給 bot; 解析/格式化/貼圖全在 bot 端做。
    去抖動: session 停止變動 debounce 秒後才上傳; 持續變動則最遲 max_wait 秒強制上傳一次;
    兩次上傳至少間隔 min_interval 秒。bot 保留最新快照供離線 pull。"""
    webhook = _e("WEBHOOK_URL")
    if not webhook:
        sys.exit(".env 缺 WEBHOOK_URL")
    user = user_name()
    wl = workspaces()
    wd = watch_dir()
    chunk, debounce, min_interval, max_wait = _snap_params()

    route_list = routes_mod.load_routes()  # 啟動時載入一次; 改路由後重跑 sync 生效

    post_hello(webhook, user)  # 讓 bot 先建好 forum + 資訊 thread
    print(f"[ks] sync(快照模式) 中: {wd} -> Discord  使用者={user}  "
          f"切片={chunk // 1024 // 1024}MB  去抖={debounce}s  路由={len(route_list)} 條")

    observed: dict[str, tuple] = {}       # sid -> 最近在磁碟上看到的 (jsonl_size, meta_mtime)
    last_change: dict[str, float] = {}    # sid -> 最近一次變動的時刻
    first_pending: dict[str, float] = {}  # sid -> 這輪待送從何時開始
    last_sent_sig: dict[str, tuple] = {}  # sid -> 上次已上傳的 signature
    last_sent_at: dict[str, float] = {}   # sid -> 上次上傳時刻

    try:
        while True:
            now = time.time()
            for jf in sorted(wd.glob("*.jsonl")):
                sid = jf.stem
                try:
                    jsize = jf.stat().st_size
                except OSError:
                    continue
                if jsize == 0:
                    continue  # 空殼 session 還沒內容, 不送
                mj = wd / f"{sid}.json"
                try:
                    mtime = mj.stat().st_mtime if mj.exists() else 0.0
                except OSError:  # exists 與 stat 之間被刪
                    mtime = 0.0
                cwd = _read_cwd(mj) or ""
                if not _in_workspaces(cwd, wl):  # 空清單=全部同步
                    continue
                sig = (jsize, mtime)
                if sig != observed.get(sid):
                    observed[sid] = sig
                    last_change[sid] = now
                    first_pending.setdefault(sid, now)
                # 尚未上傳過此 signature, 且 (已閒置 or 等太久), 且距上次上傳夠久 -> 送
                if sig != last_sent_sig.get(sid) and sid in first_pending:
                    idle = now - last_change.get(sid, now) >= debounce
                    waited = now - first_pending[sid] >= max_wait
                    spaced = now - last_sent_at.get(sid, 0.0) >= min_interval
                    if (idle or waited) and spaced:
                        cat = routes_mod.category_for(cwd, route_list)
                        res = _snapshot_session(webhook, user, wd, sid, chunk, cat=cat)
                        last_sent_at[sid] = now  # 失敗也記, 下次至少隔 min_interval 再試
                        if res is None:
                            continue
                        nparts, nbytes = res
                        last_sent_sig[sid] = sig
                        first_pending.pop(sid, None)
                        print(f"[ks] snapshot {sid[:8]} gen={nbytes} "
                              f"{nparts}片 ({nbytes / 1024:.0f}KB)", flush=True)
            time.sleep(interval())
    except KeyboardInterrupt:
        print("\n[ks] 結束。")


def cmd_export(args) -> None:
    """一次性把指定 session 快照上傳 (跟 sync 同一條 Snap 管線)。
    上傳後在 Discord 打 /link <sid> 拿下載連結, 或直接在目標機器 pull。"""
    webhook = _e("WEBHOOK_URL")
    if not webhook:
        sys.exit(".env 缺 WEBHOOK_URL")
    user = user_name()
    wd = watch_dir()
    chunk, *_ = _snap_params()
    matches = sorted(wd.glob(f"{args.session_id}*.jsonl"))
    if not matches:
        sys.exit(f"在 {wd} 找不到符合 '{args.session_id}' 的 session 檔")
    if len(matches) > 1 and not args.all:
        print("符合多個 session, 請給更完整的 id, 或加 --all 全部匯出:")
        for m in matches:
            print("   ", m.stem)
        return

    route_list = routes_mod.load_routes()
    for jsonl_path in matches:
        sid = jsonl_path.stem
        cat = routes_mod.category_for(_read_cwd(wd / f"{sid}.json") or "", route_list)
        res = _snapshot_session(webhook, user, wd, sid, chunk, cat=cat)
        if res is None:
            continue
        nparts, nbytes = res
        print(f"[ks] 已匯出 {sid} ({nbytes / 1024:.0f} KB zip, {nparts} 片) → Discord")
    print("[ks] export 完成。到 Discord 打 `/link <sid>` 取得下載連結, "
          "目標機器跑: python run.py pull <連結...>")


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
    """從 Discord 附件連結還原 session。多個連結 = 一個 zip 的多個切片, 依序併接後解開。
    零憑證, 純向外。加 --cwd 可把 session 的工作目錄改寫成本機路徑。"""
    wd = watch_dir()
    wd.mkdir(parents=True, exist_ok=True)
    parts = []
    for i, url in enumerate(args.url):
        blob = fetch_bytes(url)
        if blob is None:
            sys.exit(f"[ks] 第 {i} 片下載失敗, 中止 (連結可能過期, 到 Discord 重打 /link)")
        parts.append(blob)
    blob = b"".join(parts)
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        sys.exit("[ks] 併接後不是有效的 zip — 請確認把 /link 給的連結「全部、依序」都帶上了")
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
        print(f"[ks] 已還原到 {wd}: {', '.join(written)} ({len(args.url)} 片)")
        print("[ks] pull 完成。Kiro CLI 現在應該看得到這個 session 了。")
    else:
        print("[ks] zip 內沒有 .jsonl/.json 可還原")


def _count_lines(path: Path) -> int:
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def cmd_check(args) -> None:
    """自檢 .env 設定 (WEBHOOK_URL / USER_NAME / Kiro 目錄)。"""
    import check
    raise SystemExit(check.run(["--no-net"] if args.no_net else []))


def cmd_route_add(args) -> None:
    cid = (args.category_id or "").strip()
    if not cid.isdigit():
        sys.exit("category_id 要填 Discord 分類的數字 ID "
                 "(Discord 開開發者模式 -> 右鍵分類 -> 複製頻道 ID; 或到 command 頻道打 /categories), "
                 f"收到: {args.category_id!r}")
    routes_mod.add_route(args.folder, cid, args.label or "")
    tag = f"（{args.label}）" if args.label else ""
    print(f"[ks] 已設定路由: {args.folder} -> 分類 {cid}{tag}")


def cmd_route_list(args) -> None:
    rs = routes_mod.load_routes()
    if not rs:
        print("[ks] 尚未設定任何路由 (所有 session 進個人 forum)。")
        return
    for r in rs:
        lbl = f"（{r.get('label')}）" if r.get("label") else ""
        print(f"{r.get('folder')} -> 分類 {r.get('category_id')}{lbl}")


def cmd_route_remove(args) -> None:
    ok = routes_mod.remove_route(args.folder)
    print(f"[ks] 已移除路由: {args.folder}" if ok else f"[ks] 沒有符合的路由: {args.folder}")


def cmd_sessions(args) -> None:
    """列出本機 Kiro session。直接讀 session 目錄, 不需要任何本機狀態。"""
    wd = watch_dir()
    if not wd.exists():
        sys.exit(f"找不到 session 目錄 {wd} (Kiro CLI 用過嗎? 或用 WATCH_DIR 指定)")
    files = sorted(wd.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print(f"[ks] {wd} 裡沒有 session。")
        return
    for jf in files:
        sid = jf.stem
        meta = _read_meta(wd / f"{sid}.json")
        state = meta.get("session_state")
        model = None
        if isinstance(state, dict):
            info = state.get("rts_model_state")
            info = info.get("model_info") if isinstance(info, dict) else None
            model = info.get("model_name") if isinstance(info, dict) else None
        print(f"{sid[:8]}  {_count_lines(jf):>3} 則  {model or '?':<8}  "
              f"{meta.get('title') or '(無標題)'}  [{meta.get('cwd') or ''}]")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="ks-client", description="KiroSync client — 擷取+上行")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("check", help="自檢 .env (WEBHOOK_URL / USER_NAME 有沒有填好)")
    pc.add_argument("--no-net", action="store_true", help="不連 Discord, 只做本機格式檢查")
    pc.set_defaults(func=cmd_check)

    psy = sub.add_parser("sync", help="監看並把 raw session 快照上傳到 Discord (bot 端渲染)")
    psy.set_defaults(func=cmd_sync)

    px = sub.add_parser("export", help="一次性把指定 session 快照上傳 Discord (供他機 pull 還原)")
    px.add_argument("session_id", help="session id (可只給前幾碼)")
    px.add_argument("--all", action="store_true", help="前綴符合多個時全部匯出")
    px.set_defaults(func=cmd_export)

    pp = sub.add_parser("pull", help="從 Discord 附件連結還原 session 到本機")
    pp.add_argument("url", nargs="+", help="Discord 附件連結 (可多個)")
    pp.add_argument("--cwd", help="還原時把 session 的 cwd 改寫成此路徑 "
                                  "(連 permissions 可讀/可寫路徑一起換), 讓它落在本機資料夾")
    pp.set_defaults(func=cmd_pull)

    ps = sub.add_parser("sessions", help="列出本機 session")
    ps.set_defaults(func=cmd_sessions)

    pr = sub.add_parser("route", help="設定『資料夾 → Discord 分類 ID』路由")
    rsub = pr.add_subparsers(dest="action", required=True)
    ra = rsub.add_parser("add", help="新增/覆蓋一條路由")
    ra.add_argument("folder", help="本機資料夾 (含子資料夾都算)")
    ra.add_argument("category_id", help="Discord 分類的數字 ID")
    ra.add_argument("--label", default="", help="給人看的顯示名 (選填)")
    ra.set_defaults(func=cmd_route_add)
    rl = rsub.add_parser("list", help="列出目前路由")
    rl.set_defaults(func=cmd_route_list)
    rr = rsub.add_parser("remove", help="移除一條路由")
    rr.add_argument("folder", help="要移除的資料夾")
    rr.set_defaults(func=cmd_route_remove)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
