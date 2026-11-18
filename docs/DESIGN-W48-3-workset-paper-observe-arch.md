# 技术契约：W48-3「工作集短名单模拟盘观察」

> 作者：Quant Architect · 2026-11-13（Asia/Shanghai）
> 产品方案（已冻结，本契约不得改口）：`/workspace/docs-design-W48-3-workset-paper-observe.md`
> 调研备忘（读码事实，不是产品口径）：`/workspace/w48-3-research.md`
> 上游依赖与体例：`/workspace/docs-design-W48-2-workset-backtest-accept-arch.md`（及已冻产品稿 `docs-design-W48-2-workset-backtest-accept.md`）；W48-1 工作集表/API 见 `docs-design-W48-1-sparse-orthogonal-arch.md`
> 设计锚点：日常最小闭环「因子筛选 → 回测验收 → 模拟盘观察 → 一页日报」的第三环；已冻 W48-2 工作集短名单验收、W47-1 模拟盘体验、W46-1/W46-2 成本与滚动 OOS
> **硬闸门**：不开实盘；密钥不进源码；不擅自推远程；**不可改裁判**（验收测试标准、成本/PIT/复权口径）；**本文只写设计，不改业务代码。**
> **冻结状态（2026-11-13 Asia/Shanghai）**：**Product 已冻结**。对照产品稿 `/workspace/docs-design-W48-3-workset-paper-observe.md` 与 main `bbd5bdb676ddc21bde5f3352f461e27368900451`：**无产品改口**。下文全部「推荐」实现补全一并锁定（见文末冻结清单）。不重开已冻交易规则 / 默认成本 / 观察对象与逐本簿 / 前瞻观察区间 / 表达式完整性 / 注册表约定 / 幂等与副作用 / 失败态。请 Planner 派 Desk。本轮不自行拉 Desk / QA / Watch，不默认拉全员。硬闸不变。
>
> 读码基线（本契约独立在 GitHub 核实，不以 Planner 口述或调研备忘为唯一来源）：GitHub `ljew/quant-platform` **origin/main** [`bbd5bdb676ddc21bde5f3352f461e27368900451`](https://github.com/ljew/quant-platform/commit/bbd5bdb676ddc21bde5f3352f461e27368900451)（committer `2026-11-12T21:00:53Z` = **2026-11-13 05:00 Asia/Shanghai** 合入 origin/dev，提交说明 `Merge remote-tracking branch 'origin/dev' into HEAD`）。该 SHA **已是当前 main HEAD**（`get_branch(main)` 与 `get_commit` 交叉核对）。**不要**用 `/workspace/quant-latest` 或历史 tarball（曾停在 `4e63519` 附近，约 2026-11-09，已滞后）。下文代码锚点均按该 SHA 从 GitHub 取原文。
>
> **基线核实（W48-1 / W48-2 是否已在 main）**：该 SHA 的 git tree（601 条目，未截断）**已含** W48-1（`backend/app/services/factor_workset.py`、`web` 侧 `WorksetCard` / 实验室工作集、表 `factor_workset`）与 **W48-2 全部落地代码**（`WorksetAcceptPanel.tsx`、`workset_backtest_accept.py`、`workset_long_topn.py`、表 `factor_workset_accept_run`、路由 `POST`/`GET /strategy/backtest/workset-accept`、测例 `test_workset_backtest_accept.py` / `test_workset_long_topn.py`、仓内 `docs/DESIGN-W48-2-workset-backtest-accept-arch.md`）。因此本切片 **不是**「等 W48-2 合入后再设计」——代码依赖已在该 SHA 上；运行库若缺 W48-2 迁移，按 §6 明确拒绝，不在本切片重做验收。仓内该 SHA **没有**产品稿 `docs/DESIGN-W48-2-workset-backtest-accept.md`（与调研备忘一致，不影响本契约）。

---

## 0. 结论 / 推荐 / 取舍 / 落地序（先读）

**只做一件事**：让「同一份未过期验收快照」里 **verdict=通过** 的成员，按与验收相同的交易规则和默认成本，**逐人各建（或打开）一本** `PaperTask`，在已有 `#paper` / `PaperPage` 观察面里从建盘日起向前看。主入口在 `#btopt` 验收卡「去模拟盘观察」；深链复用已有 `inner`（**推荐锁定** `#paper/0/workset-observe`），不是新 hash Tab。`#factorlab` 不加此出口、不复制观察 UI。不改持仓 UI，不改 `cost_defaults`，不改裁判，不 bump `n_tested`，不自动启用/移出因子，**禁止**把 `WorksetMemberLongTopN` 写入公开注册表，**禁止**用 `enhanced_factor` / `enhanced_factor_hs300` / `csi800_enhanced` / `hs300_enhanced` 或任何固定库顶替，**禁止**把多名成员合成一本组合簿（M3-8），**禁止**把验收历史窗 `2023-01-01`～`2025-06-30` 填作纸面起始日，**禁止**截断 `expr` 后开盘。

**同口径观察可否在本切片安全落地：可以（走路径 A）。** 未发现必须改用注册表策略顶替、或必须新写专用 paper runner 的阻塞。证据见 §2.1：纸面引擎三处 `get_strategy` 之后的组合装配（PIT 成分、预热、`pool_mode=index`、`PortfolioBacktestEngine`）是通用的；W48-2 已提供 `build_member_meta(expr)`（`cls=WorksetMemberLongTopN`，`pool_mode="index"`，warmup 参数齐全）。路径 A 只需：(1) 专用建盘通道读未过期快照并落完整 `expr`；(2) 引擎增加「已有 / 可重建 meta 则不再 `get_strategy`」，**其余 `_load_portfolio` 装配不翻修**。墙钟远低于 W48-2（每人一次短窗组合跑，不是约 10 次全历史）。残余风险见 §9.2（表达式存储、三处解析都要接到——尤其 `run_paper_task` 里那次取出后未使用的 `get_strategy`、调度器子进程同样走它），不是不可落地。**任何情况下禁止静默顶替。实现中若系统性 PIT / 成交对不齐，停工回 Planner / Product，走产品 §4.6，禁止减规则或换对象。**

**模块落地（与产品 §7 对齐，路径写死）**

| 层 | 本切片 | 不要动 |
|---|---|---|
| 接入路径 | **路径 A（已选）**：新增专用通道 `POST /paper/tasks/workset-observe`（入参 `run_id`），服务端读未过期验收快照，对每名「通过」成员调用已有 `build_member_meta(expr)`；`paper_engine` 增加 `_resolve_paper_meta(task)`（观察任务重建内部 meta，不再 `get_strategy`） | **路径 B 禁止**：把薄壳写入 `STRATEGY_REGISTRY`（即使前台过滤）。用 `enhanced_factor` / `csi800_enhanced` / `hs300_enhanced` 等顶替。走 `POST /paper/auto-ensure` 或 `#today` `BuildPoolModal` |
| 备选 | **路径 C 不选**（A 不必大面积翻修通用加载；C 复制 `_load_portfolio` 会双轨漂移）。**路径 D** 仅当实现期 1–2 小时内无法安全落地时启用：验收卡「无法按同口径观察」+ 原因 + 回验收深链，禁止顶替 | 复制一套撮合引擎；把结果 `_persist` 进 `backtests`；改 `_load_portfolio` 的 PIT / 预热 / 财务注入公式 |
| 编排 | 新增 `backend/app/services/workset_paper_observe.py`：读 `get_latest_accept` / `is_stale`（**原样调用** W48-2，不改过期条件）、锁死规则与默认成本、幂等建盘、完整保存 `expr`、读时过期标记 | 改 `workset_backtest_accept.py` 的集合状态机 / `is_stale` / 裁判；改 `WorksetMemberLongTopN` 选股规则；改 `cost_defaults` |
| 路由 | `backend/app/routers/paper.py` 新增上述 POST（及只读查询若需要）；**不**扩展通用 `POST /paper/tasks` 去收内部 key 或 `expr`；`web/src/api/client.ts` 补方法 | 修改 `GET /strategy/strategies` 语义把薄壳露出来；改 `/factor/mine`、`/factor/workset/admit` |
| 存储 | `paper_tasks` **加法列**绑定 `run_id` + `registry_id` + 完整 `expr`（TEXT，见 §3「推荐」）；迁移由 `_migrate_sqlite` 挂钩。过期读时算，不自动删停 | 把完整 `expr` 只写入现有 `params_json String(1024)` 并不加防护；改验收表语义；写 `factor_mt_state` |
| 前端 | `WorksetAcceptPanel` 加按钮与失败态；`App.tsx` `HubContent` 把 `#paper` 的 `inner` 传给 `PaperPage` 并消费；任务仍用已有列表 / `DetailPanel`；观察横幅展示集合状态与口径句 | 大改 `PaperPage` 布局；动 `TodayPage` / `WorksetCard` / `OptimizePage` / `#strategy-overview`；新开 `#workset-paper` Tab；在验收卡里再做一套净值/持仓 |
| 测试 | 新增 `backend/tests/test_workset_paper_observe.py`；保留 W48-2 注册表断言与 W47-1 / 手动建盘 / 验收块回归 | 弱化已有 paper / accept / cost / workset 测试 |

**实现细节推荐（产品已冻规则下的补全，请 Product 一并锁定；Desk 按此写死）**

1. **路径 A，本切片可安全落地。** 见 §2.1。路径 B 禁止。路径 C 不采用。路径 D 是实现期安全退路，不是本契约的默认交付。
2. **`expr` 完整存储（不得靠 1024 列）。** 在 `paper_tasks` 增加可空列：`workset_observe_run_id INTEGER`、`workset_observe_registry_id INTEGER`、`workset_observe_expr TEXT`（完整快照 `expr`），可选 `workset_observe_rules_version`。对非空的 `(run_id, registry_id)` 建唯一索引（幂等）。**禁止**把完整 `expr` 作为唯一来源写入 `params_json`（该列仍是 `String(1024)`，`FactorRegistry.expr` 是 `Text`）。写入后**回读比对**，不一致则该成员进入「无法按同口径观察」，**不得**截断后开盘。`params_json` 最多放得下的短规则回显（`top_n` 等），即使写入也**不得**在解析时用它覆盖 TEXT 列里的 `expr`。不必为了本切片去改所有既有纸面任务的 `params_json` 列类型（若 Desk 额外把该列改为 Text 作防御，可以，但不能代替专用 TEXT 列，且写入路径仍须有比对）。
3. **引擎解析钩子 `_resolve_paper_meta(task) -> (meta, params)`。** 若任务带观察绑定且 TEXT 列有完整 `expr`：`build_member_meta(expr)`，**直接使用其 params**（含 `warmup_days=126` 等，只为取数窗口），不再 `get_strategy`，不再用 `_merge_params` 把 `params_json` 里可能被截的 `expr` 合并进去。否则走今天的 `get_strategy` + `_merge_params`。此钩子必须接到 **全部** 现网入口：`paper_engine._load_single`（约 L57）、`_load_portfolio`（约 L117）、`run_paper_task`（约 L314，见 §2.1 的「未使用但仍会抛错」）、以及因此覆盖的 `paper_scheduler` / `paper_runner.run_in_subprocess`。通用 `POST /paper/tasks` 仍只 `get_strategy`：传入 `_workset_member_long_topn` → 维持 400「未知策略」。
4. **内部 `strategy_key` 标记。** 观察任务可把 `strategy_key` 写成 `_workset_member_long_topn`（24 字符，现列 `String(32)` 放得下），便于识别；**不得**写入 `STRATEGY_REGISTRY`。已冻 W48-2 测试「类与 `_workset_member_long_topn` 不在注册表」必须仍绿。`GET /strategy/strategies` 与 `#paper` / `#btopt` 策略下拉不得出现该类名或内部 key。列表/详情**推荐**展示友好名「工作集观察」，不要把内部 key 渲染成用户可点选的策略。
5. **深链形式。** 锁定 `#paper/0/workset-observe`（用已有 `inner`，禁止新 Tab，禁止 `#workset-paper` 一级路由）。若需钉住 `run_id`，把编号放进 **inner 段内部**（推荐 `#paper/0/workset-observe-{run_id}`），因为 `App.parseHash` 只按 `/` 拆三段，`#paper/0/workset-observe/123` 的第四段会丢失。省略编号 = 最新一次已完成验收（仍须未过期才建盘）。`HubContent` 把 `inner` 传给 `PaperPage`；页面消费 inner：落地即走幂等 POST（或打开已有），不是只预填手动表单。
6. **观察起始日。** 通道**不接受**客户端 `start_date`（传入则 400，防止把验收窗 `2023-01-01`～`2025-06-30` 塞进来「用纸面重跑回测」）。一律 `start_date = _last_index_date(db, "000906")`（已有函数）；指数无行情 → 400，文案沿用现网「先跑一次 ETL」。页上必须有口径句：纸面从建盘日（该指数最近交易日）向前观察，不是回放验收历史窗。本切片不提供改起始日的控件。
7. **建盘同步写库，首次运行走已有后台。** POST 只创建/返回任务（秒级，可同步）。首次 `run_paper_task` 走已有 `POST /paper/tasks/{id}/run` 的后台或组合子进程（`run_in_subprocess`），前端轮询列表，避免 HTTP 墙钟。**推荐**将这本 `PaperTask.enabled=True`——这是**纸面调度开关**（调度器只跑 `enabled==True`，组合 19:00 后日频），以便「从建盘日起向前观察」能续跑；**与产品禁止的 `FactorRegistry.enabled` 自动切换不是同一件事**。禁止写因子注册表 `enabled`/`disabled`、禁止 `admit`/`remove_member`、禁止 `bump_n_tested`。
8. **过期与「最新一次」。** 只消费 GET 验收的「最新一次已完成」快照；若其 `stale` 或未完成/失败，**不建盘、不回退到更早一次记录**（与产品「结论已过期，请先重跑」字面一致，不收紧也不放宽）。旧任务读时打「快照已过期」：绑定 run 经 W48-2 `is_stale` 为真，**或** 其 `run_id` 已不是当前最新一次已完成——轻标记，不自动停止、不自动重建、不自动删盘。`is_stale` 条件一字不改。
9. **通道入参。** Body 只收 `run_id`（推荐可省略，省略则取最新一次已完成）。默认为该快照全部「通过」成员，不提供成员子集参数（避免做成「自选合成」入口）。显式传入 `adj≠qfq`、改写死规则、非默认成本、验收窗 `start_date` 等 → 400 `rules_param_forbidden`。**推荐** `initial_cash` 锁死 `1_000_000`（与验收默认一致，通道不开放）。不把成本输入暴露给该通道用户。
10. **任务名与唯一约束。** 产品示例 `工作集观察 · {成员名} · run#{run_id}`。现 `PaperTask.name` 为 `String(64) UNIQUE`；成员名本身最长 64。**推荐**：按该公式命名，超长则截断成员名并在尾部保留 `r{registry_id}`（及 `run_id`）以保证唯一且可区分。不要用全局固定名导致第二次验收无法建盘。
11. **集合状态可见。** `#paper` 在工作集观察模式下用验收 GET 的 `collection` 做顶栏横幅（通过/拒绝/未齐/无法判定 + n_pass 等），避免把「某成员已通过」读成「整份短名单已通过」。返回验收深链 `#btopt/0/workset-accept`。
12. **失败态文案**沿用产品 §4.6 语义（可微调字，不得弱化），见 §7.4。API 用明确 `error_code`，前端原样展示原因，禁止改用其他策略。

**落地序（约 0.5–1 Desk 日）**：存储迁移 + 解析钩子 + 单测 → 观察服务（快照/幂等/过期/锁规则）+ 路由 → 验收卡按钮与失败态 + `inner` 接线 + 横幅/口径句 → 口径回归。详见 §9.1。

---

## 1. 背景 / 目标 / 非目标

### 1.1 对照产品冻结稿（不得改口）

本契约落实已冻产品方案，**不改口径**。产品已冻要点与契约落点：

| 产品已冻口径 | 本契约如何落实 | 是否改口 |
|---|---|---|
| 页面 `#paper` / `PaperPage`；主入口 `#btopt` 验收卡「去模拟盘观察」；深链建议 `#paper/0/workset-observe`（复用已有 `inner`，不是新 hash Tab）；`#factorlab` 不加出口、不复制观察 UI | §7 | 否 |
| 对象 = 最新一次未过期验收快照中 verdict=通过的成员，逐人一本 `PaperTask`；集合未通过仍可观察已通过成员，但须可见集合状态；禁止合成组合簿（M3-8） | §3、§4、§7 | 否 |
| 规则与默认成本从该次验收锁死（中证800 `sh000906`、多头等权 Top 50、21 日、单只≤5%、无空头无中性、`adj=qfq`、万 2.5+万 1）；同口径通道收到非 qfq 或改写死规则 → 400 | §4.2、§6 | 否 |
| 纸面从建盘日（该指数最近交易日）向前观察，不回放验收历史窗 `2023-01-01`～`2025-06-30` | §4.3、推荐第 6 条 | 否 |
| `expr` 必须完整保存；现 `params_json String(1024)` 不得截断后开盘 | §3.1、推荐第 2 条 | 否 |
| 薄壳 `WorksetMemberLongTopN` 不进公开注册表、不进策略下拉；禁止用 `enhanced_factor` / `enhanced_factor_hs300` / `csi800_enhanced` / `hs300_enhanced` 等顶替 | §2.1、§4.5、§8 | 否 |
| 同一 `run_id`+`registry_id` 幂等；不 bump `n_tested`、不自动 enabled/移出；旧任务可打过期标记，不自动删停 | §3.2–3.3、推荐第 7–8 条（纸面 `PaperTask.enabled` 与因子 `enabled` 分开，见推荐第 7 条） | 否（推荐第 7 条是实现补全，不是放松「不自动启用因子」） |
| 若本轮无法安全接入：验收卡「无法按同口径观察」+ 原因 + 回验收深链 `#btopt/0/workset-accept`；禁止静默顶替；系统性 PIT 对不齐须停工，禁止顶替 | §6.3、§7.4、§9.2 | 否 |
| 产品 §7 倾向路径 A；B 禁止；C 仅当 A 要大面积翻修且能避免双轨漂移；1–2 小时不能安全落地 → D | §0、§2.1：**选 A，C 不选，D 为退路** | 否（实现路径由契约定，未改产品冻结规则） |

「推荐」项（§0 列表、§3 存储形态、深链是否附 `run_id` 的具体拼法、POST 省略 `run_id` 的默认、首次运行异步、纸面任务 `enabled`、`initial_cash` 锁定等）是产品已冻规则下的实现补全，**不是新口径**，请 Product 冻结本契约时一并锁定。

### 1.2 背景（读码，main `bbd5bdb`）

- **W48-2 已在该 SHA**：验收块 `WorksetAcceptPanel`（`id="workset-accept"`）、`POST`/`GET /strategy/backtest/workset-accept`、表 `factor_workset_accept_run`、薄壳 `WorksetMemberLongTopN` + `build_member_meta`（内部 meta 的 `key: "_workset_member_long_topn"`，**不写入** `STRATEGY_REGISTRY`）。测例 `test_build_member_meta_cls_and_not_in_registry` 断言类与该 key 都不在表里。成交只走 `_run_portfolio`，禁止 `_do_backtest` / `_persist`。`is_stale`：当前工作集 `{(registry_id, expr)}` 与快照不同，或 `rules_version ≠ W48-2-v1`，或请求区间/`adj` 与行不一致。GET 载荷含 `run_id`、`stale`、`rules`、成员完整 `expr` + `verdict`、`collection`，**字段足够**作为建盘输入。验收卡**没有**「去模拟盘观察」。
- **纸面创建强制公开注册表**：`POST /paper/tasks`（`paper.py` `create_task`）`get_strategy(req.strategy_key)`，未知或停用 → 400「未知策略」。前端 `PaperPage.create` 只对已注册策略，`params_json` 恒为 `"{}"`，不传 `adj`/费率/`start_date`（省略即默认成本与 `qfq`，`start_date` 为该指数最近交易日）。
- **引擎三处强制 `get_strategy`**：`paper_engine._load_single`、`_load_portfolio`、`run_paper_task`（见 §2.1）。无「已有 meta 则跳过」分支。组合轨已通（`kind=portfolio`，`PortfolioBacktestEngine`）；`build_member_meta` 已设 `pool_mode="index"`，与 `_load_portfolio` 非 `signal_union` 的 PIT 成分轨兼容——**前提是引擎拿到的就是这份内部 meta**。
- **`PaperTask` 列**（`models.py`）：`name String(64) UNIQUE`；`strategy_key String(32)`；`params_json String(1024)`；`adj` 默认 `"qfq"`；`commission`/`slippage` 默认 `COMMISSION`/`SLIPPAGE`（万 2.5+万 1）；`state_json` / `equity_curve_json` / `factor_analysis_json` 已是 `Text`；**无** `run_id` / `registry_id` / 完整 expr 列。`FactorRegistry.expr` 为 `Text`。创建默认 `enabled=False`。调度器（`paper_scheduler`）只跑 `enabled==True`；组合任务 19:00 后走 `run_in_subprocess` → 仍进 `run_paper_task`。
- **`#paper` 不消费 `inner`**：`App.parseHash` 已支持 `#paper/0/…` 三段式（无 id 时 `0` 占位，与 `#btopt/0/workset-accept` 同构）；`HubContent` 现为 `case "paper": return <PaperPage />`，不传 `inner`。观察面（净值、持仓、账本成交、vs 基准、vs 理论、W47-1 徽章与口径句「与回测默认成本同契约，非券商实盘」）已齐，本切片不翻修。
- **不要误用**：`DISABLED_STRATEGIES` 中的 `enhanced_factor` / `enhanced_factor_hs300` / `csi800_enhanced`（`get_strategy` 会拒，也禁止改去启用）；仍启用的 `hs300_enhanced`（默认 `top_n=30`、沪深300）；`POST /paper/auto-ensure`（建议费率万 1+滑点万 15，不是 `cost_defaults`）；`#today` `BuildPoolModal`（已注册 xq + `399317`）。另：`GET /paper/strategies` 遍历原始 `STRATEGY_REGISTRY`，不过 `DISABLED_STRATEGIES`（`PaperPage` 当前不用这条）；本切片不要改它来「露出」薄壳，也不要把薄壳写进表以免被它裸露。
- 本仓 `/paper/*`、`/strategy/*` **无鉴权**（个人本机投研台）。API 前缀 `/api/v1`。

### 1.3 目标

1. 验收卡在最新结论**未过期**且至少一名成员「通过」时，主按钮「去模拟盘观察」可用；点击后进入 `#paper`，为每名通过成员各有一本绑定该 `run_id` 的纸面任务（已有则打开）。
2. 每本任务使用验收当时的完整 `expr`、写死规则、`adj=qfq`、默认成本；观察面复用已有组件；任务名能区分成员与验收轮次；起始日为建盘日（最近指数交易日），页上有口径句。
3. 集合状态在观察侧可见；过期不建盘；同一 `run_id`+`registry_id` 幂等；旧任务最多打过期标记。
4. 薄壳不进注册表与下拉；无法安全接入或无法完整存 `expr` 时走明确失败态，无静默顶替。
5. 建盘不自动启用/移出因子、不 bump `n_tested`。

### 1.4 非目标（本切片刻意不做）

| 不做 | 归属 |
|---|---|
| 多名成员合成一本组合簿，或新开组合 IR / 风险平价门槛 | 产品非目标（M3-8）；现引擎一个 `PaperTask` 也只跑一个策略 |
| 把 `WorksetMemberLongTopN` 写入公开 `STRATEGY_REGISTRY`；改 `EnhancedFactorStrategy` / `csi800_enhanced` / `hs300_enhanced` 去消费 `factor_workset` | 产品明确禁止（路径 B） |
| 大改持仓 UI、成交表改版、镜像组合与模拟盘任务合并 | W47-1 / 产品非目标 |
| 改 `cost_defaults`、成本徽章短标、验收裁判、PIT / 复权 / WF 窗常数 / `fragile` 定义；把成本网格或滚动 OOS 挂到 Paper；用纸面回放验收历史窗 | 已冻 / 产品禁止 |
| 一页日报；把 W46-3 稳健摘要搬回 `#today`；本切片改 `#today` 职责；在总览展示工作集 + 验收结论 | 留下一轮 |
| 复活 GP；资讯；智能体；开实盘；重做今早已合项 | 硬闸门 / 非目标 |
| 新开 hash；建盘失败自动改用其他策略；自动停删旧盘；`#factorlab` 工作集卡加跳过验收的出口 | 产品禁止 |

---

## 2. 模块边界与路径取舍

### 2.1 关键取舍：路径 A（专用通道 + 注入/重建 meta），不选 C，禁止 B，D 为退路

| 方案 | 做法 | 利 | 弊 | 结论 |
|---|---|---|---|---|
| **A. 专用建盘通道 + 引擎可注入已有 meta** | `POST /paper/tasks/workset-observe`（`run_id`）；`build_member_meta`；`_resolve_paper_meta` 替换三处 `get_strategy`；**不改** `_load_portfolio` 其余装配 | 遵守「薄壳不进注册表」；复用已冻薄壳与已通的组合纸面轨；改动面小 | 必须改三处入口（含调度器间接路径）；须单独解决 `expr` 存储 | **选 A。本切片可以安全落地。** |
| **B. 薄壳写入公开注册表**（隐私 key / 前台过滤） | 写入 `STRATEGY_REGISTRY`，走现有 `POST /paper/tasks` | 少一条路由 | **破坏**已冻 W48-2 测试与「验收对象和 dual_ma 等表单分离」；`GET /paper/strategies` 会裸露（不过 `DISABLED_STRATEGIES`） | **禁止**（产品已写死） |
| **C. 专用 paper runner** | 不改通用 `get_strategy` 语义，复制/抽出 `_load_portfolio` 再 `engine.run(WorksetMemberLongTopN, params)` | 表面不碰通用入口 | `_load_portfolio` 约 170 行含预热、PIT、财务、OOM 历史坑；双轨必然漂移。产品规定 **仅当 A 会大面积翻修且 C 能避免漂移** 才用 | **不选**：A **不必**大面积翻修（见下方证据），C 更易漂移 |
| **D. 本轮无法安全接入** | 不改引擎；验收卡失败态 + 原因 + 回验收深链 | 口径不被偷换 | 闭环仍断在第三环 | **仅当实现期无法在约 1–2 小时安全落地（系统性 PIT / 成交对不齐）时采用**；禁止顶替 |

**为何 A 不必大翻修（该 SHA 证据）**

1. `_load_portfolio`（`paper_engine.py` 约 L113–L300）在 `meta = get_strategy(task.strategy_key)` 之后，只用 `meta` 的 `index_code` / `index_symbol` / `pool_mode` / `cls`，以及 `_merge_params` 得到的 `params`（`rebalance_period`、`warmup_days`、lookback、`max_position_pct`）。这与已有注册表组合策略同一条装配，**不是**写死某个注册表 key 的专用逻辑。
2. `build_member_meta`（`workset_backtest_accept.py` 约 L152–L176）已经给出恰好这些字段：`cls=WorksetMemberLongTopN`，`multi_asset=True`，`index_code="000906"`，`index_symbol="sh000906"`，`pool_mode="index"`，`default_params={}`，params 含完整 `expr`、`top_n=50`、`rebalance_period=21`、`max_weight=0.05`、`max_position_pct=0.05`、`warmup_days=126` 及 lookback。注入后走「否则 PIT 成分」分支（约 L175–L186），与验收侧 `_run_portfolio` 同一套池。
3. 因此 A 的引擎改动是 **解析钩子**，不是重写加载。复制整段装配（路径 C）没有收益，只增加与验收/既有纸面组合的双轨风险。
4. **必须改到的第三处（易漏）**：`run_paper_task` 约 L314–L315 先执行 `meta = get_strategy(task.strategy_key)`，**随后未使用该 `meta`**（真正加载在 `_load_*` 里再次 `get_strategy`）。若观察任务的 `strategy_key` 不在注册表，**这一行仍会 `KeyError`**，被外层 `except Exception` 写成 `error_msg`，任务看起来「跑过但失败」，账本为空。调度器与 `run_in_subprocess` 都进 `run_paper_task`，故钩子必须覆盖这里，不能只改 `_load_portfolio`。
5. `_load_single` 对工作集观察不会走到（`kind` 由 meta.`multi_asset` 强制为 `portfolio`），但钩子应同样替换，以免将来误用。
6. 墙钟：每人一次「最近交易日 + warmup」的中证800 组合跑，与现网已有组合纸面任务同量级，远低于验收的「n×约 10 次全历史」。不构成改走 C 或 D 的理由。

**禁止在 runner / 通道里**：`get_strategy("enhanced_factor")` 等；实例化 `EnhancedFactorStrategy` / `Csi800EnhancedStrategy`；把多名 `expr` 一次喂给一个策略；把薄壳 `cls` 登记进 `STRATEGY_REGISTRY`。

### 2.2 后端路径表

| 层 | 路径 | 本切片职责 |
|---|---|---|
| 薄壳（已有，只调用） | `backend/app/core/strategies/workset_long_topn.py` → `WorksetMemberLongTopN` | **不改**选股/下单规则 |
| 已有 meta 工厂 | `workset_backtest_accept.build_member_meta` / `is_stale` / `get_latest_accept` / `check_forbidden_params` / `VERDICT_PASS` / 规则常量 | **只调用，不改符号与过期条件**。禁止参数可在观察服务复用同一套写死值（`ACCEPT_*`），或从该次 `run.rules` 读出后再与写死值校验一致 |
| 新服务 | `backend/app/services/workset_paper_observe.py` | 建盘编排、完整 `expr`、幂等、过期标记、锁规则、迁移 |
| 纸面引擎 | `backend/app/core/engine/paper_engine.py` | **仅**增加 `_resolve_paper_meta` 并接到三处；不改撮合、补录水位、预热公式、PIT 财务注入 |
| 调度 | `paper_scheduler.py` / `paper_runner.py` | **不改调度策略**；因走 `run_paper_task` 而自动覆盖 |
| 路由 | `backend/app/routers/paper.py` | 新增观察 POST（及可选 GET）；通用 `create_task` 行为不变 |
| ORM / 迁移 | `models.py` 给 `PaperTask` 加法列；`database._migrate_sqlite` 在工作集验收迁移之后调用 | 加法、可逆 |
| 工作集 / 计数 | `factor_workset` / `factor_mt.bump_n_tested` / `factor_lab_gate` | **禁止** bump / admit / remove / 改门禁 |
| 验收表 | `factor_workset_accept_run` | **只读** |

### 2.3 前端路径表

| 挂载点 | 说明 |
|---|---|
| `#btopt` → `WorksetAcceptPanel` | 「去模拟盘观察」按钮、过期/无通过禁用原因、失败态「无法按同口径观察」 |
| `#paper` → `PaperPage` | 接收 `inner` 并消费；与手动创建表单视觉分离；已有列表 / `DetailPanel` 观察；顶栏集合状态 + 口径句 + 过期轻标记 |
| `App.tsx` `HubContent` | `case "paper": return <PaperPage inner={inner} />`（对称于已有 `btopt` 接线）。**不**改 `HUBS` / `LEGACY_TAB_MAP` |
| `web/src/api/client.ts` | `paperWorksetObserve`（及类型）；可继续用已有 `worksetAcceptGet`、`paperTasks`、`paperRun`、`paperDetail` |
| `FactorMinePage` `WorksetCard` / `TodayPage` / `StrategyOverviewPage` / `OptimizePage` | **不改** |
| 已有 W47-1 徽章与四条创建校验 | **不改行为**；同口径通道不把成本输入暴露给用户，成功后详情仍回显费率 |

### 2.4 与已有对象的边界（勿混）

| | 本切片观察盘 | 手动 `#paper` 创建 | W48-2 验收 | `hs300_enhanced` 等注册表策略 |
|---|---|---|---|---|
| 对象 | 某次未过期验收快照中「通过」成员的当时 `expr` | 用户点选的注册表 key | 工作集成员快照逐本簿 + 集合汇总 | 固定库 / 默认参数（如 Top 30、沪深300） |
| 规则与成本 | 从该次 `run.rules` 锁死，用户不可改 | 省略即默认；下拉可能选到不同池 | 同一套写死规则 + 默认成本 + WF | 各策略 `default_params` |
| 区间 | 建盘日 → 今日（前瞻） | 默认也是最近交易日 | 默认 `2023-01-01`～`2025-06-30`（历史窗） | 随创建 |
| 通过一词 | 不在纸面重做裁判；只观察已通过成员 | 无 | WF `final` + 非成本 `fragile` | 无关 |
| 计数 | 不 bump | 不经 mine | 已规定不 bump | — |

页上口径句（产品已给，可上卡片；观察页须同时写清「不是回放验收窗」）：

> **工作集模拟盘观察**：把最近一次未过期验收里「通过」的成员，按与验收相同的交易规则和默认成本（万 2.5+万 1），各建一本模拟盘，从建盘日起向前观察。这不是把验收区间再回放一遍，也不是把多名成员合成一个组合。

---

## 3. 对象、存储、幂等与过期

### 3.1 观察对象（产品 §4.1 字面，不收紧不放宽）

- 对象 = **最新一次已完成、未过期** 的验收快照（`run_id` + 当时各成员 `registry_id` + `expr` + 锁定的 `rules` / `rules_version` / 默认成本）。
- **只**为 snapshot 中 **verdict=通过** 的成员各建一本 `PaperTask`。拒绝 / 样本不足 / 无法按回测口径验收的成员不建盘。
- 集合未通过（拒绝或未齐）时，**仍允许**观察其中已通过的成员，但必须同时可见集合状态。
- 过期（沿用 W48-2 `is_stale`，不改条件）不得建盘，须先在验收块重跑。不回退更早一次 run。
- 禁止注册表固定库顶替；禁止多成员合成一本簿。
- `expr` 取快照当时值（GET 成员已含完整 `expr`，不是 excerpt，也不是现场工作集里可能已改的表达式）。

### 3.2 存储（推荐：专用 TEXT 列，加法迁移）

产品要求「任务绑定 `run_id` + `registry_id`；`expr` 完整保存（加长或另存，禁止 1024 截断）」——形态由契约定，标「推荐」：

```text
-- 由 migrate_paper_workset_observe(conn) 在 _migrate_sqlite 中、
-- 工作集验收迁移之后调用；列均允许 NULL（既有纸面任务不受影响）

ALTER TABLE paper_tasks ADD COLUMN workset_observe_run_id INTEGER;
ALTER TABLE paper_tasks ADD COLUMN workset_observe_registry_id INTEGER;
ALTER TABLE paper_tasks ADD COLUMN workset_observe_expr TEXT;
ALTER TABLE paper_tasks ADD COLUMN workset_observe_rules_version TEXT;
-- 幂等（仅观察任务；SQLite 允许多个 NULL）
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_workset_observe
  ON paper_tasks (workset_observe_run_id, workset_observe_registry_id)
  WHERE workset_observe_run_id IS NOT NULL
    AND workset_observe_registry_id IS NOT NULL;
```

ORM：在现有 `PaperTask` 上加法映射，可空。`kind` 必须为 `portfolio`；`index_code="000906"`；`adj="qfq"`；`commission=COMMISSION`；`slippage=SLIPPAGE`；`initial_cash=1_000_000`（推荐锁定）；`start_date` 为建盘日；`strategy_key="_workset_member_long_topn"`（不进注册表，见推荐第 4 条）；`params_json` **不要**承担完整 `expr`（可放短规则回显或保持 `"{}"`，解析以 TEXT 列 + `build_member_meta` 为准）。

写入契约：

1. 将快照 `expr` 原样写入 `workset_observe_expr`。
2. `commit` 后回读，与快照字符串**完全一致**（含空白）。不一致或空 → 该成员失败，不得调用引擎。
3. 若整次操作没有任何成员能完整保存，进入产品 §4.6（整次「无法按同口径观察」）。部分成功则成功的成员照常建盘，返回里逐人标明失败原因，横幅写清。

不把观察结论写入主区 `backtests`。不改验收表。不存 `stale` 列（读时算）。

### 3.3 何时创建、幂等、副作用

```text
同一 run_id + registry_id 已有未删除任务
    → 打开已有，不新建，不重置 start_date / 账本 / last_bar_date
否则
    → 新建一本，绑定快照 expr 与锁死规则
```

硬删除（现 `DELETE /paper/tasks/{id}` 是真删）之后允许按同一键再建（唯一索引不再冲突）——与产品「已有未删除任务 → 打开」字面一致，不发明软删除。

副作用（产品 §4.5 字面）：

- **不写** `factor_mt_state`，不改历史 mine 评级，不自动把因子 `enabled`/`disabled`，不自动移出工作集，不 bump `n_tested`。
- 不自动停止、重建、删除绑定旧 `run_id` 的纸面任务。
- 纸面任务 `enabled` 见推荐第 7 条（调度开关，不是因子启用）。

### 3.4 过期标记（读时，沿用 `is_stale`）

对已绑定观察任务，列表/详情**推荐**带：

- `workset_observe_run_id` / `registry_id`
- `workset_observe_stale: bool` 与 `stale_reason`
- 计算：**推荐**（请 Product 锁定）：调用 W48-2 `is_stale(绑定的那一行验收, 当前工作集)` 为真，**或者** 该 `run_id` 已不是「最新一次已完成」的验收 → 文案「快照已过期」。不自动删停，不据此改账本。
- 过期标记是展示层；用户仍可打开旧任务看当时纸面，但验收卡按钮在结论过期时不可用、不会再为过期快照建新盘。

---

## 4. 规则锁定、引擎运行与观察区间

### 4.1 交易规则与成本（从该次验收锁死，用户不可改）

对每本观察盘，与 W48-2 写死规则一致，从该次 `run.rules` / `rules_version` 校验后锁死：

1. 股票池 / 基准：中证800（`index_code=000906`，`index_symbol=sh000906`）。
2. 因子值：该成员验收当时的完整 `expr`。
3. 调仓：多头等权 Top 50、21 个交易日、单只上限 5%；不做空头；不开启行业 / 市值中性。
4. 复权：`adj=qfq`。通道若收到非 qfq 或试图改写死规则 → 400，不得悄悄改口径。
5. 成本：省略即 `cost_defaults`（佣金万 2.5、滑点万 1、印花税按引擎默认）。不要传入 user 费率，不要走 `paper_auto` 的万 1+滑点万 15。

实现：创建时把这些值写到任务列 / 由 `build_member_meta` 在每次运行时重建 params；创建后只读回显。本切片不把 top_n、调仓周期、是否中性、费率做成该通道上的可调参数。

成交与持仓装配：观察任务运行时走现有 `_load_portfolio` → `PortfolioBacktestEngine.run(WorksetMemberLongTopN, params)`，与验收共用**同一薄壳类和同一套默认成本**；补录水位仍以 `max(PaperTrade.date)` 为准（不改 2026-11-11 已修口径）。不把结果写入 `backtests`。

### 4.2 观察区间（与验收窗刻意不同）

- 纸面是 **从建盘日起向前观察**，不是把验收历史窗再回放一遍。
- 默认（也是本通道唯一）`start_date` = 中证800 最近有行情的交易日（`_last_index_date(db, "000906")`），跑到当日（引擎现行为 `date.today()`）。
- 页上口径句必须写清。不提供改起始日的高级控件。通道拒绝客户端 `start_date`（推荐第 6 条）。

净值曲线语义沿用现引擎：`date >= account_start`（首次运行时由 `start_date` 锚上）。不要为了「看起来更像回测」去把 `start_date` 改成 `2023-01-01`。

### 4.3 薄壳不进注册表（再陈述）

`WorksetMemberLongTopN` / `_workset_member_long_topn` **不得**出现在 `STRATEGY_REGISTRY`、`GET /strategy/strategies`、`#paper` / `#btopt` 策略下拉。已冻 W48-2 断言仍绿。禁止为图省事走路径 B。通用创建表单与「工作集观察」入口视觉分离，避免用户以为下拉里的 `momentum` / `hs300_enhanced` 就是工作集。

---

## 5. 副作用与失败态（产品字面）

### 5.1 不改裁判、不改计数

建盘与运行都不是一次 mine，也不是一次验收重跑。禁止 bump、禁止改 `fragile` / `final` / WF 窗 / `cost_defaults`。纸面不挂成本网格、不挂滚动 OOS。

### 5.2 无法按同口径观察（路径 D / 运行期失败，不是顶替）

出现下列任一情况时，展示明确失败态，写清原因，提供 `#btopt/0/workset-accept`：

- 纸面引擎无法在本切片安全接入内部薄壳（必须改的入口在 1–2 小时内做不到，或出现系统性 PIT / 成交对不齐）——实现期改走路径 D，停工回 Planner / Product。
- 快照过期、没有通过成员、验收未完成或失败（验收卡按钮不可用；若深链仍打到 `#paper`，顶部同一句话）。
- 无法完整保存 `expr`，或锁死规则无法落地（指数无行情、迁移失败等）。
- 运行时解析钩子发现绑定任务缺少完整 `expr`：不得截断续跑，将该任务 `error_msg` 写明，并在观察横幅反映。

文案方向（产品 §4.6，可微调，语义不得弱化）：

> **无法按同口径观察**：不能按与验收相同的交易规则和默认成本，对已通过的工作集成员建模拟盘。（原因：…）未改用其他策略顶替。请先回到验收块处理，或等待可安全接入的实现。

过期专用：「结论已过期，请先重跑」，不建盘。

**禁止**：静默改用 `enhanced_factor`、`csi800_enhanced`、`hs300_enhanced`、`momentum` 等任何注册表策略；禁止减规则、改成本、改复权来「先跑起来」。

---

## 6. API 契约

前缀与现网一致：`/api/v1` + 纸面路由前缀 `/paper`。**无鉴权。** 路径按产品示例锁定（Desk 勿再改名）。

### 6.1 `POST /paper/tasks/workset-observe`

为指定（或最新一次）未过期已完成验收快照中全部「通过」成员建盘或打开已有任务。

**请求体（推荐）**

```json
{ "run_id": 7 }
```

`run_id` 可省略：取最新一次已完成验收。指定时必须就是那次、且已完成、且未过期。**禁止**接受并生效：`strategy` / `strategy_key`、`adj`（非 qfq）、`commission`、`slippage`、`start_date`、`index_code`、`top_n`、`rebalance_period`、`max_weight`、`neutralize_*`、`short`、`expr`、`params_json`、成员子集。显式冲突 → 400 `rules_param_forbidden`。未知字段忽略即可。

**成功 200**

```json
{
  "ok": true,
  "run_id": 7,
  "stale": false,
  "collection": {
    "verdict": "通过",
    "n": 3, "n_pass": 2, "n_reject": 0,
    "n_insufficient": 0, "n_unevaluable": 1
  },
  "start_date": "2026-11-12",
  "rules_version": "W48-2-v1",
  "tasks": [
    {
      "registry_id": 12,
      "name": "工作集观察 · 动量20 · run#7",
      "task_id": 41,
      "created": false,
      "existing": true,
      "expr_complete": true
    }
  ]
}
```

`created`/`existing` 用于幂等回显。本响应**建议**不回传完整超长 `expr`（详情若需要再给），但必须能证明未截断（`expr_complete=true` 或长度与快照一致）。创建后不要求在同一 HTTP 内跑完引擎（推荐第 7 条）；可带 `run_started: true` 表示已投递首次运行。

**错误**

| HTTP | `error_code` | 何时 |
|---|---|---|
| 400 | `rules_param_forbidden` | 非 qfq 或改写死规则 / 非默认成本 / 传入 `start_date` 等 |
| 400 | `snapshot_stale` | 最新（或指定）结论已过期；message 指向「请先重跑」 |
| 400 | `no_passed_members` | 没有 verdict=通过 的成员 |
| 400 | `accept_not_ready` | 无验收、未完成、`status=error` |
| 400 | `accept_unavailable` | 验收表/服务缺失（运行库落后于该 SHA） |
| 400 | `index_no_bars` | `000906` 尚无行情（沿用现网 ETL 提示） |
| 400 / 409 | `cannot_observe` | 无法完整保存 `expr`、或引擎无法按同口径接入（路径 D / 存储失败）；`message` 写清原因；**不**创建截断任务 |
| 500 | — | 未捕获总失败；应尽量落到 `cannot_observe` 而不是静默 200 |

过期 / 空通过集：**不要** 200 后在前端偷偷建盘。验收卡在调用前就应禁用按钮；API 仍须拒绝，作为第二道闸。

### 6.2 只读：列表/详情回显（加法，不破既有字段）

`GET /paper/tasks` 与 `GET /paper/tasks/{id}`（及 `get_paper_task_detail`）为观察任务**追加**字段（既有任务这些为 null / 缺省，前端不崩）：

- `workset_observe_run_id`、`workset_observe_registry_id`
- `workset_observe_stale`、`workset_observe_stale_reason`
- 可选：`workset_observe: true`、友好 `strategy_label: "工作集观察"`
- 详情可回显锁死规则与费率（W47-1 已有 `commission`/`slippage`/`cost_profile`）

不必新开「观察专用详情页」。可选薄只读 `GET /paper/tasks/workset-observe?run_id=` 一次返回集合状态 + 本轮任务 id 列表，便于横幅；也允许前端组合 `worksetAcceptGet` + `paperTasks`，两种都可以，**推荐**前者减少竞态（标「推荐」）。

### 6.3 与通用 `POST /paper/tasks`、`POST /paper/auto-ensure` 的关系

- 通用创建行为**不变**：仍只接受已注册未停用 key；内部 key → 400「未知策略」。
- **不要**让浏览器对观察走 `paperCreate({ strategy_key: "_workset_member_long_topn", params_json: "…" })`。
- **不要**走 `auto-ensure`（费率不是默认契约）。
- 手动运行、开关、删除、偏离、快照接口对观察任务同样可用（运行必须走解析钩子）。删除是用户显式动作，不是本切片自动行为。

---

## 7. 前端契约

### 7.1 挂载与入口（不新开 hash）

- **唯一主入口**：`WorksetAcceptPanel` 结论区，「去模拟盘观察」。有通过成员且未过期时可用。点击：`quant-nav` `{ tab: "paper", inner: "workset-observe" }` 或 `<a href="#paper/0/workset-observe">`（须触发已有 `hashchange`）；可把 `run_id` 编入 inner（推荐第 5 条）。**不要**在验收卡里再做一套净值 / 持仓 UI。集合状态只做展示，不另做「集合一本盘」按钮。空集仍只深链 `#factorlab`（已有），不出现观察按钮。
- 过期：按钮不可用，文案「结论已过期，请先重跑」，不建盘。
- 无通过成员 / 验收未完成或失败：按钮不可用并说明原因。
- `#factorlab` `WorksetCard`：**不加**「去模拟盘观察」，不复制观察 UI（会跳过验收）。
- `#today` / `#strategy-overview`：本切片不展示验收结论、不挂观察入口。

### 7.2 `#paper` 只补建盘入口与接线，观察面不翻修

- `App.tsx`：`HubContent` 把 `inner` 传给 `PaperPage`（允许的小改，不是新 Tab）。**禁止**在 `HUBS` 增加 `workset-observe` key。
- `PaperPage` 消费 `inner`：若为 `workset-observe` 或 `workset-observe-{run_id}`，幂等调用 `POST /paper/tasks/workset-observe`，然后刷新已有任务列表，必要时滚到/选中本轮任务。失败则顶部展示 §5.2 文案 + 回验收深链 `#btopt/0/workset-accept`。
- 保留现有手动创建表单（仍只对已注册策略），与「工作集观察」入口**视觉分离**。不要把薄壳或工作集成员放进策略下拉，不要预填手动表单为内部 key。
- 建盘成功后，用户就在已有列表和 `DetailPanel` 里观察：净值、持仓、最近成交（账本）、vs 基准、vs 理论、W47-1 徽章与口径句。任务名能看出成员与 `run#{run_id}`。
- **推荐**观察模式顶栏：产品口径句 + 「起始日 = 建盘日 {date}（该指数最近交易日），不是验收窗 2023-01-01～2025-06-30」+ 集合状态徽章（`collection.verdict` 与计数）+ 回验收链接。旧任务行打「快照已过期」轻标记（灰文案即可），不隐藏、不自动停。
- 再次点击同一 `run_id` + 同一通过成员：打开已有，列表不出现重复行。

### 7.3 深链（不是新 hash Tab）

本仓 hash 形如 `#paper`，第三段 `inner` 已有惯例（`#btopt/0/workset-accept`；`parseHash` / 写回 `#${sub}/0/${inner}`）。

**推荐锁定**：`#paper/0/workset-observe`；附 run 时 `#paper/0/workset-observe-{run_id}`。

**禁止** `#workset-observe` 或 `#workset-paper` 作为一级 hash（会被 `parseHash` 判为未知并掉到默认页）。**禁止**依赖第四段路径。

### 7.4 文案与按钮

- 主按钮：「去模拟盘观察」。
- 过期禁用：「结论已过期，请先重跑」。
- 无通过成员：「没有已通过的成员，无法建盘」（或等价，不得暗示可以顶替）。
- 失败态：产品 §4.6 那段「无法按同口径观察…（原因：…）未改用其他策略顶替。」
- 口径句：§2.4 产品原文 + 前瞻观察说明。
- `client.ts`：`paperWorksetObserve`；类型与 §6 对齐。超时按「同步写库」短超时即可；首次运行靠已有列表轮询，不必把 POST 做成 10 分钟同步跑引擎。

---

## 8. 测试清单（对照产品 §5）

新文件：`backend/tests/test_workset_paper_observe.py`。编排用**假引擎 / spy `run_paper_task`**（不必每次打真实中证800 全市场）；规则与存储、幂等、拒绝码用真服务函数。薄壳行为已由 W48-2 `test_workset_long_topn.py` 覆盖，本切片不重写选股测试，只断言调用的是同一 `cls` 与同一份 `build_member_meta`。

### 8.1 产品勾选 → 测例 / 手册

| 产品验收 | 工程覆盖 |
|---|---|
| 验收卡在未过期且至少一名通过时可见「去模拟盘观察」；过期 / 无通过时不可用并说明 | 手册 + 面板条件；API 对过期 / 空通过集拒绝 |
| 点击后进入 `#paper`（`#paper/0/workset-observe` 或等价 inner，不是新 Tab），每名通过成员一本任务，绑定该 `run_id` | 手册；hash 解析；POST 对通过成员逐本、拒绝/不足/无法验收不建 |
| 完整 `expr`、写死规则、`adj=qfq`、默认成本万 2.5+万 1 | 单测：任务列与 `_resolve_paper_meta` 的 params；非 qfq / 改规则 → 400 |
| 观察面复用已有；任务名区分成员与轮次 | 手册；名称公式单测（含 64 字截断仍唯一） |
| 起始日为建盘日（最近指数交易日），不是验收历史窗；页上有口径句 | 单测 `start_date` 来自 `_last_index_date`，传入 `2023-01-01` → 400；手册看文案 |
| 再次点击不重复建盘；过期快照不建盘；旧任务最多打过期标记，不自动删停 | 幂等单测；过期标记单测；spy 无 `delete`/`enabled=False` 批量动作 |
| 不被注册表策略顶替；不合成组合簿；薄壳不出现在策略下拉 | 源码 / import 断言；一次任务一个 `registry_id`；`STRATEGY_REGISTRY` 断言仍绿；`list_strategies()` 不含内部 key |
| `#factorlab` 不复制观察 UI、不加跳过验收的出口；`#today` / 总览本切片不展示验收结论 | 手册；源码检索无新出口 |
| 无法安全接入时失败态 + 原因 + 回验收深链，无静默顶替 | API `cannot_observe`；手册；禁止路径的负向测例 |
| 不自动启用、不移出、不 bump；`expr` 无截断 | spy `bump_n_tested` / `admit` / `remove_member` / 因子 `enabled`；回读比对；把超长 `expr`（>1024）写入 TEXT 列仍完整，且即便误写入 1024 列也不用截断值开盘 |

### 8.2 工程单测（产品 §5 工程项细化）

- 未过期 + 通过成员可建盘；`kind=portfolio`，`index_code=000906`，成本为 defaults，`adj=qfq`。
- 过期 / 空通过集 / 验收未完成 → 拒绝，零新建。
- 非 qfq / 改写死规则 / 传入验收窗 `start_date` / 非默认费率 → 400 `rules_param_forbidden`。
- 同一 `run_id`+`registry_id` 第二次 POST 返回已有 `task_id`，不新增行。
- 截断防护：超长 `expr` 完整落 TEXT 列；回读失败或空 expr → 该成员 `cannot_observe`，库中无截断开盘任务。
- `n_tested` 不增加；因子 `status` 与工作集行不变。
- `_resolve_paper_meta` 对观察任务返回 `cls is WorksetMemberLongTopN`，且**不调用** `get_strategy`；对普通任务仍 `get_strategy`（回归）。
- `run_paper_task` 在观察 key 不在注册表时**不再**因约 L314 的 `get_strategy` 写成 `error_msg`（用假/注入 meta 测）。
- 类与 `_workset_member_long_topn` 仍不在 `STRATEGY_REGISTRY`（复用或调用已冻 W48-2 断言）。
- 源码级：观察服务不 `get_strategy("enhanced_factor")` 等，不 `_persist` / `_do_backtest`。
- 集合未通过但有通过成员：仍建通过者的盘；载荷含 `collection`。
- 不回退到更早 run；最新一次 stale 时即使更早一次「看起来未过期」也不建。

### 8.3 回归（不得弱化）

跑绿并保持断言强度：`test_workset_backtest_accept.py`（尤其注册表断言）、`test_workset_long_topn.py`、`test_paper_ux.py`（W47-1 徽章与四条校验原文）、手动 `#paper` 创建（注册表策略）、成交按账本、vs 理论偏离、验收块与深链 `#btopt/0/workset-accept` 行为不变。`test_factor_workset.py` 入集语义不变。

无密钥进源码；不推远程。

---

## 9. 风险、落地序与硬闸门

### 9.1 落地序（约 0.5–1 Desk 日）

| 步 | 内容 | 约 |
|---|---|---|
| 1 | `paper_tasks` 加法迁移 + `_resolve_paper_meta` 接到三处 + 单测（普通任务行为不变、观察任务不 `get_strategy`、超长 expr 完整） | 0.2d |
| 2 | `workset_paper_observe.py`：读快照、`is_stale`、锁规则、幂等、回读比对、禁止副作用；路由 POST/GET | 0.25d |
| 3 | `WorksetAcceptPanel` 按钮与失败态；`HubContent` / `PaperPage` inner 接线；横幅、口径句、过期标记、`client.ts`；首次运行投递到已有后台 | 0.2d |
| 4 | §8.3 回归 + 手册点检产品 §5；确认无 bump、无因子自动 enabled、无薄壳入表、无 enhanced_factor 顶替、无截断开盘 | 0.1d |

一轮只做本切片。不要顺手做 M3-8、日报、改裁判、把薄壳登记进公开策略列表、大改持仓 UI。

必须交付：未过期通过成员可按同口径逐本建盘并在已有 `#paper` 观察；深链；幂等；过期与失败态；`expr` 不截断；薄壳不进注册表。总览展示、集合合成盘、完整历史回放均非本切片必须。

若步骤 1–2 发现系统性 PIT / 成交对不齐或入口无法在工时内安全改完：**停工**，交付路径 D 失败态，回 Planner / Product，禁止顶替。本契约判断此为残余风险而非当前阻塞（§0、§2.1）。

### 9.2 风险

| 风险 | 缓解 |
|---|---|
| **`run_paper_task` 约 L314 未使用但仍调用的 `get_strategy`**：只改 `_load_portfolio` 会让调度/手动运行写成 `error_msg` | 钩子必须覆盖三处；单测专门打这条路径；子进程同样覆盖 |
| **`params_json String(1024)` 截断** | 专用 TEXT 列 + 回读比对；解析不用 `params_json` 覆盖 `expr`；超长测例 |
| **漏改调度器**：组合 19:00 走 subprocess | 子进程进 `run_paper_task`，钩子到位即覆盖；测例/代码路径注释写清 |
| 改 `_load_portfolio` 装配导致「回测有成交、模拟盘 0 笔」历史故障再现 | **禁止**改预热 / PIT / 财务注入；只换 meta 来源；回归既有组合纸面任务 |
| 误用路径 B 或 `enhanced_factor` / `hs300_enhanced` 顶替 | 代码审查 + §8.2 import/注册表断言；契约红线 |
| 误 bump / 自动启用因子 / 自动移出 / 自动删停旧盘 | 服务禁止调用；测例 spy |
| 把验收窗填进 `start_date` | 通道拒绝 `start_date`；单测；口径句 |
| `PaperTask.name` 64 字唯一冲突 | 命名公式 + 截断仍含 `run_id`/`registry_id`；测例 |
| 运行库缺 W48-2 表 | `accept_unavailable`，面板失败态；不重做验收 |
| 墙钟 / 组合 OOM | POST 不同步跑引擎；首次运行走已有组合子进程；不减规则 |
| 实现中系统性 PIT 对不齐 | **停工回 Planner/Product**，路径 D，禁止顶替 |

**残余风险（诚实陈述，与 W48-2「薄壳可以在本切片安全落地」同一标准）**：解析钩子三点必须齐（尤其未使用的那次 `get_strategy`）；`expr` 必须落 TEXT 而不能「先写进 1024 碰碰运气」（SQLite 对 `VARCHAR(1024)` 往往不强制截断，**不得**依赖这一实现细节）；纸面 `_load_portfolio` 与验收 `_run_portfolio` 本就是两条装配、历史上出过 0 成交——本切片通过「同一薄壳 + 同一套 warmup params + 不改装配」把漂移面压到最小，但仍须在实现期用一只通过成员做一次手工对照（成交是否按账本出现、池是否中证800）。这些是落地纪律，不是改走 C 或顶替的理由。

### 9.3 硬闸门（再陈述）

- 不开实盘；密钥不进源码；不擅自推远程；本文只设计、不改业务代码（Desk 落地时仍遵守前三条）。
- 不改裁判（验收测试、成本 / PIT / 复权 / WF / `fragile`），不改 `cost_defaults`，不 bump `n_tested`。
- 不用 `enhanced_factor` / `enhanced_factor_hs300` / `csi800_enhanced` / `hs300_enhanced` 或任何注册表策略顶替；不把薄壳写入公开注册表；不把多成员合成一本簿。
- 不新开 hash Tab；不在 `#factorlab` 加跳过验收的出口；不改 `#today` 职责。

### 9.4 关键取舍摘要

1. **路径 A：专用 `POST /paper/tasks/workset-observe` + `_resolve_paper_meta`，复用 `build_member_meta`。**——**推荐，已选；本切片可以安全落地**
2. **路径 B 写入公开注册表。**——**禁止**
3. **路径 C 专用 paper runner。**——**不选**（A 不必大翻修，C 更易漂移）
4. **路径 D 失败态。**——实现期安全退路，不是默认交付；禁止顶替
5. **`expr` 专用 TEXT 列 + 回读比对，不用 `params_json` 1024 作为唯一存储。**——**推荐**
6. **深链 `#paper/0/workset-observe`（run_id 放 inner 内部）；`HubContent` 传 `inner`。**——**推荐**
7. **`start_date` 锁最近指数交易日，通道拒客户端传入。**——**推荐**（落实已冻「前瞻观察、不回放验收窗」）
8. **POST 同步建盘、首次运行异步；纸面 `PaperTask.enabled=True` 仅作调度开关。**——**推荐**
9. **过期读时算；不回退更早 run；不自动删停。**——已冻 + 推荐补全计算式
10. **系统性 PIT 对不齐只许停工上报，不许顶替。**——已锁

---

## 10. 附录：路径与现状速查（main `bbd5bdb`）

| 主题 | 路径 / 符号 |
|---|---|
| 本契约（box） | `/workspace/docs-design-W48-3-workset-paper-observe-arch.md` |
| 产品方案（已冻） | `/workspace/docs-design-W48-3-workset-paper-observe.md` |
| 调研备忘 | `/workspace/w48-3-research.md` |
| 读码基线 | `ljew/quant-platform` [`bbd5bdb676ddc21bde5f3352f461e27368900451`](https://github.com/ljew/quant-platform/commit/bbd5bdb676ddc21bde5f3352f461e27368900451)（当前 main HEAD） |
| W48-2 产品 / 契约 | `/workspace/docs-design-W48-2-workset-backtest-accept.md`、`…-arch.md`（仓内该 SHA 有 arch：`docs/DESIGN-W48-2-workset-backtest-accept-arch.md`） |
| W48-1 契约 | `/workspace/docs-design-W48-1-sparse-orthogonal-arch.md` |
| 验收块 / 深链 | `web/src/components/WorksetAcceptPanel.tsx`（`id="workset-accept"`）；`#btopt/0/workset-accept` 已接线 |
| 验收服务 / 薄壳 | `backend/app/services/workset_backtest_accept.py` → `build_member_meta` / `is_stale` / `get_latest_accept` / `VERDICT_PASS` / `ACCEPT_*`；`backend/app/core/strategies/workset_long_topn.py` |
| 验收路由 / 表 | `backend/app/routers/strategy.py` → `POST`/`GET /backtest/workset-accept`；`FactorWorksetAcceptRun` / `factor_workset_accept_run` |
| 纸面路由 | `backend/app/routers/paper.py` → `PaperTaskCreate` / `create_task`（`get_strategy`）/ `_last_index_date` / `GET /strategies`（不过 `DISABLED_STRATEGIES`）/ `list_tasks` / `run_task` |
| 纸面引擎 | `backend/app/core/engine/paper_engine.py` → `_merge_params` / `_load_single`（约 L57 `get_strategy`）/ `_load_portfolio`（约 L117）/ `run_paper_task`（约 L314，meta 未随后使用）/ `get_paper_task_detail` |
| 调度 | `paper_scheduler.py`（`enabled==True`；组合 ≥19:00 + `run_in_subprocess`）；`paper_runner.py` |
| 自动建盘（勿用） | `backend/app/services/paper_auto.py`；`POST /paper/auto-ensure` |
| 模型 | `PaperTask`（`params_json String(1024)`，`name String(64)` unique，`strategy_key String(32)`）；`FactorRegistry.expr Text` |
| 注册表 | `backend/app/core/strategies/registry.py` → `get_strategy` / `STRATEGY_REGISTRY` / `DISABLED_STRATEGIES` / `list_strategies`（`GET /strategy/strategies`，`PaperPage` 在用） |
| 前端 | `web/src/pages/PaperPage.tsx`；`web/src/App.tsx` `parseHash` / `HubContent`（`paper` 尚不传 `inner`）；`web/src/api/client.ts`（`paperCreate` / `paperTasks` / `worksetAcceptGet` 已有） |
| 实验室卡（本切片不改） | `WorksetCard`（仅「去回测验收」） |
| 成本 | `app.core.cost_defaults`（`COMMISSION=0.00025`，`SLIPPAGE=0.0001`）；W47-1 `EvalBadges` / `test_paper_ux.py` |
| 迁移挂钩 | `backend/app/database.py` → `_migrate_sqlite` |
| 已冻测例（须仍绿） | `test_workset_backtest_accept.py` / `test_workset_long_topn.py` / `test_paper_ux.py` / `test_factor_workset.py` |
| 本切片测例 | `backend/tests/test_workset_paper_observe.py`（新增） |
| 不要用的过期快照 | `/workspace/quant-latest`（历史 HEAD `4e63519`） |

### 产品对齐核对（Architect 自检）

| 产品冻结项 | 本契约 | 改口？ |
|---|---|---|
| 页面 `#paper` / `PaperPage`；入口验收卡「去模拟盘观察」；深链 `#paper/0/workset-observe`（已有 inner，非新 Tab）；`#factorlab` 不加出口 | §7 | 无 |
| 对象 = 最新未过期快照中通过成员，逐人一本；集合未通过仍可观察已通过者，集合状态可见；禁合成簿 | §3.1、§7.2 横幅 | 无 |
| 规则与默认成本从该次验收锁死；非 qfq / 改写死规则 → 400 | §4.1、§6.1 | 无 |
| 从建盘日（指数最近交易日）向前观察，不回放 `2023-01-01`～`2025-06-30` | §4.2、推荐第 6 条 | 无 |
| `expr` 完整保存；不得用 `params_json` 1024 截断后开盘 | §3.2、推荐第 2 条（TEXT 列是实现形态，满足产品「加长或另存」） | 无 |
| 薄壳不进公开注册表与下拉；禁 `enhanced_factor` 等顶替 | §2.1、§4.3、§8 | 无 |
| 同一 `run_id`+`registry_id` 幂等；不 bump、不自动 enabled/移出；旧任务可打过期标记，不自动删停 | §3.3–3.4；推荐第 7 条明确纸面调度 `enabled` ≠ 因子 `enabled` | 无 |
| 无法安全接入 → 「无法按同口径观察」+ 原因 + `#btopt/0/workset-accept`；禁静默顶替；PIT 对不齐停工 | §5.2、§9.2 | 无 |
| 倾向路径 A；B 禁止；C 仅特定条件；否则 D | §2.1 选 A、不选 C、B 禁止、D 为退路；有代码锚点证据 | 无 |
| 非目标（M3-8 / 改裁判 / 日报 / 新 hash / 实验室复制 UI / 实盘 等） | §1.4 | 无 |

无产品改口。无待重开的已冻口径。实现补全均标「推荐」，**Product 已一并锁定**（见文末冻结记录）。

---

## 冻结记录

2026-11-13 Asia/Shanghai · Quant Product 冻结本技术契约。对照产品稿无改口。路径 A 为默认交付；路径 B 禁止；路径 C 不选；路径 D 仅当实现期 1–2 小时内无法安全落地（系统性 PIT / 成交对不齐）时启用，禁止顶替。以下「推荐」实现补全一并锁定，Desk 按此写死：

1. 路径 A 为默认交付，D 只作实现期退路；B 禁止，C 不选。
2. `paper_tasks` 加法 TEXT 列完整保存 expr（`workset_observe_run_id` / `workset_observe_registry_id` / `workset_observe_expr`），写入后回读比对，禁止把完整 expr 只放进 `params_json String(1024)`。
3. `_resolve_paper_meta` 必须接到全部三处 `get_strategy`（`_load_single`、`_load_portfolio`、尤其 `run_paper_task` 约 L314）；内部 key `_workset_member_long_topn` 仅作标记，不进 `STRATEGY_REGISTRY`，列表用友好名「工作集观察」。
4. 深链锁定 `#paper/0/workset-observe`，`run_id` 放 inner 内部（如 `workset-observe-{run_id}`），因 `parseHash` 只拆三段；`HubContent` 传 inner，落地即幂等 POST。
5. 通道不接受客户端 `start_date`，一律 `_last_index_date(db, "000906")`，页上写清前瞻口径句。
6. POST 同步写库，首次运行走已有后台/组合子进程；`PaperTask.enabled=True` 仅作纸面调度开关，不是 `FactorRegistry.enabled`。
7. 过期读时算：绑定 run 的 W48-2 `is_stale`（不改条件）为真，或已不是最新一次已完成 →「快照已过期」；不回退更早 run；不自动删停。
8. Body 只收 `run_id`（可省略=最新一次已完成）；改规则/非 qfq/非默认成本 → 400 `rules_param_forbidden`；`initial_cash` 锁 1_000_000。
9. 任务名按产品公式，`name String(64)` 超长则截断成员名并保留 `run_id`/`registry_id`。
10. `#paper` 观察横幅展示集合状态 + 回验收深链 `#btopt/0/workset-accept`。

残余风险是落地纪律（三处钩子必须齐、expr 必须落 TEXT），不是改走 C 或顶替的理由。实现中若系统性 PIT / 成交对不齐须停工回 Planner / Product，禁止顶替。请 Planner 派 Desk。硬闸门：不开实盘，密钥不进源码，不擅自推远程，不改裁判。

