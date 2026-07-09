"""
Client 端「上行」: 把一則事件 POST 到 Discord Webhook。

只用標準庫 (urllib)。兩機之間沒有任何直接連線 — 只有「本機 -> discord.com」向外 HTTPS。

架構 B 的 wire format (bot 端 parse_ingest 對應):
    Hello:  KSV1 {"u":..,"k":"Hello","s":"","ts":..}\n
    Snap :  KSV1 {"u":..,"s":..,"k":"Snap","g":世代,"p":片號,"n":總片,"title":..,"cwd":..}
            附件 = session zip 的一個切片; bot 收齊併回、渲染、並留存供離線 pull。
"""

from __future__ import annotations

import datetime
import json
import time
import urllib.error
import urllib.request
import uuid
import zlib
from typing import Callable, Optional

SNAP_CHUNK_BYTES = 24 * 1024 * 1024  # 快照切片上限 (Discord 單附件約 25MB, 留安全值)
_UA = "DiscordBot (KiroSync, 1.0)"


def post_hello(webhook_url: str, user: str, log: Callable[[str], None] = print) -> None:
    """client 一啟動就送: 讓 bot 立刻建好該使用者的 forum + 一則資訊 thread。"""
    ts = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    header = {"u": user, "k": "Hello", "s": "", "ts": ts}
    content = "KSV1 " + json.dumps(header, ensure_ascii=False) + "\n"
    _post(webhook_url, content, log)


def _send_raw(url, body: bytes, ctype: str, log: Callable[[str], None]) -> None:
    # Discord/Cloudflare 會 403 擋掉預設 Python-urllib UA, 一定要帶
    headers = {"Content-Type": ctype, "User-Agent": _UA}
    for _ in range(5):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited
                retry = 1.0
                try:
                    retry = float(json.loads(e.read().decode("utf-8")).get("retry_after", 1.0))
                except Exception:
                    pass
                time.sleep(retry + 0.1)
                continue
            log(f"[uplink] HTTP {e.code}")
            return
        except Exception as e:
            log(f"[uplink] 失敗: {e}")
            return


def _post(url: str, content: str, log: Callable[[str], None]) -> None:
    body = json.dumps({"content": content}).encode("utf-8")
    _send_raw(url, body, "application/json", log)


def post_snapshot(
    webhook_url: str, user: str, session_id: str, zip_bytes: bytes,
    *, title: Optional[str] = None, cwd: Optional[str] = None,
    gen: Optional[str] = None, chunk_bytes: int = SNAP_CHUNK_BYTES,
    log: Callable[[str], None] = print,
) -> int:
    """架構 B 的上行: 把一個 session 的 zip (內含 .jsonl+.json) 依 chunk 上限切片,
    每片一則 k:Snap 訊息 (帶 g 世代 / p 片號 / n 總片) 上傳。bot 收齊後併回 zip:
      - 讀 .jsonl 渲染新增行到 thread (格式化/抽圖都在 bot 端)
      - 保留這些片訊息當作「可離線拉取」的來源, /link 回其現簽連結
    回傳送出的片數。gen 預設用「大小-crc32」唯一標識這一版內容 (避免同大小不同內容撞世代)。"""
    gen = gen or f"{len(zip_bytes)}-{zlib.crc32(zip_bytes) & 0xffffffff:08x}"
    if chunk_bytes < 1:
        chunk_bytes = SNAP_CHUNK_BYTES
    parts = [zip_bytes[i:i + chunk_bytes] for i in range(0, len(zip_bytes), chunk_bytes)] or [b""]
    n = len(parts)
    for p, part in enumerate(parts):
        header = {
            "u": user, "s": session_id, "k": "Snap",
            "g": gen, "p": p, "n": n, "title": title, "cwd": cwd,
        }
        content = "KSV1 " + json.dumps(header, ensure_ascii=False)
        _post_multipart(webhook_url, content, f"{session_id}.{gen}.p{p}.zip", part, log)
    return n


def fetch_bytes(url: str, log: Callable[[str], None] = print) -> Optional[bytes]:
    """GET 一個 (Discord CDN) 附件連結, 回傳位元組; 失敗回 None。零憑證, 純向外。"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        log(f"[uplink] 下載失敗 HTTP {e.code} (連結可能已過期, 到 Discord 重新複製)")
    except Exception as e:
        log(f"[uplink] 下載失敗: {e}")
    return None


def _post_multipart(url, content: str, filename: str, file_bytes: bytes,
                    log: Callable[[str], None]) -> None:
    boundary = "----KiroSync" + uuid.uuid4().hex
    payload = json.dumps({"content": content}).encode("utf-8")
    pre = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="payload_json"\r\n'
        f"Content-Type: application/json\r\n\r\n"
    ).encode("utf-8") + payload + (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    body = pre + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
    _send_raw(url, body, f"multipart/form-data; boundary={boundary}", log)
