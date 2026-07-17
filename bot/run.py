"""
KiroSync 中央 Bot 進入點。

用法:
    cd bot
    pip install -r requirements.txt
    cp .env.example .env   # 填好後
    python run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from envcfg import get, load_env

HERE = Path(__file__).resolve().parent


def main() -> None:
    env = load_env()
    cfg = {
        "bot_token": get(env, "BOT_TOKEN"),
        "guild_id": get(env, "GUILD_ID"),
        "category_id": get(env, "CATEGORY_ID"),
        "ingest_channel_id": get(env, "INGEST_CHANNEL_ID"),
        "command_channel_id": get(env, "COMMAND_CHANNEL_ID"),
        "include_tools": (get(env, "INCLUDE_TOOLS") or "1").strip().lower()
        not in ("0", "false", "no", "off"),
        "gemini_api_key": get(env, "GEMINI_API_KEY"),
        # 可填多個 (逗號/空白分隔) 依序嘗試; GEMINI_MODEL 是單數的舊名, 一併接受
        "gemini_models": get(env, "GEMINI_MODELS") or get(env, "GEMINI_MODEL"),
    }
    db_path = HERE / (get(env, "DB_PATH") or "ks_bot.db")
    from bot import run_bot  # 延遲載入, 讓缺 discord.py 時錯誤更清楚
    run_bot(cfg, db_path)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
