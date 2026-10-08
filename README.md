# QUANT·DESK · 个人量化投研平台

> 因子挖掘 · 策略回测 · 信号跟踪 · 模拟盘 —— 单机全链路闭环
> 个人研究工具，不含实盘交易通道

---

## 快速开始

**日常使用只需要后端一个服务**（launchd 已托管，开机自启）：

```bash
open http://127.0.0.1:8000
```

前端由后端托管构建产物，打开即用。

**改前端代码时**（才需要临时开 Vite）：

```bash
cd web && npm install && npx vite      # 打开 http://localhost:5173
# 改完务必同步构建产物，否则 8000 上仍是旧版：
npm run build
```

> Vite 已配好 `/api` → `http://127.0.0.1:8000` 代理，前端页面与数据都能正常工作。

**首次部署 / 重启后端**：

```bash
cd backend
PYTHONPATH=$(pwd) uvicorn app.main:app --host 0.0.0.0 --port 8000
```

日常维护用现成脚本：`scripts/relaunch_backend.sh`（需在**你自己的终端**执行，见 `DEPLOY.md`）。

---

## 目录结构

```
quant-platform/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口
│   │   ├── config.py            # 全局配置（token / 路径 / 端口）
│   │   ├── models.py            # SQLAlchemy ORM（29 张表）
│   │   ├── routers/             # 14 个路由：market/data/strategy/paper/
│   │   │                        #   signal/factor/auto/monitor/research/…
│   │   ├── core/
│   │   │   ├── engine/          # 回测引擎（单标的 + 组合 + 对冲）
│   │   │   │                    #   factor_expr DSL 引擎 / 指标库
│   │   │   ├── strategies/      # 策略注册表（21 个，6 个启用）
│   │   │   ├── datahub/         # 数据枢纽（Bronze/Silver 分层）
│   │   │   └── broker/          # 模拟撮合 + 风控
│   │   ├── services/
│   │   │   ├── xq/              # 信号引擎（9 选股策略 + 判定 + 组合镜像）
│   │   │   ├── factor_mining.py # 因子挖掘 + 有效性评级
│   │   │   ├── factor_gp.py     # GP 挖矿（已熔断，见下）
│   │   │   ├── etl.py           # Medallion 数据管道
│   │   │   └── research_card_svc.py # 研究卡状态机
│   │   └── datahub/             # 数据源接入
│   ├── scripts/                 # 运维脚本（rerun_signals / seed_* / verify_*）
│   ├── tests/                   # 回归测试（5 组 29 条断言）
│   └── requirements.txt
├── web/                         # React + Vite 前端（四入口枢纽）
│   └── public/docs/             # 侧边栏直链文档（架构/手册/方法论/研究卡）
├── data/                        # 数据层（不入 git）
│   ├── quant_dev.db             # SQLite 3.1GB：K线982万行 + 业务写表
│   ├── quant.duckdb             # DuckDB 846MB：分析读表
│   └── raw/ silver/             # Bronze 层原始与清洗数据
├── docs/
│   ├── REBUILD-2026-10.md       # ⭐ 效果诊断与重构方案（含执行状态）
│   ├── decisions.json           # 决策记录活文档（27 条）
│   └── ARCHIVE/                 # 历史方案归档
└── scripts/                     # 服务启停
```

**规模**：后端 130 个 py 文件 / 26232 行 · 29 张表 · K线 982 万行（2017-06 至今，2237 个交易日）

---

## 架构

### 数据层（双库读写分离）

| 角色 | 存储 | 内容 |
|---|---|---|
| 业务写表 | SQLite `quant_dev.db` | K线、复权因子、信号、回测记录、账本、事件 |
| 分析读表 | DuckDB `quant.duckdb` | factor_daily、index_membership、fundamentals、stocks |

> **注意**：K线数据在 SQLite，DuckDB 里没有 `kline_daily` 表。
> `xq/data.py` 已做自动探测 + attach SQLite 回退，改动数据源位置时需留意。

### Medallion 分层

```
L0  sources      tushare / akshare / 东财 / 新浪
     ↓
Bronze  raw/     原始快照（Parquet）
     ↓
Silver          清洗 + 标准化
     ↓
Gold            factor_daily（14 因子）/ signal_daily（选股信号）
     ↓
应用层          回测 / 模拟盘 / 信号 / 归因
```

### 每日流水线

| 时间 | 步骤 |
|---|---|
| 15:35 | 收盘后采集 → 清洗 → 因子计算（14 个） |
| 19:00 | 因子增量入库（tushare 日线 17:00 前未就绪，故后移） |
| 每周六 | 周复盘（有效性统计 + 策略升降级） |

节假日自动静默，非交易日不误报「数据停更」。

---

## 策略

**6 个启用**（`app/core/strategies/registry.py`）：

| Key | 名称 | 类型 | 独立假设 |
|---|---|---|---|
| `chan` | 缠论买卖点 | 单标的择时 | 中枢背驰 + 三类买卖点 |
| `turtle_classic` | 经典海龟 | 单标的趋势 | ATR 仓位 + 金字塔加仓 + 2N 止损 |
| `momentum` | 动量突破 | 单标的趋势 | N 日 ROC 阈值 |
| `xq_rps_breakout` | RPS 强度突破 | 横截面选股 | 相对强度排名 |
| `xq_high_tight_flag` | 高旗形整理 | 横截面选股 | 高紧旗突破 |
| `hs300_enhanced` | 沪深300 增强 | 组合多因子 | 因子合成 + 行业市值中性 |

**15 个已停用**（代码保留，`DISABLED_STRATEGIES` 可随时恢复）：
趋势系 5 个（dual_ma / ma_cross / ma_alignment / turtle / macd_boll）、
均值回归系 2 个（rsi_reversal / bollinger）、
组合多因子 5 个（csi800_enhanced / enhanced_factor / enhanced_factor_hs300 / jk001 / jk002）、
选股信号 3 个（xq_dual_param / xq_private_placement / xq_chan_buy）。

> 停用原因：21 个策略归并后仅约 3 类独立 alpha 假设，保留同质策略只稀释注意力。
> 详见 `docs/REBUILD-2026-10.md` §1.6。

### 信号引擎

9 个选股策略（turtle_trade / chan_buy / rps_breakout / dual_param / ma_volume /
private_placement / high_tight_flag / uptrend_limit_down / limit_up_shakeout）
→ 连续出现天数（streak）聚合 → 质量分级 → 买卖动作判定（含风控三件套：止损 −8% →
移动止盈 → MA10 减仓 / MA20 清仓 → 时间止盈 15 天）→ 镜像组合重放（T+1 开盘成交）。

---

## ⚠️ 评估口径（2026-10-08 修复，务必了解）

**修复前平台输出的所有「策略表现」都不可信**，存在四个系统性缺陷。已修复：

| 问题 | 修复前 | 修复后 |
|---|---|---|
| **风控复权口径** | MA 用后复权、close 用未复权 → 比值 3-13 倍，**MA 破位风控从未触发** | 统一取 `c_hfq` |
| **因子评级丢方向** | `abs(ic_mean)` → 26/37 因子负 IC，7 个被误判「可用」 | 加 `has_pos` 判据，负 IC 单列「反向」 |
| **成本口径双轨** | 主引擎零滑点、无印花税 | 万2.5 + 万1滑点 + 万5印花税（卖出） |
| **Sharpe 未减 rf** | 隐含 rf=0，夏普虚高约 0.03-0.05 | 减 2.5% |

**修复后夏普普遍下降 0.3-1.0，这是暴露真实水平，不是变差。**
旧记录里夏普 0.9-1.2 的单标的回测大多是单股反复调参（`is_tuning=true`），不代表策略有效性。

**GP 挖矿已熔断**（`QUANT_GP_ENABLED=1` 可恢复，但**不推荐**）：
37 个输出全是 `roc(c,6)` 一类单变量变体、无交互项；fitness 与选择在同全样本内完成
（结构性过拟合）；26/37 负 IC 污染注册表。复活前置：样本外框架 + 估值因子 PIT 修复。

### 已知局限（诚实披露）

| 局限 | 影响 |
|---|---|
| **估值因子 PIT 前视** | `Stock` 表只有最新一列快照，复用于所有历史截面 → 2019 截面用 2026 年的 PE。**含估值/市值因子的历史结论不可信** |
| **单标的引擎用收盘价成交** | 简化模型，比次日开盘口径乐观。组合镜像引擎已是 T+1 开盘（正确） |
| **IC 截面数仅 13-31** | 月度截面，t 检验约束极松，因子检验功效不足 |
| **无样本外切分** | 因子挖掘未做 IS/OOS，M3 待补 |
| **组合策略超额 ≈ 0** | hs300_enhanced IR=0.19 是目前最好水平 |

---

## 开发

```bash
# 后端测试（29 条回归断言，M1-6 建立）
cd backend && pytest tests/ -q

# 重跑历史信号（独立脚本，不依赖后端进程）
python scripts/rerun_signals.py --days 250 --with-portfolio
python scripts/rerun_signals.py --days 250 --dry-run    # 预演不写库
python scripts/rerun_signals.py --days 250 --resume     # 断点续跑

# 数据入库
python scripts/seed_index.py --index 000300 --since 2021
python scripts/seed_attributes.py
```

---

## 相关文档

| 文档 | 内容 |
|---|---|
| `docs/REBUILD-2026-10.md` | **效果诊断与重构方案**（审计证据 + M1/M2/M3 路线） |
| `docs/decisions.json` | 决策记录活文档（重大变更自动留痕） |
| `web/public/docs/architecture.html` | 系统架构设计（侧边栏直链） |
| `web/public/docs/manual.html` | 使用说明书（侧边栏直链） |
| `web/public/docs/methodology.html` | 量化方法论（《量化交易》思想提炼） |
| `docs/ARCHIVE/` | 历史方案归档（已实施的提案、已废弃的路线） |
| `DEPLOY.md` | 部署与 launchd 运维 |

---

## 风险提示

本平台为**个人量化研究工具**，非商业级交易系统。实盘交易具有真实资金风险，
请先在模拟环境验证、小资金试跑后再逐步放量。平台不构成任何投资建议。
