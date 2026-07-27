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
    python run.py kiro        # 全域啟動器: 問上傳分類 -> 背景同步 -> 啟動 Kiro CLI (見 ks-kiro)
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
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


def _decide_cat(raw: str, existing: Optional[str]) -> tuple[str, Optional[str]]:
    """把使用者對『上傳到哪個分類』的輸入判成動作。純函式, 好測。
    回 (action, value): keep=沿用現有 / default=改用預設(清除) / set=設成新 id /
    invalid=非數字(不改, 這次沿用現有)。"""
    raw = (raw or "").strip()
    if raw == "":
        return "keep", existing
    if raw == "-":
        return "default", None
    if not raw.isdigit():
        return "invalid", existing
    return "set", raw


def _current_cat_line(cwd: str, route_list: list) -> str:
    """『目前分類』那行的文字。命中的是上層資料夾時要講清楚是繼承來的,
    否則使用者會以為自己在這個資料夾設定過。"""
    hit = routes_mod.route_for(cwd, route_list)
    if not hit:
        return "  目前分類: 預設(個人 forum)"
    line = f"  目前分類: {hit.get('category_id')}"
    if hit.get("label"):
        line += f"（{hit['label']}）"
    if routes_mod.norm_path(hit.get("folder", "")) != routes_mod.norm_path(cwd):
        line += f"  ← 繼承自 {hit.get('folder')}"
    return line


def _prompt_category(cwd: str, route_list: list) -> Optional[str]:
    """互動詢問這個資料夾要上傳到哪個分類; 需要時更新 routes.json。回最終 category_id。"""
    existing = routes_mod.category_for(cwd, route_list)
    print(f"[ks-kiro] 目前資料夾: {cwd}")
    print(_current_cat_line(cwd, route_list))
    known = sorted({str(r.get("category_id")) for r in route_list
                    if r.get("category_id") and str(r.get("category_id")) != existing})
    if known:
        print("  已設定過的分類 ID: " + ", ".join(known))
    if existing:
        prompt = ("  Enter 沿用 / 輸入新的分類 ID / 輸入 '-' 改用預設(個人 forum): ")
    else:
        prompt = ("  要上傳到哪個分類? 輸入 Discord 分類 ID "
                  "(在 Discord 打 /categories 可查), 或直接 Enter 用預設: ")
    try:
        raw = input(prompt)
    except EOFError:
        raw = ""
    action, value = _decide_cat(raw, existing)
    if action == "set":
        routes_mod.add_route(cwd, value)
        print(f"  已設定: 此資料夾 → 分類 {value}")
    elif action == "default":
        if routes_mod.remove_route(cwd):
            print("  已改用預設 (清除此資料夾的路由)。")
        else:
            print("  使用預設 (個人 forum)。")
    elif action == "invalid":
        print("  不是數字 ID, 未變更路由 (這次沿用現有設定)。")
    return value


def _valid_webhook(v: str) -> bool:
    import check  # 沿用自檢那份格式規則, 避免兩套標準
    return bool(check.WEBHOOK_RE.match(v))


# (鍵, 提示, 說明, 驗證函式) — ks-kiro 啟動前若空值就照這個順序問
ENV_PROMPTS = [
    ("WEBHOOK_URL", "Discord Webhook URL",
     "到 Discord 的 kiro-command 頻道看釘選訊息, 或在任一頻道打 /webhook 取得",
     _valid_webhook),
    ("USER_NAME", "你的識別名",
     "bot 用它把對話分流到你專屬的 forum; 多台機器要填一樣的",
     lambda v: bool(v.strip())),
]


def ensure_env_interactive(keys=None) -> None:
    """.env 缺必填值時當場問使用者並寫回 .env。

    非互動環境 (例如背景啟動的 sync) 不問, 直接以錯誤結束 —— 免得在看不到的地方
    卡在 input() 等輸入。"""
    wanted = [e for e in ENV_PROMPTS if keys is None or e[0] in keys]
    missing = [e for e in wanted if not (_e(e[0]) or "").strip()]
    if not missing:
        return
    names = ", ".join(e[0] for e in missing)
    if not sys.stdin.isatty():
        sys.exit(f"client/.env 缺 {names} —— 請先填好 (或跑 `ks check` 檢查)")

    env_file = HERE / ".env"
    if not env_file.exists():  # 沒有 .env 就先從範本複製一份 (保留註解說明)
        example = HERE / ".env.example"
        if example.exists():
            env_file.write_bytes(example.read_bytes())
    print(f"[ks] 首次設定: {names} 還沒填, 現在補上 (會寫進 {env_file})")

    for key, label, hint, valid in missing:
        print(f"\n  {label}\n  {hint}")
        for attempt in range(3):
            try:
                val = input(f"  {key} = ").strip()
            except EOFError:  # 讀不到輸入 (被導向/isatty 判斷不準) — 別空轉三次
                sys.exit(f"\n沒有輸入可讀, 中止。請手動編輯 {env_file} 填好 {key}。")
            val = val.strip("\"'“”＂「」")  # 使用者常連引號一起貼
            if valid(val):
                envcfg_set(key, val)
                print(f"  已寫入 {key}")
                break
            print("  格式看起來不對, 再試一次。" if attempt < 2 else "  仍然無效。")
        else:
            sys.exit(f"{key} 未設定, 中止。可手動編輯 {env_file} 後再跑一次。")


def ensure_kiro_interactive() -> str:
    """找出 Kiro CLI 執行檔; 找不到就當場問使用者並把路徑寫進 .env 的 KIRO_CMD。

    已經在 PATH 上 (或 KIRO_CMD 已指對) 就直接用, 不打擾使用者 —— 只有真的找不到才問。"""
    import check
    cmd = (_e("KIRO_CMD") or "").strip()
    exe = check.resolve_kiro(cmd or "kiro")
    if exe:
        return exe

    where = f"KIRO_CMD={cmd!r} 指不到執行檔" if cmd else "PATH 上找不到 kiro 指令"
    if not sys.stdin.isatty():
        sys.exit(f"{where} —— 請在 client/.env 設定 KIRO_CMD 為 Kiro CLI 執行檔的完整路徑")

    print(f"\n[ks] {where}。\n"
          "  請貼上 Kiro CLI 執行檔的完整路徑 (或它所在的資料夾)\n"
          "  Windows 例: C:\\Users\\你\\AppData\\Local\\Programs\\kiro\\kiro.exe")
    for attempt in range(3):
        try:
            raw = input("  KIRO_CMD = ").strip()
        except EOFError:
            sys.exit(f"\n沒有輸入可讀, 中止。請手動在 {HERE / '.env'} 填好 KIRO_CMD。")
        exe = check.resolve_kiro(raw)
        if exe:
            envcfg_set("KIRO_CMD", exe)  # 存解析後的路徑, 下次直接用
            print(f"  已寫入 KIRO_CMD = {exe}")
            return exe
        print("  這個路徑上找不到執行檔, 再試一次。" if attempt < 2 else "  仍然找不到。")
    sys.exit(f"KIRO_CMD 未設定, 中止。可手動編輯 {HERE / '.env'} 後再跑一次。")


def envcfg_set(key: str, value: str) -> None:
    """寫回 .env, 同時更新本行程的環境 (讓 _e / 背景 sync 子行程立刻讀得到)。"""
    import envcfg
    envcfg.set_env_value(key, value, str(HERE / ".env"))
    ENV[key] = value
    os.environ[key] = value


def _sync_grace_seconds() -> float:
    """kiro 結束後等多久再停背景 sync, 讓最後一次快照有機會 flush。"""
    _, debounce, min_interval, _ = _snap_params()
    return debounce + min_interval + 3.0


def cmd_kiro(args) -> None:
    """全域啟動器: 先問這個資料夾要上傳到哪個分類, 背景啟動 sync, 再前景啟動 Kiro CLI;
    Kiro 結束後等最後一次同步 flush 再停 sync。目的: 讓使用者不會忘了開同步。"""
    ensure_env_interactive()  # .env 有空值就當場問使用者並寫回
    exe = ensure_kiro_interactive()  # 先確定 kiro 找得到, 免得設完路由才發現不能啟動
    user = user_name()
    cwd = str(Path.cwd())

    if args.cat is not None:  # --cat 跳過詢問
        action, value = _decide_cat(args.cat, routes_mod.category_for(cwd, routes_mod.load_routes()))
        if action == "set":
            routes_mod.add_route(cwd, value)
        elif action == "default":
            routes_mod.remove_route(cwd)
        cat = value
    else:
        cat = _prompt_category(cwd, routes_mod.load_routes())

    # 背景啟動 sync (log 導到檔, 免得洗掉 Kiro 的互動畫面)
    log_path = HERE / "ks-sync.log"
    logf = open(log_path, "a", encoding="utf-8")
    logf.write(f"\n==== ks-kiro sync @ {time.strftime('%Y-%m-%d %H:%M:%S')} cwd={cwd} ====\n")
    logf.flush()
    sync_proc = subprocess.Popen(
        [sys.executable, str(HERE / "run.py"), "sync"], stdout=logf, stderr=logf)
    print(f"[ks-kiro] 背景同步已啟動  使用者={user}  分類={cat or '預設(個人 forum)'}  "
          f"(log: {log_path})")
    time.sleep(1.5)
    if sync_proc.poll() is not None:
        print("[ks-kiro] ⚠ 警告: 背景同步啟動後立刻結束, 請看上面的 log; "
              "Kiro 仍會啟動, 但對話可能不會上傳。")

    kiro_args = list(args.kiro_args or [])
    if kiro_args and kiro_args[0] == "--":  # argparse REMAINDER 會保留分隔的 '--', 去掉
        kiro_args = kiro_args[1:]
    print(f"[ks-kiro] 啟動 Kiro CLI ({exe}) …\n")
    try:
        subprocess.run([exe] + kiro_args)
    except KeyboardInterrupt:
        pass
    finally:
        if sync_proc.poll() is None:
            grace = _sync_grace_seconds()
            print(f"\n[ks-kiro] Kiro 已結束, 等待最後一次同步 ({grace:.0f}s)…")
            try:
                time.sleep(grace)
            except KeyboardInterrupt:
                pass
            sync_proc.terminate()
            try:
                sync_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                sync_proc.kill()
            print("[ks-kiro] 背景同步已停止。")
        try:
            logf.close()
        except Exception:
            pass


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

    pk = sub.add_parser("kiro", help="問上傳分類 → 背景啟動同步 → 前景啟動 Kiro CLI (全域啟動器)")
    pk.add_argument("--cat", default=None,
                    help="直接指定分類 ID 跳過詢問 ('-' = 用預設); 省略則互動詢問")
    pk.add_argument("kiro_args", nargs=argparse.REMAINDER,
                    help="'--' 之後的參數原樣轉給 Kiro CLI")
    pk.set_defaults(func=cmd_kiro)

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
