# KiroSync 中央 Bot 容器
# 只需 outbound 連 Discord Gateway, 不用 EXPOSE 任何 port。
# 設定用環境變數餵 (envcfg 讓環境變數優先於 .env), DB 放 /data volume。
FROM python:3.12-slim

# 即時 log; DB 預設落在掛載點, 不蓋到 /app 程式碼
ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/ks_bot.db

WORKDIR /app

# 先裝依賴 (吃 layer cache), 再建非 root 使用者與資料目錄
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd -m app \
    && mkdir -p /data && chown app:app /data

# --chown 讓容器內非 root 的 app 擁有程式碼:
# 就算來源檔被 host ACL 設成 0660 (others 無讀), owner=app 仍讀得到,
# 避免 pull 進新檔後又出現 run.py Permission denied。
COPY --chown=app:app . .

USER app
VOLUME ["/data"]

CMD ["python", "run.py"]
