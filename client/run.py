"""
KiroSync Client 進入點 (擷取 + 上行)。

架構 B: client 只把 raw session 快照 (zip) 上傳; 解析/格式化/貼圖/離線拉取全由 bot 端做。
client 因此是**無狀態的** — 不解析、不存 DB, 重啟後靠比對檔案 signature 決定要不要上傳。

用法:
    cd client
    cp .env.example .env      # 填好 WEBHOOK_URL / USER_NAME 後
    python run.py sync        # 監看並把 raw 快照上傳 Discord (bot 端渲染)
    python run.py export <sid>      # 一次性把某 session 快照上傳 (供他機搬移)
    python run.py pull <url...>     # 從 /link 給的連結還原 session 到本機 (多片依序併接)
    python run.py sessions    # 列出本機 session (直接讀 session 目錄)
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


def user_name() -> str:
    return _e("USER_NAME") or os.environ.get("USERNAME") or "kiro-user"


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
                      chunk: int) -> Optional[tuple[int, int]]:
    """打包並上傳一個 session 快照, 回傳 (片數, zip 大小)。
    session 檔在 glob 之後被刪 (Kiro 清理) 等 OSError 不往外丟, 回 None 讓呼叫端略過,
    不能讓它炸掉 sync 的監看迴圈。"""
    try:
        blob, title, cwd = _build_session_zip(wd, sid)
    except OSError as e:
        print(f"[ks] 略過 {sid[:8]}: 讀 session 檔失敗 ({e})", flush=True)
        return None
    nparts = post_snapshot(webhook, user, sid, blob, title=title, cwd=cwd, chunk_bytes=chunk)
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

    post_hello(webhook, user)  # 讓 bot 先建好 forum + 資訊 thread
    print(f"[ks] sync(快照模式) 中: {wd} -> Discord  使用者={user}  "
          f"切片={chunk // 1024 // 1024}MB  去抖={debounce}s")

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
                if wl and (_read_cwd(mj) or "") not in wl:  # 空清單=全部同步
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
                        res = _snapshot_session(webhook, user, wd, sid, chunk)
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

    for jsonl_path in matches:
        sid = jsonl_path.stem
        res = _snapshot_session(webhook, user, wd, sid, chunk)
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

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
