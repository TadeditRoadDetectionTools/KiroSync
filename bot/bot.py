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
import io
import json
import re
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
        self.ingest_id: Optional[int] = None   # 實際監聽的 ingest 頻道 (env 指定或 bot 自建)
        self.command_id: Optional[int] = None  # 狀態頻道 (public)
        self._webhook_url: Optional[str] = None
        self._started: Optional[datetime.datetime] = None
        self.tree = discord.app_commands.CommandTree(self)
        self._register_commands()

    def _lock(self, key: str) -> asyncio.Lock:
        lk = self._locks.get(key)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[key] = lk
        return lk

    def _register_commands(self) -> None:
        tree = self.tree

        @tree.command(name="ping", description="檢查 KiroSync bot 是否在線")
        async def _ping(interaction: discord.Interaction):
            await interaction.response.send_message("🟢 pong", ephemeral=True)

        @tree.command(name="status", description="顯示 KiroSync bot 狀態")
        async def _status_cmd(interaction: discord.Interaction):
            nf = self.store.db.execute("SELECT COUNT(*) AS n FROM user_forums").fetchone()["n"]
            nt = self.store.db.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE thread_id IS NOT NULL"
            ).fetchone()["n"]
            up = "?"
            if self._started:
                secs = int((datetime.datetime.now().astimezone() - self._started).total_seconds())
                up = f"{secs // 3600}h {secs % 3600 // 60}m"
            await interaction.response.send_message(
                f"🟢 上線中\n使用者論壇: {nf}\n同步的 session: {nt}\n運行時間: {up}",
                ephemeral=True,
            )

        @tree.command(name="webhook", description="取得 client 用的 webhook URL")
        async def _webhook(interaction: discord.Interaction):
            url = self._webhook_url or self.store.get_kv("webhook_url")
            if not url:
                await interaction.response.send_message("尚未建立 webhook。", ephemeral=True)
                return
            await interaction.response.send_message(
                f"把這條填進 client 的 `.env` 的 `WEBHOOK_URL`:\n{url}", ephemeral=True
            )

        @tree.command(name="help", description="列出 KiroSync 指令")
        async def _help(interaction: discord.Interaction):
            await interaction.response.send_message(
                "**KiroSync 指令**\n"
                "`/ping` 檢查在線\n"
                "`/status` bot 狀態\n"
                "`/webhook` 取得 client webhook URL\n"
                "`/help` 這個說明",
                ephemeral=True,
            )

    async def _status(self, text: str) -> None:
        if not self.command_id:
            return
        try:
            ch = self.get_channel(self.command_id) or await self._fetch(self.command_id)
            if ch is not None:
                await ch.send(text)
        except Exception as e:
            print(f"[bot] 狀態訊息送失敗: {e}")

    async def on_ready(self):
        print(f"[bot] 已登入: {self.user}")
        if self.ingest_id is None or self.command_id is None:
            await self._resolve_channels()
        if not self._announced:
            self._announced = True
            self._started = datetime.datetime.now().astimezone()
            try:  # 註冊 slash commands 到本 guild (guild-scoped 即時生效)
                g = discord.Object(id=int(self.cfg["guild_id"]))
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
                print("[bot] slash commands 已同步")
            except Exception as e:
                print(f"[bot] slash 同步失敗 (是否用 applications.commands scope 邀請?): {e}")
            await self._status(f"🟢 **Bot 上線** `{_now()}`")
            await self._publish_webhook()
        else:
            await self._status(f"🔁 **Bot 重新連線** `{_now()}`")

    async def _resolve_channels(self) -> None:
        """解析 ingest 與 command 頻道 (env 指定 > bot 之前自建 > 現在自建)。"""
        self.ingest_id = await self._resolve_channel(
            "ingest_channel_id", self.cfg.get("ingest_channel_id"),
            "kiro-ingest", "KiroSync 上行中繼頻道 (bot 自動建立, 勿手動貼文)",
            public=False,
        )
        print(f"[bot] 監聽 ingest 頻道 {self.ingest_id}")
        self.command_id = await self._resolve_channel(
            "command_channel_id", self.cfg.get("command_channel_id"),
            "kiro-command", "KiroSync 狀態與指令頻道",
            public=True,
        )
        print(f"[bot] command 頻道 {self.command_id}")
        # 確保 command 頻道 (即使是既有的) 開放 @everyone 檢視 + 發言
        if self.command_id:
            ch = self.get_channel(self.command_id) or await self._fetch(self.command_id)
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.set_permissions(
                        ch.guild.default_role, view_channel=True, send_messages=True
                    )
                except Exception as e:
                    print(f"[bot] 設定 command 頻道發言權限失敗: {e}")

    async def _resolve_channel(
        self, kv_key: str, env_id, name: str, topic: str, *, public: bool
    ) -> Optional[int]:
        # 1. .env 指定
        if env_id:
            ch = self.get_channel(int(env_id)) or await self._fetch(int(env_id))
            if isinstance(ch, discord.TextChannel):
                return ch.id
        # 2. bot 之前自建的 (存在 kv)
        saved = self.store.get_kv(kv_key)
        if saved:
            ch = self.get_channel(int(saved)) or await self._fetch(int(saved))
            if isinstance(ch, discord.TextChannel):
                print(f"[bot] 沿用自建頻道 {name} ({ch.id})")
                return ch.id
        # 3. 現在自建一個隱藏頻道
        guild = self.get_guild(int(self.cfg["guild_id"]))
        if guild is None:
            print("[bot] 找不到 guild, 無法建頻道")
            return None
        category = None
        if self.cfg.get("category_id"):
            c = guild.get_channel(int(self.cfg["category_id"]))
            if isinstance(c, discord.CategoryChannel):
                category = c
        me_perm = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_messages=True, manage_webhooks=True,
        )
        if public:  # 大家看得到也能發言
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True
                ),
                guild.me: me_perm,
            }
        else:  # ingest: 純管線, 只有 bot 看得到
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: me_perm,
            }
        try:
            ch = await guild.create_text_channel(
                name, category=category, overwrites=overwrites, topic=topic
            )
        except discord.Forbidden:
            print(f"[bot] 缺 Manage Channels 權限, 無法自建 {name}")
            return None
        self.store.set_kv(kv_key, str(ch.id))
        print(f"[bot] 自建隱藏頻道: #{ch.name} ({ch.id})")
        return ch.id

    async def _publish_webhook(self) -> None:
        """在 ingest 頻道自動建/取 webhook, 把 URL 貼到 command 頻道並釘選。"""
        if self.ingest_id is None:
            print("[bot] 尚無 ingest 頻道, 無法建 webhook")
            return
        ingest = self.get_channel(self.ingest_id) or await self._fetch(self.ingest_id)
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
        self._webhook_url = hook.url
        self.store.set_kv("webhook_url", hook.url)
        print(f"[bot] webhook 就緒: {hook.url}")

        dest = None
        if self.command_id:
            dest = self.get_channel(self.command_id) or await self._fetch(self.command_id)
        if not isinstance(dest, (discord.TextChannel, discord.Thread)):
            return
        # command 頻道公開; 直接把 URL 貼出來並釘選 (已存在就更新)
        marker = "🔗 **Client Webhook URL**"
        body = f"{marker}\n把這條填進 client 的 `.env` 的 `WEBHOOK_URL`:\n{hook.url}"
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
        if self.ingest_id is None or message.channel.id != self.ingest_id:
            return
        h = parse_ingest(message.content or "")
        if h is None:
            return
        try:
            await self.route(h, message)
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

    async def route(self, h: dict, message: "discord.Message" = None):
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
        # 依角色加標籤: Prompt -> user, AssistantMessage -> response
        text = re.sub(r"\n{3,}", "\n\n", text.strip())  # 壓掉連續 3+ 空行
        label = {"Prompt": "user", "AssistantMessage": "response",
                 "ToolResults": "🔧 tool"}.get(h.get("k"), "")
        if text:
            body = f"{label}:\n{text}" if label else text
            for piece in chunk(body, THREAD_PIECE):
                await thread.send(piece)

        # 轉貼附件 (貼圖) 到 thread
        if message and message.attachments:
            cap = f"{label}: 🖼️ 附件" if label else "🖼️ 附件"
            first = True
            for att in message.attachments:
                try:
                    raw = await att.read()
                    await thread.send(
                        content=(cap if first else None),
                        file=discord.File(io.BytesIO(raw), filename=att.filename),
                    )
                    first = False
                except Exception as e:
                    print(f"[bot] 附件轉貼失敗: {e}")

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
    for k in ("bot_token", "guild_id", "category_id"):  # ingest 可留空, bot 會自建
        if not cfg.get(k):
            raise SystemExit(f".env 缺 {k.upper()}")
    store = Store(Path(db_path))
    KiroBot(cfg, store).run(cfg["bot_token"])
