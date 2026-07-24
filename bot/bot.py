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
import time
from pathlib import Path
from typing import Literal, Optional

import discord

import gemini
import summary as summarize_mod
from kiroparse import parse_line
from store import Store
from util import chunk, read_snapshot_zip

THREAD_PIECE = 1990  # thread 內單則訊息上限 2000
REPORT_PIECE = 1900  # 報告訊息切段 (留給前後綴)
REPORT_AS_FILE = 6000  # 超過這個長度改用 .md 附件, 不刷屏


def _now() -> str:
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def member_is_admin(member) -> bool:
    """Discord 原生管理權限 (Administrator 或 Manage Guild)。"""
    perms = getattr(member, "guild_permissions", None)
    if perms is None:
        return False
    return bool(getattr(perms, "administrator", False)
                or getattr(perms, "manage_guild", False))


def member_may_summary(member, allowed_role_ids) -> bool:
    """能不能用 /summary: 原生管理權限, 或持有被指定的身分組。

    不用 default_permissions 擋 —— 那是 Discord 在指令層直接擋掉沒有管理權的人,
    白名單身分組就永遠進不來了; 所以權限一律在 runtime 判定。"""
    if member_is_admin(member):
        return True
    allowed = set(allowed_role_ids or [])
    if not allowed:
        return False
    return any(getattr(r, "id", None) in allowed
               for r in (getattr(member, "roles", None) or []))


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
        self.include_tools = bool(cfg.get("include_tools", True))
        self.gemini_key = cfg.get("gemini_api_key") or ""
        self.gemini_models = gemini.parse_models(cfg.get("gemini_models"))
        self._locks: dict[str, asyncio.Lock] = {}
        # 快照分片重組緩衝: (session_id, gen) -> {part: bytes} / {part: message_id}
        self._snap_buf: dict[tuple, dict] = {}
        self._snap_msgs: dict[tuple, dict] = {}
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
            await interaction.response.send_message("pong", ephemeral=True)

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
                f"上線中\n使用者論壇: {nf}\n同步的 session: {nt}\n運行時間: {up}",
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

        @tree.command(name="link",
                      description="取得指定 session 的搬移下載連結 (需先在來源機 export)")
        @discord.app_commands.describe(session_id="session id (可只給前幾碼)")
        async def _link(interaction: discord.Interaction, session_id: str):
            await interaction.response.defer(ephemeral=True)
            await self._session_link(interaction, session_id)

        @tree.command(name="summary", description="彙整 session 變化摘要 (需管理權限或指定身分組)")
        @discord.app_commands.describe(
            scope="範圍: all=全部使用者 / user=某使用者 / session=某 session",
            target="scope=user 時填使用者名; scope=session 時填 session id (可只給前幾碼)",
            since="起始日期 YYYY-MM-DD; 省略 = 從上次總結之後到現在",
        )
        async def _summary(
            interaction: discord.Interaction,
            scope: Literal["all", "user", "session"] = "all",
            target: Optional[str] = None,
            since: Optional[str] = None,
        ):
            if not member_may_summary(interaction.user, self.store.get_summary_roles()):
                await interaction.response.send_message(
                    "只有管理員或被指定身分組可以用 `/summary`。", ephemeral=True)
                return
            await interaction.response.defer()  # 彙整+LLM 會超過 3 秒
            await self._run_summary(interaction, scope, target, since)

        @tree.command(name="summary-check",
                      description="檢查 /summary 服務是否可用 (需管理權限或指定身分組)")
        async def _summary_check(interaction: discord.Interaction):
            if not member_may_summary(interaction.user, self.store.get_summary_roles()):
                await interaction.response.send_message(
                    "只有管理員或被指定身分組可以用 `/summary-check`。", ephemeral=True)
                return
            await interaction.response.defer()  # 會真的打一次 Gemini
            await self._run_summary_check(interaction)

        @tree.command(name="summary-access",
                      description="管理可用 /summary 的身分組 (需管理權限)")
        @discord.app_commands.describe(action="add=加入 / remove=移除 / list=列出",
                                       role="要加入或移除的身分組")
        async def _summary_access(
            interaction: discord.Interaction,
            action: Literal["add", "remove", "list"] = "list",
            role: Optional[discord.Role] = None,
        ):
            if not member_is_admin(interaction.user):
                await interaction.response.send_message(
                    "只有管理員可以改 `/summary` 的授權身分組。", ephemeral=True)
                return
            roles = self.store.get_summary_roles()
            if action in ("add", "remove"):
                if role is None:
                    await interaction.response.send_message(
                        f"請指定要 {action} 的身分組。", ephemeral=True)
                    return
                s = set(roles)
                s.add(role.id) if action == "add" else s.discard(role.id)
                self.store.set_summary_roles(s)
                roles = self.store.get_summary_roles()
            listing = "\n".join(f"• <@&{r}>" for r in roles) or "(無, 只有管理員可用)"
            await interaction.response.send_message(
                f"**可用 `/summary` 的身分組**\n{listing}", ephemeral=True)

        @tree.command(name="help", description="列出 KiroSync 指令")
        async def _help(interaction: discord.Interaction):
            await interaction.response.send_message(
                "**KiroSync 指令**\n"
                "`/ping` 檢查在線\n"
                "`/status` bot 狀態\n"
                "`/webhook` 取得 client webhook URL\n"
                "`/link <sid>` 取得某 session 搬移包的下載連結 (先在來源機 export)\n"
                "`/summary [scope] [target] [since]` 彙整 session 變化摘要 (管理員)\n"
                "`/summary-check` 檢查 /summary 服務是否可用 (管理員)\n"
                "`/summary-access` 管理可用 /summary 的身分組 (管理員)\n"
                "`/help` 這個說明",
                ephemeral=True,
            )

    async def _session_link(self, interaction: "discord.Interaction", session_id: str) -> None:
        """回傳指定 session 最新快照各分片的下載連結。連結由 Discord 在此刻讀取時
        重新簽章, 所以每次都是新鮮的 (不會過期)。來源 client 關機也拉得到。"""
        sid = (session_id or "").strip()
        matches = self.store.find_sessions(sid)
        if not matches:
            await interaction.followup.send(
                f"找不到符合 `{sid}` 的 session (它同步過嗎?)", ephemeral=True)
            return
        if len(matches) > 1:
            listing = "\n".join(f"• `{m}`" for m in matches[:10])
            await interaction.followup.send(
                f"符合多個 session, 請給更完整的 id:\n{listing}", ephemeral=True)
            return
        full_sid = matches[0]
        snap = self.store.get_snapshot(full_sid)
        if not snap or not snap.get("msg_ids"):
            await interaction.followup.send(
                f"`{full_sid[:8]}` 還沒有快照。請先在來源機跑 `python run.py sync` "
                f"(或 `export {full_sid[:8]}`)。", ephemeral=True)
            return
        urls = [a.url for a in await self._snapshot_parts(full_sid)]  # 依片序取現簽 URL
        if not urls:
            await interaction.followup.send(
                f"`{full_sid[:8]}` 的快照分片已不可用, 請在來源機重跑 sync/export。",
                ephemeral=True)
            return
        joined = " ".join(f'"{u}"' for u in urls)
        cmd = f"python run.py pull {joined}"
        header = f"**Session 搬移包** `{full_sid[:8]}` ({len(urls)} 片)  加 `--cwd \"<路徑>\"` 可換資料夾"
        if len(header) + len(cmd) + 12 <= 1990:
            await interaction.followup.send(f"{header}\n```\n{cmd}\n```", ephemeral=True)
        else:  # 片太多、指令太長 -> 分段送純文字, 使用者自行接成一行
            await interaction.followup.send(
                f"{header}\n指令較長, 分段如下, 請接成一整行執行:", ephemeral=True)
            for pc in chunk(cmd, 1900):
                await interaction.followup.send(pc, ephemeral=True)

    async def _run_summary(self, interaction: "discord.Interaction", scope: str,
                           target: Optional[str], since: Optional[str]) -> None:
        """彙整指定範圍內「基準點之後」的新增對話, 產生摘要報告貼回頻道。

        資料一律從 Discord 上的快照分片拉回 (跟 /link 同一條路), 所以來源機離線也能跑。"""
        # 1. 基準點: 有 since 就用日期, 沒有就用各 session 自己的游標
        since_ts = None
        if since:
            try:
                since_ts = summarize_mod.parse_since(since)
            except ValueError:
                await interaction.followup.send(
                    f"日期格式看不懂: `{since}` (要 YYYY-MM-DD, 例如 2026-07-16)")
                return
        since_label = f"{since} 起" if since else "距上次總結"

        # 2. 範圍
        if scope == "session":
            if not target:
                await interaction.followup.send("scope=session 要用 `target` 指定 session id。")
                return
            rows = self.store.list_sessions(prefix=target)
        elif scope == "user":
            if not target:
                await interaction.followup.send("scope=user 要用 `target` 指定使用者名。")
                return
            rows = self.store.list_sessions(user_key=target)
        else:
            rows = self.store.list_sessions()
        if not rows:
            await interaction.followup.send(
                f"找不到符合的 session (scope={scope}"
                + (f", target=`{target}`" if target else "") + ")。")
            return

        # 3. 逐 session: 拉快照 -> 取新增行 -> 統計 -> LLM 摘要
        entries, skipped = [], []
        models, used_models = list(self.gemini_models), []
        for row in rows:
            sid = row["session_id"]
            blob = await self._snapshot_blob(sid)
            if blob is None:
                skipped.append(sid)
                continue
            got = read_snapshot_zip(blob)
            if got is None:
                skipped.append(sid)
                continue
            jsonl_text, meta = got
            lines = jsonl_text.splitlines()
            start = summarize_mod.select_start(
                lines, cursor=self.store.get_summarized(sid), since_ts=since_ts)
            new_lines = lines[start:]
            if not new_lines:
                continue  # 這個 session 在區間內沒有新內容
            title = meta.get("title") or sid[:8]
            text = None
            if self.gemini_key:
                transcript = summarize_mod.render_transcript(
                    new_lines, include_tools=self.include_tools)
                if transcript:
                    text, used, errs = await gemini.call_chain(
                        summarize_mod.build_prompt(
                            transcript, title=title, cwd=meta.get("cwd")),
                        api_key=self.gemini_key, models=models)
                    for m, e in errs:
                        print(f"[bot] gemini `{m}` 不可用: {e}")
                    if used:
                        if used not in used_models:
                            used_models.append(used)
                        if models[0] != used:
                            # 前面的 model 這輪不通, 把成功的挪到最前面 —— 否則報告裡
                            # 每個 session 都會再白試一次那些已知不通的 model。
                            models.remove(used)
                            models.insert(0, used)
            entries.append({
                "user_key": row.get("user_key"), "session_id": sid,
                "title": title, "stats": summarize_mod.collect_stats(new_lines),
                "summary": text,
            })
            # 游標一律推到檔尾: 兩種模式的終點都是「現在」
            self.store.set_summarized(sid, len(lines))

        # 4. 出報告 (標明實際用的是哪個 model —— 有退過的話不只一個)
        if not self.gemini_key:
            note = "未設定 GEMINI_API_KEY, 只出統計 (無語意摘要)"
        elif used_models:
            note = "摘要引擎: " + ", ".join(f"`{m}`" for m in used_models)
        elif entries:
            note = "摘要引擎全部不可用, 只出統計 — 打 `/summary-check` 看原因"
        else:
            note = ""
        scope_label = {"user": f"使用者 `{target}`",
                       "session": f"session `{target}`"}.get(scope, "全部使用者")
        report = summarize_mod.format_report(
            entries, scope_label=scope_label, since_label=since_label,
            note=note, skipped=skipped)
        await self._send_report(interaction, report)

    async def _check_engine(self) -> dict:
        """實際打一次 Gemini 驗證摘要引擎; 失敗要把原因報出來, 不能只說「不可用」。"""
        name = "摘要引擎 (Gemini)"
        if not self.gemini_key:
            return summarize_mod.check(
                name, summarize_mod.WARN,
                "未設定 GEMINI_API_KEY — `/summary` 仍可用, 但只出統計卡",
                "要語意摘要請在 bot/.env 填 GEMINI_API_KEY 後重啟 bot")
        t0 = time.monotonic()
        text, model, errors = await gemini.call_chain(
            "回覆 OK 兩個字。", api_key=self.gemini_key, models=self.gemini_models)
        ms = int((time.monotonic() - t0) * 1000)
        detail = "; ".join(f"跳過 `{m}`: {e}" for m, e in errors)
        chain = " → ".join(f"`{m}`" for m in self.gemini_models)
        if model is None:
            return summarize_mod.check(
                name, summarize_mod.FAIL,
                f"{len(errors)} 個 model 全部不可用 — `/summary` 會降級成只出統計卡 "
                f"(依序試過: {chain})", detail)
        if errors:
            # 能用但退過: 前面那些每次呼叫都會白試一輪, 值得講出來讓人去掉
            return summarize_mod.check(
                name, summarize_mod.WARN,
                f"可用 · 退到第 {len(errors) + 1} 個 model=`{model}` · {ms}ms — "
                f"前面 {len(errors)} 個每次都會白試一輪, 建議從 GEMINI_MODELS 移除",
                detail)
        return summarize_mod.check(
            name, summarize_mod.OK, f"可用 · model=`{model}` · {ms}ms")

    async def _check_snapshots(self) -> dict:
        """驗快照來源。DB 有記錄 != 分片還在, 所以真的抓一個回來走完整條路。"""
        name = "快照來源"
        rows = self.store.list_sessions()
        if not rows:
            return summarize_mod.check(
                name, summarize_mod.WARN, "還沒有任何 session — 沒東西可總結",
                "在使用者機器上跑 `python run.py sync` 之後就會有")
        snap = [r["session_id"] for r in rows if self.store.get_snapshot(r["session_id"])]
        if not snap:
            return summarize_mod.check(
                name, summarize_mod.FAIL,
                f"{len(rows)} 個 session 都沒有快照, 無法總結",
                "請在來源機重跑 `python run.py sync` (或 `export <sid>`)")
        blob = await self._snapshot_blob(snap[0])
        if blob is None or read_snapshot_zip(blob) is None:
            return summarize_mod.check(
                name, summarize_mod.FAIL,
                f"抓不回 `{snap[0][:8]}` 的快照分片 — 總結會略過這些 session",
                "DB 有記錄但 ingest 頻道的分片訊息已不可用 (被刪?), 請在來源機重跑 sync")
        status = summarize_mod.OK if len(snap) == len(rows) else summarize_mod.WARN
        return summarize_mod.check(
            name, status,
            f"{len(snap)}/{len(rows)} 個 session 有快照 · 實際抓取驗證通過"
            + ("" if status == summarize_mod.OK else " (其餘會被略過)"))

    def _check_access(self) -> dict:
        roles = self.store.get_summary_roles()
        who = "Discord 管理員 (Administrator / Manage Guild)"
        if roles:
            who += " + 身分組 " + ", ".join(f"<@&{r}>" for r in roles)
        return summarize_mod.check(
            "授權", summarize_mod.OK, f"可用者: {who}",
            "" if roles else "用 `/summary-access add` 可以額外授權身分組")

    async def _run_summary_check(self, interaction: "discord.Interaction") -> None:
        """檢查 /summary 的三個前提: 摘要引擎、快照來源、授權設定。"""
        checks = []
        for probe in (self._check_engine(), self._check_snapshots()):
            try:
                checks.append(await probe)
            except Exception as e:  # 檢查本身壞掉也要報, 不能讓指令沒有回應
                checks.append(summarize_mod.check(
                    "檢查", summarize_mod.FAIL, "檢查過程出錯", f"{type(e).__name__}: {e}"))
        checks.append(self._check_access())
        await self._send_report(interaction, summarize_mod.format_health(checks))

    async def _send_report(self, interaction: "discord.Interaction", report: str) -> None:
        """送報告: 短的切段直接貼, 長的改成 .md 附件避免刷屏。"""
        if len(report) > REPORT_AS_FILE:
            fp = io.BytesIO(report.encode("utf-8"))
            stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M")
            await interaction.followup.send(
                "**KiroSync 總結** (內容較長, 見附件)",
                file=discord.File(fp, filename=f"summary-{stamp}.md"))
            return
        for piece in chunk(report, REPORT_PIECE):
            await interaction.followup.send(piece)

    async def _snapshot_parts(self, session_id: str) -> list:
        """取某 session 最新快照各分片的附件物件 (依片序)。分片訊息已不可用回 []。
        `/link` 用它取現簽 URL, `/summary` 用它讀回位元組 —— 兩邊同一條路。"""
        snap = self.store.get_snapshot(session_id)
        if not snap or not snap.get("msg_ids"):
            return []
        cid = snap.get("channel_id")
        ch = self.get_channel(int(cid)) or await self._fetch(int(cid))
        if ch is None:
            return []
        out = []
        for mid in snap["msg_ids"]:
            try:
                msg = await ch.fetch_message(int(mid))
                if msg.attachments:
                    out.append(msg.attachments[0])
            except Exception:
                pass
        return out

    async def _snapshot_blob(self, session_id: str) -> Optional[bytes]:
        """把某 session 最新快照的分片下載併回原 zip; 不可用回 None。"""
        parts = await self._snapshot_parts(session_id)
        if not parts:
            return None
        try:
            return b"".join([await p.read() for p in parts])
        except Exception as e:
            print(f"[bot] 讀快照分片失敗 {session_id[:8]}: {e}")
            return None

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
            await self._status(f"**Bot 上線** `{_now()}`")
            await self._publish_webhook()
        else:
            await self._status(f"**Bot 重新連線** `{_now()}`")

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
        marker = "**Client Webhook URL**"
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
            await self._status(f"**Bot 下線** `{_now()}`")
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
            keep = await self.route(h, message)
        except Exception as e:
            print(f"[bot] route 失敗: {e}")
            return
        if keep:
            return  # 快照分片要保留當「離線可拉取」來源, 不刪 (舊世代另行清理)
        # 其餘控制訊息處理完就刪掉, 保持 ingest 乾淨 (需 Manage Messages)
        try:
            await message.delete()
        except discord.Forbidden:
            print("[bot] 提示: 缺 Manage Messages 權限, 無法清理 ingest 原始訊息")
        except Exception:
            pass

    async def route(self, h: dict, message: "discord.Message" = None) -> bool:
        """處理一則 ingest。回傳 True 表示「此訊息需保留」(快照分片)。"""
        user = str(h.get("u") or "kiro-user")
        kind = h.get("k")
        if kind == "Hello":  # client 上線: 立刻建 forum + 資訊 thread
            await self._hello(user, h.get("ts") or "")
            return False
        if kind == "Snap":
            return await self._on_snap(h, message)
        return False  # 舊格式 / 未知 kind: 忽略

    async def _on_snap(self, h: dict, message: "discord.Message") -> bool:
        """收集一個 session 快照的分片; 收齊就重組 zip 並套用。回傳 True 保留此片。"""
        if message is None or not message.attachments:
            return False
        s = str(h.get("s") or "")
        if not s:
            return False
        gen = str(h.get("g"))
        try:
            p, n = int(h.get("p", 0)), int(h.get("n", 1))
        except (TypeError, ValueError):
            return False
        try:
            data = await message.attachments[0].read()
        except Exception as e:
            print(f"[bot] 讀 snap 附件失敗: {e}")
            return False
        key = (s, gen)
        # client 對同一 session 是依序送世代的; 新世代的片到了, 代表更舊世代已確定
        # 收不齊 (有片上傳失敗) — 清掉殘片, 讓 _snap_buf 有界, 不會無限堆積
        for stale in [k for k in self._snap_buf if k[0] == s and k != key]:
            self._snap_buf.pop(stale, None)
            self._snap_msgs.pop(stale, None)
        self._snap_buf.setdefault(key, {})[p] = data
        self._snap_msgs.setdefault(key, {})[p] = message.id
        if len(self._snap_buf[key]) < n:
            return True  # 還沒收齊
        try:
            blob = b"".join(self._snap_buf[key][i] for i in range(n))
            ordered_ids = [self._snap_msgs[key][i] for i in range(n)]
        except KeyError:
            return True  # 片不連續(理論上不會), 再等
        self._snap_buf.pop(key, None)
        self._snap_msgs.pop(key, None)
        try:
            await self._apply_snapshot(h, s, gen, blob, ordered_ids, message.channel.id)
        except Exception as e:
            print(f"[bot] 套用快照失敗 {s[:8]}: {e}")
        return True  # 這些片保留當 pull 來源 (舊世代在 _apply_snapshot 內刪)

    async def _apply_snapshot(
        self, h: dict, s: str, gen: str, blob: bytes, msg_ids: list, channel_id: int
    ) -> None:
        user = str(h.get("u") or "kiro-user")
        got = read_snapshot_zip(blob)
        if got is None:
            print(f"[bot] snap {s[:8]} 併回不是有效 zip, 略過 (可能上傳不完整)")
            return
        jsonl_text, m = got
        title = h.get("title") or m.get("title") or s[:8]
        cwd = h.get("cwd") or m.get("cwd") or ""

        forum = await self.ensure_forum(user)
        if forum is None:
            return
        thread = await self.ensure_thread(forum, s, title, cwd, user)
        if thread is None:
            return
        if (h.get("title") or m.get("title")) and thread.name != title[:100]:
            try:
                await thread.edit(name=title[:100])
            except Exception as e:
                print(f"[bot] 重新命名 thread 失敗: {e}")

        # 渲染: .jsonl append-only, 只貼「超過已貼行數」的新行
        lines = jsonl_text.splitlines()
        rendered = self.store.get_rendered(s)
        total = len(lines)
        if rendered > total:
            # 行數倒退 = session 檔被重寫變短 (append-only 假設被打破)。已貼的訊息
            # 收不回來, 但進度要重設到新檔尾端, 否則之後的新行永遠不會再渲染。
            print(f"[bot] {s[:8]} 行數倒退 ({rendered} -> {total}), 重設渲染進度")
            self.store.set_rendered(s, total)
            rendered = total
        for i in range(rendered, total):
            ev = parse_line(lines[i], i, self.include_tools)
            if ev is not None:
                await self._post_event(thread, ev)
        if total > rendered:
            self.store.set_rendered(s, total)

        # 更新「可離線拉取」指標: 記本世代分片, 刪掉已被取代的舊分片 (容量有界)。
        # 不只比 gen — client 重啟後會把同一世代重送一次, 訊息 id 是新的,
        # 舊的同世代分片一樣是孤兒, 不刪會在 ingest 頻道慢慢堆積。
        old = self.store.get_snapshot(s)
        self.store.set_snapshot(s, gen, channel_id, msg_ids)
        if old:
            current = set(msg_ids)
            stale = [m for m in (old.get("msg_ids") or []) if m not in current]
            if stale:
                await self._delete_msgs(old.get("channel_id"), stale)

    async def _post_event(self, thread: "discord.Thread", ev: dict) -> None:
        """把一個解析後事件貼到 thread: 文字(加角色標籤/切段) + 圖片附件。"""
        label = {"Prompt": "user", "AssistantMessage": "response",
                 "ToolResults": "🔧 tool"}.get(ev.get("kind"), "")
        text = ev.get("text")
        if text:
            text = re.sub(r"\n{3,}", "\n\n", text.strip())
            body = f"{label}:\n{text}" if label else text
            for piece in chunk(body, THREAD_PIECE):
                await thread.send(piece)
        for att in ev.get("attachments") or []:
            try:
                await thread.send(
                    content=(f"{label}: 🖼️ 附件" if label else "🖼️ 附件"),
                    file=discord.File(io.BytesIO(att["data"]), filename=att["filename"]),
                )
            except Exception as e:
                print(f"[bot] 附件貼失敗: {e}")

    async def _delete_msgs(self, channel_id, msg_ids: list) -> None:
        if not channel_id or not msg_ids:
            return
        ch = self.get_channel(int(channel_id)) or await self._fetch(int(channel_id))
        if ch is None:
            return
        for mid in msg_ids:
            try:
                msg = await ch.fetch_message(int(mid))
                await msg.delete()
            except Exception:
                pass

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
                    await th.send(f"重新連線 `{ts}`")
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
                f"**新 thread 已建立** → <#{thread.id}>\n"
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
    # ingest / command 頻道可留空 (bot 會自建), 其餘必填。一次列出全部缺的,
    # 不要少填幾個就讓人重啟幾次才看完。
    missing = [k.upper() for k in ("bot_token", "guild_id", "category_id") if not cfg.get(k)]
    if missing:
        raise SystemExit(
            "缺少必填設定: " + ", ".join(missing) + "\n"
            "來源可以是 bot/.env 或環境變數 (環境變數優先)。\n"
            "跑 Docker 的話: image 裡沒有 .env, 設定是 compose 用 env_file 從 bot/.env "
            "餵成環境變數的 — 要改請改 bot/.env 再 `docker compose up -d` (不用重 build)。"
        )
    store = Store(Path(db_path))
    KiroBot(cfg, store).run(cfg["bot_token"])
