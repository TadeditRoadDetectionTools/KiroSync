# KiroSync (ks)

把 Kiro CLI 的對話即時同步到 Discord。監看 `~/.kiro/sessions/cli/*.jsonl`,一個
session 對應一條 Discord thread。完整規劃見 [PLAN.md](PLAN.md)。

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

## `client/` — 擷取 + 上行(每位使用者機器上跑)

零依賴(純標準庫)。

```bash
cd client
cp .env.example .env        # 填 WEBHOOK_URL / USER_NAME
python run.py sync          # 監看並上行 (預設只傳啟動後的新對話)
python run.py sync --backfill   # 連既有內容也傳

# 不碰 Discord 的本機模式:
python run.py tail          # 即時印出 + 存 ks_client.db
python run.py once
python run.py sessions
python run.py events <sid>
```

`.env` 欄位:`WEBHOOK_URL`、`USER_NAME`、`SYNC_WORKSPACES`(逗號分隔,空=全部)、
`WATCH_DIR`、`POLL_INTERVAL`、`DB_PATH`。

---

## `bot/` — 中央 Bot(你的主機上跑一隻)

```bash
cd bot
pip install -r requirements.txt   # discord.py
cp .env.example .env              # 填 BOT_TOKEN / GUILD_ID / CATEGORY_ID / INGEST_CHANNEL_ID
python run.py
```

### 一次性 Discord 設定
1. 開發者後台 → 你的 App → Bot → 開 **MESSAGE CONTENT INTENT**。
2. 邀 bot 進 server,給 **Manage Channels / Create Public Threads /
   Send Messages in Threads** 權限。
3. 建一個**分類**(category,放 forum)+ 一個**隱藏的 ingest 文字頻道**;
   在 ingest 頻道建 **Webhook**,把 URL 交給 client 填進它的 `.env`。

`.env` 欄位:`BOT_TOKEN`、`GUILD_ID`、`CATEGORY_ID`、`INGEST_CHANNEL_ID`、`DB_PATH`。

---

## 多人擴充
其他人不需要自己的 bot、也不用跟你的機器連線 —— 只要拿到一條 webhook URL,把
`WEBHOOK_URL` / `USER_NAME` 填進自己 `client/.env` 跑 `python run.py sync` 即可。
Bot 靠 payload 裡的 `USER_NAME` 分流到各自的 forum。
