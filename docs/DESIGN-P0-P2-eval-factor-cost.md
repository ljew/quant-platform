# 技术设计：评估口径可信度 × 因子实验室报告层 × 成本/OOS 面板

> **与 W45 看板对齐**（`/workspace/quant-planner/BACKLOG-2026-W45.md`，2026-11-08）  
> 本设计的**落地顺序不以 Product 原 P0 徽章 → P1 → P2 实现序**，而以本周 Desk Ready 工程债为先：  
> **W45-1（密钥）→ W45-2（成本默认对齐）→ W45-3（PEAD PIT）**；产品项「评估口径徽章」排在 W45-2 之后，「因子实验室报告层」排在 W45-3 之后；「成本敏感 + Walk-forward OOS」归 **M3-7 / Backlog**，本周不进 Desk Ready。  
> 工作区快照：`/workspace/quant-latest`（本机 `~/quant-platform`，HEAD `4e63519`）  
> 对照：`quant-arch-snapshot.md`、`quant-latest-review.md`、`docs/REBUILD-2026-10.md`、Product P0–P2 草稿  
> 作者：Quant Architect · 日期：2026-11-08（Asia/Shanghai）  
> **硬闸门**：不开实盘下单通道；密钥不进源码；不擅自推远程；本周不轮换 token（OPS-1 由老刘决定）。  
> **本文只写设计，不改业务代码。**

---

## 0. 落地顺序总览（Desk Ready 切片）

一次只一项 **Doing**。下列切片可直接派 Desk；验收对齐看板 ID。

| 序 | 看板 ID | 切片名 | Owner | 前置 | 本周？ |
|---|---|---|---|---|---|
| 1 | **W45-1** | P0-1 密钥出库 | Desk → QA | 无 | ✅ Doing |
| 2 | **W45-2** | P0-3 成本 schema/ORM/前端默认对齐 + API 不传参测试 | Desk → QA | W45-1 合入本地 | ✅ Ready |
| 3 | **W45-3** | P0-2 PEAD `_es_map` 截面 as-of + 量纲统一 | Desk → QA | W45-2 | ✅ Ready |
| 4 | **W45-badge**（产品） | 评估口径徽章（可见性字段 + UI） | Desk → QA | **必须 W45-2 后** | 设计完整；实现跟 W45-2 |
| 5 | **W45-4** | P1-1 成长三因子截面 PIT | Desk → QA | W45-3 | Ready（本周可顺延） |
| 6 | **W45-5** | P1-3 估值 PIT 回退显式标注 | Desk → QA | 可与徽章同批 | Ready |
| 7 | **W45-6** | 测试手抄/行号同步（可并入 W45-2） | QA | W45-2 | Ready |
| 8 | **W45-lab**（产品） | 因子实验室报告层 | Desk → QA | **必须 W45-3 后** | 设计完整；本周后半或下周 |
| — | Backlog | 成本敏感 + Walk-forward OOS（M3-7/M3-3） | — | 评估地基稳 | ❌ 本周不拉 |
| — | OPS-1 / OPS-2 | token 轮换 / 推远程 | 老刘 | W45-1 后 | Blocked |

**为什么徽章不先做**：徽章若在成本默认仍为万3+零滑点时上线，会把「错误默认」钉成用户可见真相，比静默更糟。  
**为什么报告层等 PEAD**：含 `earnings_surprise` 的 IC / 分层 / 相关矩阵在 W45-3 前不可信，报告层会固化污染结论。

---

## 1. 背景与目标 / 非目标

### 1.1 背景

M1/M2 与 B1–B3 在**引擎/挖矿主路径**已修（复权、评级方向、引擎成本万2.5+万1+印花税万5、估值 pe/pb/mv PIT、IC 周频、单次 IS/OOS）。但审查确认：

1. 源码仍有 Tushare token **明文默认回退**（`config.py`、`fetch_alla_bronze.py`）。
2. **API/ORM/前端成本默认仍 0.0003 / 0.0**，UI 不传参 → 绕过 M1-3。
3. 挖矿 **PEAD `_es_map` 非截面 as-of**，且与回测 `_pit_earnings_surprise` **量纲不一致**（挖矿/etl `/100`，回测不除）。
4. 产品侧需要「口径可见」与「因子实验室报告层」，但必须建在真实默认与 PIT 封口之上。

### 1.2 目标

- **工程债（本周）**：密钥出库；成本契约单一真相源；PEAD 挖矿与回测 PIT/量纲对齐。
- **产品（跟排）**：评估路径上佣金·滑点·印花税、PIT/as-of、IS/OOS **可见**；因子实验室默认产出 IC 时序、分层、相关、生命周期；OOS 不过关禁止进研究卡/注册表。
- **设计预留（Backlog）**：成本网格 + 滚动 walk-forward OOS 面板（M3-7/M3-3），本周不实现。

### 1.3 非目标（刻意不做）

- 不挂载 `live.py` / 不开实盘下单通道；不改 QMT 边界。
- 不整仓替换 Qlib / RD-Agent / vn.py 网关。
- 不复活 GP（`QUANT_GP_ENABLED` 保持默认关）；GP fitness 去 `abs(ic)` 仍为复活前置（Backlog P1-4）。
- 本周不轮换 token（OPS-1）；不擅自推远程（OPS-2）。
- 不改策略 alpha 逻辑（多因子权重、选股规则本身）。
- CORS/鉴权加固另开（P1-5）；知识库/NSI 工程化闭环不做。
- PEAD 迁 `ann_date`（中期）；本轮只消「静态全历史」前视 + 量纲统一。

---

## 2. 模块边界

### 2.1 单一真相源（成本）

建议新增（或内联常量模块，Desk 二选一，推荐小文件）：

```
backend/app/core/cost_defaults.py   # 新建（推荐）
  COMMISSION = 0.00025      # 万 2.5
  SLIPPAGE   = 0.0001       # 万 1（分向）
  STAMP_TAX  = 0.0005       # 万 5（仅卖出）
  MIN_COMMISSION = 5.0
  RISK_FREE_RATE = 0.025
```

| 消费者 | 文件 | 动作 |
|---|---|---|
| 引擎（已正确） | `core/engine/backtest_engine.py`、`portfolio_backtest.py` | 改为 import 常量，避免再漂移 |
| API schema | `schemas.py`：`BacktestRequest`、`OptimizeRequest` | 默认改对齐 |
| Paper API | `routers/paper.py`：`PaperTaskCreate` | 默认改对齐 |
| ORM | `models.py`：`BacktestResult`（约 282–283）、`PaperTask`（约 337–338） | 列 default 改对齐；**不强制 migration 改历史行** |
| 路由透传 | `routers/strategy.py`（回测/寻优把 schema 塞进引擎） | 确认透传；可不改逻辑 |
| 纸面引擎 | `core/engine/paper_engine.py` | 读 task 上已存 commission/slippage |
| 前端 | `BacktestPage` / `OptimizePage` / `PaperPage`；`api/client.ts` | 可不显式传（吃新默认）；徽章阶段再展示 |
| 测试 | `tests/test_cost_model.py` + **新建** API 层断言 | 护住「不传参」 |

xq 组合轨（`xq/portfolio.py` 万2.5 + 滑点 0.001）属次要双轨，**本周不改**（记入 Backlog 备注）。

### 2.2 密钥

| 文件 | 动作 |
|---|---|
| `backend/app/config.py` | 删除 `os.getenv(..., "<明文>")` 的默认串；无 env → 空串或显式失败策略（见 §4.1） |
| `backend/scripts/fetch_alla_bronze.py` | 同步：仅读 env（建议统一 `QUANT_TUSHARE_TOKEN`，兼容旧 `TUSHARE_TOKEN`）；无则 `sys.exit` 明确错误 |
| `.env.example` | 已有引导则强化注释「必填，无默认」 |
| **禁止** | 在设计/提交/日志中抄写旧 token 字面量；不在本项轮换 |

### 2.3 PEAD / PIT

| 文件 | 动作 |
|---|---|
| `services/factor_mining.py`：`_es_map` | 改为「按 symbol 存 (report_date, profit_yoy) 序列 + 截面 as-of」；或复用/包装 `multi_factor._pit_earnings_surprise` |
| 挖矿截面循环（约 555、672） | `esv = es_asof(es_tl, sym, snap)`，禁止静态 `es_map.get(sym)` |
| `services/factor_gp.py` | 同改（即使 GP 默认关，避免复活即回归） |
| `services/etl.py`：`_earnings_surprise_latest` | 量纲与 as-of 策略对齐；日频 ETL 可用「当日 as-of」 |
| `core/strategies/multi_factor.py`：`_pit_earnings_surprise` | **量纲真相源**：保持「不除 100」的原始百分点差；挖矿/etl 去掉 `/100` 与之对齐 |
| `_corr_with_existing` | 若仍吃 `es_map`，改为 as-of(latest) 或同序列接口 |

### 2.4 产品层（徽章 / 报告）

| 层 | 文件 | 职责 |
|---|---|---|
| 报告字段 | `factor_mining.evaluate` 返回 dict；回测 `BacktestResult` / `extra_json`；寻优 trial meta；paper create 响应 | 写入 `cost_profile` / `valuation_pit` / `pead_pit` / `oos` |
| API | `routers/strategy.py`、`routers/factor.py`、`routers/paper.py` | 透出字段；注册表门禁 |
| 前端 | `BacktestPage`、`OptimizePage`、`PaperPage`、`FactorMinePage`；可选共享 `EvalBadges.tsx` | 渲染徽章；实验室图表 |
| 研究卡 | `services/research_card*` / 相关 router（若有） | OOS 门禁与实验室生命周期联动 |

### 2.5 责任分工

| 角色 | 负责 |
|---|---|
| Desk | 按序实现 W45-1→3 及后续产品切片；本地小步提交 |
| QA | 每项合入前跑对应测试；W45-6 可与 W45-2 合并 |
| Watch | 服务健康、回归失败复现；不碰密钥/远程/实盘 |
| Architect | 本文；Desk 卡契约时补丁说明 |
| Product | 验收文案/徽章文案；与 Planner 冲突时以 Planner 为准改稿 |
| Planner | 看板 ID / Doing 唯一 |

---

## 3. 数据与评估口径

### 3.1 成本（单一真相）

| 参数 | 值 | 含义 |
|---|---|---|
| commission | `0.00025` | 双边佣金率万 2.5 |
| slippage | `0.0001` | 分向滑点万 1（买加卖减） |
| stamp_tax | `0.0005` | 仅卖出印花税万 5（引擎内，API 可不暴露为请求字段，但报告要写进 `cost_profile`） |
| min_commission | `5.0` | 单笔最低佣金（元） |
| risk_free_rate | `0.025` | Sharpe 减 rf |

**规则**：请求省略 `commission`/`slippage` 时，**必须**落到上表，不得回落 0.0003/0.0。显式传入则尊重调用方（高级用户/网格扫描）。

### 3.2 PIT 门禁规则

| 字段/因子 | 正确口径 | 当前 | 切片 |
|---|---|---|---|
| pe / pb / mv | `StockValuationDaily` + `_attrs_asof` | ✅；表空静默回退 | W45-5 加标记 |
| earnings_surprise | 截面日 as-of + 与回测量纲一致 | ❌ 静态全历史 + `/100` | **W45-3** |
| roe / revenue_yoy / profit_yoy | FundamentalsHistory / FinancialsRaw as-of | ❌ Stock 最新快照 | W45-4 |
| 现金流类 | `fin_asof` by ann_date | ✅ | 保持 |
| 未过 PIT 的因子 | **不得静默**进评估主结论；至少报告标红 / 禁止 register | 部分静默 | 徽章 + 实验室门禁 |

**PEAD 量纲决策（拍板进设计）**：以 `multi_factor._pit_earnings_surprise` 为准（**原始百分点差，不除 100**）。挖矿与 etl 删除 `/100.0`。理由：回测调仓已用该值进多因子；改回测会牵动历史策略可比性；挖矿侧改完后重跑 IC 即可。

**已知残留（诚实披露，中期）**：即便 as-of `report_date`，公告日前视仍可能存在；迁 `ann_date` 不在本周。

### 3.3 IS/OOS

- 因子：`_is_oos_split`，默认 `oos_ratio=0.3`；verdict ∈ {样本不足, 过拟合, 未通过, 通过, 未启用}。
- 过拟合 → 评级降为「弱」；优秀+未通过 → 降为「可用」（已有）。
- **注册表门禁（实验室切片）**：`verdict in {过拟合, 样本不足}` 或 rating ∉ {优秀, 可用} → 禁止 `enabled`；研究卡同门禁。
- Walk-forward 滚动多窗：**Backlog M3-3**，本周不做。

---

## 4. Desk Ready 切片详细设计

### 4.1 W45-1 · P0-1 密钥出库

**改动要点**

1. `config.py`：`TUSHARE_TOKEN = os.getenv("QUANT_TUSHARE_TOKEN")`（无默认）。`Settings.tushare_token` 允许空；在**实际调用 Tushare 的入口**（ETL/拉取）检查：若空则跳过并打明确日志 / 返回可测错误，**禁止静默用硬编码**。
2. `fetch_alla_bronze.py`：`TOKEN = os.environ.get("QUANT_TUSHARE_TOKEN") or os.environ.get("TUSHARE_TOKEN")`；若空：`print` 原因 + `sys.exit(2)`，且**不要**在 import 时就 `ts.pro_api(TOKEN)` 用空串糊弄。
3. 测试：静态扫描测试（推荐）——对 `config.py` / `fetch_alla_bronze.py` 断言不存在长 hex token 字面量；或 `rg` 进 CI 脚本。行为测试：清空 env 后调用拉取路径得到明确失败。
4. **不做**：轮换 token、改 `.git` 历史、推远程。

**验收（对齐看板）**

- [ ] 源码无 token 字面量（两文件）
- [ ] 缺 env 行为可测（失败或禁用，非静默成功）
- [ ] 本项不包含轮换

**风险 / 回滚**：本地若只靠默认串、未设 `.env`，ETL 会挂——这是期望行为。回滚=恢复 getenv 默认（不推荐）；正确做法是补 `.env`（不入库）。

---

### 4.2 W45-2 · P0-3 成本默认对齐 + API 不传参测试

**改动路径（必须点名）**

| 位置 | 旧默认 | 新默认 |
|---|---|---|
| `schemas.py` L119–120 `BacktestRequest` | 0.0003 / 0.0 | 0.00025 / 0.0001 |
| `schemas.py` L213–214 `OptimizeRequest` | 同上 | 同上 |
| `routers/paper.py` L28–29 `PaperTaskCreate` | 同上 | 同上 |
| `models.py` L282–283 Backtest 表 | 同上 | 同上 |
| `models.py` L337–338 PaperTask | 同上 | 同上 |
| 引擎构造 | 已正确 | import 共享常量（防再漂） |
| 前端三页 | 不传 | **可继续不传**（吃新默认）；可选显式传与徽章一致 |

**API 默认行为**

- `POST /strategy/backtest`（及 async）、`POST /strategy/optimize`（及 async）、`POST /paper/tasks`：body 省略成本字段 → 引擎侧参数 == `COMMISSION`/`SLIPPAGE`。
- 向后兼容：显式传旧值仍可用（例如复现历史乐观回测）；文档/README 评估口径段注明「默认已改，旧记录可能偏乐观」。
- SQLite 已有行：不改写历史 `commission`/`slippage` 列；仅影响**新**任务默认。

**测试（W45-6 可合并）**

```python
# tests/test_cost_api_defaults.py（新建）
def test_backtest_request_defaults():
    from app.schemas import BacktestRequest
    r = BacktestRequest(symbol="sh600000", start="2020-01-01", end="2020-12-31", strategy="chan")
    assert r.commission == 0.00025
    assert r.slippage == 0.0001

def test_optimize_and_paper_defaults():
    ...
```

可选：TestClient 打一发最小回测 mock，断言构造引擎时的 commission/slippage（若现有 fixture 重，至少护 schema + 静态「strategy 路由把 req.commission 传入」的源码契约）。

现有 `test_cost_model.py` 必须仍绿。

**前端**：本切片可不改 UI；若 Desk 顺手，在请求里显式带 `commission`/`slippage` 常量亦可（双保险）。

**验收**

- [ ] 不传 commission/slippage 时 schema 默认 = 万2.5 + 万1
- [ ] 引擎既有 cost 测试绿
- [ ] API 层新测试绿

**风险**：用户习惯「不传=旧乐观」会看到夏普下降——属暴露真实水平（README 已有说明）。回滚=改回 schema 默认（不推荐）。

---

### 4.3 W45-3 · P0-2 PEAD 截面 as-of + 量纲统一

**目标实现（推荐接口）**

```python
# factor_mining.py
def _es_timeline(db, syms) -> dict[str, list[tuple[date, float]]]:
    """symbol -> [(report_date, profit_yoy), ...] 升序，仅非空 yoy。"""

def _es_asof(tl, sym, as_of: date) -> float | None:
    """对齐 multi_factor._pit_earnings_surprise：as_of 及以前 ≥2 期 →
    最新期 yoy − 历史均值；**不除以 100**。"""
```

截面循环：

```python
es_tl = _es_timeline(db, syms)
# ...
esv = _es_asof(es_tl, sym, snap)   # 禁止 es_map.get(sym) 静态值
```

**复用策略**：可将 snaps 转成 `_pit_earnings_surprise` 所需 dict 列表再调用，避免两套公式；若循环热路径在意开销，内联同一公式但**单测锁死与 multi_factor 一致**。

**同步改**

- `factor_gp.py`：build_context / eval 路径同样 as-of。
- `etl._earnings_surprise_latest`：改为对「计算日」as-of；去掉 `/100.0`；注释改「与 `_pit_earnings_surprise` 同口径」。
- `_corr_with_existing`：用 `_es_asof(..., latest_date)`。

**测试**

- 构造两期+报告日后置：断言早期截面得不到后期 surprise。
- 同输入下 `_es_asof` == `_pit_earnings_surprise`。
- 断言源码/结果不再 `/100`（挖矿路径）。

**验收（看板）**

- [ ] 历史截面不用最新报告
- [ ] 挖矿与回测量纲一致
- [ ] 相关测试补齐

**风险**：ETL 写入的 `factor_daily.earnings_surprise` 历史行量纲变化 → 需**重算截面**或文档标明断点日；Desk 在 PR 说明「需重跑 factor 日频」。回滚：恢复旧函数（不推荐）。中期 ann_date 另开。

---

## 5. 产品设计 A：评估口径徽章（实现排 W45-2 之后）

### 5.1 目标

在回测 / 挖矿 / 寻优 / 新建模拟盘路径上，让用户**看见**：

- 成本：佣金、滑点、印花税（及是否默认）
- PIT：估值 / PEAD 状态
- IS/OOS：切分与 verdict

不改 alpha；不做成本滑块（那是 M3-7）。

### 5.2 报告字段 JSON shape（规范）

所有成功评估响应**增加**（或写入已有 `extra` / 挖矿 report 顶层）下列字段；缺省由服务端填充，前端只读。

```json
{
  "cost_profile": {
    "commission": 0.00025,
    "slippage": 0.0001,
    "stamp_tax": 0.0005,
    "min_commission": 5.0,
    "risk_free_rate": 0.025,
    "source": "default",
    "label": "万2.5+万1滑点+卖出万5印花税"
  },
  "valuation_pit": {
    "mode": "asof",
    "coverage": 0.92,
    "note": null
  },
  "pead_pit": {
    "mode": "asof",
    "scale": "raw_pp",
    "aligned_with": "multi_factor._pit_earnings_surprise",
    "note": null
  },
  "oos": {
    "enabled": true,
    "verdict": "通过",
    "oos_ratio": 0.3,
    "is": {"n": 100, "ic_mean": 0.03, "t_stat": 2.1},
    "oos": {"n": 40, "ic_mean": 0.02, "t_stat": 1.4}
  }
}
```

**枚举约定**

| 字段 | 取值 | 含义 |
|---|---|---|
| `cost_profile.source` | `default` \| `user` | 是否显式传参 |
| `valuation_pit.mode` | `asof` \| `fallback` \| `unavailable` | B1 时序 / 回退最新快照 / 无数据 |
| `pead_pit.mode` | `asof` \| `legacy_static` \| `unavailable` | W45-3 后应为 `asof`；过渡期若未改完标 `legacy_static` 且 UI 警告 |
| `pead_pit.scale` | `raw_pp` \| `div100` | 只允许 `raw_pp` 进主结论 |
| `oos.verdict` | 见 §3.3 | 与 `_is_oos_split` 一致 |

回测路径若无因子 PIT，`valuation_pit`/`pead_pit` 可为 `{"mode":"n/a"}`（单标的技术指标策略）；多因子/挖矿必须有真实 mode。

**进度回调（挖矿 `progress`）**：在既有 0.06 估值覆盖日志外，增加结构化片段或最终 report 必含上表（进度字符串可不强求 JSON）。

### 5.3 API 契约

| 端点 | 变更 |
|---|---|
| 回测结果 `BacktestResult` | 增可选 `cost_profile: dict`；落库可进 `extra_json` |
| 寻优 `OptimizeTrial` / job 汇总 | 顶层或每 trial 带同一 `cost_profile`（任务级一份即可） |
| `POST /paper/tasks` 响应 | 回显 `commission`/`slippage` + `cost_profile` |
| 因子挖掘 report | 顶层合并 §5.2 字段（`oos` 已有则扩展，不破坏现有 key） |

向后兼容：只增字段；旧前端忽略即可。

### 5.4 前端契约

- 新建 `web/src/components/EvalBadges.tsx`（建议）：输入 `cost_profile` / `valuation_pit` / `pead_pit` / `oos`，输出 3–4 枚 `Badge`。
- 挂载点：`BacktestPage` 结果区、`OptimizePage` 结果头、`PaperPage` 创建成功提示或任务详情、`FactorMinePage` 报告头（已有 Rating Badge，并排）。
- 文案示例：`成本·默认万2.5+万1`、`估值PIT·as-of 92%`、`PEAD·as-of`、`OOS·通过`；`fallback` / `legacy_static` 用警告色。

### 5.5 与 W45-5 合并点

估值 `fallback` 标记与徽章同一 JSON；Desk 可在徽章切片一并做 W45-5（看板允许）。

---

## 6. 产品设计 B：因子实验室报告层（实现排 W45-3 之后）

### 6.1 目标

`FactorMinePage` 默认产出：

1. **IC 时序图**（已有 `ic_series` → 确认默认展示）
2. **多 horizon 热力**（需 API：对 forward∈{5,10,20,40} 或可配列表跑 IC mean；可异步 job）
3. **分层收益**（已有 `group_means` / `long_short`）
4. **相关矩阵**（已有 `corr_with_existing`；扩展为热力表）
5. **生命周期**：`pending_eval → adopted | rejected`（映射注册表 status：`candidate` / `enabled` / `disabled` + 拒绝原因）

### 6.2 模块边界

| 项 | 说明 |
|---|---|
| 复用 | `factor_mining.evaluate` 主报告；不强上 GP |
| 新增 API（建议） | `POST /factor/mine/horizons`：body `{expr, start, end, forwards: [5,10,20,40], ...}` → `{horizon: int, ic_mean, icir, t_stat, oos}[]` |
| 门禁 | `POST /registry/register` 与 `toggle→enabled`：服务端校验最近一次 mine report 的 `oos.verdict=="通过"` 且 `rating in {"优秀","可用"}` 且 `pead_pit.mode!="legacy_static"`（若表达式依赖 es）；失败 400 + 原因 |
| 研究卡 | 创建/推进状态时读同一门禁；OOS 不过关禁止「采纳」 |
| 前端 | `FactorMinePage`：报告区固定五块布局；生命周期按钮调 register/toggle |

### 6.3 生命周期状态机

```
待评估(pending_eval/candidate)
    ├─(OOS 通过 ∧ rating 优秀|可用)→ 采纳(adopted/enabled)
    ├─(用户拒绝 ∨ OOS 过拟合 ∨ 反向未确认)→ 拒绝(rejected/disabled)
    └─(未通过)→ 可保留 candidate，禁止 enabled
```

### 6.4 验收（产品）

- [ ] 默认报告含 IC 时序、分层、相关；多 horizon 至少 3 个窗口
- [ ] OOS 过拟合无法 enabled
- [ ] 不复活 GP
- [ ] W45-3 未合入前本切片不上生产路径

---

## 7. 产品设计 C：成本敏感 + Walk-forward OOS（Backlog / M3-7·M3-3）

本周**不进 Desk Ready**。仅保留设计锚点，避免丢需求。

- **UI**：回测复盘页三块——净值、成本滑块/网格（commission×slippage 网格）、滚动 OOS 窗口表。
- **配置**：单一 `cost_profile` + `oos_windows` 写回；扫描/回测/评分共用（延伸 `cost_defaults`）。
- **引擎**：已有成本模型可参数化；walk-forward 需新服务（多切段 IS→test），替代单次 `oos_ratio`。
- **非目标**：组合层优化 M3-8 二期。

Desk 拉票条件：W45-1…3 完成且徽章/实验室稳定后，由 Planner 写入新周 Ready。

---

## 8. 迁移步骤与落地顺序（给 Desk 的 PR 切片）

### PR-A · W45-1 密钥出库（Doing）

1. 删两处明文默认；拉接口无 env 失败。  
2. 补静态/行为测试。  
3. 更新 `.env.example` 注释。  
4. **停**：不轮换、不 push。

### PR-B · W45-2 成本对齐（+ 可选 W45-6）

1. 加 `cost_defaults.py`（或等价）。  
2. 改 schemas / paper / models / 引擎 import。  
3. `test_cost_api_defaults.py`；跑 `test_cost_model.py`。  
4. README「评估口径」补一句：API 默认已与引擎对齐（文档可另 PR）。  
5. 前端可不改。

### PR-C · W45-3 PEAD

1. `_es_timeline` + `_es_asof`；截面/GP/etl/corr 切换。  
2. 去 `/100`；单测锁与 `_pit_earnings_surprise` 一致。  
3. PR 说明：需重跑 `factor_daily` 中 earnings_surprise。

### PR-D · 徽章（+ W45-5）

1. 服务端填 `cost_profile` / `valuation_pit` / `pead_pit`。  
2. `EvalBadges` + 四页挂载。  
3. 前置：**PR-B 已合**。

### PR-E · 因子实验室报告层

1. horizons API；门禁 register/toggle；生命周期 UI。  
2. 前置：**PR-C 已合**。

### 不做的本周 PR

- 成本网格 / walk-forward 面板  
- GP 复活或 fitness  
- CORS 鉴权  
- 远程推送

---

## 9. 风险与回滚

| 风险 | 缓解 | 回滚 |
|---|---|---|
| 未设 env 导致 ETL 停 | README/.env.example；Watch 告警 | 临时 export env（勿恢复源码默认） |
| 成本默认变严，夏普「变差」 | README 已有话术；徽章标明默认 | 不建议回旧默认 |
| PEAD 量纲变更污染旧 factor_daily | 重算；报告标 scale | 短时 feature flag（可选，非必须） |
| 徽章在旧默认上线 | **硬性前置 W45-2** | N/A |
| 实验室在 PEAD 未修时采纳因子 | **硬性前置 W45-3** + 门禁 | disable 误采纳项 |
| SQLite 双写 | 仍遵守 compose 与宿主机二选一 | — |

---

## 10. 验收清单（可测汇总）

### W45-1

- [ ] `rg` 两文件无长 hex token 字面量  
- [ ] 缺 `QUANT_TUSHARE_TOKEN` 时拉取路径明确失败  

### W45-2

- [ ] `BacktestRequest()` / `OptimizeRequest` / `PaperTaskCreate` 默认 0.00025 / 0.0001  
- [ ] `test_cost_model.py` 全绿  
- [ ] 新 API 默认测试绿  

### W45-3

- [ ] 截面日 T 看不到 T 之后报告期 surprise  
- [ ] 与 `_pit_earnings_surprise` 同输入同输出  
- [ ] 挖矿路径无 `/100`  

### 徽章

- [ ] 回测/挖矿/寻优/建模拟盘响应含 `cost_profile`；UI 可见  
- [ ] fallback / legacy_static 警告色  
- [ ] 不传参时徽章数字 = 万2.5+万1  

### 实验室

- [ ] 五块报告默认可见  
- [ ] OOS 不过关无法 enabled  
- [ ] GP 仍默认关  

### 硬闸门

- [ ] `live.py` 未挂载  
- [ ] 无密钥进源码  
- [ ] 无擅自 push / 无本周 token 轮换  

---

## 11. 取舍说明

| 先做 | 不做/后做 | 理由 |
|---|---|---|
| 密钥出库 | 轮换 token | 安全一票否决且改动面小；轮换属 OPS-1 老刘动作 |
| 成本 schema 对齐 | 先做徽章 UI | 徽章必须反映真实默认 |
| PEAD as-of + 量纲 | 先做实验室美化 | 否则 IC/分层/相关被前视污染 |
| 量纲跟回测（不 `/100`） | 改回测量纲 | 少动策略路径、保历史回测可比 |
| 单次 OOS 门禁 | walk-forward（M3-3） | 地基未稳前滚动窗徒增复杂度 |
| 报告层产品化现有 mine | 复活 GP / 换 ML 栈 | REBUILD 已熔断；OOS+PIT 前置未完 |
| 成本面板 Backlog | 本周 Desk Ready | Planner W45 明确 M3-7 不拉 |
| 不改 alpha | — | Product 边界：只做口径与可见性 |

---

## 12. 附录：关键路径速查

| 主题 | 路径 |
|---|---|
| W45 看板 | `/workspace/quant-planner/BACKLOG-2026-W45.md` |
| 架构快照 | `/workspace/quant-arch-snapshot.md` |
| Code Review | `/workspace/quant-latest-review.md` |
| REBUILD | `quant-latest/docs/REBUILD-2026-10.md` |
| README 评估口径 | `quant-latest/README.md` §评估口径 |
| 成本 schema | `backend/app/schemas.py` |
| Paper 默认 | `backend/app/routers/paper.py` |
| ORM 默认 | `backend/app/models.py` |
| 引擎成本 | `backend/app/core/engine/backtest_engine.py`、`portfolio_backtest.py` |
| PEAD 挖矿 | `backend/app/services/factor_mining.py` `_es_map` |
| PEAD 回测 | `backend/app/core/strategies/multi_factor.py` `_pit_earnings_surprise` |
| GP / ETL | `factor_gp.py`、`etl.py` |
| 注册表 | `backend/app/routers/factor.py` `registry_register` / `toggle` |
| 前端 | `web/src/pages/{Backtest,Optimize,Paper,FactorMine}Page.tsx`、`api/client.ts` |
| 成本测试 | `backend/tests/test_cost_model.py` |

---

*文档结束。Desk 从 PR-A（W45-1）开工；卡片契约以本文 §5.2 为准。*

## 附录 A · Product 徽章中文短标签定稿（2026-11-08）

> Product 定稿；与本文 §5.4 一致。Desk 徽章切片（W45-2 后）可直接用。需要微调文案找 Product。

### 短标签（Badge 主文案）

1. **成本**
   - `cost_profile.source=default` → `成本·默认万2.5+万1`
   - 自定义传参 → `成本·自定义`
   - 悬停/详情用 `cost_profile.label`：`万2.5+万1滑点+卖出万5印花税`
2. **估值PIT**
   - as-of + 有 coverage → `估值PIT·as-of`，可拼 ` {pct}%`（如 `估值PIT·as-of 92%`）
   - `fallback` → `估值PIT·回退`（警告色）
   - `unavailable` → `估值PIT·不可用`（警告）
   - `n/a` → 不渲染该枚
3. **PEAD**
   - `PEAD·as-of`
   - `legacy_static` → `PEAD·静态`（警告色）
   - `unavailable` → `PEAD·不可用`
   - `n/a` → 不渲染
4. **OOS**
   - `OOS·` + `oos.verdict`（如 `OOS·通过`）
   - `enabled=false` → `OOS·未切`

### 色规

- 警告色：`fallback` / `legacy_static` / `unavailable` / OOS 非「通过」
- 其余：默认/中性

### 挂载

`EvalBadges.tsx`；回测 / 寻优 / 模拟盘 / 挖矿报告头。
