# KiroSync (ks)

把 Kiro CLI 的對話即時同步到 Discord。監看 `~/.kiro/sessions/cli/*.jsonl`,一個
session 對應一條 Discord thread,並支援用 Discord 當中繼在多台機器之間搬移 session。

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

## 測試

純標準庫 `unittest`,**零依賴** —— 不用裝 pytest,也不用裝 discord.py
(`tests/_stub_discord.py` 提供假的 discord 讓 `bot/bot.py` 可以被 import)。
從**專案根目錄**跑:

```bash
python3 -m unittest discover -s tests        # 加 -v 看逐條
```

涵蓋 KSV1 wire format 的 round-trip(client 產生 ↔ bot 解析)、`.jsonl` 解析、快照
分片/併回、`cwd` 改寫、store 與 envcfg。**沒有**涵蓋 Discord API 互動與 `sync` 的
debounce 迴圈 —— 那些仍要接真的 guild 驗。沒有 lint 或 CI。

---

## 先決條件:Kiro CLI

client 監看的是 Kiro CLI 寫在本機的 session 檔,所以那台機器要裝好、用過 Kiro CLI。

- **確認已安裝**:`kiro-cli --version`。沒有的話到官方管道安裝,並且**一定要 `kiro-cli login`
  再至少開一場對話** —— session 目錄是第一次對話才建出來的,沒登入過的機器連
  `~/.kiro/sessions/` 都不存在,client 會盯著空目錄什麼也不做。
- **session 檔位置**(client 監看目標,預設自動抓;已對 Kiro CLI 2.12.1 實測確認):
  - Windows:`C:\Users\<你>\.kiro\sessions\cli\`
  - macOS/Linux:`~/.kiro/sessions/cli/`
  - 每個 session 有 `<id>.jsonl`(逐事件對話)、`<id>.json`(標題/cwd/model)、`<id>.lock`。
- **如果你設過 `KIRO_HOME`**:Kiro 用它取代 `~/.kiro`,但 **client 目前不會讀這個變數**,
  要自己把 `WATCH_DIR` 指到 `$KIRO_HOME/sessions/cli`,否則 client 會找錯地方。
- **與 agent 無關**:不管你用哪個 agent(`kiro-cli chat`、`--agent xxx`),對話都會寫進上面的檔,client 照抓。不用改 Kiro 設定,也不用綁特定 agent。
- **查目前有哪些 session**:`cd client && python run.py sessions`(直接讀上面那個目錄),
  或 Kiro 內建 `kiro-cli chat --list-sessions -f json`。

---

## `bot/` — 中央 Bot(你的主機上跑一隻)

```bash
cd bot
pip install -r requirements.txt   # discord.py
cp .env.example .env              # 只需填 BOT_TOKEN / GUILD_ID / CATEGORY_ID
python run.py
```

### 用 Docker 跑 bot(建議;上雲尤其適合)
bot 只需 outbound 連 Discord、不用開 port,容器化很乾淨。設定用環境變數餵
(`envcfg` 讓環境變數優先於 `.env`),`ks_bot.db` 導到 `/data` volume 保住 forum/thread 對應。

```bash
# 1. 先填好 bot/.env (BOT_TOKEN / GUILD_ID / CATEGORY_ID) — image 裡不會有這個檔
cd bot && cp .env.example .env && $EDITOR .env && cd ..

# 2. 從「專案根目錄」建置並啟動 (只起 bot 一個服務)
docker compose up -d --build

# 3. 確認真的上線了 (up -d 只代表「建好了」, 不代表「活著」)
docker compose ps -a               # 要看到 Up; Restarting/Exited 都是有問題
docker compose logs -f bot         # 成功會看到 `[bot] 已登入: <名字>`
```

成功的 log 長這樣 —— 頻道和 webhook 全自動建好,你不用手動設定任何東西:

```
[bot] 已登入: kiro Sync Bot#7366
[bot] 自建隱藏頻道: #kiro-ingest (…)
[bot] 自建隱藏頻道: #kiro-command (…)
[bot] slash commands 已同步
[bot] webhook 就緒: https://discord.com/api/webhooks/…
[bot] 已把 webhook URL 貼到 command 頻道並釘選
```

#### 改東西之後要下哪個指令(常踩)

| 你改了什麼 | 要下的指令 | 為什麼 |
|---|---|---|
| `bot/.env` | `docker compose up -d --force-recreate` | 環境變數是**容器建立當下**烙進去的。容器已存在時,單純 `up -d` 常常只是 `Started` 舊容器、**沿用舊設定** —— 換了 token 卻還是 401 通常就是這個。 |
| 程式碼 | `docker compose up -d --build` | 程式碼在 image 裡,不重 build 就還是跑舊的。 |
| 都沒改,只想重開 | `docker compose restart bot` | |

DB 在 named volume(`kirosync_bot_data`),`--force-recreate` / `--build` 都**不會**動到它,
forum/thread 對應與自建頻道 id 都保得住。真的要重來才 `docker compose down -v`(會清空 DB)。

#### 出問題時的排查順序

```bash
docker compose ps -a       # 1. 狀態 — 「-a」不能省, 已經 Exited 的容器不加就完全看不到
docker compose logs bot    # 2. 為什麼死
docker compose config      # 3. compose 最終看到的設定 (會展開 env_file, 確認有餵進去)
```

`restart` policy 是 `on-failure:3`:**失敗只會重試三次就停**。這是刻意的 —— 設定錯不會因為
無限重啟就變對,只會把真正的錯誤洗掉。所以看到 `Exited (1)` 是正常的診斷起點,不是壞掉。

| log 裡看到 | 意思 |
|---|---|
| `缺少必填設定: BOT_TOKEN, …` | `bot/.env` 沒填或沒被餵進去 |
| `LoginFailure: Improper token has been passed.`(401) | token 無效 —— 多半是後台按了 Reset Token 之後複製到舊的那顆。到 Bot → Reset Token 重拿,改完記得 `--force-recreate` |
| `PrivilegedIntentsRequired` | 開發者後台的 **MESSAGE CONTENT INTENT** 沒開 |
| bot 上線但 slash 指令不出現 | 邀請連結漏了 `applications.commands` scope,要重邀 |
| `缺 Manage Channels 權限, 無法自建…` | bot 在 guild 的權限不足,見下方權限清單 |

或不經 compose 直接跑:
```bash
docker build -t kirosync-bot ./bot
docker run -d --name kirosync-bot --env-file bot/.env \
  -v kirosync_bot_data:/data kirosync-bot   # DB_PATH 已由 Dockerfile 設成 /data/ks_bot.db
```
> 祕密與既有 `*.db` 不會進 image(見 `bot/.dockerignore`);`.env` 只在執行期以
> `env_file`/`--env-file` 餵進去。

### 備份 / 交接給別人託管

**bot 全世界只能有一隻。** 同一個 token 跑兩隻會同時收到同一批 ingest、把同一段對話
**貼兩次**,而且各自的 DB 會各建一套頻道互相打架。所以交接的第一步永遠是**先停掉舊的**。

要搬的東西只有三樣,其中只有 DB 是無法重算的:

| 東西 | 怎麼來 | 一定要嗎 |
|---|---|---|
| image | `docker save` 或叫對方 `git clone` + `--build` | 對方機器有網路的話,clone 更省事 |
| `bot/.env` | 你手上那份 | **要,但走加密管道**(內含 token = bot 的完整控制權) |
| `ks_bot.db`(volume) | 下面的匯出指令 | 只有「要保住既有 forum/thread 對應」時才要 |

```bash
# ── 你這邊 ──
docker compose stop bot          # 1. 必須先停 (順便讓 SQLite 落到一致狀態)
docker save kirosync-bot:latest | gzip > kirosync-bot.tar.gz          # 2. image (~50MB)
docker run --rm -v kirosync_bot_data:/d -v "$PWD":/b \
  alpine tar czf /b/ksdb.tar.gz -C /d .                               # 3. DB (~3KB)

# ── 對方那邊 ──
docker load < kirosync-bot.tar.gz
docker volume create kirosync_bot_data
docker run --rm -v kirosync_bot_data:/d -v "$PWD":/b \
  alpine tar xzf /b/ksdb.tar.gz -C /d          # 注意是 xzf (解開); czf 是打包
docker run -d --name kirosync-bot --restart on-failure:3 \
  --env-file .env -v kirosync_bot_data:/data kirosync-bot
```

不搬 DB 也能跑 —— bot 會自己重建 forum 和頻道,只是**舊 thread 就對不上了**,等於重新開始。
反過來說,如果對方是要在**另一個 guild** 開新的一套,**不要**給他 DB:裡面記的是你的頻道 id,
在他的 guild 根本不存在。

> **`.env` 不要打包進 image。** 拿掉 `.dockerignore` 裡的 `.env` 確實能讓 image「開箱即用」,
> 但 token 會**明文躺在 image layer 裡**,任何人拿到 tar 都能用兩行指令挖出來(不必啟動容器),
> 而且 rotate token 就得重 build、重發給每個人。用 `--env-file` 一樣能達到「你配置好、
> 對方只管部署」,代價只是多一個檔案。

> **volume 名稱陷阱。** `docker run -v <名字>:/d` 遇到**不存在**的 volume 會**默默建一顆空的、
> 不報錯** —— 名字打錯的下場是拿到一個空備份,等還原時才發現 DB 沒了。本專案已在
> `docker-compose.yml` 用 `name:` 把 volume 釘死成 `kirosync_bot_data`(否則 compose 會加
> 專案目錄名當前綴,換台機器 clone 到不同名字的資料夾就變成另一顆)。不確定就用
> `docker inspect kirosync-bot-1 --format '{{range .Mounts}}{{.Name}}{{end}}'` 查,別用猜的。

> **token 給了,app 還是你的。** 對方能跑 bot,但進不了開發者後台(改不了 intent/權限),
> 而你一按 Reset Token 他就 401。真的要長期移交,請在 Discord 開發者後台把 **application
> 轉移**給他或轉進 team。

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
`INGEST_CHANNEL_ID`、`COMMAND_CHANNEL_ID`(留空=自動建)、
`INCLUDE_TOOLS`(渲染時是否含工具呼叫/結果,`0`=只貼純文字對話)、
`DB_PATH`(跑 Docker 時由 compose 蓋成 `/data/ks_bot.db`,不用自己設)。

### Slash 指令
- `/ping` 檢查在線 · `/status` 論壇數/同步 session 數/運行時間 ·
  `/webhook` 取得 client 要填的 webhook URL · `/link <sid>` 取得某 session 搬移包下載連結 ·
  `/help` 說明。
- `/summary` 彙整對話摘要(管理員)· `/summary-check` 檢查總結服務是否可用(管理員)·
  `/summary-access` 管理授權身分組(管理員)。

### `/summary` — 彙整「這段時間做了什麼」

管理員在 Discord 下一個指令,bot 就把指定範圍內**新增的對話**濃縮成摘要貼回頻道。
資料是 bot 自己從 Discord 上的快照分片拉回來的(跟 `/link` 同一條路),所以
**來源機器關機也能總結**,使用者端不用開著、也不用做任何設定。

```
/summary                                  # 全部使用者, 距上次總結以來的變化
/summary scope:user target:alice          # 只看 alice
/summary scope:session target:a1b2        # 只看某個 session (id 可只給前幾碼)
/summary since:2026-07-16                 # 改用日期當起點, 而不是上次總結
```

- **兩種基準**:`since` 省略 = 從「上次總結到的地方」接下去(bot 每個 session 記一個
  游標);給了 `since` = 從那天 00:00 起算。兩種模式跑完都會把游標推到現在。
- **報告內容**:依使用者分段,每個 session 一節,含統計(新增行數/問答數/工具次數/
  時間區間)與語意摘要(完成事項 / 重要決策 / 未解問題)。太長會改用 `.md` 附件。
- **摘要引擎**:`bot/.env` 的 `GEMINI_API_KEY`([去這裡拿](https://aistudio.google.com/apikey))。
  **沒填也能用** —— 只是只出統計卡,沒有語意摘要;LLM 呼叫失敗時也會自動降級。
- **多 model 依序退場**:`GEMINI_MODELS` 可以填多個(逗號或空白分隔),前面的不可用
  (名稱不存在、被下架、額度用完)就自動往後退,第一個成功的拿來出摘要,**報告會標明
  實際用的是哪一個**。預設鏈:`gemini-3.1-flash-lite` → `gemini-2.5-flash-lite` → `gemma-4-31B`。
- **誰能用**:有 Discord 管理權限(Administrator / Manage Guild)的人,加上被
  `/summary-access add` 指定的身分組。用 `/summary-access list` 看目前授權清單。

#### `/summary-check` — 壞了先打這個

`/summary` 出來的東西不對(沒有語意摘要、某些 session 沒出現)時,先打 `/summary-check`,
它會**當場實測**三個前提並把**失敗原因原樣印出來**,而不是只說「不可用」:

| 檢查 | 做什麼 | 失敗會怎樣 |
| --- | --- | --- |
| 摘要引擎 | 真的打一次 Gemini(整條 model 鏈) | ❌ 印出每個 model 真正的錯誤(如 `HTTP 400 — API key not valid`);`/summary` 降級成只出統計卡 |
| 快照來源 | 真的抓一個快照回來解開 | ❌ 那些 session 會被 `/summary` 略過,要在來源機重跑 `sync` |
| 授權 | 列出目前誰能用 | — |

```
🩺 /summary 服務檢查 — ⚠️ 可用, 但有降級
⚠️ 摘要引擎 (Gemini) — 可用 · 退到第 2 個 model=gemini-2.5-flash-lite · 412ms —
   前面 1 個每次都會白試一輪, 建議從 GEMINI_MODELS 移除
> 跳過 gemini-3.1-flash-lite: HTTP 404 — models/gemini-3.1-flash-lite is not found
✅ 快照來源 — 12/12 個 session 有快照 · 實際抓取驗證通過
✅ 授權 — 可用者: Discord 管理員 (Administrator / Manage Guild)
```

- **沒填 key 是 ⚠️ 不是 ❌** —— 那是設計中的降級,`/summary` 照樣能用,只是沒有語意摘要。
- **退到後面的 model 也是 ⚠️** —— 能用,但前面那些每次呼叫都會白試一輪(多一次 404 的
  來回)。照 `/summary-check` 指的把不通的從 `GEMINI_MODELS` 拿掉就會變 ✅。

---

## `client/` — 擷取 + 上行(每位使用者機器上跑)

零依賴(純標準庫)、**無狀態**:client 只把 raw 快照上傳,不做解析(渲染在 bot 端),
也不存任何本機 DB。

```bash
cd client
cp .env.example .env        # 填 WEBHOOK_URL / USER_NAME
python run.py sync          # 監看並把 raw 快照 (zip) 切片上傳 (bot 端渲染)
python run.py export <sid>  # 一次性把某 session 快照上傳 (供他機搬移)
python run.py pull <url...> # 搬移: 把 /link 給的連結(多片依序)還原 session 到本機
python run.py sessions      # 列出本機 session (直接讀 session 目錄)
python run.py kiro          # 全域啟動器: 問上傳分類 -> 背景同步 -> 啟動 Kiro CLI
```

### `ks-kiro` — 一個指令搞定「同步 + 開 Kiro」(推薦)

怕忘了先開 `sync`?用 `ks-kiro` 取代直接打 `kiro`:它會**先問這個資料夾要上傳到哪個分類**
(直接 Enter = 預設個人 forum),**在背景啟動同步**,再**前景啟動 Kiro CLI**;你結束 Kiro 後
它會等最後一次同步送完才停背景同步。這樣「開 Kiro」與「同步」永遠綁在一起,不會漏。

**安裝成全域指令**(一次性,用 pip 本地安裝):

```bash
pip install -e ./client
```

`-e`(editable)讓程式就地執行 —— `.env`、`routes.json` 仍留在 `client/`,`git pull` 更新後
**不用重裝**。裝完會多兩個指令:`ks-kiro`(啟動器)和 `ks`(完整 CLI,等同 `python run.py …`)。
之後在任何專案資料夾:

```bash
cd D:\Work\ProjectA
ks-kiro                     # 問分類 -> 背景同步 -> 啟動 Kiro
ks-kiro --cat 1529383717565370479   # 跳過詢問, 直接指定分類 ID ('-' = 用預設)
ks-kiro -- --agent xxx      # '--' 之後的參數原樣轉給 Kiro CLI

ks check                    # 其他子指令一樣可用
ks route list
```

- **`.env` 沒填也能開始**:第一次跑 `ks-kiro` 時,若 `WEBHOOK_URL` / `USER_NAME` 是空的,
  它會**當場問你**並寫回 `client/.env`(貼上時連引號一起貼也會自動處理)。
- **找不到 Kiro 執行檔也會當場問**:預設用 PATH 上的 `kiro`;沒有的話 `ks-kiro` 會請你貼上
  執行檔完整路徑(或**它所在的資料夾**),解析後寫進 `.env` 的 `KIRO_CMD`,之後不再問。
  `ks check` 也會檢查這一項。
- 背景同步的輸出寫到 `client/ks-sync.log`(不洗掉 Kiro 的互動畫面);要看同步狀況去翻它。
- 若把專案資料夾搬走,重跑一次 `pip install -e <新路徑>/client` 即可。

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

`.env` 欄位:`WEBHOOK_URL`、`USER_NAME`(**必填**,多台機器填一樣的才會歸同一 forum;留空拒絕啟動)、`SYNC_WORKSPACES`(逗號分隔 cwd,空=全部)、
`WATCH_DIR`(留空=自動抓 `~/.kiro/sessions/cli`)、`POLL_INTERVAL`、
快照調校:`SNAP_CHUNK_MB`(切片上限)、`SNAP_DEBOUNCE`(停止變動幾秒後上傳)、
`SNAP_MIN_INTERVAL`(兩次上傳最小間隔)、`SNAP_MAX_WAIT`(持續變動時最遲上傳間隔)。
要不要渲染工具呼叫改由 **bot 端**的 `INCLUDE_TOOLS` 決定(因為渲染在 bot 做)。

### 依資料夾分流到不同分類(選用)

預設每位使用者的所有 session 都進自己的一個 forum。若想把**某資料夾底下**的 session
送到指定的 **Discord 分類(Category)**(例如依專案分),可用 `route` 指令設定:

```bash
python run.py route add "D:\Work\ProjectA" 1529383717565370479 --label ProjectA
python run.py route list
python run.py route remove "D:\Work\ProjectA"
```

- **分類用 ID**(不是名稱):在 Discord 開開發者模式右鍵分類→複製 ID,或到 command 頻道打 **`/categories`** 一次看全部分類的 ID。跟 `.env` 的 `CATEGORY_ID` 同一種東西。
- **含子資料夾**:在該資料夾**底下**開的 session 都算(不分大小寫、斜線方向);巢狀時最長前綴優先。
- **路由到的分類下,每位使用者仍各有自己的 forum**(第一次有訊息時才建)。
- 對應存本機 `client/routes.json`(已 gitignore);沒對應到、或分類找不到 → **fallback 回個人 forum**,不會漏同步。
- 分類**要先在 Discord 建好**,client 只引用;bot 不會自動建分類。

---

## 快速上手(單機自用)
0. 先做完上面的**一次性 Discord 設定**(開 intent、邀 bot、建 category)。
1. 起 bot,擇一:
   - Docker(建議):填好 `bot/.env`,在專案根目錄 `docker compose up -d --build`
   - 直接跑:`cd bot && pip install -r requirements.txt && cp .env.example .env`,填好後 `python run.py`
2. 確認上線:`docker compose logs -f bot`(或看終端機)出現 `[bot] 已登入: <名字>`。
3. bot 上線後,`kiro-command` 頻道會有釘選的 webhook URL(或打 `/webhook`)。
4. 裝 client:`pip install -e ./client`(裝好 `ks-kiro` / `ks` 指令)。
5. 到你的專案資料夾打 **`ks-kiro`** —— 第一次會問 webhook URL、識別名與上傳分類,
   填完就自動開始同步並啟動 Kiro CLI。
6. 開始對話 → `kiro-<USER_NAME>` 論壇會自動長出對應 thread。

> 第 5 步之前要先 `kiro-cli login`,不然 Kiro CLI 起不來、也沒有 session 檔可同步。
> 不想裝也行:`cd client && cp .env.example .env` 填好後 `python run.py sync`。

## 多人擴充
其他人不需要自己的 bot、也不用跟你的機器連線 —— 只要拿到那條 webhook URL,
`pip install -e ./client` 後跑 `ks-kiro`(第一次會問 webhook 與識別名)即可。
Bot 靠 payload 裡的 `USER_NAME` 分流到各自的 forum。

> **信任模型:guild 成員彼此互信。** webhook URL 釘選在人人可見的 `kiro-command`
> 頻道,而 webhook 沒有身分驗證 —— 拿到 URL 的任何人都能用任意 `USER_NAME` 上傳,
> 也就能假冒別人或灌垃圾快照。這是刻意的取捨(換來 client 零設定、零憑證),
> 所以請只在**自己人**的私有 guild 使用;若 guild 有不受信任的成員,至少把
> `kiro-command` 頻道改為私有,不要讓 webhook URL 外流。
