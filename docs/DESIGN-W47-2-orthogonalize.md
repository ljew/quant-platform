# 技术契约：W47-2 / M3-4 正交化进挖矿主流程

> 作者：Quant Architect · 2026-11-08（Asia/Shanghai）  
> 看板/产品：`/workspace/docs-next-batch-product-priority.md` §2 **W47-2 / M3-4** · Ready 序 **W47-1 → W47-2 → W47-3**  
> 设计锚点：`/workspace/docs-design-P0-P2-eval-factor-cost.md`；REBUILD-2026-10.md **M3-4**「正交化进主流程」  
> 对照体例：`docs-design-W46-1-cost-sensitivity.md`、`docs-design-W47-1-paper-ux.md`  
> **硬闸门**：不开实盘；密钥不进源码；不擅自推远程；**本文只写设计，不改业务代码。**  
> **冻结状态（2026-11-08）**：**Product 已冻结**。默认 `orthogonalize=True`（主表单默认勾选）；开时 **resid 进评级**，raw/resid 报告并列；相关矩阵与优秀门 `max_abs_corr` 仍对 **raw**；代表集=GP 五式写死。非目标：SVD 全库正交、GP 复活、策略中性混称。无微调。Desk 待 W47-1 合入后拉；Architect 可按指针备 W47-3。  
> 前置：W47-1 **已 Product 冻结**（体验不堵即可并行本契约）。  
> 读码基线：本机 `~/quant-platform` tip `6470e2f`（W46-3 已合）；box `/workspace/quant-latest` 可能滞后，**以本机仓为准**。

---

## 1. 背景 / 目标 / 非目标

### 1.1 背景

- REBUILD **M3-4**：把「对基准因子残差化」应用到**因子挖掘主流程**，挖增量 alpha 而非重复 alpha。实验侧已有对照（卦象残差 IC 翻负），主路径仍未接。
- 读码现状（`~/quant-platform` tip `6470e2f`）：
  - **`POST /factor/mine`** → `mine_factor`（`backend/app/services/factor_mining.py`）只评 **raw** Spearman IC；评级用全样本 `ic_mean`/`t_stat` + OOS 降档；**无** `orthogonalize` 入参。
  - **相关矩阵**：`_corr_with_existing` 在最新截面对 `FACTOR_COLUMNS`（14 列：`ep/bp/momentum/reversal/low_vol/size/...`）算 Spearman；报告字段 `corr_with_existing` / `max_abs_corr`。前端 `#factorlab` → `FactorMinePage` ④ 热力已挂；`max_abs_corr>0.5` 文案已写「可考虑正交化处理」，**但主路径不能一键残差化**。
  - **GP 侧已有残差**：`factor_gp._orthogonalize(fv, fac_bucket)` + `build_context` 内 `ORTHO_EXPRS`（动量/反转/低波/价值/规模）预填 `ortho_cache`；`GpMinePayload.orthogonal` **默认 False**；UI「正交增量模式」仅挂在 GP 区。**GP 复活 / fitness 改造仍属 Backlog，本切片不拉。**
  - **策略层行业/市值中性**（`csi800_enhanced._neutralize_by_group` / 行业配额选股）是**组合构建**去风格，与挖矿截面「候选 vs 代表因子 OLS 残差再评 IC」**不是同一件事**——契约禁止混称、禁止复用其 demean 实现冒充 ortho。
- 产品验收（Ready）：`#factorlab` 主路径可对代表集残差化后再评 IC；报告并列 raw vs residual；相关矩阵仍保留；默认开关须本契约给出推荐。

### 1.2 目标

1. 挖矿主路径（`POST /factor/mine` 及复用它的 `POST /factor/mine/horizons`）增加 **`orthogonalize`** 开关 → 按日截面相对代表集 OLS 取残差 → 再评 IC。  
2. 报告并列 **`ic_raw` / `ic_resid`**，并回显 **`ortho_basis`**（代表集名称+表达式+降级统计）。  
3. **开启时**：主指标与评级走 **residual**；关闭时行为与今日一致（只评 raw）。  
4. **`_corr_with_existing` 相关矩阵保留**，不因残差化删除或改口径。  
5. 复用 GP 已验证的残差算法与代表集表达式；抽到共享模块，避免 `factor_mining` ↔ `factor_gp` 环依赖。

### 1.3 非目标（本切片刻意不做）

| 不做 | 归属 |
|---|---|
| 对称 SVD / 全库因子两两正交 / 施密特全库 | Product 非目标 |
| GP 复活、改 GP fitness（去 abs(ic)）、默认打开 GP | Backlog / M3-8·P1-4 |
| M3-5「少而正交」组合规则（3–5 低相关因子） | **必须跟在 M3-4 后** |
| W47-3 跨 run 累计 `n_tested` / Bonferroni / DSR | 下一 Ready |
| 把策略层行业/市值中性搬进挖矿 IC | 概念混淆；见 §1.1 |
| 用 `factor_daily` 14 列整体替换代表集（本切片） | 可选二期加速；主路径锁定表达式集 |
| 改 IC 公式 / OOS 切分数字 / luck_bar 公式 | 只换「评哪一路 IC」 |
| 改相关矩阵列集合或热力布局大改 | 保留；文案可微调一句 |
| 实盘 / 密钥 / 推远程 | 硬闸门 |

---

## 2. 模块边界

### 2.1 关键取舍：抽共享 ortho vs 就地 import GP

| 方案 | 利 | 弊 | 结论 |
|---|---|---|---|
| A. `factor_mining` 直接 `from factor_gp import _orthogonalize` | 改动最少 | GP 文件承载主流程依赖；GP 熔断叙事下耦合差；`ORTHO_EXPRS`/`ortho_cache` 仍困在 `build_context` | **不选** |
| B. **新建** `backend/app/services/factor_ortho.py`，GP 与 mining 同改 import | 单一真相；可单测；不碰 GP 搜索逻辑 | 多一次行为保持型搬迁 | **选 B** |
| C. 在 `factor_mining` 内复制一份 OLS | 无依赖 | 双份算法必然漂移 | **禁止** |

**共享模块职责（本切片新增）** `backend/app/services/factor_ortho.py`：

| 符号 | 职责 |
|---|---|
| `ORTHO_EXPRS: dict[str, str]` | 代表集写死清单（§4） |
| `build_ortho_cache(...)` | 按 `snap_pairs`/`aligned`/attrs 预计算 `snap → {fac → {sym: val}}`（从 `factor_gp.build_context` 抽出） |
| `orthogonalize_cross_section(fv, fac_bucket) -> list[tuple]` | 原 `_orthogonalize`：截面 OLS 残差；失败/共线/样本不足 → **原样返回 fv**（该截面降级为 raw） |

`factor_gp.py`：删除本地 `_orthogonalize` / 内联 `ORTHO_EXPRS`，改为 import 共享；`evaluate_candidate(..., orthogonal=)` 行为不变（仍默认 False）。  
`factor_mining.mine_factor`：新增 `orthogonalize: bool = True`；开启时在每截面 IC 前调共享函数。

### 2.2 后端路径表

| 层 | 路径 | 本切片职责 |
|---|---|---|
| 共享 | `backend/app/services/factor_ortho.py` | **本切片新增**；算法+代表集 |
| 挖矿 | `backend/app/services/factor_mining.py` → `mine_factor` | 接开关；双路 IC；报告字段；评级用 resid（开）或 raw（关） |
| 相关 | 同文件 `_corr_with_existing` | **只读复用**；不改列、不改阈值 |
| GP | `backend/app/services/factor_gp.py` | 改为调用共享；**不**改默认 `orthogonal=False`、不改搜索 |
| 路由 | `backend/app/routers/factor.py` | `MinePayload` / `HorizonsPayload` **本切片新增** `orthogonalize`；查重 `params_json` 签名纳入开关；透传 `mine_factor` |
| ORM | `FactorMineResult` | **不加列**；`result_json` 承载新字段；`params_json` 签名扩展（§5.3） |
| 门禁 | `factor_lab_gate.py` | 仍读 `rating` + OOS；开启 ortho 后评级已按 resid，**门禁逻辑无需改符号**（测例补一条 resid 降档） |

### 2.3 前端路径表

| 挂载点 | 说明 |
|---|---|
| `#factorlab` → `web/src/pages/FactorMinePage.tsx` | 主表单（非 GP 区）增加「对代表集残差后再评 IC」；**默认勾选**；报告 KPI/文案并列 raw vs resid；④ 相关矩阵**保留** |
| `web/src/api/client.ts` → `FactorMineReport` | **本切片新增**可选 `ic_raw` / `ic_resid` / `ortho_basis` / `orthogonalize` |
| GP 区既有 `gpOrtho` | **不动默认**（仍 false）；文案可注明「与主路径同一代表集/算法」 |

### 2.4 与策略中性的边界（勿混）

| | 挖矿截面残差（本切片） | 策略行业/市值中性（已有） |
|---|---|---|
| 位置 | `mine_factor` / GP evaluate | `csi800_enhanced.rebalance` |
| 输入 | 候选因子值 vs 5 个代表风格因子 | 因子得分 vs 市值分位 / 行业配额 |
| 输出 | 残差序列 → **IC/评级** | 组合权重 / 选股集合 |
| 目的 | 检验增量预测力 | 控制组合风格暴露 |

---

## 3. 代表因子集（契约写死）

### 3.1 清单（与 GP `ORTHO_EXPRS` 对齐，禁止本切片擅自改式）

| `name`（`ortho_basis` 键） | 中文 | 表达式 | 风格含义 |
|---|---|---|---|
| `momentum` | 动量 | `roc(c_m, 20)` | 中期动量 |
| `reversal` | 反转 | `-roc(c_r, 5)` | 短期反转（已取负） |
| `low_vol` | 低波 | `std(c_v)` | 波动（高波暴露；残差剥离） |
| `value` | 价值 | `safe_inv(pe_ttm, 0.001, 1000)` | 盈利收益率代理 |
| `size` | 规模 | `log(market_cap + 1)` | 对数市值 |

数据来源（与挖矿同构，**禁止**另开数据源）：

- 价量：`mine_factor` / GP 已加载的前复权对齐序列 → `make_ns` 的 `c_m`/`c_r`/`c_v`；  
- 估值/市值：截面 `attrs` + PIT `_attrs_asof`（`StockValuationDaily`）覆盖 `pe_ttm`/`market_cap`；  
- 求值：`eval_factor(expr, ns)`（`app.core.engine.factor_expr`）。

### 3.2 与 `factor_daily` / `factor_library` 的关系（说明，非本切片改点）

- 相关矩阵仍对 **14 列** `FACTOR_COLUMNS`（含 `ep/bp/momentum/...`）——这是「与库存因子冗余度」诊断。  
- 残差基底是上表 **5 条表达式**，与 `factor_library` 公式**略有差异**（例：库内 `low_vol=std(returns(c_v))`，ortho 用 `std(c_v)`；库内 `size=log(market_cap)`，ortho 用 `log(market_cap+1)`）。  
- **本切片锁定 GP 已用表达式**，保证「主路径残差」与「GP 正交增量」可比；不为对齐 library 改式。二期若统一公式，须另开契约 + 回归。

### 3.3 缺失 / 降级行为（算法契约摘要，细节 §6）

| 条件 | 行为 |
|---|---|
| 某 snap 某 fac 对某 sym 求值失败 | 该 sym 不进入该 fac 桶 |
| 候选截面中，同时具备**全部**代表 fac 的股票数 `< 20` | **该截面不残差**，IC 记为 raw（与 GP `_orthogonalize` 一致） |
| OLS 主元 `< 1e-12` 或求解异常 | 同上，降级 raw |
| 全区间有效截面仍 `< 3` | `mine_factor` 仍返回 `ok=False`「有效截面不足」 |
| 代表集表达式本身校验失败 | 启动期/单测应拦住；运行时单 fac 全空 → 等价于该 fac 不参与；若五 fac 全空则全程降级 raw，`ortho_basis.n_snaps_fallback_raw` 升高并 `note` 标明 |

---

## 4. API

### 4.1 请求：`orthogonalize`（本切片新增）

**`MinePayload` / `HorizonsPayload`**（`backend/app/routers/factor.py`）：

```text
orthogonalize: bool = True
  # 默认 True（主路径推荐开，见 §7 取舍）
  # True  → 截面残差后再评 IC；评级与主 KPI 用 resid；报告必含 ic_raw + ic_resid + ortho_basis
  # False → 与今日完全一致：只评 raw；ic_resid=null；ortho_basis 仍回显清单（exprs）但 n_snaps_used=0
```

路由透传：

```text
mine_factor(..., orthogonalize=payload.orthogonalize)
mine_horizons 每个 forward 同样透传
```

**兼容别名（可选，非必须）**：若请求体误传 GP 风格字段 `orthogonal`，路由可映射到 `orthogonalize`；响应只文档化 `orthogonalize`。Desk 实现时二选一，测例锁一种即可。

**非本切片**：不新增独立 `POST /factor/evaluate`；产品口中的 evaluate/mine 即现有 `mine_factor` / `/factor/mine`。

### 4.2 查重签名（必须改）

今日：

```text
params_json = f"{sd}|{ed}|{groups}|{forward}"
```

本切片：

```text
params_json = f"{sd}|{ed}|{groups}|{forward}|o={1 if orthogonalize else 0}"
```

理由：同一表达式 raw 与 resid 报告不可互相覆盖；`force` 就地更新仍按完整签名匹配。旧记录无 `|o=` 后缀 → 仅当请求 `orthogonalize=False` 且走 legacy 日期序列兼容时可能命中（与今日 `_find_existing` ② 一致）；**`orthogonalize=True` 不得命中无 ortho 字段的旧缓存**（避免把 raw 报告当成 resid）。

### 4.3 响应字段（`mine_factor` 报告 / `FactorMineReport`）

**保持不变（语义在 `orthogonalize=True` 时指向 resid 主路）**：

| 字段 | 开启 ortho 时含义 |
|---|---|
| `ic_mean` / `icir` / `t_stat` / `ic_win_rate` / `ic_series` | **残差 IC** 序列的汇总与时序 |
| `group_means` / `mono_score` / `long_short` | 按**残差因子值**排序分组（与 IC 同源） |
| `rating` / `oos` / `luck_bar` / `t_thresholds` / `n_tested` | 仍按现逻辑，但输入 IC 为 resid |
| `corr_with_existing` / `max_abs_corr` | **仍对 raw 候选** vs 14 列（诊断冗余；不改成对残差） |

**本切片新增**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `orthogonalize` | `bool` | 回显实际使用的开关 |
| `ic_raw` | `object \| null` | `{ ic_mean, icir, t_stat, ic_win_rate, n_periods, ic_series? }`；关开关时与主 `ic_*` 同口径，可省略重复或与主字段相等 |
| `ic_resid` | `object \| null` | 同上结构；`orthogonalize=False` 时为 `null` |
| `ortho_basis` | `object` | 见下 |

`ortho_basis` 形状：

```json
{
  "names": ["momentum", "reversal", "low_vol", "value", "size"],
  "exprs": {
    "momentum": "roc(c_m, 20)",
    "reversal": "-roc(c_r, 5)",
    "low_vol": "std(c_v)",
    "value": "safe_inv(pe_ttm, 0.001, 1000)",
    "size": "log(market_cap + 1)"
  },
  "min_cross_section": 20,
  "n_snaps_resid": 0,
  "n_snaps_fallback_raw": 0,
  "note": "截面股票对全部代表因子对齐数<20或共线时该期降级为raw IC"
}
```

开启时：`ic_mean` 等主字段 **必须等于** `ic_resid` 对应汇总（避免前端读错）；`ic_raw` 必填。  
关闭时：`ic_resid=null`；主字段 = raw；`ortho_basis.n_snaps_resid=0`。

### 4.4 ORM / 落库

- `row.ic_mean` / `row.icir` / `row.rating`：存**主路**（开=resid，关=raw），与今日消费者（历史列表、lab gate）一致。  
- 完整 `ic_raw`/`ic_resid`/`ortho_basis` 进 `result_json`。  
- **无 migration**。

---

## 5. 截面残差算法契约

### 5.1 单截面步骤（与现 GP 一致）

对每个挖矿截面日 `snap`：

1. 得到候选 `fv = [(sym, factor_value, fwd_ret), ...]`（今日 `mine_factor` 已有；有效截面门槛保持 **≥30** 只，与 mining 一致；GP 快评仍为 25，不强行统一）。  
2. 取 `fac_bucket = ortho_cache.get(snap)`：`{fac_name: {sym: float}}`。  
3. 过滤：`sym` 须在候选中，且对 `ORTHO_EXPRS` **每一个** `fac_name` 都有值。  
4. 若过滤后 `n < 20` → 返回原 `fv`（降级）。  
5. 设计矩阵 `X = [1, f1, f2, f3, f4, f5]`，`y = factor_value`；正规方程 `(X'X)b = X'y`，高斯消元求 `b`。  
6. `resid = y - Xb`；输出 `[(sym, resid, fwd_ret), ...]`（仅过滤后的 sym）。  
7. `ic_raw = spearman(raw_vals, rets)`（全 fv）；`ic_resid = spearman(resid_vals, rets)`（残差列表；若降级则 `ic_resid = ic_raw`）。

**注意**：残差列表可能比 raw fv 短（缺代表因子值的股票被剔除）。IC 在残差子集上计算——与 GP 现状一致；契约接受该偏差，并在 `ortho_basis.note` 说明。

### 5.2 `ortho_cache` 构建

- 对每个 `(snap ∈ snapshots)`、每个 `(fac_name, fexpr) ∈ ORTHO_EXPRS`、每个 `sym`：用与候选相同的窗口/`make_ns`/`eval_factor` 写入桶。  
- 实现上复用 GP `build_context` 抽出的 `build_ortho_cache`；`mine_factor` 在截面循环前调用一次（有成本：约 5× 截面×股票求值）。  
- **性能护栏（本切片最低要求）**：开关关闭时**不**建 cache、不跑 OLS。  
- 二期可选：用 `factor_daily` 近似加速——**非本切片**。

### 5.3 共线与数值

- 主元绝对值 `< 1e-12` → 该截面降级 raw（与现实现一致）。  
- 任何异常 → 降级 raw，不抛到 API 400。  
- 不引入外部线性代数库（保持纯 Python，与 GP 一致）。

---

## 6. 报告与评级

### 6.1 哪路进评级（契约推荐，待 Product 冻结）

| `orthogonalize` | 主 KPI / 分组 / OOS / **rating** | 并列展示 |
|---|---|---|
| `True`（默认） | **resid** | 必示 `ic_raw` vs `ic_resid` |
| `False` | **raw**（今日行为） | `ic_resid=null`；可灰字提示「开启残差化可检验增量 IC」 |

评级门槛数字（`t_excellent`/`t_usable`、`ic_mean>0.02`、`mono_score`、`max_abs_corr<0.8` 进优秀、OOS 降档）**一律不改**；只替换输入 IC 序列。

**相关矩阵与优秀门**：`max_abs_corr` 仍基于 **raw** 候选 vs 14 列。含义：残差 IC 再好，若 raw 与库存因子 |ρ|≥0.8，仍不能评「优秀」——避免「改个正交标签就绕过冗余门」。若 Product 冻结时希望优秀门改为看残差后相关，须显式改口；**本文推荐维持 raw 相关门**。

### 6.2 默认开关取舍（推荐写死如下）

**推荐：`orthogonalize` 默认 `True`（API + `#factorlab` 主表单勾选默认开）；开启后 residual 进评级；raw 并列。**

| 选项 | 含义 | 为何不选/选 |
|---|---|---|
| A. 默认关，显式可选 | 与 GP 勾选习惯一致 | 主路径继续挖重复 alpha，M3-4 债仍在 → **不选作主路径默认** |
| B. **默认开，结果进评级** | 主路径即增量检验 | **推荐**：对齐 REBUILD「应用在挖掘主流程」；用户可关回 raw 对照 |
| C. 默认开但评级仍用 raw、resid 仅展示 | 打假弱 | 不满足「残差化后再评」→ **不选** |

GP 区 `orthogonal` **保持默认 False**（非本切片复活/改默认），避免悄悄改变 GP 行为。

### 6.3 文案（产品可冻）

- 开关标签：**对代表集残差后再评 IC**  
- 悬停：`候选因子对动量/反转/低波/价值/规模做横截面回归取残差，再算 IC——度量增量预测力；相关矩阵仍保留`  
- 相关矩阵 `max_abs_corr>0.5` 提示：可由「可考虑正交化处理」改为「可开启上方残差化后再评」（Desk 小改，非必须）

---

## 7. 前端 `#factorlab` 挂载

1. **主挖掘表单**（表达式/区间/分组旁，**非** GP 折叠区）：Checkbox，绑定 `orthogonalize`，**默认 `true`**。  
2. **请求**：`api.factorMine({ ..., orthogonalize })`；horizons 同步带上。  
3. **报告头**：Badge 或灰字回显 `残差IC·开` / `残差IC·关`。  
4. **KPI**：主卡仍绑 `report.ic_mean`（主路）；旁增一小卡或双列「raw / resid」读 `ic_raw.ic_mean` 与 `ic_resid.ic_mean`（关开关时 resid 显示「—」）。  
5. **④ 相关矩阵**：组件 `CorrHeat` **保留**；数据源不变。  
6. **类型**：`FactorMineReport` 扩展可选字段；旧缓存无新字段时前端用可选链，不崩。  
7. **不做**：大改五块报告布局；不把 GP 默认勾上。

---

## 8. 门禁 / 验收清单

### 8.1 产品验收（冻结勾选）

- [ ] `#factorlab` 主路径可勾选/默认对代表集残差化后再评 IC  
- [ ] 报告并列可见 raw vs residual（均值或等价 KPI）  
- [ ] 相关矩阵热力仍在，且不开 ortho 时行为与今日一致  
- [ ] 默认开关符合冻结结论（本文推荐：默认开且 resid 进评级）

### 8.2 工程验收（Desk / QA）

- [ ] `factor_ortho.orthogonalize_cross_section` 单测：已知 X/y → 残差与手算一致；n<20 / 共线 → 返回原 fv  
- [ ] `ORTHO_EXPRS` 五条均可 `validate_expr` 通过  
- [ ] `POST /factor/mine` `orthogonalize=false` 与改前同表达式同区间指标一致（回归）  
- [ ] `orthogonalize=true` 响应含 `ic_raw`/`ic_resid`/`ortho_basis`；`ic_mean == ic_resid.ic_mean`  
- [ ] `params_json` 含 `|o=0/1`；true/false 缓存不串  
- [ ] GP `orthogonal=true/false` 行为相对搬迁前一致（import 共享后的冒烟）  
- [ ] 前端默认勾选；关开关后报告无 resid KPI 或不显示  
- [ ] `factor_lab_gate`：用 resid 评「优秀/可用」且 OOS 过的报告仍可通过（补测）  
- [ ] 不改业务策略中性代码；无密钥；不推远程

### 8.3 REBUILD M3-4 对齐句

> 正交化方法（对基准因子残差化）应用在因子挖掘主流程，挖增量 alpha 而非重复 alpha。

本切片交付即视为 M3-4 工程落地；「至少一个因子 OOS IC≥…」属研究产出，**不**作本切片 Desk 出门硬门槛（研究门槛仍在 REBUILD 总验收，由挖矿使用方后续满足）。

---

## 9. 落地序与风险

### 9.1 Desk 落地序（建议 1–1.5 Desk 日）

| 步 | 内容 | 估计 |
|---|---|---|
| 1 | 新增 `factor_ortho.py`：搬迁 `ORTHO_EXPRS` + `_orthogonalize` + 抽出 `build_ortho_cache`；单测 | 0.3d |
| 2 | `factor_gp` 改 import（行为保持）；冒烟 GP ortho on/off | 0.2d |
| 3 | `mine_factor` 接 `orthogonalize`；双路 IC；报告字段；评级主路 | 0.4d |
| 4 | 路由 Payload + `params_json` 签名；horizons 透传 | 0.1d |
| 5 | 前端开关 + 报告并列 + 类型；相关矩阵保留 | 0.3d |
| 6 | 回归测 + 手册点检 §8 | 0.2d |

### 9.2 风险

| 风险 | 缓解 |
|---|---|
| ortho_cache 使单次 mine 变慢 | 仅开关开时构建；进度条文案可加「预计算代表因子…」；二期再谈 daily 近似 |
| 残差子集 vs raw 全集 IC 不完全同口径 | 契约接受（同 GP）；`note` 写明 |
| 旧缓存无 `|o=` | true 请求不命中 legacy；false 可走兼容 |
| 与 library 公式漂移被误解为 bug | §3.2 写明；文档/悬停指向五条 exprs |
| 误把策略中性当 ortho | §2.4；Code review 拒复用 demean |
| 默认开导致历史「优秀」变少 | 预期内（打假）；可关开关对照 |

### 9.3 关键取舍摘要（供 Product 冻结）

1. **共享模块抽出**（非 mining import GP、非复制算法）。  
2. **代表集 = GP 五条 `ORTHO_EXPRS` 写死**。  
3. **默认 `orthogonalize=True`，resid 进评级；raw 并列；相关矩阵仍对 raw**。  
4. **GP 默认与搜索逻辑不动**。  
5. **查重签名加入 `o=0/1`**。

---

## 10. 附录：路径与现状速查

| 主题 | 路径 |
|---|---|
| 本契约（box） | `/workspace/docs-design-W47-2-orthogonalize.md` |
| 本契约（本机） | `~/quant-platform/docs/DESIGN-W47-2-orthogonalize.md` |
| 产品 Ready | `/workspace/docs-next-batch-product-priority.md` §2 W47-2 |
| REBUILD M3-4 | `docs/REBUILD-2026-10.md`（约 L278） |
| 挖矿主路径 | `backend/app/services/factor_mining.py` → `mine_factor` / `_corr_with_existing` |
| GP 残差（待抽出） | `backend/app/services/factor_gp.py` → `_orthogonalize` / `ORTHO_EXPRS` / `ortho_cache` |
| 路由 / Payload | `backend/app/routers/factor.py` → `MinePayload` / `POST /mine` |
| 14 因子列 | `backend/app/models.py` → `FACTOR_COLUMNS` |
| 策略中性（对照勿混） | `backend/app/core/strategies/csi800_enhanced.py` |
| 前端 | `web/src/pages/FactorMinePage.tsx` · `web/src/api/client.ts` |
| 实验室门禁 | `backend/app/services/factor_lab_gate.py` |

### W47-3 指针

本切片冻结并合入后起草：**W47-3 / M3-6 多重检验累计（报告层）**——跨 run 累计 `n_tested`、luck_bar/校正门槛可见；在正交化之后抬门槛，少误杀冗余因子。非本文范围。

---

*文档结束。**Product 已冻结（2026-11-08）**。Planner 于 W47-1 合入后拉 Desk；按 §9.1 落地。*
