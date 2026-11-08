# 技术契约：W46-1 / M3-7 成本敏感网格

> 作者：Quant Architect · 2026-11-08（Asia/Shanghai）  
> 看板/产品：`/workspace/docs-next-batch-product-priority.md` · Ready 序 **W46-1 → W46-2 → W46-3**  
> 设计锚点：`/workspace/docs-design-P0-P2-eval-factor-cost.md` §7、§2.1、§3.1、§5.2–5.4  
> 对照：REBUILD M3-7「成本翻倍后收益」；引擎已参数化（`backtest_engine` 万2.5+万1+印花税万5）  
> **硬闸门**：不开实盘；密钥不进源码；不擅自推远程；**本文只写设计，不改业务代码。**
> **冻结状态（2026-11-08）**：Quant Product 已冻结，无微调。默认网格刻度保持；徽章主文案仍 `成本·自定义`（`grid_pick` 悬停用 `网格选中·` 区分，**不上**独立短标 `成本·网格`）；挂载 `#btopt`→`BacktestPage`。Desk 按本冻结稿落地。

---

## 1. 背景 / 目标 / 非目标

### 1.1 背景

- 评估地基（W45 成本默认对齐、徽章 `cost_profile`）就绪后，回测复盘仍缺「一眼打假」杠杆：用户看不到策略在更高摩擦下是否塌陷。
- 引擎侧 `commission` / `slippage` / `stamp_tax` 已参数化；缺的是 **API 网格扫描 + 前端复盘挂载 + 与徽章合并规则**。
- Product 定稿回测复盘三块：**净值 | 成本网格 | 滚动 OOS**。本切片只做中间块。

### 1.2 目标

1. 回测复盘页可对同一策略/区间跑 **commission×slippage 网格**，或等价的 **成本×1/×2 曲线**。
2. **默认格必须含万2.5+万1**（与 `cost_defaults` / 徽章一致）；网格结果可与 `cost_profile`、EvalBadges 对齐。
3. 选中某一格后，可写回单一配置源（请求参数 + 响应 `cost_profile`），供后续单次回测 / 评分 / 模拟盘创建复用。
4. 筛掉「只在低摩擦下成立」的 marginal alpha（REBUILD M3-7 验收口径：成本翻倍后超额仍应可观察）。

### 1.3 非目标（本切片刻意不做）

| 不做 | 归属 |
|---|---|
| 实盘 / live 下单通道 | 硬闸门 |
| M3-8 组合层优化（风险平价等） | Backlog |
| 完整 walk-forward / 滚动多窗 OOS 表 | **W46-2 / M3-3** |
| 今日稳健摘要驾驶舱 | W46-3 |
| 改策略 alpha / 选股权重 | Product 边界 |
| 改裁判口径数字（夏普公式、印花税规则） | 只扫参数，不改模型 |
| xq 组合轨滑点双轨统一 | 仍记备注；本切片不强制 |
| 寻优全空间嵌套成本网格（易爆炸） | 二期；本切片挂在**单次回测复盘** |

---

## 2. 模块边界

### 2.1 后端

| 层 | 路径（建议） | 职责 |
|---|---|---|
| 常量 | `backend/app/core/cost_defaults.py`（W45-2 已引入或应已引入） | `COMMISSION=0.00025`、`SLIPPAGE=0.0001`、`STAMP_TAX=0.0005`… |
| 服务 | `backend/app/services/cost_sensitivity.py`（新建） | 展开网格 → 循环调用既有 `_do_backtest` / 引擎入口 → 汇总指标；**不复制成交逻辑** |
| Schema | `backend/app/schemas.py` | `CostSensitivityRequest` / `CostSensitivityResult` / `CostGridCell` |
| 路由 | `backend/app/routers/strategy.py` | `POST /strategy/backtest/cost-sensitivity`（及可选 async 变体） |
| 复用 | 现有 `_do_backtest` / `_run_single` / `_run_portfolio` | 每格构造 `BacktestRequest` 覆盖 `commission`/`slippage`；`stamp_tax` 固定读 defaults |
| 落库 | **默认不落每格完整净值曲线** | 响应内存返回；可选把「选中格」写回既有 `BacktestResult.extra_json.cost_profile`（见 §5） |

### 2.2 前端

| 挂载点 | 说明 |
|---|---|
| `web/src/pages/BacktestPage.tsx` | 结果区在**净值曲线块下方**新增「成本敏感」块（三块布局之②） |
| `web/src/components/CostSensitivityPanel.tsx`（建议新建） | 热力/曲线 + 默认格高亮 + 点击选格 |
| `EvalBadges.tsx` | 选格后刷新成本徽章文案（`source=grid_pick` → 主文案 `成本·自定义`；悬停 `网格选中·`，见 §5.3；**无**独立短标 `成本·网格`） |
| `api/client.ts` | `costSensitivity(req)` → 上述端点 |

**不改**：OptimizePage 全量嵌套（可二期「对最佳 trial 补扫」）；PaperPage 本切片只读徽章，不强制网格。

### 2.3 责任

| 角色 | 动作 |
|---|---|
| Desk | W45-xq / P1-2 合入后实现本契约 |
| QA | §6 验收清单 |
| Architect | 本文；契约冲突时补丁 |
| Product | 文案/默认网格刻度若需微调 |
| Planner | Ready 拉票顺序 |

---

## 3. 网格形态

### 3.1 两种 mode（API 二选一；UI 默认可切换）

| `mode` | 形态 | 产品对应 | 默认是否推荐 |
|---|---|---|---|
| `grid` | `commissions[]` × `slippages[]` 笛卡尔积热力 | commission×slippage 网格 | **默认**（信息密度高） |
| `multipliers` | 相对 **base** 的成本倍率曲线（同时乘 commission 与 slippage） | 「成本×1/×2」曲线；对齐 REBUILD「翻倍」 | 轻量备选 / 移动端 |

`stamp_tax` / `min_commission` / `risk_free_rate`：**不进扫描轴**，一律用 `cost_defaults`（与徽章一致）。

### 3.2 默认刻度（必须含万2.5+万1）

**`grid` 默认**（请求省略数组时服务端填充）：

```text
commissions = [0.00015, 0.00025, 0.00030, 0.00050]   # 万1.5 / 万2.5 / 万3 / 万5
slippages   = [0.0000,  0.0001,  0.0002,  0.0005]    # 0 / 万1 / 万2 / 万5
```

- **默认格** = `(0.00025, 0.0001)`，响应里 `is_default: true`，UI 描边/角标「默认」。
- 旧乐观格 `(0.0003, 0.0)` 可出现在网格中作对照，但**不得**再当系统默认。

**`multipliers` 默认**：

```text
base = { commission: 0.00025, slippage: 0.0001 }   # 若请求未给 base，吃 cost_defaults
multipliers = [1.0, 2.0]                           # 可扩 [0.5, 1.0, 1.5, 2.0]
```

- `×1.0` 即默认格；`×2.0` 对齐 M3-7「成本翻倍」验收话术。

### 3.3 每格产出指标（最小集）

| 字段 | 含义 |
|---|---|
| `total_return` / `annual_return` / `sharpe` / `max_drawdown` | 与单次回测同口径 |
| `excess_return` | 若单次回测已有则透出；热力默认着色轴建议用它或 `sharpe` |
| `trade_count` | 可选，助判断摩擦是否改变成交行为 |
| `final_equity` | 可选 |

**不做**：每格完整 `equity_curve` / `trades`（体积爆炸）。需要净值时：用户「应用此成本」→ 触发一次普通 `/backtest`。

### 3.4 规模护栏

| 规则 | 值 |
|---|---|
| `len(commissions) * len(slippages)` 上限 | **16**（默认 4×4）；超限 400 |
| `multipliers` 上限 | **8** |
| 同步接口建议墙钟 | 单标的短区间优先；长区间 / 组合策略走 **async**（镜像现有 backtest/async） |
| 进度 | async：`progress` 按格计数 `done/total` |

---

## 4. 请求 / 响应 JSON 契约

### 4.1 端点

```text
POST /api/strategy/backtest/cost-sensitivity          # 同步（短任务）
POST /api/strategy/backtest/cost-sensitivity/async    # 可选；返回 task_id
GET  /api/strategy/backtest/tasks/{task_id}            # 复用现有任务查询（result 内嵌 sensitivity）
```

路由前缀以现网 `strategy` router 为准（与 `/backtest` 同级）。

### 4.2 Request

```json
{
  "symbol": "sh600000",
  "start": "2020-01-01",
  "end": "2023-12-31",
  "strategy": "chan",
  "params": {},
  "initial_cash": 1000000.0,
  "adj": "qfq",
  "mode": "grid",
  "commissions": [0.00015, 0.00025, 0.0003, 0.0005],
  "slippages": [0.0, 0.0001, 0.0002, 0.0005],
  "multipliers": null,
  "base_commission": null,
  "base_slippage": null,
  "metric": "excess_return"
}
```

| 字段 | 规则 |
|---|---|
| 回测身份字段 | 与 `BacktestRequest` 同名同义（symbol/start/end/strategy/params/initial_cash/adj） |
| `mode` | `grid` \| `multipliers`；默认 `grid` |
| `commissions` / `slippages` | `mode=grid` 时可选；省略 → §3.2 默认刻度 |
| `multipliers` | `mode=multipliers` 时可选；省略 → `[1.0, 2.0]` |
| `base_commission` / `base_slippage` | 仅 multipliers；省略 → `cost_defaults` |
| `metric` | UI 热力主色轴：`excess_return` \| `sharpe` \| `total_return`；默认 `excess_return` |
| **禁止** | 请求体带 `stamp_tax` 覆盖（本切片锁 defaults） |

`mode=multipliers` 时服务端展开：

```text
cell_i.commission = base_commission * m_i
cell_i.slippage   = base_slippage   * m_i
```

### 4.3 Response

```json
{
  "mode": "grid",
  "metric": "excess_return",
  "base_cost_profile": {
    "commission": 0.00025,
    "slippage": 0.0001,
    "stamp_tax": 0.0005,
    "min_commission": 5.0,
    "risk_free_rate": 0.025,
    "source": "default",
    "label": "万2.5+万1滑点+卖出万5印花税"
  },
  "axes": {
    "commissions": [0.00015, 0.00025, 0.0003, 0.0005],
    "slippages": [0.0, 0.0001, 0.0002, 0.0005],
    "multipliers": null
  },
  "cells": [
    {
      "commission": 0.00025,
      "slippage": 0.0001,
      "multiplier": null,
      "is_default": true,
      "total_return": 0.12,
      "annual_return": 0.04,
      "sharpe": 0.8,
      "max_drawdown": -0.15,
      "excess_return": 0.03,
      "trade_count": 42,
      "final_equity": 1120000.0
    }
  ],
  "summary": {
    "default_cell_ok": true,
    "double_cost_excess_positive": true,
    "fragile": false,
    "note": "×2 相对默认格：commission/slippage 均×2 后 excess_return 仍 > 0"
  },
  "strategy_key": "chan",
  "symbol": "sh600000",
  "start": "2020-01-01",
  "end": "2023-12-31"
}
```

**`summary` 计算约定（可测）**

- `default_cell`：`is_default=true` 的那一格；若不在用户自定义轴中，服务端**仍须追加跑一格默认**并标 `is_default`（保证「默认格必含」）。
- `double_cost_excess_positive`：取默认格的 `(c,s)`，再跑或查找 `(2c, 2s)`（若网格未覆盖则临时加跑一格，不计入轴展示也可，但 summary 要有值）；`excess_return > 0` → true。
- `fragile`：默认格 `excess_return > 0` 且加倍格 `excess_return ≤ 0` → true（UI 警告「仅低摩擦成立」）。

### 4.4 选格写回（前端 → 单次回测）

用户点击某 cell「应用此成本」：

```json
{
  "symbol": "sh600000",
  "start": "2020-01-01",
  "end": "2023-12-31",
  "strategy": "chan",
  "params": {},
  "commission": 0.0005,
  "slippage": 0.0002,
  "cost_source": "grid_pick"
}
```

- 走既有 `POST /strategy/backtest`；`commission`/`slippage` 显式传入。
- `cost_source` 为**可选扩展**（若 Desk 不想改 BacktestRequest，可仅前端记住，靠「显式传参」推断 `source=user`；推荐加上以便徽章区分 `grid_pick`）。

---

## 5. 与徽章 / `cost_profile` 合并规则

### 5.1 `cost_profile.source` 枚举扩展

| `source` | 何时 | 徽章主文案（对齐 Product 短标签） |
|---|---|---|
| `default` | 未显式传 commission/slippage，吃万2.5+万1 | `成本·默认万2.5+万1` |
| `user` | 表单/API 显式自定义（非网格选格） | `成本·自定义` |
| `grid_pick` | 从成本敏感面板选格写回 | `成本·自定义`（主文案不变）；悬停 `label` 可加前缀 `网格选中·` | 

> **已冻结**：不上独立短标 `成本·网格`；仅悬停 `网格选中·` 区分。

### 5.2 合并算法（服务端填徽章时）

```text
输入：请求上的 commission/slippage（可缺省）、可选 cost_source、cost_defaults
输出：cost_profile

1. stamp_tax / min_commission / risk_free_rate ← 永远来自 cost_defaults
2. 若 commission/slippage 均缺省：
     c,s ← defaults; source ← "default"
   否则：
     c,s ← 请求值（缺的一侧仍补 defaults）
     source ← req.cost_source if in {user, grid_pick} else "user"
3. 若 c==COMMISSION ∧ s==SLIPPAGE ∧ source≠grid_pick：
     source 可归一为 "default"（避免「显式传了默认值」显示成自定义）
   若 source==grid_pick：即使数值等于默认，仍保留 grid_pick（表示用户从网格确认过）
4. label ← 人类可读；默认值用既定句「万2.5+万1滑点+卖出万5印花税」
```

### 5.3 网格面板自身徽章

- 面板标题旁固定展示 `base_cost_profile`（扫描基准）。
- 高亮 `is_default` 格；`fragile=true` 时整块警告色。
- 选中格预览：临时 `cost_profile`（`source=grid_pick`）传给 `EvalBadges`，**在用户点「应用」并完成单次回测前，不覆盖主结果区徽章**（避免未写回就改结论）。

### 5.4 与单次回测结果的关系

| 场景 | 主结果区 `cost_profile` |
|---|---|
| 仅跑网格、未应用 | 仍为本次主回测的 profile（多半 `default`） |
| 应用选格并重跑成功 | 新结果 `source=grid_pick`（或 `user`），数值=选格 |
| 历史 ORM 行 | 不回填；仅新结果 |

---

## 6. 迁移 / 落地顺序（Desk）

**前置**：W45 主切片合 main；**P1-2 / W45-xq（xq 成本双轨）合入后**再拉本项 Doing（契约本周可并行已完成）。

### 6.1 PR 切片建议

1. **Schema + service + 同步 API**：`cost_sensitivity.py` 循环调 `_do_backtest`；护栏 16 格；强制追加 default 格；`summary` 字段。  
2. **测试**：见下清单。  
3. **前端**：`CostSensitivityPanel` 挂 BacktestPage 净值块下；热力 + 默认格 + 应用写回。  
4. **徽章合并**：`cost_source=grid_pick` 路径（可与 3 同 PR）。  
5. **（可选同周）** async 变体；不做则文档注明长任务限制。

**停**：不改 live、不推远程、不做 OOS 滚动表、不动 M3-8。

### 6.2 可测验收清单

- [ ] 省略 `commissions`/`slippages` 时响应轴含 `(0.00025, 0.0001)` 且 `is_default=true`
- [ ] 自定义轴若不含默认值，响应仍含 default 格
- [ ] `mode=multipliers` 默认含 `1.0` 与 `2.0`；`×1` 的 c/s == cost_defaults
- [ ] 格数 >16 → 400
- [ ] `summary.double_cost_excess_positive` / `fragile` 与公式一致（fixture 可 mock 引擎）
- [ ] 选格写回后单次回测 `cost_profile.source` ∈ {`grid_pick`,`user`}，数值匹配
- [ ] 数值等于默认且非 grid_pick → 徽章 `成本·默认万2.5+万1`
- [ ] 不落每格 equity_curve；响应体积可控
- [ ] `test_cost_model.py` 与既有回测测试仍绿
- [ ] 硬闸门：无实盘、无密钥、无 push

---

## 7. 风险与取舍

| 风险 | 缓解 | 回滚 |
|---|---|---|
| N 格同步超时 / 打满 DB | 上限 16；长任务 async；不落库曲线 | 降默认刻度为 3×3 |
| 组合/多标的策略每格很慢 | UI 提示预估格数；强制 async | 本切片仅开放单标的（feature flag） |
| 与主回测随机性/数据不一致 | 同 `adj`/同 params/同引擎入口 | — |
| 用户把乐观格当主结论 | 默认格高亮 + fragile 警告 + 徽章 source | — |
| `source` 枚举扩展令旧前端炸 | 只增不改；旧前端忽略 `grid_pick` 当自定义 | 去掉 cost_source 字段，仅用 user |
| 与 W46-2 同页抢布局 | 本切片只占「成本」槽；OOS 槽留空位/灰提示「下项」 | — |

### 取舍说明

| 选择 | 理由 |
|---|---|
| 先网格、OOS 下周 | Product/Planner Ready 序；工程风险低、复用 `cost_profile` |
| 默认可切 multipliers | 满足 REBUILD「翻倍」话术与轻量视图，不强迫只做一个 |
| 不返回每格净值 | 体积与耗时；应用选格再跑一次完整回测即可 |
| stamp_tax 不上轴 | 法规固定；避免三维爆炸；徽章仍展示印花税 |
| 同步+可选 async | 对齐现有 backtest 模式，Desk 可分期 |

---

## 8. 附录：路径速查

| 主题 | 路径 |
|---|---|
| 本契约 | `/workspace/docs-design-W46-1-cost-sensitivity.md` |
| 产品 Ready | `/workspace/docs-next-batch-product-priority.md` |
| 总设计 §7 | `/workspace/docs-design-P0-P2-eval-factor-cost.md` |
| REBUILD M3-7 | `quant-latest/docs/REBUILD-2026-10.md` |
| 成本默认 | `backend/app/core/cost_defaults.py`（目标）/ 引擎默认 |
| 回测 API | `backend/app/routers/strategy.py`、`schemas.py` |
| 前端复盘 | `web/src/pages/BacktestPage.tsx` |
| 徽章 | `EvalBadges.tsx` + §5.2 `cost_profile` |

---

*文档结束。Desk 在 P1-2/W45-xq 合入后按 §6 开工；W46-2 滚动 OOS 另文。*
