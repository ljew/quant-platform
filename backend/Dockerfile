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
# docs/ 也要进来：前端有页面用 `?raw` 直接 import 仓库根的规格文档
# （web/src/pages/EtfFlowPage.tsx → ../../../docs/ETF-FLOW-SPEC.md）。
# 只 COPY web/ 的话宿主机 build 能过、镜像 build 报 Could not resolve。
COPY docs/ /docs/
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
# ⚠️ 时区必须显式设置：基础镜像默认 UTC，否则所有定时任务（17:30 编排 / 19:00 ETL /
#    20:47 挖矿 / 周六复盘）都会按 UTC 触发 = 北京时间凌晨，等于全错 8 小时。
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

VOLUME /data
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
