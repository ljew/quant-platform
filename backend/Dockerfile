# 个人量化投研平台 — 后端容器镜像
# 构建上下文 = 项目根（包含 backend/ 与 frontend/）
# 架构（设计 v1.0）：业务写表 SQLite(/data/quant_dev.db) + 分析读表 DuckDB(/data/quant.duckdb)，
# 数据目录 /data 通过 volume 持久化
FROM python:3.12-slim

WORKDIR /app

# akshare 等依赖部分需编译，安装基础工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY backend/scripts ./scripts
COPY frontend /frontend

ENV PYTHONPATH=/app
ENV QUANT_FRONTEND_DIR=/frontend
ENV QUANT_DATABASE_URL=sqlite:////data/quant_dev.db

VOLUME /data
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
