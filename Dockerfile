FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/festival.db \
    SEED_DEMO_DATA=false \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite 数据目录（挂载卷可持久化）；启动时自动建表完成初始化
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 服务启动即完成 schema 初始化（见 app/db.py），无需额外迁移步骤
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
