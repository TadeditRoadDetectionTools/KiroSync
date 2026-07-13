# KiroSync (ks)

把 Kiro CLI 的對話即時同步到 Discord。監看 `~/.kiro/sessions/cli/*.jsonl`,一個
session 對應一條 Discord thread。完整規劃見 [PLAN.md](PLAN.md)、原理見
[docs/原理筆記.md](docs/原理筆記.md)。

## 架構:兩個獨立部署(client 只傳 raw,bot 端渲染)

```
client (每位使用者機器)                Discord (公開中繼)              bot (你的主機)
┌────────────────┐   webhook POST   ┌──────────────┐   Gateway   ┌──────────────┐
│ 監看 .jsonl    │ ──raw 快照(zip)─▶ │ #ingest 頻道 │ ◀──(向外)── │ 中央 Bot     │
│ 切片上傳快照   │    (向外)         └──────────────┘             │ 解析/渲染/   │
└────────────────┘                                                │ 貼圖、留快照 │
                                    forum: kiro-<user>            └──────────────┘
                                      ├ thread: session A
                                      └ thread: session B
```

**設計(架構 B)**:client **不做解析**,只把整個 session 打成 zip、依上限切片上傳;
**所有處理(格式化、切段、貼圖、渲染)都在 bot 端**(解析器見 [bot/kiroparse.py](bot/kiroparse.py))。
bot 收齊一個世代的分片後,只渲染 `.jsonl` 中**新增的行**(append-only),並**保留該世代的分片訊息**
當作「離線可拉取」的來源——所以**來源機關機時,別台仍可只靠 bot 拉回整個 session**(打 `/link <sid>`)。

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

### 用 Docker 跑 bot(建議上雲用)
bot 只需 outbound 連 Discord、不用開 port,容器化很乾淨。設定用環境變數餵
(`envcfg` 讓環境變數優先於 `.env`),`ks_bot.db` 導到 `/data` volume 保住 forum/thread 對應。

```bash
# 專案根目錄, 先填好 bot/.env (BOT_TOKEN / GUILD_ID / CATEGORY_ID)
docker compose up -d --build       # 讀 docker-compose.yml, 只起 bot 服務
docker compose logs -f bot         # 看 log
```
或不經 compose 直接跑:
```bash
docker build -t kirosync-bot ./bot
docker run -d --name kirosync-bot --env-file bot/.env \
  -v kirosync_bot_data:/data kirosync-bot
```
> 祕密與既有 `*.db` 不會進 image(見 `bot/.dockerignore`);`.env` 只在執行期以
> `env_file`/`--env-file` 餵進去。

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

零依賴(純標準庫)。client 只把 raw 快照上傳,不做解析(渲染在 bot 端)。

```bash
cd client
cp .env.example .env        # 填 WEBHOOK_URL / USER_NAME
python run.py sync          # 監看並把 raw 快照 (zip) 切片上傳 (bot 端渲染)
python run.py export <sid>  # 一次性把某 session 快照上傳 (供他機搬移)
python run.py pull <url...> # 搬移: 把 /link 給的連結(多片依序)還原 session 到本機

# 不碰 Discord 的本機模式:
python run.py tail          # 即時印出 + 存 ks_client.db
python run.py once
python run.py sessions      # 列出本機 session (需先跑過 tail/once)
python run.py events <sid>
```

**`WEBHOOK_URL` 從哪來?** bot 上線後,到 Discord 的 `kiro-command` 頻道看**釘選訊息**,
或在任何頻道打 **`/webhook`**,把那條 URL 複製進 client 的 `.env`。

### 跨機搬移 session(靠 bot 拉,來源可離線)
因為 `sync` 會持續把最新快照留在 bot,搬移不需要來源機在線:

1. **來源機**:平常 `python run.py sync` 就會持續上傳快照(或針對單一 session 跑
   `python run.py export <sid>` 立刻上傳一次)。
2. **取得連結**:在 Discord 打 `/link <sid>` — bot 回傳最新快照**各分片的現簽連結**,
   並直接組好要跑的 `pull` 指令。**來源機關機也拉得到**(快照存在 Discord 上)。
3. **目標機**:把 `/link` 給的指令貼上執行:
   ```
   python run.py pull "<片1>" "<片2>" ...
   ```
   client 依序併接分片 → 解開寫回 `~/.kiro/sessions/cli/`,Kiro CLI 就能接續這個 session。

> **落在本機資料夾**:session 的「屬於哪個資料夾」是存在 `<id>.json` 的 `cwd` 欄位(不是靠檔案位置)。
> 原封還原會沿用來源機的 `cwd`;若目標機路徑不同,加 `--cwd` 改寫,連 permissions 可讀/可寫路徑一起換:
> `python run.py pull <連結...> --cwd "D:\work\myproj"`

> **內容過大**:快照是 zip(文字壓縮率高),再依 `SNAP_CHUNK_MB`(預設 24MB,Discord 單附件約上限)
> 切片;過大只是**片數變多**,`/link` 會把每片都列出、`pull` 依序併接,沒有硬上限。
> 分片連結是 Discord CDN 簽章連結,但 `/link` 每次都**當場重簽**,不會拿到過期的。

`.env` 欄位:`WEBHOOK_URL`、`USER_NAME`、`SYNC_WORKSPACES`(逗號分隔 cwd,空=全部)、
`WATCH_DIR`(留空=自動抓 `~/.kiro/sessions/cli`)、`POLL_INTERVAL`、`DB_PATH`、
快照調校:`SNAP_CHUNK_MB`(切片上限)、`SNAP_DEBOUNCE`(停止變動幾秒後上傳)、
`SNAP_MIN_INTERVAL`(兩次上傳最小間隔)、`SNAP_MAX_WAIT`(持續變動時最遲上傳間隔)。
`SYNC_TOOLS` 已移到 **bot 端**(改用 bot 的 `INCLUDE_TOOLS`,因為渲染在 bot 做)。

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
