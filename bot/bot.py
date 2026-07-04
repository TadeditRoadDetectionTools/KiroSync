"""
KiroSync 中央 Bot (單一一隻)。

職責:
  1. 監聽 ingest 頻道 (使用者的 client 透過 Webhook 把事件貼到這裡)。
  2. 依 payload 的 user -> 在「指定分類」下建立/取得該使用者的 forum 頻道。
  3. 依 session_id -> 在該 forum 裡建立/取得對應 thread。
  4. 把對話文字貼進 thread。

只有這隻 bot 持有 token, 且只走 Discord Gateway (向外連線), 不需要對外開 port。
需求: 開發者後台開啟 MESSAGE CONTENT INTENT; bot 在 guild 有 Manage Channels /
      Create Public Threads / Send Messages in Threads 權限。
"""

from __future__ import annotations

import asyncio
import datetime
import json
from pathlib import Path
from typing import Optional

import discord

from store import Store
from util import chunk

THREAD_PIECE = 1990  # thread 內單則訊息上限 2000


def _now() -> str:
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def parse_ingest(content: str) -> Optional[dict]:
    """把 client uplink 的 wire format 解析回 dict; 不是我們的格式就回 None。"""
    if not content.startswith("KSV1 "):
        return None
    nl = content.find("\n")
    if nl == -1:
        header_str, text = content[5:], ""
    else:
        header_str, text = content[5:nl], content[nl + 1:]
    try:
        h = json.loads(header_str)
    except Exception:
        return None
    h["_text"] = text
    return h


class KiroBot(discord.Client):
    def __init__(self, cfg: dict, store: Store):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.cfg = cfg
        self.store = store
        self._locks: dict[str, asyncio.Lock] = {}
        self._announced = False

    def _lock(self, key: str) -> asyncio.Lock:
        lk = self._locks.get(key)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[key] = lk
        return lk

    async def _status(self, text: str) -> None:
        cid = self.cfg.get("command_channel_id")
        if not cid:
            return
        try:
            ch = self.get_channel(int(cid)) or await self._fetch(int(cid))
            if ch is not None:
                await ch.send(text)
        except Exception as e:
            print(f"[bot] 狀態訊息送失敗: {e}")

    async def on_ready(self):
        print(f"[bot] 已登入: {self.user}  監聽 ingest 頻道 {self.cfg['ingest_channel_id']}")
        if not self._announced:
            self._announced = True
            await self._status(f"🟢 **Bot 上線** `{_now()}`")
            await self._publish_webhook()
        else:
            await self._status(f"🔁 **Bot 重新連線** `{_now()}`")

    async def _publish_webhook(self) -> None:
        """在 ingest 頻道自動建/取 webhook, 把 URL 貼到 command 頻道並釘選。"""
        ingest = self.get_channel(int(self.cfg["ingest_channel_id"])) or \
            await self._fetch(int(self.cfg["ingest_channel_id"]))
        if not isinstance(ingest, discord.TextChannel):
            print("[bot] ingest 不是文字頻道, 無法建 webhook")
            return
        try:
            hooks = await ingest.webhooks()
        except discord.Forbidden:
            print("[bot] 缺 Manage Webhooks 權限, 無法自動建 webhook")
            return
        hook = discord.utils.get(hooks, name="kiro-ingest")
        if hook is None:
            try:
                hook = await ingest.create_webhook(name="kiro-ingest")
                print(f"[bot] 建立 webhook: {hook.id}")
            except discord.Forbidden:
                print("[bot] 缺 Manage Webhooks 權限, 無法建 webhook")
                return
        url = hook.url

        cid = self.cfg.get("command_channel_id")
        dest = None
        if cid:
            dest = self.get_channel(int(cid)) or await self._fetch(int(cid))
        if not isinstance(dest, (discord.TextChannel, discord.Thread)):
            print(f"[bot] Client webhook URL (請填進 client .env WEBHOOK_URL):\n{url}")
            return

        marker = "🔗 **Client Webhook URL**"
        body = f"{marker}\n把這條填進 client 的 `.env` 的 `WEBHOOK_URL`:\n{url}"
        try:
            pins = await dest.pins()
        except Exception:
            pins = []
        existing = discord.utils.find(
            lambda m: m.author.id == self.user.id and m.content.startswith(marker), pins
        )
        if existing:
            if existing.content != body:
                await existing.edit(content=body)
            print("[bot] webhook URL 已在 command 頻道 (沿用釘選)")
            return
        msg = await dest.send(body)
        try:
            await msg.pin()
        except discord.Forbidden:
            print("[bot] 缺 Manage Messages 權限, 無法釘選 webhook 訊息")
        print("[bot] 已把 webhook URL 貼到 command 頻道並釘選")

    async def close(self) -> None:
        # Ctrl-C / 正常關閉時, discord.py 會呼叫這裡; 趁連線還在先送下線訊息
        try:
            await self._status(f"🔴 **Bot 下線** `{_now()}`")
        except Exception:
            pass
        await super().close()

    async def on_message(self, message: discord.Message):
        if message.channel.id != int(self.cfg["ingest_channel_id"]):
            return
        h = parse_ingest(message.content or "")
        if h is None:
            return
        try:
            await self.route(h)
        except Exception as e:
            print(f"[bot] route 失敗: {e}")
            return
        # 處理完就把 ingest 的原始 KSV1 訊息刪掉, 保持 ingest 乾淨 (需 Manage Messages)
        try:
            await message.delete()
        except discord.Forbidden:
            print("[bot] 提示: 缺 Manage Messages 權限, 無法清理 ingest 原始訊息")
        except Exception:
            pass

    async def route(self, h: dict):
        user = str(h.get("u") or "kiro-user")

        if h.get("k") == "Hello":  # client 上線: 立刻建 forum + 資訊 thread
            await self._hello(user, h.get("ts") or "")
            return

        session_id = str(h.get("s") or "")
        text = h.get("_text", "")
        title = h.get("title") or (session_id[:8] if session_id else "session")
        cwd = h.get("cwd") or ""

        forum = await self.ensure_forum(user)
        if forum is None:
            return
        thread = await self.ensure_thread(forum, session_id, title, cwd, user)
        if thread is None:
            return
        # title 常在第一則事件後才由 Kiro 產生; 一旦拿到就把貼文改成 title
        if h.get("title") and thread.name != title[:100]:
            try:
                await thread.edit(name=title[:100])
            except Exception as e:
                print(f"[bot] 重新命名 thread 失敗: {e}")
        for piece in chunk(text, THREAD_PIECE):
            await thread.send(piece)

    async def _hello(self, user: str, ts: str) -> None:
        forum = await self.ensure_forum(user)
        if forum is None:
            return
        sentinel = f"__info__:{user}"
        async with self._lock(f"thread:{sentinel}"):
            tid = self.store.get_thread(sentinel)
            if tid:
                th = self.get_channel(tid) or await self._fetch(tid)
                if isinstance(th, discord.Thread):
                    await th.send(f"🔌 重新連線 `{ts}`")
                    return
            created = await forum.create_thread(
                name=f"ℹ️ {user}"[:100],
                content=f"**使用者** {user}\n**論壇建立時間** `{ts}`",
            )
            thread = created.thread
            self.store.set_thread(sentinel, thread.id, forum.id)
            try:  # 把使用者資訊貼文釘選到論壇頂端 (需 Manage Threads)
                await thread.edit(pinned=True)
            except discord.Forbidden:
                print("[bot] 提示: 缺 Manage Threads 權限, 無法釘選 info 貼文")
            except Exception as e:
                print(f"[bot] 釘選 info 貼文失敗: {e}")
            print(f"[bot] 建立 info thread for {user} @ {ts}")

    async def ensure_forum(self, user_key: str) -> Optional[discord.ForumChannel]:
        async with self._lock(f"forum:{user_key}"):
            fid = self.store.get_forum(user_key)
            if fid:
                ch = self.get_channel(fid) or await self._fetch(fid)
                if isinstance(ch, discord.ForumChannel):
                    return ch  # 還在 -> 用它
            guild = self.get_guild(int(self.cfg["guild_id"]))
            if guild is None:
                print(f"[bot] 找不到 guild {self.cfg['guild_id']}")
                return None
            category = guild.get_channel(int(self.cfg["category_id"]))
            if not isinstance(category, discord.CategoryChannel):
                print(f"[bot] category_id {self.cfg['category_id']} 不是分類頻道")
                return None
            forum = await guild.create_forum(name=f"kiro-{user_key}"[:100], category=category)
            self.store.set_forum(user_key, forum.id)
            print(f"[bot] 建立 forum: kiro-{user_key} ({forum.id})")
            return forum

    async def ensure_thread(
        self, forum: discord.ForumChannel, session_id: str, title: str, cwd: str, user: str
    ) -> Optional[discord.Thread]:
        async with self._lock(f"thread:{session_id}"):
            tid = self.store.get_thread(session_id)
            if tid:
                th = self.get_channel(tid) or await self._fetch(tid)
                if isinstance(th, discord.Thread):
                    return th
            name = title or session_id[:8]  # 還沒 title 時暫用 id, 之後會自動改名
            now = datetime.datetime.now().astimezone().strftime("%Y/%m/%d %H:%M:%S")
            content = (
                f"Session name: {title or '(no title)'}\n"
                f"Session ID: {session_id}\n"
                f"Time: {now}\n"
                f"User: {user}"
            )
            created = await forum.create_thread(name=name[:100], content=content)
            thread = created.thread
            self.store.set_thread(session_id, thread.id, forum.id)
            print(f"[bot] 建立 thread: {name[:30]} ({thread.id}) in {forum.name}")
            await self._notify_info(user, thread, title, session_id, now)
            return thread

    async def _notify_info(
        self, user: str, thread: discord.Thread, title: str, session_id: str, when: str
    ) -> None:
        """在使用者的 info 貼文裡通知有新 thread 建立。"""
        tid = self.store.get_thread(f"__info__:{user}")
        if not tid:
            return
        info = self.get_channel(tid) or await self._fetch(tid)
        if not isinstance(info, discord.Thread):
            return
        try:
            await info.send(
                f"🆕 **新 thread 已建立** → <#{thread.id}>\n"
                f"Title: {title or '(no title)'}\n"
                f"Session ID: {session_id}\n"
                f"Time: {when}"
            )
        except Exception as e:
            print(f"[bot] info 通知失敗: {e}")

    async def _fetch(self, cid: int):
        try:
            return await self.fetch_channel(cid)
        except Exception:
            return None


def run_bot(cfg: dict, db_path) -> None:
    for k in ("bot_token", "guild_id", "category_id", "ingest_channel_id"):
        if not cfg.get(k):
            raise SystemExit(f".env 缺 {k.upper()}")
    store = Store(Path(db_path))
    KiroBot(cfg, store).run(cfg["bot_token"])
