# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案概要

KiroSync 把 Kiro CLI 的本機對話檔 (`~/.kiro/sessions/cli/*.jsonl`) 即時同步到 Discord,一個
session 對應一條 Discord forum thread,並支援用 Discord 當中繼在多台機器之間搬移 session。
README.md 是使用者面向的完整說明(中文)。

## 執行指令

`bot/` 與 `client/` 是**兩個各自獨立的部署**,各有自己的 `.env`、`requirements.txt`、進入點。
模組之間用裸名 import(`from uplink import ...`),所以**一定要先 `cd` 進該資料夾再跑**,
不能從 repo 根目錄執行。

```bash
# bot (中央,一隻就夠)
cd bot && pip install -r requirements.txt && cp .env.example .env && python run.py
docker compose up -d --build      # 從 repo 根目錄;只起 bot 服務,DB 落在 named volume

# client (每台使用者機器,零依賴純標準庫、無狀態)
cd client && cp .env.example .env
python run.py sync            # 主要模式:監看並上傳 raw 快照
python run.py export <sid>    # 一次性上傳某 session
python run.py pull <url...>   # 還原 session(`/link` 會直接組好這行指令)
python run.py sessions        # 列出本機 session(直接讀 session 目錄)
```

測試在 `tests/`,純標準庫 `unittest`、零依賴(**不用裝 pytest 也不用裝 discord.py**;
`tests/_stub_discord.py` 提供假 discord 讓 `bot/bot.py` 可以被 import)。從 repo 根目錄跑:

```bash
python3 -m unittest discover -s tests        # 全部;加 -v 看逐條
```

`tests/_paths.py` 負責把 `bot/`、`client/` 塞進 `sys.path`(client 優先,因為兩邊都有
`run.py` / `envcfg.py` 同名模組)——新測試檔開頭要 `import _paths`。改 KSV1 wire format
時 `tests/test_wire_contract.py`(client 產生 ↔ bot 解析的 round-trip)是主要防線。
測試沒蓋到的部分(Discord API 互動、`cmd_sync` 的 debounce 迴圈)仍要接真的 guild 驗;
沒有 lint 或 CI。

## 架構

### 資料流(單向,兩機之間沒有直連)

```
client 機器  ──webhook POST──▶  Discord #kiro-ingest  ──Gateway──▶  bot 主機
```

兩端都**只向外連 discord.com**,不開 port、不需公開 IP。Discord 同時是傳輸層跟儲存層。

### 「架構 B」:client 只傳 raw,bot 端渲染

這是理解整個 codebase 最關鍵的一點。client **不解析任何東西**——它把整個 session 打成 zip、
依上限切片、上傳。所有解析/格式化/切段/貼圖都在 bot 端 ([bot/kiroparse.py](bot/kiroparse.py),
**全專案唯一一份解析邏輯**)。改渲染格式只要動 bot,不用重佈所有 client。

### KSV1 wire format(合約橫跨兩個部署)

訊息格式定義在 [client/uplink.py](client/uplink.py) 的 docstring,解析在
[bot/bot.py](bot/bot.py) 的 `parse_ingest`。**改一邊必須同步改另一邊**,而且目前沒有任何
測試攔得住你只改一邊——這是這個 codebase 最脆弱的地方。

- `Hello` — client 啟動時送,讓 bot 先建好該使用者的 forum + info thread。
- `Snap` — 附件是 zip 的一個切片,header 帶 `g`(世代)/`p`(片號)/`n`(總片)/`title`/`cwd`。
  世代 `g` = `"<len>-<crc32>"`,同大小不同內容不會撞。

### 快照的生命週期

1. client `cmd_sync` 用 debounce 決定何時上傳(閒置 `SNAP_DEBOUNCE` 秒、或持續變動滿
   `SNAP_MAX_WAIT` 秒強制送),signature = `(jsonl size, json mtime)`。
2. bot `_on_snap` 把分片收在記憶體 `_snap_buf`(**重啟會掉,未收齊的世代要等 client 重送**),
   收齊 `n` 片才併回 zip 並 `_apply_snapshot`。
3. `_apply_snapshot` 靠 `sessions.rendered`(已貼行數)只渲染 `.jsonl` **新增的行**——
   append-only 是這裡的核心假設;行數倒退(檔案被重寫變短)時會把進度重設到新檔尾端
   (不重複貼舊行,已貼的訊息也不會收回)。
4. **分片訊息刻意不刪**,留在 ingest 頻道當「離線可拉取」來源;`/link` 現場重簽 CDN URL,
   所以來源機關機也拉得到。套用快照時刪掉已被取代的舊分片訊息(不只比世代——同世代
   重送的舊訊息也是孤兒,一樣刪),容量有界。

### 狀態放哪:只有 bot 有

**client 是完全無狀態的**,不存 DB。`sync`/`export`/`pull`/`sessions` 全靠現場讀
session 目錄;`sync` 重啟後用檔案 signature 重新決定要不要上傳(最多重送一次快照,
bot 端靠 `rendered` 去重,所以不會重複貼)。

bot 端 [bot/store.py](bot/store.py) 的 SQLite 只存無法重算的東西:`user_forums`
(user → forum)、`sessions`(session → thread + `rendered` 進度)、`snapshots`
(最新世代的分片 message id)、`kv`(自建頻道 id、webhook URL)。對話內容不存這裡——
真相在各 client 的 `.jsonl` 與 Discord thread 裡。

### 零手動設定

bot 開機自建 `kiro-ingest`(隱藏)+ webhook、`kiro-command`(公開),id 記在 `kv` 表沿用。
頻道解析順序:`.env` 指定 > `kv` 裡之前自建的 > 現在自建。webhook URL 自動貼到 command 頻道
並釘選。使用者只需填 `BOT_TOKEN` / `GUILD_ID` / `CATEGORY_ID`。

### envcfg 的環境變數優先

[bot/envcfg.py](bot/envcfg.py) / [client/envcfg.py](client/envcfg.py)(兩份相同)讓
**環境變數蓋過 `.env` 檔**。Docker 就是靠這個:image 裡沒有 `.env`,compose 用 `env_file`
把設定變成環境變數餵進去,再用 `DB_PATH: /data/ks_bot.db` 導到 volume。

## 已知的坑

- **bot 的 `_snap_buf` 是純記憶體的**:收齊分片前重啟就掉那個世代,要等 client 重送。
  單檔 session 通常只有一片,實務上很少踩到,但大 session 要留意。
- Kiro CLI 的 `.jsonl` 格式(2.10.0)是逆向來的:每行 `{"version","kind","data"}`,文字在
  `data.content[]` 裡 `kind=="text"` 的 `data`。解析器對未知 kind 一律不當掉。
- session 「屬於哪個資料夾」看的是 `<id>.json` 的 `cwd` 欄位,不是檔案位置;`pull --cwd` 會
  連 `session_state.permissions.filesystem` 的可讀/可寫路徑一起改寫。
- `.jsonl` 存在但 `.json` 缺席或壞掉是正常情況(session 剛開、或寫到一半),各處都要能容忍。

## 慣例

程式碼註解與使用者面向輸出都是**繁體中文**;commit message 用英文 conventional commits
(`feat:` / `refactor:`)。
