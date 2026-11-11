# 技术契约：W48-1 / M3-5「少而正交工作集」

> 作者：Quant Architect · 2026-11-11（Asia/Shanghai）
> 产品方案（已冻结，本契约不得改口）：`/workspace/docs-design-W48-1-sparse-orthogonal.md`
> 设计锚点：REBUILD-2026-10.md **M3-5**「目标 3-5 个低相关因子，而非 20 个」；已冻 W47-2 / W47-3
> 对照体例：`docs-design-W47-2-orthogonalize.md`、`docs-design-W47-3-multiple-testing.md`、`docs-design-OPS-FN-1-foreign-news.md`
> **硬闸门**：不开实盘；密钥不进源码；不擅自推远程；**本文只写设计，不改业务代码。**
> **冻结状态（2026-11-11 Asia/Shanghai）**：**Product 已冻结本技术契约**。对照已冻产品稿确认 **无改口**。下文原标「推荐」的实现补全一并锁定，不重开已冻阈值 / 质量门 / 计数口径。Planner 可派 Desk；本轮不默认拉全员。
>
> **一并锁定的实现补全**（不是改口）：阈值写死 `WORKSET_CORR_ABS_MAX=0.50`，本切片不提供调参；as-of=`max(factor_daily.trade_date)`；有对端时 `n_pairs<20` 或无 as-of → 无法判定、暂不入集；空集第 1 人只过质量门（不因缺 as-of 被拒）；admit/remove 用 `BEGIN IMMEDIATE`，事务内重算，不信任过期 preview，UNIQUE + 提交前 COUNT≤5；`candidate`/`enabled` 可入集，`disabled` 须先改回 candidate（已在集内后切 disabled 不自动驱逐）；入集不自动 enabled，不 bump `n_tested`；同 t 不替换；并列时更早 `added_at`（再比更小 `registry_id`）破平。
> 读码基线：GitHub `ljew/quant-platform` main `9d3245a`（全 SHA `9d3245a16418ee10ca5753aee81af93f6ca5857b`，2026-11-10T21:01:25Z / 2026-11-11 05:01 Asia/Shanghai 合入 origin/dev）。**不要**用 `/workspace/quant-latest` 或 `quant-platform-src-latest.tgz`（约 2026-11-09，HEAD `4e63519`，已滞后）。

---

## 0. 结论 / 推荐 / 取舍 / 落地序（先读）

**只做一件事**：在已有 `#factorlab` / `FactorMinePage` 上给出容量硬顶 5 的「少而正交工作集」——质量复用实验室门禁，相关对**工作集成员**最新截面 **raw Spearman**，满员或高相关只给替换建议（须确认，一次只换一人，替换后重算新集合全部两两 `|ρ|`）。不新开 hash，不重做正交引擎，不改 W47-2/3，入集 / 预览 / 移出 **不 bump** `n_tested`，不自动 `enabled`。

**模块落地（与产品 §7 对齐，路径写死）**

| 层 | 本切片 | 不要动 |
|---|---|---|
| 服务 | 新增 `backend/app/services/factor_workset.py` | `factor_ortho.py` 算法与 `ORTHO_EXPRS`；`factor_mt.py` 的 `bump_n_tested`；`mine_factor` 评级公式 / luck_bar 常数 |
| 相关复用 | 抽出并复用 `factor_mining._corr_with_existing` 的最新截面 raw Spearman 口径（对端改为成员表达式，`eval_factor` 求值；不必已写入 `factor_mined_daily`） | 改 14 列 `FACTOR_COLUMNS`、改优秀门 `max_abs_corr<0.8`、把工作集相关改成 resid |
| 门禁 | 直接调 `factor_lab_gate.check_enable_gate` / `assert_enable_allowed` | 放宽或另写一套评级门槛 |
| 路由 | `backend/app/routers/factor.py`：`GET /factor/workset`、`POST /factor/workset/preview`、`POST /factor/workset/admit`、`POST /factor/workset/{registry_id}/remove` | 修改 `/factor/mine` 的 ortho / mt 语义 |
| ORM | 薄表 `factor_workset`，`registry_id` UNIQUE + FK → `factor_registry.id` | 给 `FactorRegistry.status` 加第四态；把工作集写进 `factor_mt_state` |
| 前端 | 仅 `web/src/pages/FactorMinePage.tsx` + `web/src/api/client.ts` | 新开 `#workset`；大改列表布局；动 GP 区默认；动 `#paper` / `#cards` |
| 测试 | 新增 `backend/tests/test_factor_workset.py`；补 n_tested / W47-2/3 回归 | 弱化已有 `test_factor_ortho.py` / `test_factor_mt.py` / `test_factor_lab.py` |

**实现细节推荐（产品已冻规则下的补全，Desk 按此写死）**

1. 相关阈值常量 `WORKSET_CORR_ABS_MAX = 0.50`（入集当且仅当 `max_i |ρ_raw| < 0.50`）；本切片 **不** 提供 API/UI 调参。
2. 最新截面 as-of = `max(factor_daily.trade_date)`，与 `_corr_with_existing` 同一取日；股票池 = 该日核心指数 PIT 成分并集 ∩ 上市/退市过滤（见 §4）。
3. 路径 `{registry_id}` 用注册表业务主键，不用工作集行 id。
4. 写入接口（admit / remove）在 SQLite 上 `BEGIN IMMEDIATE`，事务内重算决策后再写——禁止信任过期 preview，禁止并发写出第 6 人。
5. `disabled` 不可入集（须先在生命周期回到 `candidate`）；`candidate` / `enabled` 均可，入集不 toggle 状态。
6. 主路 `rating` / `t_stat` **现场**读最近一次挖掘报告（`load_mine_report`），不在薄表快照一份以免和实验室漂移。
7. 同 t 不替换（产品已冻）；「最低 t」或「最大 |ρ|」并列时，用 `added_at` 更早（再比 `registry_id` 更小）做稳定破平。

**落地序（约 0.75–1 Desk 日）**：表+迁移 → 相关抽出与工作集服务+单测 → 路由 → 前端卡片/⑦/client.ts → 口径回归。详见 §9.1。

---

## 1. 背景 / 目标 / 非目标

### 1.1 背景（读码，main `9d3245a`）

- REBUILD **M3-5**：目标 3–5 个低相关因子，而非 20 个。W47-2 / W47-3 已在 main；注册表只有 `candidate/enabled/disabled`，**无容量、无成员两两相关**。
- 路由 `#factorlab`：`web/src/App.tsx`（`key: "factorlab"` → `FactorMinePage`）。列表 / 详情两屏已在 `FactorMinePage`；报告块 ①–⑥ 已齐；④ `CorrHeat` 展示 14 列 raw 相关；⑤ 生命周期已接 `api.factorRegistryRegister/List/Toggle`。
- 登记 / 启用门禁：`backend/app/services/factor_lab_gate.py` → `check_enable_gate` / `assert_enable_allowed` / `load_mine_report`；`POST /factor/registry/register` 与切 `enabled` 共用。开正交时报告主路 `rating` / `t_stat` 已是 **resid**（W47-2）。
- 相关：`factor_mining._corr_with_existing` 在最新 `FactorDaily.trade_date` 上，对候选 raw 表达式 vs 14 列 `FACTOR_COLUMNS` 算 Spearman（有效配对 ≥20）；优秀门仍要 `|max_abs_corr|<0.8`。该矩阵 **不是** 工作集入集硬门。
- 计数：`factor_mt.get_n_tested` / `bump_n_tested`；一次真实 mine 才 +1。工作集操作不是 mine。
- 缺口：无 workset 模型 / API / 列表置顶卡 / 详情「申请入集」；`_corr_with_existing` 对端写死 14 列。
- 本仓 `/factor/*` **无鉴权**（个人本机投研台，与现网其它因子接口一致）。API 前缀 `settings.api_prefix=/api/v1` + `APIRouter(prefix="/factor")`。

### 1.2 目标

1. 持久化 0–5 人工作集（薄表，FK → `factor_registry.id`）。
2. 预览（无副作用）给出三种明确结果：**可入集** / **不可入（原因）** / **建议替换某某（原因 + raw |ρ| + 两边主路 t）**；写入须用户确认，且服务端事务内重算。
3. `#factorlab` 列表置顶「少而正交工作集（n/5）」+ 成员表 + 成员间 raw 小热力 + 移出；详情在 ⑥ 之后加「⑦ 申请加入工作集」。不新开 hash。
4. 口径锁死见 §3–§5；与 W47-2/3 回归一致。

### 1.3 非目标（本切片刻意不做）

| 不做 | 归属 |
|---|---|
| M3-8 组合层优化（风险平价 / 约束优化 / 权重 / 组合 IR） | Product 非目标 |
| 复活 GP、改 GP 默认开关或 fitness | Backlog |
| 开实盘或把工作集接到券商 / live | 硬闸门 |
| SVD / 全库两两正交 / 施密特；改 `ORTHO_EXPRS` 或残差算法 | 已冻 W47-2 |
| 改优秀门 `max_abs_corr<0.8`（仍对 14 列 raw）、改相关矩阵列集 | 已冻 W47-2 |
| 改 `factor_mt_state` 计数、luck_bar 公式（+0.3/−0.3/地板 2.0/1.5）、Bonferroni「只展示」、补 PBO/CSCV | 已冻 W47-3 |
| 用策略层行业/市值中性冒充工作集正交 | 概念混淆 |
| 给 `factor_registry.status` 加第四态；工作集与 `enabled` 打成同一集合 | 产品明确避开 |
| 新开 `#workset`；把工作集挂到 `#cards` / `#paper`；大改列表布局 / 动 GP 区 | 产品范围 |
| 入集自动 `toggle→enabled`；预览/入集/移出 bump `n_tested` | 已冻禁止 |

不对齐为本切片硬门槛（沿用 W47-2 处理）：「至少 1 个因子 OOS IC≥0.03 且 OOS t≥2.0」「组合 OOS 超额>0 且 IR≥0.3」——后者更接近 M3-8。

---

## 2. 模块边界

### 2.1 关键取舍：新服务 + 抽复用相关，不 fork 引擎

| 方案 | 利 | 弊 | 结论 |
|---|---|---|---|
| A. 在 `factor_mining.py` / `mine_factor` 内塞入集状态机 | 少文件 | 挖矿主路径与短名单耦死；易误 bump 计数 | **不选** |
| B. **新建** `factor_workset.py`，质量调 `factor_lab_gate`，相关抽复用 `_corr_with_existing` 的求值/Spearman 口径 | 边界清；可单测；不碰评级公式 | 需一次行为保持型抽出 | **选 B** |
| C. 复制一份 Spearman / 截面求值 | 无依赖 | 双份算法必然漂移 | **禁止** |
| D. 只读 `factor_mined_daily` / 14 列当成员值 | 实现快 | 成员不必 `enabled`、不必已落日频；产品明确可用 `eval_factor` | **不选** |

**新服务职责** `backend/app/services/factor_workset.py`：

| 符号 | 职责 |
|---|---|
| `WORKSET_CAP = 5` | 容量硬顶 |
| `WORKSET_TARGET_MIN = 3` | 仅文案提示，少于 3 不报错 |
| `WORKSET_CORR_ABS_MAX = 0.50` | 直接入集硬门：`max_abs_corr_ws < 0.50` |
| `WORKSET_MIN_PAIRS = 20` | 配对有效股票数下限（与 `_corr_with_existing` / 正交截面一致） |
| `migrate_factor_workset(conn)` | SQLite `CREATE TABLE IF NOT EXISTS`（由 `database._migrate_sqlite` 调用） |
| `list_workset(db) -> dict` | 成员 + 现场主路指标 + 两两 raw 矩阵 |
| `preview_admit(db, registry_id) -> dict` | **无写**；质量 + 相关 + 容量/替换建议 |
| `admit(db, registry_id, *, confirm, replace_registry_id=None, note="") -> dict` | 事务内重算后写入或拒绝 |
| `remove_member(db, registry_id) -> dict` | 移出，无门禁；推荐幂等 |

**允许的行为保持型抽出**（可放在 `factor_mining.py` 供两边 import，或同文件私有再被 workset 调用）：

| 符号（建议名） | 职责 | 约束 |
|---|---|---|
| `load_latest_cs_context(db) -> CsContext \| None` | 取 as-of、PIT 股票池、对齐 K 线 / attrs / 生命周期，窗口为 as-of 前约 300 自然日（与 `_corr_with_existing` 的 `idx-130` / `len(seg)<60` 跳过规则一致） | 不得改变 `_corr_with_existing` 对 14 列的输入输出 |
| `eval_raw_values_on_cs(ctx, expr) -> dict[str, float]` | 把 `_corr_with_existing` 里「新因子值（最新截面）」循环抽成函数：`make_ns` + `eval_factor`；失败 / 非有限值跳过 | 只评 **raw 表达式**，不走 `orthogonalize_cross_section` |
| `pairwise_raw_spearman(a, b) -> tuple[float \| None, int]` | 两边都有值的配对；`n < 20` 返回 `(None, n)`；否则 `(round(_spearman(...), 4), n)` | 复用已有 `_spearman`（并列平均秩；`n<5` 在本路径不会走到，因为先截在 20） |

`_corr_with_existing` 可改为调用抽出函数填「新因子值」，**14 列仍读 `FactorDaily`，`pairs>=20`、`round(..., 4)`、`max_abs_corr` 保留 3 位，优秀门 0.8 一字不改**。禁止把工作集成员写进 `FACTOR_COLUMNS`。

### 2.2 后端路径表

| 层 | 路径 | 本切片职责 |
|---|---|---|
| 新服务 | `backend/app/services/factor_workset.py` | 预览 / 入集 / 替换 / 移出 / 列表 |
| 相关抽出 | `backend/app/services/factor_mining.py` → `_spearman` / `_corr_with_existing`（及新抽出函数） | 行为保持；对端扩展给工作集用 |
| 求值 | `app.core.engine.factor_expr.eval_factor` | **只读复用** |
| 门禁 | `factor_lab_gate.py` → `load_mine_report` / `check_enable_gate` / `assert_enable_allowed` | **只调用，不改符号** |
| 计数 | `factor_mt.py` → `get_n_tested` / `bump_n_tested` | **禁止**从 workset 路径调用 bump |
| 正交 | `factor_ortho.py` / `ORTHO_EXPRS` | **禁止改** |
| 路由 | `backend/app/routers/factor.py` | 新增 4 个端点；薄封装 + `HTTPException` |
| ORM | `backend/app/models.py` → 新 `FactorWorkset` | 见 §3；不改 `FactorRegistry` / `FactorMtState` / `FactorMineResult` 列 |
| 迁移 | `database._migrate_sqlite` 调 `migrate_factor_workset`；`Base.metadata.create_all` 覆盖新库 | 加法、可逆（DROP 薄表不影响注册表） |

### 2.3 前端路径表

| 挂载点 | 说明 |
|---|---|
| `#factorlab` → `FactorMinePage.tsx` | **仅此页**。列表：置顶卡 + 页眉「工作集（n/5）」滚到 `id="factor-workset"`。详情：`MineReport` 在 ⑥ 之后加 ⑦ |
| `web/src/api/client.ts` | 新增类型 + `factorWorksetGet` / `factorWorksetPreview` / `factorWorksetAdmit` / `factorWorksetRemove` |
| `CorrHeat` | 14 列热力 **保留不动**。工作集小热力可复用其着色思路，数据源改为成员两两 `rho`（键是成员名，不是 `FACTOR_COLUMNS`） |
| `App.tsx` | **不改** hash 表 |

### 2.4 与已有集合的边界（勿混）

| | 工作集（本切片） | `factor_registry.status=enabled` | 14 列 / 代表集 |
|---|---|---|---|
| 目的 | 少而正交短名单，给人看、给人换 | 进每日计算 `factor_mined_daily` | 库存风格诊断 / 残差基底 |
| 容量 | 硬顶 5 | 无顶 | 14 列写死；`ORTHO_EXPRS` 五式写死 |
| 相关 | 成员两两 raw，`|ρ|<0.50` 才直接入 | 无 | `max_abs_corr` 对 14 列 raw；优秀门 0.8 |
| 质量 | 入集时走 `factor_lab_gate`（resid 主路） | 登记 / 切 enabled 同一套门 | 不负责本切片入集 |

---

## 3. 存储与迁移

### 3.1 关键取舍：薄表，不加第四态

与产品 §4.4 一致：不要给 `factor_registry.status` 增加第四态，以免和 `enabled→factor_mined_daily` 绑死。

```text
factor_workset (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  registry_id   INTEGER NOT NULL,          -- FK → factor_registry.id
  added_at      DATETIME NOT NULL,         -- UTC，与现有模型 datetime.utcnow 一致
  note          TEXT NOT NULL DEFAULT '',  -- 可选；本切片 UI 可不暴露
  source_mine_id INTEGER,                  -- 入集时用过的报告 id，仅追溯；展示仍现场读报告
  UNIQUE (registry_id)                     -- uq_fw_registry
)
```

ORM：`class FactorWorkset(Base)`，`__tablename__ = "factor_workset"`，`UniqueConstraint("registry_id", name="uq_fw_registry")`。`registry_id` 在 DDL 写 `REFERENCES factor_registry(id)`（文档 + 未来开 FK 时生效）。

**注意**：当前 `database.py` 连接 pragma **未** 开 `PRAGMA foreign_keys=ON`，本仓模型也普遍不显式用 `ForeignKey()`。因此：

- UNIQUE + 应用层校验是容量与孤儿行的真实屏障；
- GET / 预览 **JOIN** `factor_registry`，孤儿行不展示，admit/remove 前 `db.get(FactorRegistry, id)`；
- 本切片 **不** 借机全局打开 `foreign_keys`（以免掀开其它历史表）。
- **若注册表行被删**（今日无删除 API；若二期要做）：必须先移出工作集或在删除服务里级联删 `factor_workset` 行。推荐语义：`ON DELETE RESTRICT`（有成员则拒删）——与「短名单挂注册表主键」一致。本切片不必做删除 API。

**不**存第四态、不写 `factor_mt_state`、不改历史 mine 的 `rating`。成员若后来被切到 `disabled`：**不自动驱逐**（移出是用户动作）；只是不能再作为「新候选」入集（§5.2）。

展示用的主路 `rating` / `t_stat` / 诊断用 14 列 `max_abs_corr`：每次 GET/预览经 `load_mine_report(db, source_id=row.source_id, expr=row.expr)` 现场读取（开正交 = resid 主路）。薄表不冗余快照，避免和实验室最新报告不一致。

### 3.2 迁移策略（加法、可逆）

对齐 `factor_mt.migrate_factor_mt_state`：

1. `models.py` 注册 `FactorWorkset`（新库走 `create_all`）。
2. `migrate_factor_workset(conn)`：`CREATE TABLE IF NOT EXISTS ...`（含 UNIQUE）。
3. `database._migrate_sqlite` 在现有 `migrate_factor_mt_state(conn)` 之后调用。
4. 回滚：`DROP TABLE IF EXISTS factor_workset`。注册表、mine 结果、mt 计数、14 列日频均不受影响。无数据回填（工作集从空集开始）。

---

## 4. 相关计算口径（raw，对工作集成员）

### 4.1 算法（复用，不新造）

与 `_corr_with_existing` **同一套**：

| 项 | 口径 |
|---|---|
| 因子值 | **raw 表达式**（`eval_factor`），**不是**残差，不调用 `orthogonalize_cross_section` |
| 相关 | `factor_mining._spearman`（Spearman，并列平均秩） |
| 有效配对 | 两边均有有限值的股票数 `n_pairs >= 20` 才给出 ρ；否则「相关无法判定，暂不入集」，**不得放行**（预览 `undetermined=true`，decision=`reject`，`error_code=corr_undetermined`） |
| 取整 | ρ 保留 4 位小数（与 `_corr_with_existing` 一致）；门禁用取整后的 `|ρ|` |
| 硬门（已冻） | `max_abs_corr_ws = max_i |ρ_raw(candidate, member_i)|`；`< 0.50` 相关门通过；`≥ 0.50` 不能直接入，进入替换建议 |
| 空集 / 单人 | 没有对端时相关门自然通过（没有 i）。第 1 个成员只过质量门即可入集，不因缺少 `factor_daily` as-of 被拒 |
| 14 列 `max_abs_corr` | 工作集卡可只读展示报告里的值，**不作**入集硬门，本切片不重算、不改优秀门 0.8 |

只要存在至少一对需要判定的成员（直接入集时候选 vs 现有成员，或替换后「候选 vs 每一位留任」），缺 `factor_daily` 最新日、或 PIT 成分不可得、或该对 `n_pairs<20`：一律按无法判定拒绝，不静默当 ρ=0。

`_spearman` 在 `n<5` 时返回 `0.0`；工作集路径必须先用 `n_pairs>=20` 截住，避免把「样本不足」当成「零相关通过」。

### 4.2 最新截面定义（推荐写死）

产品要求「与 `_corr_with_existing` 同一套——最新截面」，该函数取日为：

```text
latest = db.execute(
    select(FactorDaily.trade_date).order_by(FactorDaily.trade_date.desc()).limit(1)
).scalar()
```

工作集采用同一 **as-of 日**。

**股票池（实现补全，推荐）**：工作集没有单次 mine 的 `start/end` 上下文，不得借用某一个成员报告的回测区间当宇宙（会在成员之间漂移）。写死为：

1. `asof = max(FactorDaily.trade_date)`；无则无法判定。
2. `syms, pit_timeline = _core_universe_pit(db, asof, asof)`（`factor_mining._core_universe_pit`，核心指数 `etl.CORE_INDEXES`）。
3. 截面活跃集 = `_pit_symbols(pit_timeline, asof)` ∩ `_eligible_symbol(lifecycle, sym, asof)`。
4. 求值循环与 `_corr_with_existing` 相同：无对齐价、`len(seg)<60`、`eval_factor` 异常 / 非有限 → 跳过该票。

这与「最新截面 + PIT + 生命周期」挖矿相关路径同构，只是把对端从 14 列换成任意成员表达式。

### 4.3 阈值是否可配

**不可配（本切片）**。`WORKSET_CORR_ABS_MAX = 0.50` 写在服务模块顶部。不进请求体、不进 UI 滑杆。单测若要覆盖边界，用取整后的值比较：`0.4999 → 通过`，`0.50` 与 `0.5000 → 不得直接入`。不必提供 monkeypatch 公共 API。刻意不对齐 0.8（产品已说明：0.8 对「少而正交」过松）。

---

## 5. 质量门与状态机

### 5.1 质量门（复用，不新造）

预览与写入都走现有门禁，入参与登记相同：

```text
report = load_mine_report(db, source_id=registry.source_id, expr=registry.expr)
reasons = check_enable_gate(report, expr=registry.expr)
# 非空 → 拒绝；话术与 POST /factor/registry/register 同一套（分号拼接 / 列表原样下发）
```

`check_enable_gate` 已包含（不得放宽）：评级 ∈ {优秀, 可用}；OOS `verdict=="通过"` 且未关；OOS n≥20；OOS t≥2.0；OOS IC>0；IS 内 walk-forward ≥3 折且通过；股票池 PIT / 估值 as-of；ES/pead 既有条。「反向」及其它评级不可入。不因「想凑满 5 个」放宽。

开正交时这些评级已按 **resid** 计算——入集 **不再** 读 raw t 当质量。比较量同样是报告主路 `t_stat`（开正交 = resid t）。

**先登记再入集**：preview / admit 只接受已存在的 `registry_id`。未登记时，前端先调现有 `POST /factor/registry/register`（失败则到此为止），再预览。服务端若收到不存在的 id → HTTP 404 `不存在`（与 `registry_toggle` 口径一致）。**禁止**在 admit 内自动登记或 `toggle→enabled`。

**推荐（状态过滤）**：`status in {"candidate", "enabled"}` 可申请；`disabled` 拒绝，`error_code=disabled`，原因：「已拒绝（disabled），请先在生命周期改回 candidate 再申请」。不改注册表状态。

### 5.2 预览状态机（无副作用）

输入：当前工作集成员（0–5）+ 候选 `registry_id`。顺序锁死：

```text
1. 注册表行不存在 → 404（路由层）
2. 已在工作集 → decision=in_set（可移出；不再跑相关写入）
3. status=disabled → decision=reject, error_code=disabled
4. 质量门失败 → decision=reject, error_code=gate_failed, reasons=门禁列表
5. 评估候选 vs 现成员的 raw 两两（及「若替换 X 后的新集合」在需要时计算）
   5a. 若当前有至少一名成员（需要两两判定），且无 asof / 任一对 n_pairs<20
       → reject, corr_undetermined。空集无对端，跳过本步
   5b. n < 5 且（无成员或 max_abs_corr_ws < 0.50）→ decision=admit
   5c. n = 5 且相关门通过：
         若候选主路 t **严格大于** 集内最低主路 t 的成员 Y
           → decision=replace，建议替换 Y（「工作集已满，建议替换最弱成员 Y」）
           且必须先按「去掉 Y、加入候选」重算全部两两；若新集合仍有 |ρ|≥0.50 或无法判定
           → 改为 reject, error_code=replace_still_conflicts，指出仍冲突的成员
         否则 → reject, error_code=cap_full_weak，「工作集已满，候选增量不足」
   5d. 相关门未过（存在 |ρ|≥0.50），无论 n 是否已满：
         令 X = 与候选 |ρ| 最大的成员（破平见 §0）
         若候选主路 t **严格大于** X 的主路 t
           → 先按「去掉 X、加入候选」重算新集合全部两两 raw |ρ|
              全部 |ρ|<0.50 且均可判定 → decision=replace，建议「与 X raw |ρ|=…，保留更强者，替换 X」
              否则 → reject, replace_still_conflicts（指出仍冲突者；一次只换一人，不得自动再换第二人）
         否则 → reject, error_code=corr_too_high（说明 X 更强或同等）
```

同 t **不** 替换（已冻）。一次只建议替换 **一人**。预览 **零写**：不 insert/delete `factor_workset`，不改 registry，不 bump `n_tested`，不改 mine 报告。

**新集合两两**：被换掉的那一对不再存在；原留任成员之间关系应重算（同一截面，数值应不变）；**新成员与每一位留任成员** 都必须 `|ρ|<0.50` 且可判定。若替换后只剩候选一人（原 n=1 的高相关替换），无对端，相关门通过。不得只清掉最大相关的那一对就给出「可替换」。

### 5.3 写入（admit）与移出

- UI 点「入集」或「替换 X」后才调 `POST /admit`。`confirm=true` 为写入必要条件；`confirm=false` → `ok=false, error_code=confirm_required`，不写库。
- **替换** 必须带 `replace_registry_id`，且必须等于**服务端此刻**重算的建议对象；否则 `replace_mismatch`，不写库。禁止客户端自选其它成员静默替换（本切片不做「自选替换对象」）。
- admit **在同一事务内重新跑完整状态机**，不信任旧 preview。
- 直接入集成功：insert 一行，`n` 增加 1，且 `n≤5`。
- 替换成功：同一事务先删 `replace_registry_id` 再插候选（只换一人），然后 `n` 不变；提交前再读计数，`n≤5`。
- 已在集内：`ok=true, decision=in_set, already=true`，幂等无写。
- 移出：无门禁；`POST /factor/workset/{registry_id}/remove`。已不在集内推荐 `ok=true, already_absent=true`（幂等）。

### 5.4 并发、幂等、硬顶

SQLite 默认 `BEGIN DEFERRED` 下，两个 admit 都看见 `n=4` 可能插出 6 行。写入路径必须：

1. `BEGIN IMMEDIATE`（`db.execute(text("BEGIN IMMEDIATE"))` 或等价；注意与已有 Session 事务衔接，Desk 用「连接上立即升级写锁」的一种写法锁死即可）；
2. `SELECT` 当前成员 → 重跑状态机 → `INSERT`/`DELETE`；
3. 提交前 `COUNT(*)` 断言 `≤ 5`，否则 rollback，`error_code=cap_reached`（HTTP 409）；
4. `UNIQUE(registry_id)` 防止同一人被插两行（冲突视为 `in_set` 幂等，不 500）。

单用户本机平台仍按此写，避免调度器 / 双标签页踩顶。`list` / `preview` 只读，不必 IMMEDIATE。

---

## 6. API 契约

前缀：`/api/v1/factor`。无鉴权。错误风格对齐现有因子路由：缺失资源 HTTP 404（`detail` 字符串）；非法参数 HTTP 400；容量并发冲突 HTTP 409。**业务拒绝**（质量不达标、相关过高、已满且增量不足等）走 **HTTP 200** + `ok=false` + `decision=reject` + `error_code` + `reasons`，便于前端把三种结果画在 ⑦，而不是进通用 catch。

### 6.1 `GET /factor/workset`

无查询参数。响应：

```json
{
  "ok": true,
  "n": 2,
  "cap": 5,
  "target_min": 3,
  "target_hint": "尚未达到目标 3–5",
  "asof": "2026-11-10",
  "corr_threshold": 0.5,
  "corr_undetermined": false,
  "members": [
    {
      "id": 1,
      "registry_id": 12,
      "name": "动量增强A",
      "expr": "roc(c_m, 20) - roc(c_r, 5)",
      "expr_excerpt": "roc(c_m, 20) - roc(c_r, 5)",
      "status": "candidate",
      "rating": "优秀",
      "t_stat": 3.21,
      "added_at": "2026-11-11T04:01:25",
      "max_abs_corr": 0.42,
      "source_id": 88
    }
  ],
  "corr_matrix": [
    {"a": 12, "b": 34, "rho": 0.21, "n_pairs": 800}
  ],
  "note": "少而正交工作集：最多保留 3–5 个低相关因子。质量看残差评级（正交开启时），相关看 raw；不是风险平价，也不改变多重检验计数。"
}
```

- `target_hint`：`n < 3` 时为「尚未达到目标 3–5」，否则 `null`。
- `max_abs_corr` 为该成员最近报告的 14 列诊断值，可缺省 `null`。
- `corr_matrix` 只含 `a < b` 的上三角；存在配对但无法判定时 `rho=null` 且 `corr_undetermined=true`。`n≤1` 时矩阵为空，`corr_undetermined=false`。
- 空集：`n=0`，`members=[]`，`corr_matrix=[]`，`asof` 仍给（若有），`note` 保留。

### 6.2 `POST /factor/workset/preview`

请求：`{ "registry_id": 12 }`。无副作用。响应核心字段：

```json
{
  "ok": true,
  "decision": "admit",
  "error_code": null,
  "reasons": [],
  "quality": { "passed": true, "reasons": [] },
  "in_set": false,
  "n": 2,
  "cap": 5,
  "t_stat": 3.4,
  "rating": "优秀",
  "corr": {
    "asof": "2026-11-10",
    "max_abs_corr_ws": 0.31,
    "undetermined": false,
    "pairs": [{"member_registry_id": 8, "name": "X", "rho": 0.31, "n_pairs": 800}]
  },
  "suggestion": null
}
```

`decision ∈ {"admit","reject","replace","in_set"}`。`decision=replace` 时 `suggestion`：

```json
{
  "replace_registry_id": 8,
  "replace_name": "X",
  "reason": "与 X raw |ρ|=0.62，保留更强者，替换 X",
  "rho": 0.62,
  "t_candidate": 3.4,
  "t_member": 2.1,
  "new_set_corr_ok": true,
  "new_set_pairs": [{"member_registry_id": 9, "name": "Y", "rho": 0.22, "n_pairs": 790}],
  "conflict_members": []
}
```

`replace_still_conflicts` 时 `decision=reject`，`suggestion` 仍可填写曾考虑的 X 与 `conflict_members`（`[{registry_id, name, rho}]`），`new_set_corr_ok=false`。

`error_code` 枚举：`null` | `not_registered`（通常走 404）| `disabled` | `gate_failed` | `corr_undetermined` | `corr_too_high` | `cap_full_weak` | `replace_still_conflicts` | `confirm_required` | `replace_mismatch` | `cap_reached`。

### 6.3 `POST /factor/workset/admit`

请求：

```json
{ "registry_id": 12, "confirm": true, "replace_registry_id": null, "note": "" }
```

- 直接入集：`confirm=true`，`replace_registry_id` 必须为 `null` / 省略。
- 替换：`confirm=true` 且 `replace_registry_id` 等于此刻建议。
- 成功：`ok=true`，`decision` 为 `admit` 或 `replace` 或 `in_set`，并附当前 `list_workset` 快照（`workset` 字段，形状同 GET）。
- 失败：同预览的 200+`ok=false`，或 409 `cap_reached`，或 404。

### 6.4 `POST /factor/workset/{registry_id}/remove`

路径参数是 **`factor_registry.id`**（不是薄表行 id）。体可空 `{}`。成功：`{ "ok": true, "removed_registry_id": 12, "n": 1, "workset": { ... } }`。

### 6.5 路由层约定

四个 handler 放在 `registry_*` 邻近，保持 `Depends(get_db)`。禁止在 handler 里调用 `mine_factor` / `bump_n_tested`。`preview` / `admit` 的 Pydantic 模型建议 `WorksetPreviewPayload` / `WorksetAdmitPayload`。

---

## 7. 前端 `#factorlab` 挂载

只改 `FactorMinePage.tsx` 与 `client.ts`。不新开 hash、不改 `App.tsx`、不把工作集挂到 `#cards` / `#paper`、不把 ⑤ 生命周期与工作集做成一个状态机。

### 7.1 `client.ts`

```ts
factorWorksetGet: () => get<FactorWorksetPayload>("/factor/workset"),
factorWorksetPreview: (registry_id: number) =>
  post<FactorWorksetPreview>("/factor/workset/preview", { registry_id }),
factorWorksetAdmit: (payload: {
  registry_id: number; confirm: boolean;
  replace_registry_id?: number | null; note?: string;
}) => post<FactorWorksetAdmitResult>("/factor/workset/admit", payload),
factorWorksetRemove: (registry_id: number) =>
  post<FactorWorksetAdmitResult>(`/factor/workset/${registry_id}/remove`, {}),
```

类型字段与 §6 对齐；本切片把类型写在 `client.ts`（与 `RegistryRow` / `FactorMineReport` 为邻）。

### 7.2 列表屏（`view=list`）

- **位置**：`PageHeader` 与 `SentimentStrategyCard`（舆情策略卡，约在详情/列表共用的页眉之后）之后；**进入** `view===list` 后、现有「新闻情绪择时」（长新闻）以及造因子 / 检验参数 / GP 区 **之前**。
- 卡片 `id="factor-workset"`，标题 **「少而正交工作集（n/5）」**（n 取 GET）。
- 卡内：成员表（名、表达式摘要、主路评级、主路 t、入集时间）+ 成员间 raw 相关小热力 + 每行移出按钮。14 列 `max_abs_corr` 可作一列诊断，文案标明「非入集门」。
- `n<3` 且非空：灰字「尚未达到目标 3–5」。
- **空集文案（写死）**：「工作集为空。从一份优秀/可用且 OOS 通过的报告申请加入，目标 3–5 个低相关因子。」
- 卡底固定口径（产品一句话，写死）：

> 少而正交工作集：最多保留 3–5 个低相关因子。质量看残差评级（正交开启时），相关看 raw；不是风险平价，也不改变多重检验计数。

- `PageHeader` actions 在「挖掘历史（k）」旁增加「工作集（n/5）」：若在详情则 `setView("list")`，下一帧 `document.getElementById("factor-workset")?.scrollIntoView`（与现有「挖掘历史」滚到 `mine-history` 对称）。不因此把工作集卡放到详情顶上。

### 7.3 详情屏「⑦ 申请加入工作集」

挂在 `MineReport` 现有 ⑥「入册为研究卡」**之后**（①–⑥ 顺序不动）。行为：

1. 按钮始终可点（质量未过不禁用，避免另开绕过通道的错觉）。
2. 未登记：先走现有 `registerFactor` 同款 `api.factorRegistryRegister`；失败则展示与 ⑤ 相同的门禁话术，**到此为止**，不调 preview。
3. 已登记：`factorWorksetPreview(regId)`，把 `decision` 画成三种明确结果之一：可入集 / 拒绝原因 / 建议替换某某（原因 + raw |ρ| + 两边主路 t）。
4. 用户再点确认 → `factorWorksetAdmit({ registry_id, confirm: true, replace_registry_id? })`。无确认不写。
5. 已在集内：展示「已在工作集」，提供移出（`factorWorksetRemove`）。
6. 入集成功后 **不** 调用 `factorRegistryToggle`，页面明确「入集不会自动启用」。

### 7.4 不要做

单独「精简集」二级路由；把工作集与「⑤ 生命周期 / ⑥ 研究卡」混成一个状态机；改 GP 区默认；改 ④ 十四列热力数据源或优秀门文案（「>0.5 中度重复，可开启上方残差化后再评」保留）。

---

## 8. 门禁 / 验收清单

### 8.1 产品验收（对齐产品 §5）

- [ ] `#factorlab` 列表可见工作集卡片，标题含 **n/5**
- [ ] 从详情「⑦ 申请加入工作集」能得到可入 / 拒绝原因 / 替换建议 三种明确结果
- [ ] 质量门与登记门禁一致（未过 OOS / 非优秀或可用者不能入）
- [ ] 相关门对 **raw**、对象为工作集成员；默认 `|ρ|<0.50` 才直接可入
- [ ] 容量永不大于 5；替换须确认、无静默写入；一次只换一人
- [ ] 替换后新集合全部两两 raw `|ρ|<0.50`，否则不得写入
- [ ] 工作集成员间 raw 相关热力可见
- [ ] 入集不自动启用、不开实盘；页上有「非组合优化 / 不改多重检验计数」口径
- [ ] 空集文案与 §7.2 一致；`n<3` 提示「尚未达到目标 3–5」
- [ ] 不新开 hash；`#cards` / `#paper` 无工作集入口

### 8.2 工程验收（对齐产品 §5/§7，并补契约级）

单测主文件：`backend/tests/test_factor_workset.py`（门禁用例可复用 `test_factor_lab` 的报告夹具思路）。相关计算用假截面向量测 Spearman / `n_pairs<20`，不必每次拉全量 K 线；状态机对 `pairwise` / `load_mine_report` 可打桩。另保留至少一条不打桩的配对函数测试。

- [ ] **可入**：空集或 `n<5` 且质量过、（无对端或 `max_abs_corr_ws<0.50`）→ preview `admit`；`confirm=true` 后写入，`n` +1。空集第一个成员不因缺少 as-of / 相关矩阵被拒
- [ ] **相关拒绝**：`|ρ|≥0.50` 且候选 t 不严格大于 X → `corr_too_high`，不写
- [ ] **满员替换最弱**：`n=5`、相关门通过、候选 t 严格大于最低 t 的 Y，且新集合两两过门 → preview `replace` Y；确认后只换 Y
- [ ] **满员增量不足**：`n=5`、相关过、t 不严格大于最低 → `cap_full_weak`，不写
- [ ] **高相关替换更强**：`|ρ|≥0.50`、候选 t 严格大于 X、新集合两两过门 → 建议替换 X；确认后写入
- [ ] **替换后仍与第三人冲突则拒**：即使候选强于 X，新集合存在其它 `|ρ|≥0.50` 或无法判定 → `replace_still_conflicts`，不写
- [ ] **n<20 拒判**：`corr_undetermined`，「相关无法判定，暂不入集」，不写
- [ ] **未登记**：preview/admit 无此 `registry_id` → 404；前端先登记失败则不入集（手册 + 前端逻辑）
- [ ] **`disabled` 拒入**（本契约推荐）：不写、不改 status
- [ ] **同 t 不替换**；破平稳定（更早 `added_at`）
- [ ] **`confirm=false` 或替换 id 不匹配** → 不写
- [ ] **幂等**：已在集内再 admit → `in_set` 且不复制行；重复 remove 不 500
- [ ] **硬顶**：服务层无法插入第 6 人（含事务内 COUNT 断言 / UNIQUE）；并发路径至少用 IMMEDIATE + 断言覆盖「连续两次直接入集在 n=4 时第二笔失败或升级为替换而不是 n=6」
- [ ] **预览无副作用**：preview 前后 `factor_workset` 行数、registry status、`get_n_tested()` 均不变
- [ ] **不 bump 计数**：preview / admit / replace / remove 前后 `get_n_tested` 不变；测试 spy 断言未调用 `bump_n_tested`；`n_tested` / `n_tested_cum` 不被工作集路径改写
- [ ] **口径回归**：`orthogonalize` 默认 True、resid 进评级、14 列 `max_abs_corr`、luck_bar / Bonferroni 展示块与改前一致（跑已有 `test_factor_ortho.py` / `test_factor_mt.py` / `test_factor_lab.py`，不得弱化）
- [ ] **源代码不动点**（字符串/符号断言即可）：不改 `ORTHO_EXPRS`；不改 `factor_ortho.orthogonalize_cross_section` 算法；不改 luck_bar 常数写法（`+0.3` / `-0.3` / 地板 `2.0` / `1.5`）；`_corr_with_existing` 仍对 `FACTOR_COLUMNS` 且优秀门比较仍是 `< 0.8`
- [ ] 前端手册：列表 n/5、热力、⑦ 三种结果、确认后才写、不自动启用
- [ ] 无密钥进源码；不推远程；不开实盘

### 8.3 REBUILD M3-5 对齐句

> 目标 3-5 个低相关因子，而非 20 个。

本切片交付即视为 M3-5 工程落地（选择/精简规则 + 可见短名单）。组合层 IR / 风险平价属 M3-8，**不作**本切片出门门槛。

---

## 9. 落地序与风险

### 9.1 Desk 落地序（建议 0.75–1 Desk 日）

| 步 | 内容 | 估计 |
|---|---|---|
| 1 | `FactorWorkset` + `migrate_factor_workset` + `_migrate_sqlite` 挂钩；空表冒烟 | 0.1d |
| 2 | 抽出 `load_latest_cs_context` / `eval_raw_values_on_cs` / `pairwise_raw_spearman`；保持 `_corr_with_existing` 行为；配对函数单测 | 0.2d |
| 3 | `factor_workset.py` 状态机（预览/入集/替换/移出/IMMEDIATE/硬顶）；`test_factor_workset.py` 覆盖 §8.2 核心分支 | 0.3d |
| 4 | `routers/factor.py` 四端点 + Payload | 0.1d |
| 5 | `client.ts` 类型/方法；`FactorMinePage` 列表卡 + 页眉滚动 + ⑦ | 0.2d |
| 6 | 跑 ortho / mt / lab 回归 + 手册点检 §8.1；确认无 bump、无自动 enabled | 0.1d |

一轮只做本切片。不要顺手改 GP、不要开 M3-8，不要把「当日估算截面」等其它主题塞进同一 PR。

### 9.2 风险

| 风险 | 缓解 |
|---|---|
| 列表 GET 每次重算 5 个表达式的最新截面，略慢 | 单截面 + 核心池，可接受；允许进程内按 `(asof, registry_id+expr 签名)` 缓存，admit/remove 失效。本切片不做落库缓存表 |
| 抽出相关循环时改坏 14 列矩阵 | 抽出必须行为保持；回归 `test_factor_ortho` / 既有挖矿相关断言；禁止改 `FACTOR_COLUMNS` 与 0.8 门 |
| 双标签页同时入集顶到 6 | `BEGIN IMMEDIATE` + 事务内重算 + COUNT 断言 + UNIQUE |
| 信任过期 preview，满员后又直接插 | admit 必须重跑状态机；`confirm` / `replace_registry_id` 校验 |
| 误 bump `n_tested` | 服务禁止 import/调用 `bump_n_tested`；测例 spy |
| 误在入集时 enabled | admit 不改 `FactorRegistry.status`；前端不调 toggle；测例断言 |
| 把 14 列 0.8 当入集门，误杀与风格相关的可用增量 | 契约与测例明确 `max_abs_corr` 只展示 |
| SQLite 未开 FK，删注册表留孤儿 | 今日无删除 API；GET JOIN 忽略孤儿；二期删除必须先/同时清工作集 |
| 截面数据缺口被当成 ρ=0 | `n_pairs<20` 或无 asof → 无法判定拒绝，不调用「ρ=0 通过」 |

### 9.3 关键取舍摘要

1. **新服务 `factor_workset.py` + 薄表 `factor_workset`**，不加 registry 第四态。
2. **质量只调用 `factor_lab_gate`**；须已登记；不自动 `enabled`。
3. **相关 = 最新 `FactorDaily` 截面上成员两两 raw Spearman**，`|ρ|<0.50` 才直接入；14 列 0.8 不作入集门。
4. **满员或高相关只给一人替换建议，须确认；写入时重算新集合全部两两，冲突则拒。**
5. **预览 / 入集 / 移出不 bump `n_tested`；不改 W47-2/3。**
6. **前端只挂 `FactorMinePage` + `client.ts`，不新开 hash。**
7. **硬顶靠事务写锁 + COUNT 断言 + UNIQUE，使第 6 人不可能提交。**

---

## 10. 附录：路径与现状速查（main `9d3245a`）

| 主题 | 路径 / 符号 |
|---|---|
| 本契约（box） | `/workspace/docs-design-W48-1-sparse-orthogonal-arch.md` |
| 产品方案（已冻） | `/workspace/docs-design-W48-1-sparse-orthogonal.md` |
| 读码基线 | `ljew/quant-platform` `9d3245a16418ee10ca5753aee81af93f6ca5857b` |
| 路由 hash | `web/src/App.tsx` → `key: "factorlab"` → `FactorMinePage` |
| 列表 / 详情 / ①–⑥ | `web/src/pages/FactorMinePage.tsx`（`view`、`PageHeader`、`SentimentStrategyCard`、新闻情绪卡、造因子、`MineReport`、`CorrHeat`、`registerFactor`） |
| API 客户端 | `web/src/api/client.ts` → `factorRegistryList/Register/Toggle`；本切片新增 `factorWorkset*` |
| 因子路由 | `backend/app/routers/factor.py` → `APIRouter(prefix="/factor")`；`registry_list` / `registry_register` / `registry_toggle`；`MinePayload.orthogonalize` 默认 True |
| 挖矿 / 相关 | `backend/app/services/factor_mining.py` → `mine_factor` / `_corr_with_existing` / `_spearman` / `_core_universe_pit` / `_pit_symbols` / `_eligible_symbol` |
| 14 列 | `backend/app/models.py` → `FactorDaily` / `FACTOR_COLUMNS` |
| 注册表 | `backend/app/models.py` → `FactorRegistry`（表 `factor_registry`，`status` ∈ candidate/enabled/disabled，`UniqueConstraint("name")`） |
| 日频启用值 | `FactorMinedDaily`（本切片不作为相关数据源） |
| 门禁 | `backend/app/services/factor_lab_gate.py` → `check_enable_gate` / `assert_enable_allowed` / `load_mine_report` / `ALLOWED_RATINGS` |
| 多重检验 | `backend/app/services/factor_mt.py` → `get_n_tested` / `bump_n_tested` / `FactorMtState` |
| 正交 | `backend/app/services/factor_ortho.py` → `ORTHO_EXPRS` / `orthogonalize_cross_section`（禁止改） |
| 求值 | `backend/app/core/engine/factor_expr.py` → `eval_factor` |
| 迁移挂钩 | `backend/app/database.py` → `_migrate_sqlite` / `init_db` / `get_db`（无鉴权，无 `foreign_keys=ON`） |
| API 前缀 | `backend/app/config.py` → `api_prefix="/api/v1"` |
| 既有测例 | `backend/tests/test_factor_lab.py` / `test_factor_mt.py` / `test_factor_ortho.py` |
| 本切片测例 | `backend/tests/test_factor_workset.py`（新增） |

### 产品对齐核对（Architect 自检）

| 产品冻结项 | 本契约 |
|---|---|
| 质量 = `factor_lab_gate`，须 candidate（不必 enabled），不自动 enabled | §5.1；disabled 仅作实现过滤，不改门禁阈值 |
| 相关 = 成员两两最新截面 raw Spearman，默认 `|ρ|<0.50` 才直接入 | §4；阈值写死不可配 |
| 14 列 `max_abs_corr` / 0.8 不作入集硬门，可展示 | §4.1、§7.2 |
| 硬顶 5；满员或高相关只给替换建议，须确认；一次只换一人 | §5.2–5.4 |
| 替换后重算新集合全部两两，冲突不得放行 | §5.2 5c/5d |
| 入集/预览/移出不 bump `n_tested` | §2.2、§8.2 |
| 不改 W47-2/3；不重做正交引擎 | §1.3、§8.2 不动点 |
| 不新开 hash；列表卡 + ⑦；空集文案 | §7 |
| 薄表 FK → `factor_registry.id`；服务 / 路由名 | §3、§6（`{id}` 明确为 `registry_id`） |
| 非目标 M3-8 / GP / 实盘 / SVD / 改常数 | §1.3 |

无产品改口。无待 Product 重开的问题。**Product 已冻结。**

---

*文档结束。**Product 已于 2026-11-11 Asia/Shanghai 冻结本技术契约**（确认无改口；实现补全已锁定）。请 Planner 派 Desk，按 §9.1 落地。硬闸门：不开实盘，密钥不进源码，不擅自推远程，本文件只设计不改业务代码。*
