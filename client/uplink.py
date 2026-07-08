"""
Client 端「上行」: 把一則事件 POST 到 Discord Webhook。

只用標準庫 (urllib)。兩機之間沒有任何直接連線 — 只有「本機 -> discord.com」向外 HTTPS。

Wire format (bot 端 parse_ingest 對應):
    KSV1 {"u":..,"s":..,"k":..,"q":..,"p":..,"n":..[,"title":..,"cwd":..]}\n<文字片段>
    長文字切成多段 (每段 <=1800 字), p=片段序號、n=總片段數;
    title/cwd 只放第一段 (p=0), bot 用來建 thread。
"""

from __future__ import annotations

import datetime
import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable, Optional

from util import chunk

PIECE = 1800  # content 上限 2000, 留給 header
ATTACH_MAX_BYTES = 8 * 1024 * 1024  # Discord webhook 附件上限 (保守 8MB)
_UA = "DiscordBot (KiroSync, 1.0)"


def post_hello(webhook_url: str, user: str, log: Callable[[str], None] = print) -> None:
    """client 一啟動就送: 讓 bot 立刻建好該使用者的 forum + 一則資訊 thread。"""
    ts = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    header = {"u": user, "k": "Hello", "s": "", "ts": ts}
    content = "KSV1 " + json.dumps(header, ensure_ascii=False) + "\n"
    _post(webhook_url, content, log)


def post_event(
    webhook_url: str, user: str, evt: dict,
    meta: Optional[dict] = None, log: Callable[[str], None] = print,
) -> None:
    meta = meta or {}
    pieces = chunk(evt.get("text") or "", PIECE)  # 無文字 -> 空 list, 不送空訊息
    n = len(pieces)
    for i, piece in enumerate(pieces):
        header = {
            "u": user, "s": evt["session_id"], "k": evt["kind"],
            "q": evt["seq"], "p": i, "n": n,
        }
        if i == 0:
            header["title"] = meta.get("title")
            header["cwd"] = meta.get("cwd")
        content = "KSV1 " + json.dumps(header, ensure_ascii=False) + "\n" + piece
        _post(webhook_url, content, log)

    # 附件 (貼圖) 用 multipart 送
    for att in (evt.get("attachments") or []):
        data = att.get("data") or b""
        if len(data) > ATTACH_MAX_BYTES:
            log(f"[uplink] 附件 {att.get('filename')} 太大 ({len(data)} bytes), 略過")
            continue
        header = {
            "u": user, "s": evt["session_id"], "k": evt["kind"], "q": evt["seq"],
            "att": att.get("filename"), "title": meta.get("title"), "cwd": meta.get("cwd"),
        }
        content = "KSV1 " + json.dumps(header, ensure_ascii=False)
        _post_multipart(webhook_url, content, att.get("filename", "file.bin"), data, log)


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


def post_transfer(
    webhook_url: str, user: str, session_id: str, filename: str, blob: bytes,
    *, title: Optional[str] = None, cwd: Optional[str] = None,
    caption: str = "", log: Callable[[str], None] = print,
) -> None:
    """把整包 session (已壓成 zip) 當單一附件送到該 session 的 thread。
    bot 會把附件轉貼到 thread; 接收端在 Discord 複製該附件連結, 用 `pull` 抓回。"""
    header = {"u": user, "s": session_id, "k": "Transfer", "title": title, "cwd": cwd}
    content = "KSV1 " + json.dumps(header, ensure_ascii=False)
    if caption:
        content += "\n" + caption
    _post_multipart(webhook_url, content, filename, blob, log)


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
