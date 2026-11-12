# 技术契约：W48-2「工作集短名单回测验收」

> 作者：Quant Architect · 2026-11-12（Asia/Shanghai）
> 产品方案（已冻结，本契约不得改口）：`/workspace/docs-design-W48-2-workset-backtest-accept.md`
> 设计锚点：日常最小闭环「因子筛选 → 回测验收 → 模拟盘观察 → 一页日报」的第二环；已冻 W46-1 成本敏感、W46-2 walk-forward、W48-1 少而正交工作集
> 对照体例：`docs-design-W48-1-sparse-orthogonal-arch.md`、`docs-design-W46-1-cost-sensitivity.md`、`docs-design-W46-2-walk-forward.md`、`docs-design-W47-2-orthogonalize.md`、`docs-design-W47-3-multiple-testing.md`
> **硬闸门**：不开实盘；密钥不进源码；不擅自推远程；**本文只写设计，不改业务代码。**
> **冻结状态（2026-11-12 Asia/Shanghai）**：**Product 已冻结本技术契约**。对照已冻产品稿 `/workspace/docs-design-W48-2-workset-backtest-accept.md` 确认 **无改口**。下文原标「推荐」的实现补全一并锁定，不重开已冻交易规则 / 成本与 WF 裁判 / 成员通过条件 / 集合状态机。请 Planner 派 Desk；本轮不默认拉全员。
>
> **一并锁定的实现补全**（不是改口）：
> 1. 异步优先（复用已有 `task_queue`）；同步为工期退路，不得减窗 / 改裁判 / 用 `enhanced_factor` 顶替。
> 2. 默认区间 `2023-01-01`～`2025-06-30`（与 `BacktestPage` 控件一致，用户可改；不够窗下限则该成员「样本不足」）。
> 3. WF 切窗日历用基准 `sh000906`，不得沿用页面 `sh600519`。
> 4. 成本×2 超额算不出且 WF `final` 已通过 → 该成员「无法按回测口径验收」，不得标通过（不能确认非 `fragile`）。
> 5. 深链 `#btopt/0/workset-accept`（用已有 `inner` 段，不是新 hash Tab；禁止 `#workset-accept` 一级路由）。
> 6. 成交只调 `_run_portfolio`，不走会落库的 `_do_backtest` / `_persist`（避免串主区净值，与产品「验收结果不写进主区曲线」一致）。
> 7. 内部类 `WorksetMemberLongTopN` 不进公开注册表；过期含工作集成员/`expr` 变化，以及区间 / `adj` / `rules_version` 与快照不一致；`POST` 若 `adj≠qfq` 或试图改写死规则 → 400 `rules_param_forbidden`。
> 集合字面推论一并锁定：n=3、两人通过、一人无法验收 → 集合 **通过**（可跑者全通过、无拒绝、n≥3）；测例须覆盖，不得补严成必须全员可跑。薄壳可以安全落地；实现中若系统性 PIT 对不齐须停工回 Planner / Product，禁止顶替。
>
> 读码基线：GitHub `ljew/quant-platform` main `79671a4e825dd4eaf2866747330efd2cdbd8481e`（committer `2026-11-11T21:00:48Z` = **2026-11-12 05:00 Asia/Shanghai** 合入 origin/dev）。**不要**用 `/workspace/quant-latest` 或 `quant-platform-src-latest.tgz`（HEAD `4e63519`，已滞后）。

---

## 0. 结论 / 推荐 / 取舍 / 落地序（先读）

**只做一件事**：在已有 `#btopt` → `BacktestOptimizePage` → 子页「单次回测」`BacktestPage` 上挂「工作集短名单验收」薄块。验收对象是当前工作集成员**快照**（`registry_id` + 当时 `expr`），**逐成员各做一本**中证800 多头等权 Top 50 交易簿，再用已冻 W46-1 / W46-2 默认成本与滚动 OOS 给出通过/拒绝，集合只汇总。不新开 hash，不改裁判，不 bump `n_tested`，不自动 `enabled` / 移出，**禁止**用 `enhanced_factor` / `enhanced_factor_hs300` / `csi800_enhanced` 的固定因子库或权重顶替，**禁止**把多名成员合成一本再评组合 IR（那是 M3-8）。

**薄壳可否在本切片安全落地：可以。** 未发现必须用 `enhanced_factor` 顶替的阻塞。路径是新建内部 `PortfolioStrategy` 薄壳 + 新编排服务，成交走现有 `_run_portfolio`（不走会落库的 `_do_backtest`），成本 / WF 调已有 **Python 服务函数**（自备 runner，不走要求注册表 `strategy` key 的 HTTP 端点）。风险是墙钟与量能序列对齐（见 §9.2），不是不可落地。**任何情况下禁止用固定风格库顶替。**

**模块落地（与产品 §7 对齐，路径写死）**

| 层 | 本切片 | 不要动 |
|---|---|---|
| 交易薄壳 | 新增 `backend/app/core/strategies/workset_long_topn.py` → `WorksetMemberLongTopN`（单表达式 → 中证800 多头 TopN 等权）。**不**写入 `registry.py` 的公开 `STRATEGIES`（不是本切片必须的「永久新策略」） | 改 `EnhancedFactorStrategy` / `Csi800EnhancedStrategy` 的固定 `FACTORS`、权重或中性开关；新写组合优化引擎；复制 `_run_portfolio` / `PortfolioBacktestEngine` 成交撮合 |
| 编排 | 新增 `backend/app/services/workset_backtest_accept.py`：读工作集快照 → 逐成员跑已有 `run_cost_sensitivity`（`mode=multipliers`，至少 ×1/×2）与 `run_walk_forward` → 按 §5 汇总并落快照结论 | 改 `cost_sensitivity.py` / `walk_forward.py` 的判定公式；改 `factor_lab_gate`；改 `factor_workset.admit` / `remove_member` / 相关阈值 |
| 路由 | `backend/app/routers/strategy.py`：`POST /strategy/backtest/workset-accept`、`GET /strategy/backtest/workset-accept`（及异步时的任务查询）；`web/src/api/client.ts` 补方法 | 修改 `/factor/mine`、`/factor/workset/admit` 语义；把验收接到已有 `/backtest/cost-sensitivity` HTTP 并塞注册表 key |
| 存储 | 薄表 `factor_workset_accept_run`（一次验收一行，绑定快照）；读取时现场算过期 | 写 `factor_mt_state`；改 `FactorRegistry.status`；把结论写进主区 `Backtest` 记录冒充「当前策略」 |
| 前端 | 仅 `BacktestPage.tsx` 增一块（可抽 `WorksetAcceptPanel`）+ `FactorMinePage` 工作集卡一个深链 + 类型；为锚点允许给 `BacktestOptimizePage` / `App.tsx` 传已有 `inner` 段（**不是**新 hash Tab） | 大改 `BacktestPage` 主表单；动 `OptimizePage` / `TodayPage` / `#paper`；新开 `#workset-accept` Tab |
| 测试 | 新增 `backend/tests/test_workset_backtest_accept.py`（及薄壳单测）；保留 W46 / W48-1 回归 | 弱化已有 cost / WF / workset / lab / ortho / mt 测试 |

**实现细节推荐（产品已冻规则下的补全，Desk 按此写死）**

1. **薄壳内部类，不进公开注册表。** 编排层自造 `meta`（`cls=WorksetMemberLongTopN`，`multi_asset=True`，`index_code="000906"`，`index_symbol="sh000906"`，`pool_mode="index"`），直接调 `_run_portfolio`。禁止 `get_strategy("enhanced_factor")` 等。
2. **求值口径**：`rebalance` 内对 PIT 成分（`ctx.universe()`）用 `app.datahub.ns_vars.make_ns` + `eval_factor`（与实验室同一命名空间，不是 `FACTORS` 循环）。价格序列取自 `PortfolioContext`（与成交同一套已载入 `req.adj`，默认 `qfq`）。估值走 `ctx.attribute`（PIT）；成长/质量、PEAD、财务走引擎已注入的 `fundamentals` / `financials_raw` + `fin_asof`。量额从已载入 `engine.data` bars 按日对齐（引擎 `hist` 目前只存收盘，**不改**成交逻辑）。按因子值**降序**取最高 50（越大越优；入集时 OOS IC 已要求为正，本切片不翻向）。
3. **交易规则写死**：Top 50、21 日、单只 `min(1/n, 0.05)`，无空头、无行业/市值中性。不足 50 能选多少持多少，结果注明 `n_held`。有效因子值为 0 则该成员「无法按回测口径验收」。`params` 传 `max_position_pct=0.05` 让引擎风控再夹一次；为满足 `make_ns` 窗口，向 `_run_portfolio` 已有 warmup 公式传入 `momentum_lookback=126` 等（**只为取数窗口**，不是启用动量因子）。
4. **成本 / WF**：一律平台默认，**不**继承表单 `grid_pick` / 残留 commission。成本：`run_cost_sensitivity`，`mode="multipliers"`，`multipliers=[1.0, 2.0]`，`base_commission=COMMISSION`，`base_slippage=SLIPPAGE`。WF：`mode=walk_forward`、`n_windows=3`、`oos_ratio=0.3`、`scheme=expanding`、`metric=sharpe`、`min_is_bars=60`、`min_oos_bars=20`，`final=stricter(agg, legacy)`。成员通过 = WF `summary.final_verdict=="通过"` **且** 成本 `summary.fragile==False`。此处 `fragile` 是 W46-1 成本定义，**不是** WF 的 `fragile_regime`。
5. **切窗日历**：WF 的 `bar_dates` 用基准 `sh000906`（`_load_index_bars`），**不得**沿用页面 `sh600519`。
6. **默认区间**：与 `BacktestPage` 控件一致，推荐默认 `2023-01-01`～`2025-06-30`，用户可改；不够 W46-2 窗下限则该成员「样本不足」。`initial_cash=1_000_000`，`adj="qfq"`。
7. **调用方式推荐异步**：每名成员约 10 次中证800 `_run_portfolio`（×1/×2 + 3 窗 IS/OOS + legacy IS/OOS），n=5 约 50 次，已有 cost/WF 路由已警告组合策略墙钟超时。推荐复用 `app.core.task_queue`（与 `submit_backtest` 同构）：`POST` 立即返回 `job_id`，`GET` 查最新结论（含 running）。同时只允许一个 running。若 Desk 工期不够，允许第一版同步 `POST`（须在 UI 写明可能数分钟、勿刷新），**不得**为此减窗、改裁判或用 `enhanced_factor` 顶替。
8. **过期**：读时比较当前工作集 `{(registry_id, expr)}` 与快照；不等则过期。区间/规则版本与快照不一致同样不得显示为有效结论。新 `POST` 覆盖「最新一次」；不把净值写入主区当前策略曲线（不调 `_do_backtest` / `_persist`）。
9. **成员失败**：单人求值失败 / 引擎异常 → 该人「无法验收」，其他人继续；不顶替净值。成本×2 算不出且 WF 已通过 → 推荐「无法验收」（不能确认非 fragile），不得标通过。
10. **W48-1 已在本 SHA 的 main 上。** 若运行分支缺工作集表/服务：面板说明「工作集服务不可用」，`POST` 拒绝；本切片不重做入集。

**落地序（约 0.75–1 Desk 日）**：薄壳+单测 → 验收服务（汇总/快照/过期）+单测 → 路由（含任务）→ 前端块+深链+client.ts → 口径回归。详见 §9.1。

---

## 1. 背景 / 目标 / 非目标

### 1.1 背景（读码，main `79671a4`）

- W48-1 已在 `#factorlab` 落地：`factor_workset.py`（`list_workset` / `admit` / `remove_member`）、`GET /factor/workset*`、`WorksetCard`。入集成功文案停在「已写入工作集。入集不会自动启用。」**无跳到 `#btopt` 的出口。**
- `#btopt`：`web/src/App.tsx` `HUBS.research.tabs` `{ key: "btopt" }` → `BacktestOptimizePage`（`MergedTabs`：`backtest`→`BacktestPage`，`optimize`→`OptimizePage`）。`BacktestPage` 默认标的 `sh600519`、区间 `2023-01-01`～`2025-06-30`、`initial_cash=1_000_000`。成本网格与滚动 OOS 已挂结果区，对象是注册表 `strategy` key，**不认识工作集**。
- 成本：`POST /strategy/backtest/cost-sensitivity` → `run_cost_sensitivity`；`cost_defaults.COMMISSION=0.00025`、`SLIPPAGE=0.0001`；`fragile` = 默认格超额>0 且 ×2 超额≤0（`cost_sensitivity.build_summary`）。徽章主文案已冻。HTTP 端点经 `get_strategy(req.strategy)`。
- WF：`POST /strategy/backtest/walk-forward` → `run_walk_forward`；默认 3 窗 expanding / sharpe / `oos_ratio=0.3`；`final=stricter(agg, legacy)`（`walk_forward.stricter` / `run_walk_forward`）。窗级四态已冻。同样要注册表 key；切窗用 `req.symbol` 的 bar 日期。`summary.fragile_regime`（窗过拟合或 final 相对 legacy 降级）**不是**成员验收用的 `fragile`。
- 组合引擎：`strategy._run_portfolio` 用 `meta.index_code` / `index_symbol`（缺省即 `000906` / `sh000906`）经 `membership_store.get_membership` 加载 PIT 成分，成交走 `PortfolioBacktestEngine.run`。`csi800_enhanced` / `enhanced_factor` 的注册条目已是这套池与基准，但策略类读**固定因子库**（`factor_library.FACTORS` 或内置动量/反转/低波），**不读 `factor_workset`**。
- 实验室求值：`factor_expr.eval_factor` + `ns_vars.make_ns`（`c_m`/`c_r`/估值/财务等）。工作集成员表达式是这条命名空间，不是 `EnhancedFactorStrategy` 内部循环。
- `_do_backtest` 会 `_persist` 成 `Backtest` 行，并成为主区「当前策略」对象。验收路径**不应**走这条，以免串对象、污染历史。
- 本仓 `/strategy/*` **无鉴权**（个人本机投研台）。API 前缀 `settings.api_prefix=/api/v1`。

### 1.2 目标

1. 用户在 `#btopt` 单次回测页看到独立块「工作集短名单验收」：n/5、成员、写死口径条、「按默认成本验收」、逐成员结论与集合汇总、过期提示。与当前选中的 `dual_ma` 等**不是同一对象**。
2. 每个成员按冻结交易规则出一本簿，复用已冻成本 ×1/×2 `fragile` 与默认 3 窗 WF `final`，给出 通过 / 拒绝 / 样本不足 / 无法按回测口径验收。
3. 集合按 §5.3 汇总为 通过 / 拒绝 / 未齐 / 无法判定（拒绝优先于未齐）。
4. `#factorlab` 工作集卡增加「去回测验收」深链到该块；实验室不复制验收 UI。
5. 快照绑定与过期；不 bump `n_tested`，不自动启用/移出。

### 1.3 非目标（本切片刻意不做）

| 不做 | 归属 |
|---|---|
| M3-8 组合层优化（风险平价 / 约束优化 / 成员加权合成 / 组合 IR 新门槛） | 产品非目标 |
| 改 `EnhancedFactorStrategy` 或 `csi800_enhanced` 去消费 `factor_workset`；用其固定库顶替结论 | 产品明确禁止 |
| 复活 GP；改实验室质量门、W47-2/3、W48-1 相关阈值或容量、admit 语义 | 已冻 |
| 改裁判：夏普 / 印花税 / PIT / 复权 / WF 窗常数 / 成本默认刻度 / `fragile` 定义 | 已冻 W46-1/2 |
| 完整 comm×slip 网格热力（可沿用已有面板，不是本切片交付）；把验收做到寻优子页 | 产品非必须 / 禁止 |
| 重做 W46-3；把稳健摘要搬回 `#today`；本切片改日报职责 | 已冻 |
| 模拟盘大改、资讯、智能体、开实盘 | 硬闸门 / 非目标 |
| 新开 hash Tab；工作集与 `enabled` 打成同一集合；验收失败自动移出或自动禁用 | 产品禁止 |
| 把薄壳做成注册表里给用户点选的永久新策略 | 产品：非必须 |

不对齐为本切片硬门槛（沿用产品 / W48-1）：「组合 OOS 超额>0 且 IR≥0.3」。成本翻倍后超额>0 只通过已有 `fragile` 落到**成员**拒绝，不新造集合阈值。也不新造「默认格超额>0」作为成员通过硬门（默认格超额≤0 时 W46-1 已规定 fragile 不适用）。

---

## 2. 模块边界

### 2.1 关键取舍：内部薄壳 + 新编排服务，不 fork 引擎、不顶替固定库

| 方案 | 利 | 弊 | 结论 |
|---|---|---|---|
| A. 改 `EnhancedFactorStrategy` 读工作集 / 关中性后当验收对象 | 少写类 | 固定 `FACTORS` 会偷换对象；默认开行业/市值中性；产品明确禁止 | **禁止** |
| B. 把工作集表达式塞进已有 HTTP `/cost-sensitivity` + 注册表新 key | 少一条路由 | 必须公开永久策略；用户可能在主表单误跑；HTTP 仍是单对象 | **不选**（非必须，且易串对象） |
| C. **新建内部 `WorksetMemberLongTopN`**，编排服务自备 `meta` 调 `_run_portfolio`；成本/WF 调 Python 服务 + 自定义 runner | 边界清；不改裁判；不污染注册表与主区历史 | 须对齐 `make_ns`；组合回测墙钟 | **选 C** |
| D. 复制一份成交/调仓引擎 | 无依赖 | 双份撮合必然漂移 | **禁止** |
| E. 多名成员合成一本再评组合 IR | 一次跑完 | 即 M3-8，产品禁止 | **禁止** |
| F. 薄壳工时内「无法安全落地」时用 `enhanced_factor` 顶替或只做入口空态 | 看起来有结论 | 产品：禁止顶替；且本切片薄壳**可以**落地，不采用空态退路冒充已验收 | **禁止顶替**；若实现中发现新的安全阻塞，停工回 Planner/Product，不要自行换对象 |

**薄壳职责** `WorksetMemberLongTopN(PortfolioStrategy)`（`init` / `rebalance`，基类 `app.core.engine.base_strategy.PortfolioStrategy`）：

- 读死参数：`expr`、`top_n=50`、`rebalance_period=21`、`max_weight=0.05`。`neutralize_industry` / `neutralize_marketcap` **不实现、不读取为真**。
- `rebalance(ctx, date)`：`universe = ctx.universe()`（引擎 PIT 成分）；对每只构造 `make_ns` + `eval_factor(expr, ns)`；非有限值跳过；降序取前 `top_n`（不足则全取）；`w = min(1/len(selected), max_weight)`；对 `universe ∪ 已持仓` 下 `order_target_percent`（入选为 `w`，其余已持为 0）。**不做空头**（`order_target_percent` 已夹在 [0, 1]；禁止负仓）。
- 通过 `ctx.report_factor` 上报该成员表达式值（调试/持仓说明用），不把结果当成实验室 IC 报告。
- 实际持仓数、有效求值数写入引擎可观察字段或由编排层从 `holdings` / `trade_count` 汇总，供 API 注明。

**编排服务职责** `backend/app/services/workset_backtest_accept.py`：

| 符号（建议名） | 职责 |
|---|---|
| `ACCEPT_RULES_VERSION = "W48-2-v1"` | 规则指纹；版本变则旧结论不得当有效 |
| `ACCEPT_TOP_N = 50`、`ACCEPT_REBALANCE_PERIOD = 21`、`ACCEPT_MAX_WEIGHT = 0.05` | 写死，API 不可调 |
| `ACCEPT_INDEX_CODE = "000906"`、`ACCEPT_INDEX_SYMBOL = "sh000906"` | 与 `registry["csi800_enhanced"]` / `["enhanced_factor"]` 的池/基准元数据一致，**只复用加载，不实例化那些策略类** |
| `ACCEPT_DEFAULT_START = "2023-01-01"`、`ACCEPT_DEFAULT_END = "2025-06-30"` | 与 `BacktestPage` 默认控件一致 |
| `migrate_factor_workset_accept(conn)` | 建薄表，由 `database._migrate_sqlite` 调用 |
| `build_member_meta(expr) -> (meta, params)` | 内部 meta + 参数（含 warmup 用 lookback、`max_position_pct`） |
| `run_member(db, member, start, end, ...) -> MemberAccept` | 一人：成本 multipliers + WF；映射 §5.2 |
| `aggregate_collection(n, member_results) -> str` | §5.3 集合状态机（纯函数，易测） |
| `snapshot_fingerprint(members) -> list[{registry_id, expr, name, added_at}]` | 快照 |
| `is_stale(row, current_workset, request_range=...) -> (bool, reason)` | 读时过期 |
| `start_accept(db, *, start, end, adj, initial_cash) -> dict` | 空集拒绝；否则开任务/同步跑并落库 |
| `get_latest_accept(db, *, start=None, end=None) -> dict` | 最新结论 + 现场过期 + 当前工作集列表 |

**允许的调用（只复用，不改公式）**

| 符号 | 用法 | 约束 |
|---|---|---|
| `strategy._run_portfolio(db, req, meta, params)` | 薄壳成交 | 不改函数；不调用 `_do_backtest` / `_persist` |
| `cost_sensitivity.run_cost_sensitivity(req, runner)` | ×1/×2 与 `fragile` | `runner(commission, slippage)` 内建 `BacktestRequest` 后调 `_run_portfolio`；`req.mode="multipliers"` |
| `walk_forward.run_walk_forward(req, bar_dates, runner)` | 默认 3 窗与 `final` | `bar_dates` 来自 `_load_index_bars(..., sh000906)`；`runner(start, end)` 用临时 req 裁区间（与现路由组合分支一致：`bt.model_copy(update={"start", "end"})`） |
| `factor_workset.list_workset` | 只读当前成员 | **禁止**从本服务调用 `admit` / `remove_member` / `bump_n_tested` / `check_enable_gate`（验收不是入集，质量门保持为入集门） |
| `eval_factor` / `make_ns` / `fin_asof` | 薄壳求值 | 不另写表达式引擎 |

**禁止**：在 runner 里 `get_strategy("enhanced_factor")` 或实例化 `EnhancedFactorStrategy` / `Csi800EnhancedStrategy`；把多名 `expr` 一次喂给一个策略；改 `DEFAULT_MULTIPLIERS` / `judge_strategy_window` / `stricter` / `build_summary`。

### 2.2 后端路径表

| 层 | 路径 | 本切片职责 |
|---|---|---|
| 薄壳 | `backend/app/core/strategies/workset_long_topn.py` | 单表达式选股下单 |
| 新服务 | `backend/app/services/workset_backtest_accept.py` | 编排、汇总、快照、过期 |
| 组合装配 | `backend/app/routers/strategy.py` → `_run_portfolio` / `_load_index_bars` | **只调用**；池与基准加载、估值 PIT、财务注入保持原样 |
| 成本 | `backend/app/services/cost_sensitivity.py` → `run_cost_sensitivity` / `build_summary` / `resolve_axes` | **只调用，不改符号** |
| WF | `backend/app/services/walk_forward.py` → `run_walk_forward` / `judge_strategy_window` / `aggregate_verdicts` / `stricter` | **只调用，不改符号** |
| 工作集 | `factor_workset.list_workset` | **只读** |
| 计数 / 门禁 | `factor_mt.bump_n_tested`、`factor_lab_gate.*` | **禁止调用 bump**；不改门禁 |
| 路由 | `backend/app/routers/strategy.py` | 新增验收端点；薄封装 + `HTTPException` |
| ORM / 迁移 | `models.py` 新 `FactorWorksetAcceptRun`；`database._migrate_sqlite` | 加法、可逆 |
| 任务（推荐） | `app.core.task_queue`（已有，`submit_backtest` 在用） | 验收任务；不要新框架 |

### 2.3 前端路径表

| 挂载点 | 说明 |
|---|---|
| `#btopt` → `BacktestOptimizePage` → **仅**「单次回测」`BacktestPage` | 新增独立块，建议组件 `web/src/components/WorksetAcceptPanel.tsx`，`id="workset-accept"` |
| `FactorMinePage.tsx` → `WorksetCard`（及可选 `WorksetApplySection`） | 「去回测验收」→ `#btopt/0/workset-accept`（见 §7.3） |
| `web/src/api/client.ts` | `worksetAcceptStart` / `worksetAcceptGet`（及任务查询）+ 类型 |
| `App.tsx` / `BacktestOptimizePage` | **仅**为已有 `inner` 段接线：`HubContent` 把 `inner` 传给 `BacktestOptimizePage`，选中 `backtest` 子页并滚动到锚点。**不**改 `HUBS` / `LEGACY_TAB_MAP`，不新增 Tab key |
| `OptimizePage` / `TodayPage` / `PaperPage` | **不改** |
| 已有 `CostSensitivityPanel` / `WalkForwardPanel` | **不改行为**；验收块不复用它们的「应用此成本」写回，以免污染主区 |

### 2.4 与已有对象的边界（勿混）

| | 本切片验收 | 主区单次回测 | 实验室入集门 | `enhanced_factor` |
|---|---|---|---|---|
| 对象 | 工作集成员快照逐本簿 | 注册表策略 key | 挖掘报告 IC / IS 内折 | 固定 10–14 个风格因子 |
| 成本 / WF | 平台默认，忽略 `grid_pick` | 用户可改、可 grid_pick | 无成交 | 若用户手动选该策略才会跑 |
| 通过一词 | WF `final` + 非成本 `fragile` | 无集合裁决 | `factor_lab_gate` | 与工作集无关 |
| 计数 | 不 bump `n_tested` | 不经 mine | 入集已规定不 bump | — |

页上必须有区分句（产品口径，可上卡片）：

> **工作集短名单验收**：按与回测相同的默认成本（万 2.5+万 1）和滚动 OOS 口径，逐个验收工作集成员。通过要看样本外净值和成本翻倍后是否还站得住，不是再看一遍挖掘报告的 IC。

---

## 3. 快照模型、存储与过期

### 3.1 关键取舍：薄表存「最近一次（及进行中）」，过期读时计算

产品只要求能读回「对哪次快照、什么结论、是否过期」。不需要完整历史（非目标）。

```text
factor_workset_accept_run (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at      DATETIME NOT NULL,          -- UTC，与现有模型 datetime.utcnow 一致
  finished_at     DATETIME,
  status          TEXT NOT NULL,              -- pending | running | done | error
  job_id          TEXT UNIQUE,                -- 异步任务 id；同步也可填
  start           TEXT NOT NULL,              -- YYYY-MM-DD
  end             TEXT NOT NULL,
  adj             TEXT NOT NULL DEFAULT 'qfq',
  initial_cash    REAL NOT NULL DEFAULT 1000000,
  rules_version   TEXT NOT NULL,              -- ACCEPT_RULES_VERSION
  -- 写死规则回显（便于旧行自解释，不以客户端为准）
  index_code      TEXT NOT NULL DEFAULT '000906',
  index_symbol    TEXT NOT NULL DEFAULT 'sh000906',
  top_n           INTEGER NOT NULL DEFAULT 50,
  rebalance_period INTEGER NOT NULL DEFAULT 21,
  max_weight      REAL NOT NULL DEFAULT 0.05,
  cost_commission REAL NOT NULL,              -- 默认 COMMISSION
  cost_slippage   REAL NOT NULL,
  wf_mode         TEXT NOT NULL DEFAULT 'walk_forward',
  wf_n_windows    INTEGER NOT NULL DEFAULT 3,
  wf_oos_ratio    REAL NOT NULL DEFAULT 0.3,
  wf_scheme       TEXT NOT NULL DEFAULT 'expanding',
  wf_metric       TEXT NOT NULL DEFAULT 'sharpe',
  snapshot_json   TEXT NOT NULL,              -- 见下
  members_json    TEXT,                       -- 逐成员结论（running 时可部分写入）
  collection_verdict TEXT,                    -- 通过|拒绝|未齐|无法判定；error 时可空
  error_message   TEXT,
  note            TEXT
)
```

ORM：`class FactorWorksetAcceptRun(Base)`，`__tablename__ = "factor_workset_accept_run"`。

**快照 `snapshot_json`**（验收开始时冻结，之后工作集再变也不改这一行）：

```json
{
  "n": 3,
  "members": [
    {
      "registry_id": 12,
      "expr": "roc(c_m, 20)",
      "name": "动量20",
      "added_at": "2026-11-11T04:01:00",
      "rating": "可用",
      "t_stat": 2.31
    }
  ]
}
```

至少必须有 `registry_id` + `expr`（产品冻结）。`name` / `added_at` / 主路评级与 t 建议一并快照作展示（列表现场评级仍以 `list_workset` 为准；过期后旧结论用快照展示，避免和当前成员对不上）。

**不存** `stale` 列：每次 `GET` / 面板刷新用 `is_stale` 现场算。

### 3.2 何时创建、幂等、与主区隔离

- 用户点「按默认成本验收」→ `POST`：对**当时** `list_workset()` 拍照并创建一行 `status=running`，然后逐成员跑。
- **空集不可跑**：`POST` 返回 400，`error_code=empty_workset`，不建有效结论行（可建也可不建；推荐不建，避免 GET 把空跑当成一次「无法判定」历史干扰过期语义——空集本身集合就是「无法判定」，面板直接根据 `list_workset.n==0` 展示即可）。
- **幂等推荐**：同一时刻只允许一个 `running`（再 `POST` → 409 `job_running`，body 带已有 `job_id`）。任务结束后新的 `POST` **新增一行**，`GET` 取 `id` 最大的已完成或正在跑的一行（进行中优先展示进度）。本切片不提供删除/列表历史 API。
- **不**调用 `_do_backtest` / `_persist`，**不**把成员净值写进主区「当前策略」曲线，**不**触发 `CostSensitivityPanel` 的 `onApplied` / `cost_source=grid_pick`。
- 验收**不写** `factor_mt_state`，不改历史 mine 评级，不改 `FactorRegistry.status`，不改 `factor_workset` 行。

### 3.3 过期触发（与产品 §4.5 对齐）

下列任一成立，上次集合与成员结论一律**不得继续显示为有效通过/拒绝**，须重跑：

1. **工作集成员集合变化**：入集 / 移出 / 替换后，当前 `{(registry_id, expr)}` ≠ 快照集合（替换在 W48-1 是删旧插新，集合差即可捕获）。
2. **同一 `registry_id` 表达式已变**：即使 id 仍在集内，`expr` 字符串与快照不同（注册表表达式被改；今日若无改 expr API，仍按此比较，防止未来漏网）。
3. **推荐（产品「结论绑定区间、池、规则、成本与 WF 默认版本」的补全）**：`rules_version` 与当前常量不同；或面板当前区间 / `adj` 与该行不一致——文案用「与当前区间/口径不一致，请重跑」，与「工作集已变更，结论已过期，请重跑」区分。二者都不得当有效结论。

过期后：`stale=true` + `stale_reason`；仍可展示旧结论（灰显），但集合徽章不得维持有效「通过/拒绝」。不自动重跑。不自动移出成员。

### 3.4 迁移（加法、可逆）

对齐 `factor_workset.migrate_factor_workset`：

1. `models.py` 注册新类（新库走 `create_all`）。
2. `migrate_factor_workset_accept(conn)`：`CREATE TABLE IF NOT EXISTS ...`。
3. `database._migrate_sqlite` 在现有 `migrate_factor_workset(conn)` **之后**调用。
4. 回滚：`DROP TABLE IF EXISTS factor_workset_accept_run`。工作集、注册表、mine、mt、回测历史均不受影响。无数据回填。

本切片**不**借机打开 `PRAGMA foreign_keys=ON`。

---

## 4. 逐成员交易簿（薄壳如何跑）

### 4.1 股票池、基准、PIT（复用装配，不新造池）

对每个成员，`_run_portfolio` 的 `meta`：

- `index_code="000906"`，`index_symbol="sh000906"`，`pool_mode="index"`（或省略，装配默认即 `"index"`）。
- 成分：`membership_store.get_membership(db, index_code, sd, ed)`（已有 PIT）。禁止 `pool_mode="all"` / `"signal_union"`。
- 基准：`_load_index_bars(..., "sh000906")`。
- 估值 / 财务 / 基本面：走 `_run_portfolio` 已有 `load_valuation_series`、`FundamentalsHistory`、`FinancialsRaw` 注入，不另写加载。

**禁止**把页面标的 `sh600519` 当成验收股票池或 WF 切窗日历。请求里的 `symbol` 字段（成本/WF schema 必填）统一填 `sh000906`，仅作身份与切窗。

### 4.2 因子求值（`make_ns` + `eval_factor`，等价 PIT）

工作集 `expr` 是实验室白名单变量（`ns_vars.var_names`：`c_m`、`pe_ttm`、`fcf_yield` 等）。薄壳必须走同一 `make_ns`，**禁止**用 `EnhancedFactorStrategy` 对 `FACTORS` 的循环来「近似」。

**推荐适配（不改引擎撮合）**，在调仓日 `date` 对 `sym`：

| `make_ns` 参数 | 来源 |
|---|---|
| `seg` | `ctx.history(sym, n)`（真实交易日收盘，与成交同一 `req.adj`，默认 `qfq`） |
| `mkt_all` | `ctx.benchmark_aligned(sym)` 或 `ctx.benchmark_history`，与 seg 对齐 |
| `attrs` | `pe_ttm`/`pb`/`market_cap` ← `ctx.attribute`（PIT）；`industry` ← 属性快照；成长三字段 ← 对 `ctx.engine.fundamentals[sym]` 做 as-of（与 `multi_factor._pit_fund_attrs` 或 `factor_mining._apply_fund_asof` 同一「report_date ≤ 今日」口径） |
| `esv` | 对 fundamentals 做盈余惊喜（`_pit_earnings_surprise` 或 `_es_asof` 等价：仅用可见报告期） |
| `vols` / `amts` | 从 `ctx.engine.data[sym]` 已载入 bars 的 `volume`/`amount` 按 `hist_dates` 对齐；bars 无该字段则 `None`（`make_ns` 已有语义：未传入为 None，量能表达式求值失败则该票跳过）。**不要**为补量能去改 `PortfolioBacktestEngine` 成交路径 |
| `fin` | `fin_asof` 作用于 `ctx.financial(sym)`（装配已按池注入；`ann_date ≤ 今日`） |
| `news` | 本 SHA `TEXT_FACTOR_ENABLED=False`，`make_ns` 不输出 `news_senti`；不必加载新闻 |

窗口长度与 `make_ns` 一致（`c_m` 126 等）。`len(seg)` 过短则该票跳过（实验室最新截面是 `len(seg)<60` 跳过；薄壳推荐同样：不足 60 根真实收盘则跳过，避免短窗假值）。

**复权**：实验室挖矿 IC 用后复权序列；本页回测成交默认 **前复权 `qfq`**（`BacktestRequest.adj` 默认，`_load_bars` 注释）。验收因子排序用**与成交相同的已载入价格**，保证信号与撮合同轨。本切片**不**把实验室 hfq 强行套进成交引擎（那会改裁判或双轨）。这是「等价 PIT」在交易簿上的落地：成分与估值/财务 PIT 与实验室一致，价格与本页回测一致。

**方向**：截面按因子值**降序**，「最高 50 只」。不在本切片做方向翻转。

**无法验收**：表达式非法 / 全程 0 只得到有限值 / `_run_portfolio` 抛出装配失败（成分空、日 K 不足 20、基准缺失、调仓连续失败且 0 成交等）——写清原因，**不得用其他成员或其他策略净值顶替**。

不足 50 只有效值：能选多少持多少，注明 `n_valid` / `n_held`，**仍可进入成本与 WF**（不是自动无法验收）。仅 0 有效值才无法验收。

### 4.3 调仓与风控（写死，用户不可调）

- 调仓周期 **21** 个交易日：`params["rebalance_period"]=21` → `_run_portfolio` 交给 `PortfolioBacktestEngine.rebalance_period`；引擎在 `(i - warmup) % period == 0` 调仓（`PortfolioBacktestEngine.run`）。本切片不改该公式。
- 多头等权 Top 50，单只上限 5%：策略侧 `w = min(1/n, 0.05)`；并设 `params["max_position_pct"]=0.05`，走已有 `risk_limits`（`_apply_risk_limits`）。n≥20 时 1/n 已低于 5%；n<20 时宁可留现金，**不**突破 5% 去加满仓。
- 无空头、无行业中性、无市值中性（不要调用 `_neutralize_by_group` / `_select_by_industry_quota`）。
- 已退出指数但仍持仓：清仓范围 = `universe ∪ held`（与 `EnhancedFactorStrategy.rebalance` 的持仓并集思路相同，但是**自写这几行，禁止继承该类**）。
- 成交、复权、停牌、涨跌停、次日开盘、佣金/滑点/印花税：全部交给 `PortfolioBacktestEngine`，薄壳只下目标权重。

### 4.4 区间与预热（产品「可改；默认与本页一致」）

| 项 | 推荐锁定 |
|---|---|
| 默认 `start`/`end` | `2023-01-01` / `2025-06-30`（`BacktestPage` 控件，前端传入；服务端缺省亦然） |
| 用户可改 | 是；不够 WF 下限（`min_is_bars=60`，3 窗各至少 `min_oos_bars=20`，即约需 ≥120 根基准交易日，见 `make_expanding_windows`）→ 该成员 **样本不足**（WF 已有此态），不是改窗常数 |
| `initial_cash` | `1_000_000` |
| `adj` | `"qfq"`；本切片面板不提供调参；若 POST 显式传入与默认不同的 `adj`：**推荐 400**（保持「同一口径」），或忽略并回显实际使用的 qfq——选 400，避免静默改口 |
| `top_n` / 调仓 / 中性 / 成本 / WF 参数 | **写死**。POST 若显式传入与冻结值不同的这些字段 → **400** `rules_param_forbidden` |

预热：`_run_portfolio` 在非 `signal_key` 时

```text
warmup_min = max(momentum_lookback 默认 120, vol_lookback 默认 60,
                 beta_lookback 默认 120, tail_lookback 默认 120) + 5
```

为让 `make_ns` 的 126 窗够用，**推荐在 params 显式传入** `momentum_lookback=126`、`beta_lookback=126`、`tail_lookback=121`、`vol_lookback=61`、`warmup_days=126`。这只影响装配取数窗口，薄壳**不**用这些参数计算动量因子。禁止改 `_run_portfolio` 的默认值本身。

### 4.5 薄壳安全落地结论（再陈述）

本切片**可以**安全落地薄壳：新类 + 已有装配 + 已有撮合 + 实验室 `make_ns`/`eval_factor`。主要残余风险是墙钟（§6、§9.2）和量能字段适配，**不是**对象偷换。若 Desk 实现中发现 PIT 命名空间有无法对齐的硬缺口（例如某类已入集表达式在组合上下文中系统性全部无法求值，超出「该成员无法验收」），**停工回 Planner / Product**，禁止用 `enhanced_factor` 顶替，也禁止悄悄开启中性或合成组合簿。

---

## 5. 成本、WF 与集合状态机

### 5.1 成本（复用 W46-1，不改裁判）

锚点：`docs-design-W46-1-cost-sensitivity.md`；代码 `cost_sensitivity.run_cost_sensitivity` / `build_summary` / `DEFAULT_MULTIPLIERS=[1.0, 2.0]`；`cost_defaults.COMMISSION=0.00025`、`SLIPPAGE=0.0001`；`STAMP_TAX` 不上轴、锁引擎默认。

验收块**必须**：

- `mode="multipliers"`，`multipliers=[1.0, 2.0]`（至少这两档；不要为验收跑完整 4×4 网格）。
- `base_commission` / `base_slippage` 为 `COMMISSION` / `SLIPPAGE`（`None` 时服务也会用这两个默认；显式传入更不易被以后的 schema 改默认带偏）。
- **不**读取用户刚给另一条策略点的 `grid_pick`、不读主表单 commission。
- 成员展示复用 `EvalBadges` 与 `summary.fragile` 文案：「仅低摩擦成立：默认格超额为正，但成本翻倍后超额≤0」（`build_summary` 已有；产品文案「仅低摩擦成立：默认格超额为正，但成本翻倍后超额≤0」——与代码「…超额>0…超额≤0」同义，**不要改** `build_summary` 字符串去「对齐用词」）。

`fragile = (默认格超额>0) ∧ (×2 超额≤0)`，加倍格算不出则 `fragile=False` 且 note 说明未能计算——此时若 WF 已通过，成员不得标「通过」，推荐「无法按回测口径验收」（原因：未能计算成本×2 超额）。不要把「未能计算」当成「非 fragile 通过」。

完整网格热力非本切片交付；已有 `CostSensitivityPanel` 保持只服务主区策略。

### 5.2 滚动 OOS（复用 W46-2，不改裁判）与成员四态

锚点：`docs-design-W46-2-walk-forward.md`；代码 `walk_forward.run_walk_forward` / `judge_strategy_window`（通过=OOS 主度量≥0，不要求与 IS 同向；过拟合=IS>0∧OOS<0）/ `aggregate_verdicts` / `stricter` / `DEFAULT_N_WINDOWS=3` 等。

验收调用锁定（与 `WalkForwardPanel` / `WalkForwardRequest` 默认相同）：

| 字段 | 值 |
|---|---|
| `mode` | `walk_forward` |
| `n_windows` | 3 |
| `oos_ratio` | 0.3 |
| `scheme` | `expanding` |
| `metric` | `sharpe` |
| `min_is_bars` | 60 |
| `min_oos_bars` | 20 |
| `commission` / `slippage` | `None` → 服务内用 `COMMISSION`/`SLIPPAGE`（默认成本，不是 ×2） |

`final = stricter(agg, legacy)`。成员门看 `summary.final_verdict`（即产品说的 WF `final`）。**不要**用 `fragile_regime` 决定成员通过（那是另一套「窗过拟合或 final 相对 legacy 降级」）。

**成员结论（产品 §4.3，实现映射）**

| 成员结论 | 条件（按此顺序判定，先命中先得） |
|---|---|
| **无法按回测口径验收** | 表达式无法求值（全程无有效值）或池/引擎无法下单（装配错误、连续调仓失败 0 成交）；或 WF/`final` 已通过但成本×2 超额无法计算。写清 `reason` |
| **样本不足** | 否则，WF `final_verdict=="样本不足"`（含区间不够窗下限、OOS 无成交且无法形成收益、IS/OOS 度量 null 等已冻条件）；或切窗失败被服务翻成此类（`run_walk_forward` 在无法生成窗口时会 HTTP 400——编排应捕获并映射为样本不足，而不是让整次验收 500） |
| **拒绝** | `final` 为「过拟合」或「未通过」，**或** 成本 `fragile==True`（即使 `final` 为通过）。`reject_reasons` 分开写清（例如 `wf_overfit` / `wf_fail` / `fragile`） |
| **通过** | `final=="通过"` **且** `fragile==False`（且×2 已成功计算） |

`final=="未启用"` 在本切片默认 `oos_ratio=0.3` 下不应出现；若出现，当作无法验收（配置错误），不要当通过。

WF 与成本都要对**同一快照 expr、同一区间、同一薄壳规则、默认成本**跑。先成本后 WF 或相反不改变判定（两者独立）；推荐先成本再 WF，×2 都跑不起来可早停 WF 以省墙钟——**仅当**已能判定「无法验收」时才可跳过 WF；若只是默认格超额≤0（fragile 不适用），仍必须跑 WF。

### 5.3 集合层汇总（只汇总，不加新门槛数字）

纯函数 `aggregate_collection(n, results)`，`n` = **当前（快照）成员数**（不是「可跑人数」）。

「可跑成员」= 结论属于 **通过或拒绝**（已给出回测口径下的明确决定）。样本不足 / 无法验收 **不算通过**，也**不单独**把集合打成拒绝。

| 集合判定 | 条件（产品 §4.4，一字不改） |
|---|---|
| **拒绝** | 至少一名成员为「拒绝」 |
| **通过** | `n≥3`，且每一个可跑成员均为「通过」，且没有「拒绝」 |
| **未齐** | 没有人被拒绝，但 `n<3`（目标下限未到）；成员结论仍展示 |
| **无法判定** | 空集，或没有可跑且通过/拒绝的成员（全部为「样本不足」或「无法按回测口径验收」） |

**拒绝优先于未齐**：n=2 且一人拒绝 → **拒绝**（不要报未齐）。实现时先判断拒绝，再判断「无可跑」→ 无法判定，再判断 n<3 → 未齐，否则通过。

字面推论（已冻，不重开）：n=3、两人通过、一人无法验收 → 可跑者全通过、无拒绝、n≥3 → **通过**。n=3 且全部无法验收 → **无法判定**。Desk 按此写测例，不要「补严」成必须全员可跑。

空集：集合 **无法判定**，且不可 `POST` 开跑。n=1 或 n=2 可以跑成员，集合最多「未齐」或「拒绝」，**不得**标通过。

### 5.4 与 W48-1 的只读关系

- 只调用 `list_workset`。不改 `WORKSET_CAP=5`、`WORKSET_TARGET_MIN=3`、`WORKSET_CORR_ABS_MAX=0.50`、质量门、admit/remove。
- 被拒绝的成员**留在**工作集。
- 本 SHA 已有工作集。若某分支 `import factor_workset` 失败或表不存在：`GET` 200 且 `workset_available=false`，文案「工作集服务不可用（需要 W48-1）」；`POST` 400 `workset_unavailable`。不在本切片实现入集。

---

## 6. API 契约

前缀与现网一致：`/api/v1` + `APIRouter(prefix="/strategy")`。**无鉴权。** 路径按产品示例锁定如下（Desk 勿再改名）。

### 6.1 `POST /strategy/backtest/workset-accept`

启动一次验收（推荐异步：立即 200/202 返回 `job_id` + `status=running`；同步实现则跑完再 200，状态直接 `done`）。

**请求体（推荐）**

```json
{
  "start": "2023-01-01",
  "end": "2025-06-30",
  "adj": "qfq",
  "initial_cash": 1000000
}
```

全部可省略（用 §4.4 默认）。**禁止**接受并生效：`strategy`、`commission`、`slippage`、`grid_pick` / `cost_source`、`top_n`、`rebalance_period`、`neutralize_*`、`n_windows`、`mode`、`multipliers`、成员 id 子集（必须是当前全集快照）。显式传入与冻结值冲突 → 400 `rules_param_forbidden`。未知字段忽略（Pydantic 默认）即可。

**成功（异步）**

```json
{
  "ok": true,
  "job_id": "…",
  "status": "running",
  "run_id": 7,
  "snapshot": { "n": 3, "members": ["…"] },
  "start": "2023-01-01",
  "end": "2025-06-30",
  "rules_version": "W48-2-v1"
}
```

同步完成时同一结构 + 下列与 GET 相同的结论字段，`status=done`。

**错误**

| HTTP | `error_code` | 何时 |
|---|---|---|
| 400 | `empty_workset` | n=0 |
| 400 | `workset_unavailable` | 工作集服务/表缺失 |
| 400 | `invalid_range` | 日期缺失、start>end、格式错误 |
| 400 | `rules_param_forbidden` | 试图改写死规则或 adj≠qfq |
| 409 | `job_running` | 已有进行中任务（body 带现有 job_id） |
| 500 | — | 未捕获的总失败（应尽量落到成员「无法验收」而不是整次 500） |

### 6.2 `GET /strategy/backtest/workset-accept`

返回**当前工作集列表**（只读 `list_workset` 的精简字段：n/cap、成员名称、主路评级与 t、入集时间、expr 摘录）+ **最新一次验收结论**（无则 `run=null`）。**推荐 200 而不是 404**，方便前端一块渲染。

查询参数（可选）：`start`、`end`——若给，用于判断「与当前控件不一致」（§3.3.3）；也可用 `job_id` 查特定任务。

**`run` 载荷（`status=done` 时）要点**

```json
{
  "run_id": 7,
  "job_id": "…",
  "status": "done",
  "stale": false,
  "stale_reason": null,
  "start": "2023-01-01",
  "end": "2025-06-30",
  "rules_version": "W48-2-v1",
  "rules": {
    "universe": "中证800",
    "index_symbol": "sh000906",
    "top_n": 50,
    "rebalance_period": 21,
    "max_weight": 0.05,
    "short": false,
    "neutralize_industry": false,
    "neutralize_marketcap": false,
    "cost": { "commission": 0.00025, "slippage": 0.0001, "multipliers": [1.0, 2.0] },
    "walk_forward": {
      "mode": "walk_forward", "n_windows": 3, "oos_ratio": 0.3,
      "scheme": "expanding", "metric": "sharpe"
    }
  },
  "snapshot": { "n": 3, "members": [] },
  "collection": { "verdict": "通过", "n": 3, "n_pass": 3, "n_reject": 0, "n_insufficient": 0, "n_unevaluable": 0 },
  "members": [
    {
      "registry_id": 12,
      "name": "动量20",
      "expr": "…",
      "verdict": "通过",
      "reject_reasons": [],
      "reason": null,
      "n_held": 50,
      "n_valid": 780,
      "wf": {
        "final": "通过", "agg": "通过", "legacy": "通过",
        "windows": [], "fragile_regime": false
      },
      "cost": {
        "fragile": false,
        "default_excess": 0.12,
        "double_excess": 0.04,
        "note": "…"
      }
    }
  ]
}
```

成员 `wf.windows` 复用已有窗级 `verdict` 结构（`WalkForwardWindow`），供前端展示；**不要**把实验室 IC 的 walk_forward 填进来。`cost` 至少含 ×1/×2 超额与 `fragile`。`equity_curve` **不必**回传（避免与主区净值混淆；产品也不要求把验收结果画进主曲线）。若要给成员一张小净值，须放在验收块内且标明对象，本切片非必须。

`stale=true` 时上述结论仍返回，前端按 §7 灰显。

### 6.3 异步任务查询（若采用推荐的任务队列）

可与 GET 合并（`status=running` 时带进度：`members_done` / `members_total` / `message`）。若需与回测任务对称，允许另加只读：

`GET /strategy/backtest/workset-accept/tasks/{job_id}`

不要做 WebSocket（非必须）。前端轮询间隔推荐 2s，与 `BacktestPage` 任务轮询同量级。

取消：本切片非必须（个人本机可等结束）。不做取消则在文案说明「请等待当前任务结束」。

### 6.4 与已有 HTTP 成本/WF 端点的关系

验收**不要**让浏览器对每个成员直接打 `POST /strategy/backtest/cost-sensitivity`（那会要求注册表 key，并容易继承页面 strategy/params）。全部由验收服务在进程内调 Python 函数。已有两条 HTTP 端点行为不变。

---

## 7. 前端契约

### 7.1 挂载（只在单次回测，不新开 hash）

- 路由：`#btopt` → `BacktestOptimizePage` → 子页 `backtest`「单次回测」`BacktestPage`。**不**挂 `OptimizePage`（该页没有成本网格 / 多窗 WF）。**不**挂 `#today` / `#paper`。
- **推荐位置**：策略运行表单之下、主区净值 / `CostSensitivityPanel` / `WalkForwardPanel` **附近但视觉分离**（独立 Card、不同标题「工作集短名单验收」）。避免用户以为正在验收 `dual_ma`。即使主区还没有回测结果，该块也要可见（它不依赖 `result`）。
- DOM：`id="workset-accept"`。
- 口径条写死展示：股票池 **中证800**（`sh000906`）、规则 **多头等权 Top 50 / 21 日调仓 / 单只≤5%**、无空头无中性、成本 **万 2.5+万 1**、WF **3 窗 expanding / sharpe**。本切片不提供这些项的控件。
- 区间：复用本页 `start`/`end` 控件（同一 state），说明「与上方区间相同；不够滚动窗下限则成员为样本不足」。**不要**再用一个隐藏的 `sh600519` 当对象说明。
- 成员表：当前工作集 n/5，名称、主路评级与 t、入集时间（`GET` 里的 live workset，`api.factorWorksetGet` 或验收 GET 一并返回）。
- 空集：说明去 `#factorlab` 申请入集，深链 `#factorlab` 并可滚到 `#factor-workset`（已有 `id` 与页眉按钮模式：先 `quant-nav` 到 factorlab，再 `getElementById("factor-workset").scrollIntoView`）。主按钮禁用。
- 主按钮：「按默认成本验收」。running 时禁用并显示进度。跑完逐成员展示：结论词、`EvalBadges`（可复用）、WF `final` / 窗级 verdict、成本 `fragile`、`n_held`。集合汇总徽章：通过 / 拒绝 / 未齐 / 无法判定。
- 过期：明确「结论已过期，请重跑」（或区间不一致文案），灰显旧结论，**不得**把过期通过画成有效绿徽章。
- 口径区分句见 §2.4。强调不改变多重检验计数、不自动启用因子。
- **不要**把验收结果写入主区净值曲线。

### 7.2 `#factorlab` 只加出口

在 `WorksetCard`（`id="factor-workset"`）卡片底部加按钮「去回测验收」。可选：`WorksetApplySection` 成功文案旁同一出口。

**不**在实验室调用成本 / WF，**不**在此处展示通过/拒绝。

### 7.3 深链（不是新 hash Tab）

本仓 hash 形如 `#btopt`，第三段 `inner` 已有惯例（`#fund-research/0/holding`；`App.parseHash` / 写回 `#${sub}/0/${inner}`）。

**推荐锁定**：`#btopt/0/workset-accept`

接线（允许的小改，仍不是新 Tab）：

1. `App.tsx` `HubContent`：`case "btopt": return <BacktestOptimizePage inner={inner} />`。
2. `BacktestOptimizePage`：把 `inner` 交给 `MergedTabs`（若组件已有 `defaultKey` / 受控 key 则用之；否则挂载后切到 `backtest`）。
3. `BacktestPage`：若 `inner==="workset-accept"`（或 location hash 以该段结尾），`useEffect` 里 `document.getElementById("workset-accept")?.scrollIntoView`。
4. 工作集卡：`quant-nav` `{ tab: "btopt", inner: "workset-accept" }` 或 `<a href="#btopt/0/workset-accept">`（须能触发已有 `hashchange`）。

**禁止**在 `HUBS` 增加 `workset-accept` key，禁止 `#workset-accept` 作为一级 hash（会被 `parseHash` 判为未知并掉到 `#today`）。

### 7.4 文案与按钮

- 按钮：「按默认成本验收」。
- 进行中：「验收中（k/n）… 中证800 组合回测可能需要数分钟」。
- 空集 / 不可用 / 过期 / 冲突 409：按 §6 错误码中文说明。
- 成员拒绝原因分开写：过拟合 / 未通过 / 成本翻倍后脆弱。
- 集合「未齐」：提示目标下限 3，成员结论仍可见。

`client.ts`：`worksetAcceptStart` / `worksetAcceptGet`，超时时间须长于普通 GET（若同步 POST，推荐 ≥ 10 分钟或改为异步后短超时 + 轮询）。类型与 §6 载荷对齐。

---

## 8. 测试清单（对照产品 §5）

新文件：`backend/tests/test_workset_backtest_accept.py`、`backend/tests/test_workset_long_topn.py`（薄壳纯逻辑可用假 ctx）。编排状态机用**假 runner**（不碰真实中证800 全市场），断言不改 cost/WF 公式（可 spy 确认调用的是原函数、源码无改或对 `judge_strategy_window` / `build_summary` 的已有测例仍绿）。

### 8.1 产品勾选 → 测例 / 手册

| 产品验收 | 工程覆盖 |
|---|---|
| `#btopt` 单次回测可见该块，n/5 与成员，与当前策略分离 | 前端手册 + 组件存在于 `BacktestPage` 且不依赖 `result`；API GET 不需 strategy key |
| 空集有 `#factorlab` 深链；实验室卡能跳回该块 | 手册；hash `#btopt/0/workset-accept` |
| 有成员可跑默认成本验收；四态成员 + 四态集合 | §8.2 单测 + 手册 |
| 成员通过 = WF `final=通过` 且非成本 `fragile`；fragile 定义与 W46-1 一致 | 单测：`final=通过` + fragile → 拒绝；×1 超额>0 且 ×2≤0 → fragile |
| WF 窗级词、`final=stricter(agg,legacy)`、默认 3 窗 expanding/sharpe 与 W46-2 一致 | 调用真实 `run_walk_forward` + 假 runner，断言请求参数与 `stricter` 结果；回归 `test_walk_forward.py` |
| 规则可见且为中证800 + Top50 + 21 + ≤5%，无空头无中性 | 薄壳单测：选 50、权重≤0.05、无负仓、不调用中性函数；meta 的 index 常量 |
| 结论不被 `enhanced_factor` 等顶替；不合成组合簿 | 单测：服务模块禁止 import 那三个策略类（或 runner 的 `meta["cls"]` 是 `WorksetMemberLongTopN`）；一次 `run_member` 只含一个 expr；无「组合 IR」字段作为门槛 |
| 工作集变更后过期 | 单测：admit/remove 模拟后 `is_stale`；同 id 改 expr 过期 |
| 不自动启用、不移出、不 bump `n_tested`；两套口径区分句 | spy `bump_n_tested` 未被调用；前后 `get_n_tested` 相等；registry.status 与 workset 行不变；手册看文案 |

### 8.2 工程单测（产品 §5 工程项细化）

- 空集 `POST` → 400 `empty_workset`，不跑 runner。
- n=2 全通过且非 fragile → 集合 **未齐**（不得通过）。
- n≥3 全通过且非 fragile → 集合 **通过**。
- n=3、两人通过、一人无法验收 → 集合 **通过**（不得补严成必须全员可跑）。
- 一人 `fragile` 或 WF 未通过/过拟合 → 集合 **拒绝**；拒绝原因分开。
- n=2 且一人拒绝 → **拒绝**（优先于未齐）。
- n=3 全部样本不足或无法验收 → **无法判定**。
- 表达式无法求值 → 该人无法验收、不顶替；其他人仍跑。
- 快照变更 → `stale=true`。
- `n_tested` 不增加；不写 `enabled`/`disabled`；不删 workset 行。
- 默认成本是万 2.5+万 1，runner 收到的 ×1 为这两个数，×2 为两倍；与 grid_pick 无关。
- WF 切窗 `bar_dates` 来自 `sh000906`（可用桩）。
- 写死参数被改 → 400。
- 工作集不可用 → 400/面板字段。
- 薄壳：不足 50 持有实际数量；0 有效值不向下单；已持仓不在池内会被清到 0。

### 8.3 回归（不得弱化）

跑绿并保持断言强度：`test_cost_sensitivity.py`、`test_walk_forward.py`、`test_factor_workset.py`、`test_factor_lab.py`、`test_factor_mt.py`、`test_factor_ortho.py`。`BacktestPage` 主路径（单策略回测、成本网格、滚动 OOS、徽章 `grid_pick` 主文案仍为「成本·自定义」）行为不变。入集门与 `|ρ|<0.50` 不变。

无密钥进源码；不推远程。

---

## 9. 风险、落地序与硬闸门

### 9.1 落地序（约 0.75–1 Desk 日）

| 步 | 内容 | 约 |
|---|---|---|
| 1 | `WorksetMemberLongTopN` + 假 ctx 单测（排序、权重帽、无空头、无中性、0 值无法下单） | 0.15d |
| 2 | `workset_backtest_accept.py` 状态机 / 快照 / 过期 / 迁移；假 runner 覆盖 §8.2 | 0.25d |
| 3 | 接到真实 `_run_portfolio` + `run_cost_sensitivity` + `run_walk_forward`；路由 POST/GET；（推荐）`task_queue` | 0.25d |
| 4 | `WorksetAcceptPanel` + `BacktestPage` 挂载 + `FactorMinePage` 深链 + `client.ts` + `inner` 接线 | 0.2d |
| 5 | §8.3 回归 + 手册点检产品 §5；确认无 bump、无自动 enabled、无 enhanced_factor 顶替 | 0.1d |

一轮只做本切片。不要顺手做 M3-8、日报、改裁判、把薄壳登记进公开策略列表。

必须交付：逐成员默认成本 + ×2 `fragile` + 默认 3 窗 WF + 集合汇总 + 过期 + 深链。完整网格热力、日报展示、永久注册表策略均非必须。

### 9.2 风险

| 风险 | 缓解 |
|---|---|
| **墙钟**：n=5 × 约 10 次中证800 全市场 `_run_portfolio`，已有成本/WF 路由已警告超时 | **推荐异步** + 前端轮询与提示；成员串行、单人失败不中断；不减窗、不改裁判。可选（非必须）在**一次任务内**缓存成分/日 K，且不得改变撮合结果；不要为此改 `_run_portfolio` 默认签名行为 |
| 薄壳命名空间与实验室不完全对齐（量能、财务单位） | 强制 `make_ns`；量能从已载入 bars 对齐；财务走 `fin_asof`（`fcf_yield` 已含亿元换算）。对不齐的成员「无法验收」，不顶替 |
| 误用 `enhanced_factor` 或合成组合 | 代码审查 + §8.2 import/cls 断言；契约本节红线 |
| 误用 WF `fragile_regime` 当成员门 | 契约与测例明确只用成本 `summary.fragile` |
| 误 bump / 自动 enabled / 自动移出 | 服务禁止调用；测例 spy |
| 验收落 `Backtest` 行，主区串对象 | 只调 `_run_portfolio`，不调 `_do_backtest` |
| 切窗用了 `sh600519` | bar_dates 锁定指数；测例 |
| 继承 `grid_pick` | 服务写死 cost_defaults；测例 |
| 同步 POST 被代理超时 | 优先异步；若第一版同步，UI 明示 |
| 实现中发现薄壳无法安全对齐 PIT | **停工回 Planner/Product**，禁止顶替 |

### 9.3 硬闸门（再陈述）

- 不开实盘；密钥不进源码；不擅自推远程；本文只设计、不改业务代码（Desk 落地时仍遵守前三条）。
- 不改 W46-1 / W46-2 判定公式与刻度，不改 `factor_lab_gate`，不改 W48-1 入集语义与阈值，不 bump `n_tested`。
- 不用 `enhanced_factor` / `enhanced_factor_hs300` / `csi800_enhanced` 顶替；不把多成员合成一本评组合 IR。
- 不新开 hash Tab；不挂寻优子页 / `#today` / `#paper`。

### 9.4 关键取舍摘要

1. **内部薄壳 `WorksetMemberLongTopN` + `_run_portfolio`**，不进公开注册表，不改增强策略类。——**推荐，已选**
2. **新服务编排**，成本/WF 调原 Python 函数 + 自备 runner。——**推荐，已选**
3. **异步任务**应对墙钟；同步为允许的工期退路，不得改口径。——**推荐异步**
4. **薄表存最近验收，过期读时算**。——**推荐，已选**
5. **默认区间与本页控件一致**（`2023-01-01`～`2025-06-30`），切窗日历用 `sh000906`。——**推荐，已选**
6. **成员通过只用 WF `final` + 成本 `fragile`**；集合按产品表，拒绝优先。——已冻，不改
7. **薄壳可以安全落地**；不能落地时只许停工上报，不许顶替。——已锁

---

## 10. 附录：路径与现状速查（main `79671a4`）

| 主题 | 路径 / 符号 |
|---|---|
| 本契约（box） | `/workspace/docs-design-W48-2-workset-backtest-accept-arch.md` |
| 产品方案（已冻） | `/workspace/docs-design-W48-2-workset-backtest-accept.md` |
| 读码基线 | `ljew/quant-platform` `79671a4e825dd4eaf2866747330efd2cdbd8481e` |
| W46-1 / W46-2 契约 | `/workspace/docs-design-W46-1-cost-sensitivity.md`、`…-W46-2-walk-forward.md` |
| W48-1 契约 | `/workspace/docs-design-W48-1-sparse-orthogonal-arch.md` |
| 路由 hash | `web/src/App.tsx` → `key: "btopt"` → `BacktestOptimizePage`；`parseHash` / `inner` |
| 页壳 | `web/src/pages/BacktestOptimizePage.tsx`（`MergedTabs`：`backtest` / `optimize`） |
| 单次回测 | `web/src/pages/BacktestPage.tsx`（默认 `sh600519`、`2023-01-01`～`2025-06-30`；`CostSensitivityPanel`、`WalkForwardPanel`、`EvalBadges`） |
| 实验室工作集卡 | `web/src/pages/FactorMinePage.tsx` → `WorksetCard`（`id="factor-workset"`）、`WorksetApplySection` |
| 策略路由 | `backend/app/routers/strategy.py` → `_do_backtest` / `_run_portfolio` / `_run_single` / `_load_bars` / `_load_index_bars` / `run_cost_sensitivity`（HTTP）/ `run_walk_forward_api` / `submit_backtest` |
| 组合引擎 | `backend/app/core/engine/portfolio_backtest.py` → `PortfolioBacktestEngine.run` / `PortfolioContext.universe` / `order_target_percent` |
| 策略基类 | `backend/app/core/engine/base_strategy.py` → `PortfolioStrategy` |
| 注册表（只对照池元数据，不调用其 cls） | `backend/app/core/strategies/registry.py` → `csi800_enhanced` / `enhanced_factor`（`index_code=000906`，`index_symbol=sh000906`，`multi_asset=True`） |
| 禁止顶替的策略类 | `backend/app/core/strategies/multi_factor.py` → `EnhancedFactorStrategy`；`csi800_enhanced.py` → `Csi800EnhancedStrategy` |
| 成本 | `backend/app/services/cost_sensitivity.py`；`backend/app/core/cost_defaults.py` |
| WF | `backend/app/services/walk_forward.py` |
| 工作集 | `backend/app/services/factor_workset.py` → `list_workset` / `admit` / `remove_member` |
| 求值 | `backend/app/core/engine/factor_expr.py` → `eval_factor`；`backend/app/datahub/ns_vars.py` → `make_ns` / `fin_asof` / `fin_vars` |
| 实验室截面（对照，验收不直接依赖最新截面） | `factor_mining.eval_raw_values_on_cs` / `load_latest_cs_context` |
| 门禁 / 计数（禁止改、禁止 bump） | `factor_lab_gate.check_enable_gate`；`factor_mt.get_n_tested` / `bump_n_tested` |
| Schema | `backend/app/schemas.py` → `BacktestRequest` / `CostSensitivityRequest` / `WalkForwardRequest`（`adj` 默认 `qfq`） |
| 任务队列 | `app.core.task_queue`（`submit_backtest` 已用） |
| 迁移挂钩 | `backend/app/database.py` → `_migrate_sqlite` |
| API 前缀 | `api_prefix="/api/v1"`，无鉴权 |
| 既有测例 | `backend/tests/test_cost_sensitivity.py` / `test_walk_forward.py` / `test_factor_workset.py` / `test_factor_lab.py` / `test_factor_mt.py` / `test_factor_ortho.py` |
| 本切片测例 | `backend/tests/test_workset_backtest_accept.py`、`test_workset_long_topn.py`（新增） |

### 产品对齐核对（Architect 自检）

| 产品冻结项 | 本契约 |
|---|---|
| 页面 `#btopt` → 单次回测 `BacktestPage`，不新开 hash，不挂寻优/`#today`/`#paper` | §7.1、§7.3（`inner` 锚点不是新 Tab） |
| 对象 = 工作集成员快照（`registry_id`+当时 `expr`），逐本簿，集合只汇总 | §3、§4、§5.3 |
| 禁止 `enhanced_factor` 等固定库顶替；禁止合成组合 IR | §2.1 方案 A/E/F、§4.5、§8.2 |
| 中证800、PIT、多头等权 Top 50、21 日、单只≤5%、无空头无中性；`eval_factor`；成交走 `_run_portfolio` / 不复制引擎 | §4；调用 `_run_portfolio` 而非复制 |
| 默认成本万 2.5+万 1，不继承 `grid_pick`；至少 ×1/×2 与已有 fragile | §5.1 |
| 默认 3 窗 expanding/sharpe，`final=stricter(agg,legacy)`；成员通过 = WF final 通过且非（成本）fragile | §5.2（明确不用 `fragile_regime`） |
| 集合 通过/拒绝/未齐/无法判定；拒绝优先于未齐 | §5.3 |
| 快照绑定与过期（入集/移出/替换或同 id 表达式变）；不 bump、不自动 enabled/移出 | §3.3、§5.4 |
| 薄壳 + 新服务 + 验收 API + 仅 BacktestPage 一块 + 实验室深链；不改公式与 admit | §2、§6、§7 |
| 薄壳若不能安全落地须标明、禁止顶替 | §0、§4.5：**可以落地**；残余是墙钟与命名空间，不是改对象 |
| 非目标 M3-8 / GP / 改裁判 / 完整热力 / 重做 W46-3 / 模拟盘资讯智能体 / 实盘 / 新 hash | §1.3 |

无产品改口。无待 Product 重开的已冻口径。实现补全均标「推荐」，待 Product 一并锁定。

---

*文档结束。**Product 已于 2026-11-12 Asia/Shanghai 冻结本技术契约**（无改口，实现补全一并锁定）。请 Planner 派 Desk，按 §9.1 落地。硬闸门：不开实盘，密钥不进源码，不擅自推远程；Architect 出稿时只设计、未改业务代码。*
