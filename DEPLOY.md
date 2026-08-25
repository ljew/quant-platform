# 量化投研平台 — Docker 部署手册

> 设计 v1.0 完整版（数据管道 ETL / 回测 / 因子 / 模拟盘 / 监控）。一键部署为容器常驻服务（崩溃自动重启 + 开机自启），数据用 bind mount 持久化到宿主机。

---

## 1. 架构一览

```
┌────────────────────────────────────────────────────────────┐
│  宿主机 ./data/  ←—— bind mount ——→  容器内 /data/           │
│    quant_dev.db    (SQLite 业务写表：回测/模拟盘/订单/因子)   │
│    quant.duckdb    (DuckDB 分析读表：K线/指数/基本面/因子)    │
│    trading_calendar.json / ...                              │
└────────────────────────────────────────────────────────────┘

┌─ 容器 backend (端口 8000) ─────────────────────────────────┐
│  FastAPI 后端（托管 React 构建产物 + /api/v1/* + WebSocket）│
│  ETL 调度器：每交易日 17:00 自动增量（tushare→因子→DuckDB）  │
│  QUANT_DATA_SCHEDULE=1 开启；tushare token 走环境变量       │
└────────────────────────────────────────────────────────────┘

┌─ 容器 redis (端口 6379) ── 预留 Celery broker（当前未依赖）─┐
└────────────────────────────────────────────────────────────┘
```

## 2. 前置条件

| 项目 | 要求 |
|---|---|
| Docker | 24+（Docker Desktop / 服务器 Docker Engine 均可）|
| docker compose | v2（`docker compose version` 确认）|
| 镜像加速 | **建议**配置 registry-mirrors（国内服务器可跳过）|
| Tushare token | 有积分即可（数据采集用，缺失时仅行情类功能可用）|

镜像加速器配置（宿主机 `/etc/docker/daemon.json`，改后 `systemctl restart docker`）：

```json
{ "registry-mirrors": ["https://docker.1ms.run", "https://docker.m.daocloud.io"] }
```

## 3. 部署文件

```
quant-platform/
├── docker-compose.yml      # 服务编排（backend + redis）
├── backend/Dockerfile      # 多阶段镜像：React 构建 + 后端（已内置国内 pip/npm 源）
├── backend/app/            # FastAPI 应用（含 monitor/live/strategy/paper 等路由）
├── backend/scripts/        # ETL 等脚本
├── web/                    # React 前端（构建进镜像，后端托管）
└── data/                   # 数据目录（bind mount；首次可为空，ETL 会初始化）
```

## 4. 快速部署（5 步）

### 步骤 1：准备代码与数据

```bash
# 拿到代码（已有则跳过）
git clone git@github.com:ljew/quant-platform.git && cd quant-platform

# 数据目录：已有 data/（SQLite+DuckDB）直接复用；
# 全新部署时留空目录即可，首次 ETL 会初始化
mkdir -p data
```

### 步骤 2：配置环境变量

```bash
cp .env.example .env 2>/dev/null || touch .env
# 编辑 .env，填入 tushare token：
#   QUANT_TUSHARE_TOKEN=你的token
```

### 步骤 3：构建镜像

```bash
docker compose build --no-cache backend
# 说明：Dockerfile 已内置国内源（pip 清华 / npm 淘宝），
#       依赖全为预编译 wheel，无需 gcc，构建通常 3~8 分钟
```

### 步骤 4：启动

```bash
docker compose up -d
# restart: unless-stopped —— 崩溃自动重启、宿主机重启后自动拉起
```

### 步骤 5：验证

```bash
# 1) 容器健康
docker compose ps
#    backend 应为 Up（healthy 由 redis 提供）

# 2) 后端健康
curl http://localhost:8000/health
#    {"status":"ok","version":"0.2.0",...}

# 3) 监控页（数据/服务状态，ETL 调度是否启用）
curl http://localhost:8000/api/v1/monitor/status | python3 -m json.tool

# 4) 浏览器访问
open http://localhost:8000    # React 前端（行情/回测/寻优/模拟盘/监控 五页）
```

## 5. 数据说明

| 事项 | 说明 |
|---|---|
| 数据位置 | 宿主机 `./data/`（bind mount），容器内外一致，删容器不丢数据 |
| 首次初始化 | 空 data/ 启动后：先跑 `docker compose exec backend python scripts/etl_daily.py` 做全量初始化（核心池 1800+ 只，约 30~40 分钟），之后 17:00 自动增量 |
| 每日增量 | 每交易日 17:00 自动：tushare 增量 K线 → 14 因子截面重算 → DuckDB 同步 |
| 双库分工 | SQLite=业务写表；DuckDB=分析读表（回测/因子读列式加速），同步自动 |

## 6. 日常运维

```bash
docker compose ps                    # 状态
docker compose logs -f backend       # 实时日志（/data 数据卷不受影响）
docker compose restart backend       # 重启后端
docker compose down && docker compose up -d   # 完全重启（数据保留）
docker compose down -v               # ⚠️ 清数据（含 data 目录，慎用）

# 手动跑一次 ETL（日常增量/补数据）
docker compose exec backend python scripts/etl_daily.py

# 更新到最新代码
git pull && docker compose build backend && docker compose up -d backend
```

## 7. 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| 构建卡在拉基础镜像 | Docker Hub 慢/墙 | 配置 registry-mirrors（见 §2）并重启 Docker；或先 `docker pull python:3.12-slim` 手动缓存 |
| 构建出的镜像还是旧代码 | BuildKit 缓存命中陈旧 COPY 层 | `docker compose build --no-cache backend` |
| `/api/v1/monitor/status` 404 | 镜像太旧（8-14 前代码） | 无缓存重建镜像并 `up -d` |
| ETL 不自动跑 | 调度未启用 | 确认 compose 环境变量 `QUANT_DATA_SCHEDULE=1`，监控页 ETL enabled=True |
| 监控页显示数据日期停在几天前 | 非交易时段/ETL 未跑 | 手动 `docker compose exec backend python scripts/etl_daily.py` |
| 端口 8000 被占用 | 宿主机已有服务 | 改 compose `ports: "18000:8000"` 或停掉旧服务 |

## 8. 迁移到正式服务器（推荐）

本机（沙箱/虚拟机）若网络受限，直接在正式服务器上按 §4 执行即可：
- 服务器网络正常时构建仅需几分钟
- 把 `data/` 目录 rsync 过去可无缝迁移已有数据
- 服务器用 `systemd`/Docker `restart: unless-stopped` 即系统级常驻
