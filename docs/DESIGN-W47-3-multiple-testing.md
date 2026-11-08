# 技术契约：W47-3 / M3-6 多重检验累计（报告层）

> 作者：Quant Architect · 2026-11-08（Asia/Shanghai）  
> 看板/产品：`/workspace/docs-next-batch-product-priority.md` §2 **W47-3 / M3-6** · Ready 序 **W47-1 → W47-2 → W47-3**  
> 设计锚点：REBUILD-2026-10.md **M3-6**「多重检验正式校正」；`factor_mining.mine_factor` 既有 `luck_bar`/`n_tested`；W47-2 文末指针  
> 对照体例：`docs-design-W47-1-paper-ux.md`、`docs-design-W47-2-orthogonalize.md`  
> **硬闸门**：不开实盘；密钥不进源码；不擅自推远程；**本文只写设计，不改业务代码。**  
> **冻结状态（2026-11-08）**：**Product 已冻结**。§8.3 四项均按推荐：①`factor_mt_state` 全局累计（非每次 COUNT）；②主校正=累计 N→luck_bar 抬评级门槛，Bonferroni α 只展示不二次抬门；③一次 mine=1（ortho 开用 resid t）；④不做 PBO/CSCV。无微调。Desk 待 W47-2 合入后拉。  
> 前置：W47-2 **已 Product 冻结**（合入后抬门槛；本契约可先备，Desk 待 W47-2 合入后拉）。  
> 读码基线：本机 `~/quant-platform` tip `fce4199`（W47-1 已合）；`factor_mine_results` 实测 72 条；box `/workspace/quant-latest` 可能滞后，**以本机仓为准**。

---

## 1. 背景 / 目标 / 非目标

### 1.1 背景

- REBUILD **M3-6**：累计跨 run `n_tested` → Bonferroni / deflated Sharpe / PBO **三选一**（报告层落地，非科研平台）。
- 产品验收（Ready）：跨 run 累计 `n_tested`（或等价）写入报告；展示 luck_bar / 校正后门槛；至少一种可见校正口径（Bonferroni 式门槛抬升 **或** deflated-Sharpe 风格数字）；**不做**完整 PBO/CSCV。
- 读码现状（`~/quant-platform` tip `fce4199`）：

| 点 | 现状 | 缺口 |
|---|---|---|
| 公式 | `luck_bar = √(2 ln max(n_tested,2))`；`t_优秀 = max(2.0, luck_bar+0.3)`；`t_可用 = max(1.5, luck_bar−0.3)`（`factor_mining.py`） | 公式可用；**N 错** |
| 入参 | `mine_factor(..., n_tested: int = 1)` | `MinePayload` **无** `n_tested`；手动 `/factor/mine` 恒为 1 |
| GP | 精评传 `n_tested=len(seen)`（单次 GP run 内候选数） | **未**跨 run 累计；REBUILD L122 已点名 |
| 落库 | `result_json` **已含** `n_tested` / `luck_bar` / `t_thresholds` | 实测样本 `n_tested=1` → `luck_bar≈1.177` → 门槛落在地板 2.0/1.5 |
| 前端 | `FactorMineReport` TS **未声明**上述三字段；`MineReport` KPI **不展示** | 校正对用户不可见 |
| 持久化累计 | **无** counter 表 / 文件 / 元数据键 | 本切片核心债 |
| 历史规模 | `factor_mine_results` ≈ **72** 行 | 若按 72 做累计种子，`luck_bar≈√(2ln72)≈2.85` → 优秀门 ≈3.15、可用门 ≈2.55 |

思想注释已写明来源（《量化交易》2.8.5 + Bailey–López de Prado deflated Sharpe 族）：**N 个独立零假设下期望最大 |t| ≈ √(2 ln N)**。债不在公式，在「N 只看本次、且 UI 不露」。

### 1.2 目标

1. **跨 run 持久化累计** `n_tested_cum`（契约定存储与计数语义，§2）。  
2. 每次真实评测用 **`n_tested = n_tested_cum`（评级前读数）** 喂既有 `luck_bar` 公式，**抬高**优秀/可用 t 门槛并写入报告。  
3. `#factorlab` 报告层 **强制可见**：累计 N、luck_bar、校正后 `t_thresholds`；并给一种 **Bonferroni 式展示数字**（见 §3，不二次抬门）。  
4. 与 W47-2 正交主路对齐：开 ortho 时评级用 resid t，累计按 **一次 mine 调用计 1**，不把 raw+resid 算两次。  
5. 类型 / API / 前端字段补齐，使缓存回放与新挖报告口径一致可读。

### 1.3 非目标（本切片刻意不做）

| 不做 | 归属 |
|---|---|
| 完整 PBO / CSCV 引擎 / 组合路径依赖图 | Product 明确非目标 |
| 引入 `pypbo` / 重型科研依赖作主路径 | 对标可参考；主路径自研轻量 |
| 形式化 Bailey DSR 全式（需策略 Sharpe 分布、非 IC t） | 二期可选；本仓 mine 主度量是 IC t |
| Holm / Benjamini–Hochberg FDR 全家桶 | 超出「至少一种」 |
| 改 IC 公式 / OOS 切分 / luck_bar **公式本身** | 只换 N 的来源与展示 |
| 改 W47-2 代表集 / resid 算法 | 只约定计数与 resid 主路关系 |
| 复活 GP 搜索 / 改 GP fitness | Backlog；仅约定 GP 精评如何读累计 N |
| 实盘 / 密钥 / 推远程 | 硬闸门 |

---

## 2. 存储与计数语义

### 2.1 关键取舍：累计存在哪

| 方案 | 利 | 弊 | 结论 |
|---|---|---|---|
| A. 每次 `COUNT(*)` `factor_mine_results` | 零新表 | force 原地更新不增行；cache hit 与「真测」难分；horizons 不落库漏计；GP 快评不落库漏计 | **不选作唯一真相** |
| B. **SQLite 专用计数表** `factor_mt_state`（单行/按 scope 行） | 原子 `UPDATE … RETURNING`；与落库解耦；可种子、可重置（管理接口二期） | 多一张表 | **选 B** |
| C. JSON/文件旁路 | 实现快 | 多进程/API 并发丢数；偏离本仓 SQLite 习惯 | **不选** |
| D. 复用某行 `result_json` 字段当全局计数 | 无迁移 | 语义脏；并发不安全 | **禁止** |

**表草案（Desk 落地）**

```text
factor_mt_state (
  scope       TEXT PRIMARY KEY,   -- 本切片仅用 'global'
  n_tested    INTEGER NOT NULL DEFAULT 0,
  updated_at  DATETIME
)
```

- 迁移：`database.py` SQLite migrate 建表；**首次**若不存在行，种子  
  `n_tested = COUNT(factor_mine_results)`（当前环境 ≈72），`scope='global'`。  
- **不**每次重扫全表；之后只走 increment。

### 2.2 计数范围（scope）

| 方案 | 说明 | 结论 |
|---|---|---|
| 全局 | 个人投研台：所有挖矿尝试共享一个 N | **本切片采用** |
| 按 universe | 核心池 vs 自定义池分开 | 二期；表已留 `scope` |
| 按 horizon / forward | 5/10/20/40 日分开 | **不采用**（多重检验关心的是「试了多少次假设」，不是窗口标签） |
| 按 ortho on/off | 两套门槛 | **不采用**（见 §2.4） |

### 2.3 何时 +1 / 何时不加

| 事件 | 是否计入 | 说明 |
|---|---|---|
| `POST /factor/mine` **新算**成功（含 `force` 重算） | **+1** | 一次真实假设检验 |
| 命中查重 **cache 回放**（`duplicate_of`） | **+0** | 未产生新检验 |
| `POST /factor/mine/horizons` 每个 forward 成功 | **各 +1** | 同一表达式多窗口 = 多次检验；报告层可在汇总注「本请求 +H」 |
| GP `evaluate_candidate` 快评 | **本切片默认 +0** | 快评非正式报告口径；避免把搜索内成千上万次灌进门槛导致不可用 |
| GP 精英 `mine_factor` 精评成功 | **各 +1** | 与手动 mine 同级；精评时传入的 `n_tested` 见 §2.5 |
| 校验 `/validate`、失败的 mine | **+0** | 未形成有效报告 |
| W47-2 `orthogonalize=true/false` | 仍按 **一次 mine +1** | 见 §2.4 |

Increment API（服务内，非对外）：`bump_n_tested(db, scope="global", delta=1) -> int` 返回 **bump 后** 的累计值；评级应使用 **bump 前** 读数或等价「读 → 评 → bump」序，避免自评时已含本次（见 §2.5）。

### 2.4 与 W47-2 resid 主路

- **计数**：`orthogonalize` 开或关，一次 `mine_factor` 成功 = **+1**。禁止 raw 路 + resid 路各计一次（同一假设的两种展示，不是两次独立发现）。  
- **评级输入**：W47-2 冻结——开 ortho 时主 `t_stat`/`ic_mean` 为 **resid**；累计抬高的 `t_thresholds` 作用在这条主 t 上。  
- **相关矩阵 / max_abs_corr**：仍对 raw（W47-2）；与多重检验计数无关。

### 2.5 读-评-写序（防自评膨胀）

推荐固定序（Desk 必须遵守）：

1. `n_before = get_n_tested(db)`  
2. `result = mine_factor(..., n_tested=max(n_before, 1))`（GP 精评：`n_tested=max(n_before + len(seen), len(seen))` 取更严者，见下）  
3. 仅当 `result.ok` 且本请求路径属于 §2.3「计入」时 `bump(+1)`  
4. 报告写入 `n_tested`（= 本次数，即步骤 2 所用值）、`n_tested_cum`（= bump 后或 bump 前+delta，契约要求二者语义在报告里写清：`n_tested` = 评级所用 N；`n_tested_cum` = 持久化累计含本次）

**GP 精评**：`n_tested = max(n_before + len(seen), len(seen))`——在跨 run 累计之上叠本次 GP 已见候选数，避免「库里才 80、本 GP 搜了 500 却只按 80 评精英」。快评仍不 bump；仅精英正式 `mine_factor` bump。

**手动 mine**：`n_tested = max(n_before, 1)`，成功后 +1。

---

## 3. 校正口径（主选 + 展示辅选）

### 3.1 关键取舍

| 口径 | 含义 | 与现码关系 | 作评级门？ | 结论 |
|---|---|---|---|---|
| **A. luck_bar（Bailey 极值 / DSR 族 t 版）** | `√(2 ln N)` → 抬 `t_excellent`/`t_usable` | **已实现**，只差累计 N + 展示 | **是（主）** | **主校正** |
| B. Bonferroni 门槛抬升另套临界 t | 名义 α/N 反查 t 临界 | 需新公式；与 luck_bar **双重抬门**易过杀 | 若作主会改评级语义 | **不作主** |
| C. 形式化 Deflated Sharpe（策略 SR） | Bailey DSR 原式 | mine 主度量是 IC t 不是策略 Sharpe；缺 SR 分布假设 | 难直接挂评级 | **二期可选数字** |
| D. PBO/CSCV | 组合过拟合概率 | Product 非目标 | 否 | **禁止本切片** |

**推荐（供冻结）**：

1. **主校正（联动评级）**：沿用既有 luck_bar 公式，**仅将 N 换成跨 run 累计**（§2）。这就是产品要求的「deflated-Sharpe **风格**数字」在本仓 IC t 语境下的落地（注释已宣称 Bailey–LdP 思想）。  
2. **辅展示（不联动评级）**：报告增加 Bonferroni 风格只读字段  
   - `bonferroni_alpha = 0.05 / max(n_tested, 1)`（保留科学计数或 4–6 位小数）  
   - 文案：「若按 Bonferroni 把家族错误率压在 5%，单次名义显著性约 α=…（仅提示，评级仍走 luck_bar 门槛）」  
   - **禁止**再用 α/N 另算一套 t 门去改 `rating`（避免双重惩罚）。

### 3.2 可见数字清单（最低集）

报告 / UI 至少可见：

| 字段 | 含义 |
|---|---|
| `n_tested` | 本次评级使用的 N |
| `n_tested_cum` | 持久化累计（含本次若已 bump） |
| `luck_bar` | `√(2 ln max(n_tested,2))` |
| `t_thresholds` | `{优秀, 可用}` |
| `bonferroni_alpha` | `0.05 / n_tested`（辅） |
| `mt_note` | 一句话口径（固定文案，§5） |

---

## 4. 报告 / API / 前端字段

### 4.1 `mine_factor` 返回（`result_json` 增补）

既有保留：`n_tested` / `luck_bar` / `t_thresholds` / `rating`。

**本切片新增**：

```json
{
  "n_tested_cum": 73,
  "bonferroni_alpha": 0.000685,
  "mt_note": "多重检验：评级门槛按累计检验次数抬升（luck_bar）；Bonferroni α 仅展示不二次抬门",
  "multiple_testing": {
    "scope": "global",
    "n_tested": 72,
    "n_tested_cum": 73,
    "luck_bar": 2.85,
    "t_thresholds": {"优秀": 3.15, "可用": 2.55},
    "bonferroni_alpha": 0.000685,
    "formula": "luck_bar=sqrt(2*ln(max(N,2))); t_exc=max(2,luck_bar+0.3); t_use=max(1.5,luck_bar-0.3)"
  }
}
```

顶层字段与 `multiple_testing` 块可并存（顶层便于旧读码；块便于前端一处渲染）。Desk 实现时 **顶层三旧字段必须与块内一致**。

### 4.2 路由

| 路径 | 变更 |
|---|---|
| `POST /factor/mine` | 读累计 → `mine_factor` → 成功 bump；响应含新字段；**cache 回放**不 bump，但应 **附带当前 `n_tested_cum`**（可标注 `mt_stale: true`：历史报告内嵌的 luck_bar 仍是旧 N，避免误导——回放时可选「仅刷新 mt 展示块、不改 rating」或全文案提示「评级按当时 N；累计已增至 …」）。**推荐**：回放不重算评级；UI 用琥珀色提示「本条评级基于当时 n_tested=…；当前累计=…」。 |
| `POST /factor/mine/horizons` | 每 horizon 走同一读-评-写；汇总可加 `n_tested_bumped: H` |
| `POST /factor/gp/mine`（或现 GP 路由） | 精英精评按 §2.5；快评不 bump |
| **只读** `GET /factor/mt/state`（可选薄） | 返回 `{scope, n_tested_cum, updated_at}` 供页眉徽章；无则前端从最近报告读 |

`MinePayload` **不必**让用户手填 `n_tested`（防绕过）；若留调试字段须仅在非生产或 `force_n_tested` 管理开关——**本切片默认不暴露**。

### 4.3 ORM

- **新增** `FactorMtState`（或等价）映射 `factor_mt_state`。  
- `FactorMineResult` **不加列**；新字段进 `result_json`（与 W47-2 同策略）。

### 4.4 前端 `#factorlab`（`FactorMinePage` / `MineReport`）

1. 扩展 `FactorMineReport`：补 `n_tested?` `n_tested_cum?` `luck_bar?` `t_thresholds?` `bonferroni_alpha?` `mt_note?` `multiple_testing?`。  
2. 报告头或 KPI 行新增一块 **「多重检验」**（不必大改五块布局）：  
   - 文案：`累计检验 N=… · luck_bar=… · 优秀门 t≥… · 可用门 t≥…`  
   - 辅行：`Bonferroni α≈…（仅提示）`  
   - 固定口径句：`mt_note` 或产品句「门槛随累计挖掘次数抬升，抑制挑运气」。  
3. t 统计量 KPI 的 `sub`：从写死「|t|≥2」改为对照 **本次** `t_thresholds.优秀`（有则用之，无则回退 2）。  
4. 历史列表不必每行展示全文；点开详情见完整块即可。  
5. 可选：页眉小徽章显示当前 `n_tested_cum`（依赖 §4.2 只读接口或上次报告）。

---

## 5. 与评级联动（明确推荐）

| 方案 | 行为 | 结论 |
|---|---|---|
| 仅展示不挡评级 | 数字好看但「优秀」仍按 N=1 地板 | **不选**（违背「门槛联动」与打假目标） |
| **累计 N → luck_bar → 抬 t 门 → 改 rating** | 与现实现一致，只修 N | **选此（主）** |
| 另用 Bonferroni 临界 t 再抬一档 | 双重惩罚 | **禁止** |

OOS 降档逻辑（过拟合→弱等）**保持不变**，仍在 luck_bar 评级之后执行。

`factor_lab_gate`：继续读报告 `rating`；门槛变严后「优秀/可用」变少是预期，门禁规则字面不改。

---

## 6. 固定口径文案（产品可微调用词，语义锁死）

> **多重检验（报告层）**：评级 t 门槛按跨次挖掘累计检验数 N 抬升（`luck_bar=√(2 ln N)`）；Bonferroni α=0.05/N 仅作对照展示，不二次抬门。非完整 PBO/CSCV。

---

## 7. 门禁 / 验收清单

### 7.1 产品验收（冻结勾选）

- [ ] 跨 run 累计 `n_tested`（或 `n_tested_cum`）写入报告  
- [ ] 报告可见 luck_bar / 校正后 `t_thresholds`  
- [ ] 至少一种可见校正口径：主 = luck_bar 门槛抬升；辅 = Bonferroni α 数字  
- [ ] **无**完整 PBO/CSCV 平台

### 7.2 工程验收（Desk / QA）

- [ ] `factor_mt_state` 迁移 + 种子 = 当时 `COUNT(factor_mine_results)`  
- [ ] 手动 mine 成功：`n_tested_cum` +1；cache 回放：+0  
- [ ] 同序两次新表达式：第二次 `n_tested` ≥ 第一次  
- [ ] `n_tested` 增大时 `luck_bar` 与 `t_thresholds` 单调不降  
- [ ] horizons：H 个成功窗口 bump H  
- [ ] GP 快评不bump；精英精评 bump，且精评 `n_tested` ≥ 累计读数  
- [ ] ortho on/off 均只 +1 / 次 mine  
- [ ] 前端报告块可见 N / luck_bar / 门槛 / Bonferroni α  
- [ ] TS 类型含新字段；旧报告缺字段时 UI 降级不崩  
- [ ] `test_rating_direction` 仍绿；补累计 N 单测（mock counter）  
- [ ] 无密钥；不推远程；不改 luck_bar 公式常数（+0.3/−0.3/地板 2.0/1.5）

### 7.3 REBUILD M3-6 对齐句

> 累计 `n_tested`（跨 run）→ Bonferroni / deflated Sharpe / PBO 三选一。

本切片交付 = **选 deflated-Sharpe 风格（luck_bar/t 门）为主 + Bonferroni 数字为辅展示**；PBO 明确不做。即视为 M3-6 **报告层**落地。

---

## 8. 落地序与风险

### 8.1 Desk 落地序（建议 0.75–1 Desk 日）

| 步 | 内容 | 估计 |
|---|---|---|
| 1 | ORM + migrate `factor_mt_state`；`get`/`bump`；种子 COUNT | 0.15d |
| 2 | `mine` / `mine_horizons` / GP 精评接读-评-写序；报告字段 | 0.3d |
| 3 | 前端类型 + MineReport 多重检验块 + t KPI sub | 0.2d |
| 4 | 单测（bump 语义、单调门槛、回放不加）+ 手册点检 §7 | 0.15d |
| 5 | （可选）`GET /factor/mt/state` 页眉徽章 | 0.05d |

### 8.2 风险

| 风险 | 缓解 |
|---|---|
| 种子 72 后门槛陡升，「优秀」锐减 | 预期内打假；UI 明示 N；研究可看 t 与门槛差，而非只看徽章 |
| GP 若误 bump 快评 | §2.3 写死快评 +0；Code review 盯 `evaluate_candidate` |
| 回放报告内旧 luck_bar 与当前累计不一致 | 琥珀色「当时 N vs 当前累计」；不静默改历史 rating |
| 并发双 bump | SQLite 单写 + `UPDATE …` 原子；API 同步 mine 为主 |
| 与 W47-2 同时改 mine 冲突 | Desk 序：W47-2 合入后再拉本切片；冲突点集中在 `mine_factor` 签名与报告 dict |
| 用户以为 Bonferroni 又抬了一道门 | 文案锁死「仅展示」 |

### 8.3 关键取舍摘要（供 Product 冻结）

1. **存储**：SQLite 表 `factor_mt_state`（scope=`global`），种子 = 现有 `factor_mine_results` 行数；**不**每次 COUNT 当真相。  
2. **校正主口径**：累计 N → 既有 **luck_bar / t_thresholds**（DSR 风格）；**Bonferroni α 仅展示**、不二次抬门；PBO/CSCV 不做。  
3. **评级联动**：**抬门槛并影响 rating**（非只展示）。  
4. **W47-2**：一次 mine（不论 ortho）计 **1**；开 ortho 时用 resid t 对抬高后的门。

---

## 9. 附录：路径与现状速查

| 主题 | 路径 |
|---|---|
| 本契约（box） | `/workspace/docs-design-W47-3-multiple-testing.md` |
| 本契约（本机） | `~/quant-platform/docs/DESIGN-W47-3-multiple-testing.md` |
| 产品 Ready | `/workspace/docs-next-batch-product-priority.md` §2 W47-3 |
| REBUILD M3-6 | `docs/REBUILD-2026-10.md`（约 L280；债述 L122） |
| 评级 / luck_bar | `backend/app/services/factor_mining.py` → `mine_factor` |
| 路由 | `backend/app/routers/factor.py` → `MinePayload` / `POST /mine` |
| ORM 结果 | `backend/app/models.py` → `FactorMineResult` |
| GP 精评 N | `backend/app/services/factor_gp.py`（`n_tested=len(seen)`） |
| 门禁 | `backend/app/services/factor_lab_gate.py` |
| 前端报告 | `web/src/pages/FactorMinePage.tsx` → `MineReport` |
| 类型 | `web/src/api/client.ts` → `FactorMineReport` |
| 评级单测 | `backend/tests/test_rating_direction.py` |
| W47-2 指针 | `docs/DESIGN-W47-2-orthogonalize.md` §文末 |

### 数值直觉（种子后）

| N | luck_bar | t_优秀 | t_可用 |
|---|---|---|---|
| 1（今日手动） | 1.177（地板前） | **2.00** | **1.50** |
| 72（种子≈现状） | ≈2.85 | ≈3.15 | ≈2.55 |
| 500 | ≈3.53 | ≈3.83 | ≈3.23 |

---

## 10. 下一指针

本切片冻结并合入后：产品 Ready 本批主序收束；并行薄切片仍是 REBUILD 文首 + `decisions.json` 卫生。M3-5「少而正交」须跟在 W47-2 后另开契约，**非**本文范围。

---

*文档结束。**Product 已冻结（2026-11-08）**。Planner 于 W47-2 合入后拉 Desk；按 §8.1 落地。*
