# 个人量化投研平台 (Quant Platform)

> 个人定量投资研究 · 策略回测 · 实盘交易一体化平台
> 对应产品设计方案：`quant-platform-design.html`

---

## 当前进度：Phase 1 + Phase 2 + 指数增强 — 基础设施 / 行情看板 / 策略回测 / 指数增强

已实现：
- ✅ FastAPI 后端（REST API + 自动文档）
- ✅ SQLAlchemy 数据层（开发用 SQLite，生产可切 Postgres+TimescaleDB）
- ✅ akshare 数据源封装（免 token，A股日K/实时/财务；指数成分股与日K走 csindex/sina 源）
- ✅ **Tushare 数据源**（已配置 token，指数成分/指数日K/个股日K 兜底，任意指数均支持）
- ✅ 数据入库服务（全市场股票列表 + 日K线，支持增量更新 / 自动回源）
- ✅ 行情 API（K线查询、实时快照、标的信息）
- ✅ 前端行情看板（ECharts K线 + A股红涨绿跌 + 自选实时行情）
- ✅ 策略 SDK 基类（StandardStrategy：init / on_bar；PortfolioStrategy：init / rebalance）
- ✅ **事件驱动回测引擎**（逐K线驱动、多头单标的、佣金/滑点、绩效归因）
- ✅ **三个单标的策略模板**：双均线趋势 / 均线金叉死叉 / 动量突破
- ✅ **组合回测引擎**（多标的、横截面因子、月度调仓、基准对比、超额收益/信息比率）
- ✅ **中证800指数增强策略**（多因子打分 + 月度调仓 + 指数基准对比，选股 TopN 最低 5 只）
- ✅ **沪深300指数增强策略**（成分股仅 300 只，比中证800更少，适合不想覆盖太多标的的场景）
- ✅ **回测 API + 前端回测页**（参数配置、净值曲线、绩效指标、成交明细、**历史点开看详情**）
- ✅ **回测引擎实盘化**（涨停买不进/跌停卖不出、A股100股整数倍、单笔最低佣金5元、现金缓冲）
- ✅ **股票基本面截面入库**（Tushare 行业/总市值/PE/PB，支撑中性化与估值因子）
- ✅ **指数增强升级**：行业中性 + 市值中性（可开关），因子合成更稳健、跟踪误差更小
- ✅ **回测详情组合结构可视化**（行业分布饼图 / 持仓数量变化 / 前十大重仓权重时序）

后续 Phase（见设计文档路线图）：
- Phase 3: 因子研究模块（因子库扩展 / 行业中性 / 市值中性）✅ 已落地
- Phase 3: 因子研究模块（因子库扩展 / 行业中性 / 市值中性）
- Phase 4: 模拟交易
- Phase 5: 实盘对接（CTP/XTP/QMT）
- Phase 6: 报告中心

---

## 快速开始（开发模式，无需 Docker）

### 1. 准备 Python 环境
```bash
# 使用本机 Python 3.13（或任意 3.10+）
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
```

### 2. 启动后端
```bash
cd backend
PYTHONPATH=$(pwd) uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### 3. 打开前端
浏览器访问 http://localhost:8000/ （由后端直接托管 `frontend/index.html`）

> 无需单独构建前端——Phase 1 原型为单页应用，修改 `frontend/index.html` 刷新即生效。

---

## 数据接入（拉取真实行情）

平台数据层支持**三后端**，自动按可用性选择：
- **akshare**（个股行情首选）：免 token，覆盖 A/港/美股全量历史，需要能访问东方财富接口的网络。
- **Tushare**（指数数据首选）：已内置 token，指数成分股/指数日K/个股日K 兜底，**任意指数（000300/000905/000906 等）均支持**，最可靠。token 位于 `backend/app/config.py`（`QUANT_TUSHARE_TOKEN` 环境变量可覆盖，建议生产放入 `.env` 不要提交）。
- **westock-data CLI**（兜底）：调用 WorkBuddy 内置 westock-data（腾讯自选股接口），在受限网络/沙箱环境下自动启用，保证看板始终有真实数据。

后端启动后，通过 API 触发数据入库（首次需联网拉取）：

```bash
# 1) 拉取全市场 A股基础信息（约 5000+ 只）
curl -X POST http://localhost:8000/api/v1/data/ingest/stocks

# 2) 拉取日K线（不指定 symbols 则全市场；首次约需数十分钟）
curl -X POST http://localhost:8000/api/v1/data/ingest/kline \
  -H "Content-Type: application/json" \
  -d '{"symbols":["sh600519","sz000858"],"start_year":2015,"adj":"qfq"}'

# 查看数据覆盖情况
curl http://localhost:8000/api/v1/data/status
```

> 说明：数据接入为后台任务。可通过 `/api/v1/data/status` 轮询进度，或查看后端日志。

### 指数增强数据（成分股日K）

指数增强策略需要**全股票池**的历史日K。运行预拉取脚本可批量入库（断点续传、并发加速）：

```bash
cd backend
# 泛化脚本，--index 指定指数：000906=中证800（默认） / 000300=沪深300 / 000905=中证500
PYTHONPATH=$(pwd) python scripts/seed_index.py --index 000906 --since 2021
PYTHONPATH=$(pwd) python scripts/seed_index.py --index 000300 --since 2021
PYTHONPATH=$(pwd) python scripts/seed_index.py --index 000300 --limit 60   # 仅前 60 只（验证用）
```

- 成分股列表：优先 Tushare `index_weight`（任意指数），回退 akshare `index_stock_cons_csindex`
- 成分股日K：akshare `stock_zh_a_daily(adjust='qfq')`（sina 源前复权）+ Tushare `daily` 兜底
- 完成后回测将从本地库秒级读取；首次回测若本地缺失会自动回源（较慢）

### 股票基本面截面（行业/市值/估值）
指数增强的中性化与估值因子需要每只股票的基本面属性。一次性拉取并写入 `stocks` 表：

```bash
cd backend
PYTHONPATH=$(pwd) python scripts/seed_attributes.py
```
- 数据来源：Tushare `stock_basic`（申万行业）+ `daily_basic`（总市值/PE_TTM/PB，最近交易日）
- 写入 `stocks` 表的 `industry` / `market_cap`(亿元) / `pe_ttm` / `pb` 字段
- 中性化开启时自动读取；缺失时策略退化为全局 TopN 选股

---

## 策略回测（Phase 2）

### 前端可视化
浏览器打开 http://localhost:8000/backtest.html ：
1. 输入标的（代码或名称，自动联想）→ 选择回测区间与初始资金
2. 选择策略：
   - 单标的：双均线趋势 / 均线金叉死叉 / 动量突破，填写参数
   - **中证800指数增强**：标的自动锁定为「中证800指数 (000906)」，参数为选股数量 TopN（最低 5 只）/调仓周期/因子权重
   - **沪深300指数增强**：标的自动锁定为「沪深300指数 (000300)」，成分股仅 300 只，比中证800更少、更易实操
3. 点击「运行回测」→ 展示绩效指标卡、净值曲线（策略 vs 基准）、成交明细（含标的列）
4. 指数增强额外展示：基准收益、超额收益、信息比率(IR)
5. **回测历史点开看详情**：历史表格任意一行可点击，加载该次回测的完整净值曲线与成交明细

### 运行指数增强回测（API）
```bash
# 中证800 增强（TopN=30）
curl -X POST http://localhost:8000/api/v1/strategy/backtest \
  -H "Content-Type: application/json" \
  -d '{"symbol":"sh000906","start":"2023-01-01","end":"2025-12-31","strategy":"csi800_enhanced","params":{"top_n":30}}'

# 沪深300 增强（TopN=20，最少可到 5）
curl -X POST http://localhost:8000/api/v1/strategy/backtest \
  -H "Content-Type: application/json" \
  -d '{"symbol":"sh000300","start":"2023-01-01","end":"2025-12-31","strategy":"hs300_enhanced","params":{"top_n":20}}'
```
返回含：`total_return`(策略总收益)、`benchmark_total_return`(基准收益)、`excess_return`(超额收益)、
`info_ratio`(信息比率)、`sharpe`、`max_drawdown`、`equity_curve`(策略净值 vs 基准净值)、`trades`(成交明细)。

> 指数增强策略需成分股日K在库（见上节 seed 脚本）；未预拉取时回测会按需自动回源，但较慢。

**行业中性 / 市值中性**（指数增强可选参数，默认开启）：
- `neutralize_industry`（1/0）：按成分股行业数量占比分配选股名额，使组合行业分布与基准一致
- `neutralize_marketcap`（1/0）：对每个因子按市值分位分组 demean，剥离规模风格暴露
- 关闭后退化为主观打分 TopN 选股。需先运行 `seed_attributes.py` 填充行业/市值数据方可生效。

### 列出可用策略
```bash
curl http://localhost:8000/api/v1/strategy/strategies
```

### 运行一次回测
```bash
curl -X POST http://localhost:8000/api/v1/strategy/backtest \
  -H "Content-Type: application/json" \
  -d '{"symbol":"sh600519","start":"2024-01-01","end":"2025-12-31","strategy":"dual_ma","params":{"fast":5,"slow":20}}'
```
返回含：`total_return`(总收益)、`annual_return`(年化)、`max_drawdown`(最大回撤)、
`sharpe`(夏普比率)、`win_rate`(胜率)、`trade_count`、`equity_curve`(权益曲线)、`trades`(成交明细)。

### 回测历史
```bash
curl "http://localhost:8000/api/v1/strategy/backtests?limit=20"        # 历史列表
curl "http://localhost:8000/api/v1/strategy/backtests/{id}"            # 单次回测完整详情（净值曲线+成交）
```

### 自定义策略
继承 `app/core/engine/base_strategy.py` 的 `StandardStrategy`，实现 `init()` 与 `on_bar()`，
通过 `ctx.buy() / ctx.sell() / ctx.order_target_percent(pct)` 下单，用 `ctx.sma/ema/roc/rsi` 取指标；
在 `app/core/strategies/registry.py` 登记即可被 API 与前端自动识别。

---

## 查询示例

```bash
# 标的列表（模糊搜索）
curl "http://localhost:8000/api/v1/market/stocks?keyword=茅台&limit=5"

# 日K线（最近 400 个交易日）
curl "http://localhost:8000/api/v1/market/kline/sh600519?limit=400"

# 实时快照（需 akshare 数据源）
curl "http://localhost:8000/api/v1/market/quote/sh600519"
```

---

## 目录结构
```
quant-platform/
├── backend/
│   ├── app/
│   │   ├── config.py          # 全局配置（DATABASE_URL 切换 SQLite/Postgres）
│   │   ├── database.py        # SQLAlchemy 引擎 / Session
│   │   ├── models.py          # ORM 模型（Stock / KlineDaily）
│   │   ├── schemas.py         # Pydantic 模型
│   │   ├── main.py            # FastAPI 应用入口
│   │   ├── routers/           # API 路由（market / data / strategy）
│   │   ├── services/          # 数据源封装 / 入库服务
│   │   └── core/
│   │       ├── engine/        # 策略 SDK + 回测引擎（indicators / backtest_engine / base_strategy）
│   │       └── strategies/    # 策略模板（dual_ma / ma_cross / momentum）+ registry
│   ├── requirements.txt
│   └── run.py
├── frontend/
│   ├── index.html             # 行情看板原型（ECharts）
│   ├── backtest.html          # 回测页（参数 / 权益曲线 / 绩效 / 历史）
│   └── echarts.min.js        # 本地 ECharts（不依赖外网 CDN）
├── data/                      # SQLite 数据库文件（开发用，自动生成）
├── docker-compose.yml         # 生产部署（Postgres+TimescaleDB/Redis/backend/frontend）
└── README.md
```

---

## 数据库切换（开发 → 生产）

编辑 `backend/app/config.py` 或将环境变量 `QUANT_DATABASE_URL` 改为：

```bash
# Postgres + TimescaleDB
export QUANT_DATABASE_URL="postgresql://quant:quant@localhost:5432/quant"
```

业务代码无需改动。生产建议通过 `docker-compose.yml` 一键拉起完整依赖。

---

## 风险提示

本平台为**个人量化研究工具**，非商业级交易系统。实盘交易具有真实资金风险，
请先在模拟环境验证、小资金试跑后再逐步放量。平台不构成任何投资建议。
