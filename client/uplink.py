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
from typing import Callable, Optional

from util import chunk

PIECE = 1800  # content 上限 2000, 留給 header


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
    pieces = chunk(evt.get("text") or "", PIECE) or [""]
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


def _post(url: str, content: str, log: Callable[[str], None]) -> None:
    body = json.dumps({"content": content}).encode("utf-8")
    for _ in range(5):
        req = urllib.request.Request(
            url, data=body,
            headers={
                "Content-Type": "application/json",
                # Discord/Cloudflare 會 403 擋掉預設的 Python-urllib UA, 一定要帶
                "User-Agent": "DiscordBot (KiroSync, 1.0)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
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
