# 个人量化投研平台 — 后端容器镜像（多阶段：React 前端构建进镜像，后端托管）
# 构建上下文 = 项目根（包含 backend/ 与 web/）
# 架构（设计 v1.0）：业务写表 SQLite(/data/quant_dev.db) + 分析读表 DuckDB(/data/quant.duckdb)
# 数据目录 /data 通过 volume/bind 持久化（compose 用 ./data 挂载，复用本机数据）

# —— 阶段1：构建 React 前端（web/ → dist）——
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm install --no-audit --no-fund --registry=https://registry.npmmirror.com
COPY web/ ./
RUN npm run build

# —— 阶段2：后端（托管 React 构建产物）——
FROM python:3.12-slim
WORKDIR /app

# 依赖全部有预编译 wheel，无需 gcc 编译工具链（apt 段已移除，构建大幅提速）
# pip 使用清华镜像加速
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY backend/app ./app
COPY backend/scripts ./scripts
COPY --from=web /web/dist /frontend

ENV PYTHONPATH=/app
ENV QUANT_FRONTEND_DIR=/frontend
ENV QUANT_DATABASE_URL=sqlite:////data/quant_dev.db
ENV QUANT_DATA_SCHEDULE=1

VOLUME /data
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
