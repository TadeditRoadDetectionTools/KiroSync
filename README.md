# KiroSync (ks)

把 Kiro CLI 的對話即時同步到 Discord。監看 `~/.kiro/sessions/cli/*.jsonl`,一個
session 對應一條 Discord thread。完整規劃見 [PLAN.md](PLAN.md)、原理見
[docs/原理筆記.md](docs/原理筆記.md)。

## 架構:兩個獨立部署

```
client (每位使用者機器)                Discord (公開中繼)              bot (你的主機)
┌────────────────┐   webhook POST   ┌──────────────┐   Gateway   ┌──────────────┐
│ watcher 監看   │ ───(向外)──────▶ │ #ingest 頻道 │ ◀──(向外)── │ 中央 Bot     │
│ .jsonl → 上行  │                  └──────────────┘             │ 建 forum/    │
└────────────────┘                                                │ thread、貼文 │
                                    forum: kiro-<user>            └──────────────┘
                                      ├ thread: session A
                                      └ thread: session B
```

兩機之間除了 Discord **沒有任何直接連線**,都只向外連 Discord,不需開 port/公開 IP。
每個資料夾**各自有自己的 `.env`**、`requirements.txt` 和進入點。

---

## 先決條件:Kiro CLI

client 監看的是 Kiro CLI 寫在本機的 session 檔,所以那台機器要裝好、用過 Kiro CLI。

- **確認已安裝**:`kiro-cli --version`(或你的執行檔名 `kiro`)。沒有的話到官方管道安裝並 `kiro-cli login`。
- **session 檔位置**(client 監看目標,預設自動抓):
  - Windows:`C:\Users\<你>\.kiro\sessions\cli\`
  - macOS/Linux:`~/.kiro/sessions/cli/`
  - 每個 session 有 `<id>.jsonl`(逐事件對話)、`<id>.json`(標題/cwd/model)、`<id>.lock`。
- **與 agent 無關**:不管你用哪個 agent(`kiro-cli chat`、`--agent xxx`),對話都會寫進上面的檔,client 照抓。不用改 Kiro 設定,也不用綁特定 agent。
- **查目前有哪些 session**:`cd client && python run.py sessions`,或 Kiro 內建 `kiro-cli chat --list-sessions -f json`。

---

## `bot/` — 中央 Bot(你的主機上跑一隻)

```bash
cd bot
pip install -r requirements.txt   # discord.py
cp .env.example .env              # 只需填 BOT_TOKEN / GUILD_ID / CATEGORY_ID
python run.py
```

### 一次性 Discord 設定
1. **開發者後台** → 你的 App → Bot → 開 **MESSAGE CONTENT INTENT**。
2. **邀 bot 進 server**,邀請連結要含 `bot` 和 `applications.commands` 兩個 scope
   (否則 slash 指令不會出現):
   ```
   https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot+applications.commands&permissions=0
   ```
3. **給 bot 這些權限**(角色或頻道層級):
   `Manage Channels`(建 forum / 頻道)、`Manage Webhooks`(建 webhook)、
   `Manage Messages`(釘選)、`Manage Threads`(釘選/改名)、
   `Send Messages` / `Send Messages in Threads` / `Create Posts`、
   `View Channels` / `Read Message History`。
4. 在 server 建一個**分類 (category)** 放 forum,把它的 ID 填進 `CATEGORY_ID`。

> **ingest / command 頻道與 webhook 都不用手動建。** bot 開機會自動:建一個隱藏的
> `kiro-ingest` 頻道 + 掛 webhook、建一個公開的 `kiro-command` 頻道,並把 webhook URL
> 貼到 command 頻道釘選。想自己指定就填 `INGEST_CHANNEL_ID` / `COMMAND_CHANNEL_ID`。

`.env` 欄位:`BOT_TOKEN`、`GUILD_ID`、`CATEGORY_ID`(必填);
`INGEST_CHANNEL_ID`、`COMMAND_CHANNEL_ID`(留空=自動建)、`DB_PATH`。

### Slash 指令
- `/ping` 檢查在線 · `/status` 論壇數/同步 session 數/運行時間 ·
  `/webhook` 取得 client 要填的 webhook URL · `/link <sid>` 取得某 session 搬移包下載連結 ·
  `/help` 說明。

---

## `client/` — 擷取 + 上行(每位使用者機器上跑)

零依賴(純標準庫)。

```bash
cd client
cp .env.example .env        # 填 WEBHOOK_URL / USER_NAME
python run.py sync          # 監看並上行 (預設只傳啟動後的新對話)
python run.py sync --backfill   # 連既有內容也傳
python run.py import <sid>  # 朔及既往: 把某個舊 session 重讀並上傳
python run.py export <sid>  # 搬移: 把某 session 打包 (zip) 上傳 Discord
python run.py pull <url>    # 搬移: 從 Discord 附件連結還原 session 到本機

# 不碰 Discord 的本機模式:
python run.py tail          # 即時印出 + 存 ks_client.db
python run.py once
python run.py sessions      # 列出本機 session
python run.py events <sid>
```

**`WEBHOOK_URL` 從哪來?** bot 上線後,到 Discord 的 `kiro-command` 頻道看**釘選訊息**,
或在任何頻道打 **`/webhook`**,把那條 URL 複製進 client 的 `.env`。

### 跨機搬移 session(export / pull)
把 A 機的某個 Kiro session 整包搬到 B 機,用 Discord 當中繼、不需兩機直連:

1. **A 機**:`python run.py export <sid>` — 把 `<sid>.jsonl` + `<sid>.json` 壓成一個 zip,
   透過 webhook 當附件上傳,落到該 session 的 Discord thread。
2. **取得連結**:在 Discord 打 `/link <sid>`(bot 掃該 session thread 裡最新的搬移包,
   回傳一條**現簽的**下載連結,直接附上要跑的 `pull` 指令)——或手動對 zip 附件「複製連結」。
3. **B 機**:`python run.py pull <連結>` — client 用 urllib 抓下 zip、解開寫回
   `~/.kiro/sessions/cli/`,Kiro CLI 就能接續這個 session。

> **落在本機資料夾**:session 的「屬於哪個資料夾」是存在 `<id>.json` 的 `cwd` 欄位(不是靠檔案位置)。
> 原封還原會沿用 A 機的 `cwd`;若 B 機路徑不同,加 `--cwd` 改寫,連 permissions 可讀/可寫路徑一起換:
> `python run.py pull <連結> --cwd "D:\work\myproj"`

> B 機一樣**零憑證**,只是把「複製一條 URL」的動作套用在附件上(跟貼 `WEBHOOK_URL` 同款)。
> 附件連結是 Discord CDN 的簽章連結,**約 24 小時後過期**,過期就回 Discord 重新複製即可。
> 打包後超過 8MB(含大量貼圖的 session)會略過並提示。

`.env` 欄位:`WEBHOOK_URL`、`USER_NAME`、`SYNC_WORKSPACES`(逗號分隔 cwd,空=全部)、
`WATCH_DIR`(留空=自動抓 `~/.kiro/sessions/cli`)、`POLL_INTERVAL`、
`SYNC_TOOLS`(1=連工具呼叫/結果一起同步,0=只同步純文字)、`DB_PATH`。

---

## 快速上手(單機自用)
1. `cd bot && pip install -r requirements.txt && cp .env.example .env`,填
   `BOT_TOKEN`/`GUILD_ID`/`CATEGORY_ID`,`python run.py`。
2. bot 上線後,`kiro-command` 頻道會有釘選的 webhook URL(或打 `/webhook`)。
3. `cd client && cp .env.example .env`,填 `WEBHOOK_URL`/`USER_NAME`,`python run.py sync`。
4. 開一場 Kiro 對話 → `kiro-<USER_NAME>` 論壇會自動長出對應 thread。

## 多人擴充
其他人不需要自己的 bot、也不用跟你的機器連線 —— 只要拿到那條 webhook URL,把
`WEBHOOK_URL` / `USER_NAME` 填進自己 `client/.env` 跑 `python run.py sync` 即可。
Bot 靠 payload 裡的 `USER_NAME` 分流到各自的 forum。
