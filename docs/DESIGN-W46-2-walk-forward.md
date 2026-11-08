# 技术契约：W46-2 / M3-3 walk-forward OOS

> 作者：Quant Architect · 2026-11-08（Asia/Shanghai）  
> 看板/产品：`/workspace/docs-next-batch-product-priority.md` · Ready 序 **W46-1 → W46-2 → W46-3**  
> 设计锚点：`/workspace/docs-design-P0-P2-eval-factor-cost.md` §3.3、§5.2、§7；W46-1 复盘三块之③  
> 对照：REBUILD M3-3「walk-forward 滚动验证」；现状单次切分见 `factor_mining._is_oos_split`、`OptimizeRequest.oos_ratio`  
> **硬闸门**：不开实盘；密钥不进源码；不擅自推远程；**本文只写设计，不改业务代码。**  
> **冻结状态（2026-11-08）**：Quant Product **已冻结**。窗级判定澄清：**通过 = OOS 主度量 ≥ 0（不要求与 IS 同向）**；**过拟合**仍 = IS>0 ∧ OOS<0；其余 OOS<0 → **未通过**。其余字段/门禁/`oos` 扩展/挂载无需再改。Desk 按本冻结稿落地。

---

## 1. 背景 / 目标 / 非目标

### 1.1 背景

- M3-1 已落地**单次** IS/OOS：因子侧 `_is_oos_split`（默认 `oos_ratio=0.3`，verdict 四态）；寻优侧按交易日末段切一刀并填 `oos_*` 指标。
- 单次切分抗不住 A 股 regime（REBUILD M3-3：2019 vs 2024）。W46-1 已把复盘面板「净值 | 成本网格 | **滚动 OOS**」的②做完；本切片补③。
- `BacktestRequest` **尚无** OOS 字段；单次回测主路径目前是全样本一把梭——面板需要独立 WF 服务，而不是默默改每次 `/backtest`。

### 1.2 目标

1. 回测复盘可跑 **滚动多窗 IS→test**：表内每窗 metrics + verdict；总门禁 **≥** 现有单次 `oos_ratio` 切分（更严，不得更松）。
2. **保留 `oos_ratio` 兼容**：`mode=single` / `n_windows=1` ≡ 今日单次切分；徽章/`oos` 字段继续带 `oos_ratio`。
3. 挂载 **`#btopt` → `BacktestOptimizePage` → `BacktestPage`（单次回测）** 结果区第三块（净值下、成本网格旁/下，与 W46-1 同页）。
4. 与 EvalBadges / `oos` JSON 合并：主文案仍 `OOS·{verdict}`；悬停可见多窗摘要。

### 1.3 非目标（本切片刻意不做）

| 不做 | 归属 |
|---|---|
| 完整参数寻优 walk-forward（每组参数 × 多窗嵌套） | Backlog；OptimizePage 仍用单次 `oos_ratio` |
| 实盘 / live 下单 | 硬闸门 |
| M3-8 组合层优化 | Backlog |
| 改 W46-1 成本网格契约 / 刻度 / 徽章成本文案 | 已冻结 |
| 改裁判口径数字（夏普公式、印花税、因子 IC 公式） | 只加切窗与门禁聚合 |
| 改策略 alpha / 选股权重 | Product 边界 |
| 复活 GP；因子实验室多 horizon（已有独立切片） | 非本周 |
| 今日稳健摘要 | W46-3 |
| 强制改写每次普通 `/backtest` 为多窗（体积/耗时） | 独立端点；主回测可选附带 `oos` 单次块即可 |

---

## 2. 模块边界

### 2.1 后端

| 层 | 路径（建议） | 职责 |
|---|---|---|
| 切窗纯函数 | `backend/app/services/walk_forward.py`（新建） | 按 bars/日期生成窗口；**不**复制成交逻辑 |
| 策略窗裁判 | 同文件 `judge_strategy_window` / `aggregate_verdicts` | 每窗 verdict + 总门禁（≥ 单次） |
| 因子窗复用 | 可选调用 `_is_oos_split` 逻辑的多窗包装 `walk_forward_ic` | **本切片前端不挂 FactorMine**；函数可先私有/测用，避免两套符号判定 |
| Schema | `schemas.py` | `WalkForwardRequest` / `WalkForwardResult` / `WalkForwardWindow` |
| 路由 | `routers/strategy.py` | `POST /strategy/backtest/walk-forward`（+ 可选 async） |
| 引擎复用 | 现有 `_run_single` / `_run_portfolio`（与寻优 OOS 段同一入口） | 每窗两次（或一次 IS + 一次 OOS）回测；成本吃请求显式值或 `cost_defaults` |
| 单次对照 | 同服务内必算 `legacy_single` | 用同一 `oos_ratio` 做全区间一刀，供总门禁取严 |

### 2.2 前端

| 挂载点 | 说明 |
|---|---|
| `#btopt` | `App.tsx` → `BacktestOptimizePage` |
| `BacktestPage.tsx` | **单次回测** Tab；净值块与成本敏感块之后新增「滚动 OOS」块（三块之③） |
| `WalkForwardPanel.tsx`（建议新建） | 窗表 + 总 verdict + 与单次对照行 |
| `EvalBadges.tsx` | 读聚合后 `oos`；主文案不变，悬停多窗摘要 |
| `api/client.ts` | `walkForward(req)` |

**不改本切片**：`OptimizePage` 批量网格仍单次 `oos_ratio`（可灰字提示「多窗见单次回测复盘」）。

### 2.3 责任

| 角色 | 动作 |
|---|---|
| Desk | Product 冻结后实现；一次一项 Doing |
| QA | §6 验收 |
| Architect | 本文；冲突补丁 |
| Product | 冻结文案/默认窗数 |
| Planner | Ready 拉票 |

---

## 3. 滚动多窗定义

### 3.1 两种 mode

| `mode` | 行为 | `oos_ratio` 角色 |
|---|---|---|
| `single` | 全区间一刀：IS=`[start, split)`，OOS=`[split, end]`；`split` 按交易日数末段占比 | **切分比例**（与寻优/因子现状一致，默认 0.3，钳制 0~0.6） |
| `walk_forward` | 多窗滚动（默认本面板） | 仍写入响应/徽章；并用于 **`legacy_single` 对照切分**；窗生成见 §3.2 |

`oos_ratio=0` → 关闭：`oos.enabled=false`，verdict=`未启用`（与因子侧一致）。

### 3.2 `walk_forward` 默认方案：`scheme=expanding`（ROLL_EX）

对加载后的交易日序列 `bars[0..T)`（与 `_load_bars` / 寻优同一数据源）：

```text
min_is_bars  = 60          # 与寻优「样本内至少 30」同量级，WF 取更稳的 60
min_oos_bars = 20          # 与寻优「验证段太短则不分」对齐
n_windows    = 3           # 默认；允许 2..6，超限 400

# 从 index=min_is_bars 起，把剩余 (T - min_is_bars) 根 bar
# 切成 n_windows 段【互不重叠】的 OOS 切片：
#   window i:  IS = bars[0 : oos_start_i]
#              OOS = bars[oos_start_i : oos_end_i]
# 若某窗 OOS < min_oos_bars 或 IS < min_is_bars → 该窗 verdict=样本不足（仍返回行）
```

- **时间顺序硬约束**：IS 终点 < OOS 起点（对齐 `test_oos_split.py` 防泄漏断言）。
- **为何 expanding**：早期窗用短 IS、晚期窗用长 IS，专打「只在某一 regime 成立」；实现简单，对齐 Qlib `ROLL_EX` 直觉。
- **可选二期** `scheme=sliding`：固定 IS 长度 + 固定 OOS 长度 + step；本切片 UI 可不暴露。

### 3.3 与单次 `oos_ratio` 兼容

| 场景 | 行为 |
|---|---|
| `mode=single` 或 `n_windows=1` | 切点 = 寻优同款：`split_i = max(int(T*(1-r)), 30)`，且 OOS 段 ≥20 才启用；响应 `windows` 仅 1 行 |
| `mode=walk_forward` | 多窗 + **额外**计算 `legacy_single`（同上公式，不占窗表必显时可作对照行） |
| 请求省略 `oos_ratio` | 默认 `0.3` |
| 徽章字段 | 始终回传 `oos.oos_ratio`（即请求生效值） |

### 3.4 每窗策略 verdict（对齐因子四态，指标换回测）

主度量默认 **`sharpe`**（可请求 `metric=excess_return|sharpe`；默认 `sharpe`）。

| verdict | 条件（风控优先；**Product 冻结澄清**） |
|---|---|
| `样本不足` | OOS bars < `min_oos_bars`，或 OOS `trade_count` < 1 且无法形成收益序列，或 IS/OOS 指标为 null |
| `过拟合` | IS 主度量 > 0 **且** OOS 主度量 < 0 |
| `未通过` | 其余 OOS 主度量 < 0（含 IS≤0 ∧ OOS<0；不要求与 IS 同向才判未通过） |
| `通过` | OOS 主度量 ≥ 0（**不要求与 IS 同向**） |


> **冻结澄清**：通过只看 OOS≥0，不要求 IS/OOS 同向；过拟合专指「样本内赚钱、样本外亏」；其余 OOS<0 一律未通过。
> 因子 IC 路径若复用：继续用 `_is_oos_split` 的 t 阈值（\|t\|<1→未通过）；**本面板主路径是策略回测指标**，不把 IC t 套到净值上。

### 3.5 总门禁（必须 ≥ 单次）

```text
agg = merge(windows[*].verdict)
     优先级（从严到宽）：过拟合 > 未通过 > 样本不足 > 通过
     规则：
       · 任一窗 过拟合 → agg=过拟合
       · 否则任一窗 未通过 → agg=未通过
       · 否则若「通过」窗数 ≥ max(1, ceil(valid/2)) 且无 过拟合/未通过 → agg=通过
         （valid = 非「样本不足」的窗）
       · 否则 agg=样本不足

legacy = legacy_single.verdict   # 同区间、同 oos_ratio、同 metric

final = stricter(agg, legacy)    # 二者取更严；保证「多窗不得宽于单次」
```

`stricter` 序：`过拟合 > 未通过 > 样本不足 > 通过 > 未启用`。

---

## 4. 请求 / 响应 JSON

### 4.1 端点

```text
POST /api/strategy/backtest/walk-forward           # 同步（窗数少、短区间）
POST /api/strategy/backtest/walk-forward/async     # 可选；长区间 / 组合策略
GET  /api/strategy/backtest/tasks/{task_id}        # 复用任务查询
```

前缀与现网 `strategy` router、`/backtest/cost-sensitivity` 同级。

### 4.2 Request

```json
{
  "symbol": "sh600000",
  "start": "2019-01-01",
  "end": "2024-12-31",
  "strategy": "chan",
  "params": {},
  "initial_cash": 1000000.0,
  "commission": null,
  "slippage": null,
  "adj": "qfq",
  "mode": "walk_forward",
  "oos_ratio": 0.3,
  "n_windows": 3,
  "scheme": "expanding",
  "metric": "sharpe",
  "min_is_bars": 60,
  "min_oos_bars": 20
}
```

| 字段 | 规则 |
|---|---|
| 回测身份字段 | 与 `BacktestRequest` 同名同义 |
| `commission`/`slippage` | 可缺省 → `cost_defaults`；与徽章/W46-1 一致 |
| `mode` | `walk_forward` \| `single`；面板默认 `walk_forward` |
| `oos_ratio` | 默认 0.3；钳制 `[0, 0.6]`；`0`=关闭 |
| `n_windows` | 默认 3；仅 `walk_forward`；范围 1..6（1≡single 切窗） |
| `scheme` | 默认 `expanding`；本切片可只实现这一种 |
| `metric` | `sharpe` \| `excess_return`；默认 `sharpe` |
| 规模 | `n_windows>6` → 400；组合策略建议走 async |

### 4.3 Response

```json
{
  "mode": "walk_forward",
  "scheme": "expanding",
  "metric": "sharpe",
  "oos_ratio": 0.3,
  "n_windows": 3,
  "cost_profile": {
    "commission": 0.00025,
    "slippage": 0.0001,
    "stamp_tax": 0.0005,
    "min_commission": 5.0,
    "risk_free_rate": 0.025,
    "source": "default",
    "label": "万2.5+万1滑点+卖出万5印花税"
  },
  "oos": {
    "enabled": true,
    "mode": "walk_forward",
    "verdict": "通过",
    "oos_ratio": 0.3,
    "n_windows": 3,
    "n_pass": 2,
    "n_fail": 0,
    "n_overfit": 0,
    "n_insufficient": 1,
    "note": "聚合=通过；legacy 单次=通过；final=通过",
    "windows_summary": "3窗·2通过·1不足",
    "is": null,
    "oos": null
  },
  "legacy_single": {
    "split_date": "2023-03-15",
    "oos_start": "2023-03-16",
    "is": {"n_bars": 1020, "sharpe": 0.9, "total_return": 0.4, "excess_return": 0.05, "max_drawdown": -0.2, "trade_count": 30},
    "oos": {"n_bars": 420, "sharpe": 0.5, "total_return": 0.08, "excess_return": 0.02, "max_drawdown": -0.12, "trade_count": 12},
    "verdict": "通过",
    "note": "单次 oos_ratio=0.3 对照"
  },
  "windows": [
    {
      "index": 0,
      "is_start": "2019-01-02",
      "is_end": "2020-06-30",
      "oos_start": "2020-07-01",
      "oos_end": "2021-06-30",
      "is": {"n_bars": 360, "sharpe": 1.1, "total_return": 0.25, "excess_return": 0.06, "max_drawdown": -0.15, "trade_count": 18},
      "oos": {"n_bars": 240, "sharpe": 0.2, "total_return": 0.03, "excess_return": 0.01, "max_drawdown": -0.1, "trade_count": 8},
      "verdict": "通过",
      "note": "IS/OOS sharpe 同向且 OOS≥0"
    }
  ],
  "summary": {
    "final_verdict": "通过",
    "agg_verdict": "通过",
    "legacy_verdict": "通过",
    "gate_ok": true,
    "fragile_regime": false,
    "note": "fragile_regime=存在窗过拟合或 final 相对 legacy 降级"
  },
  "strategy_key": "chan",
  "symbol": "sh600000",
  "start": "2019-01-01",
  "end": "2024-12-31"
}
```

**体积护栏**：不返回每窗 `equity_curve` / `trades`（与 W46-1 网格一致）；需要净值 → 用户对某窗日期再跑普通 `/backtest`。

**`gate_ok`**：`final_verdict == "通过"`。  
**`fragile_regime`**：任一看 `过拟合`，或 `final` 严于 `legacy`（例如多窗抓到单次没抓到的失效）。

---

## 5. 与徽章 / `oos` 字段合并规则

### 5.1 扩展（只增不破）

在既有徽章 `oos` shape（设计 §5.2）上增加可选字段：

| 字段 | 说明 |
|---|---|
| `oos.mode` | `single` \| `walk_forward` \| 缺省视为 `single` |
| `oos.n_windows` / `n_pass` / … | 多窗计数；single 时 `n_windows=1` |
| `oos.windows_summary` | 短串，供悬停 |
| `oos.verdict` | **永远是 final**（聚合与 legacy 取严后） |
| `oos.oos_ratio` | **保留**，等于生效切分比例 |
| `oos.is` / `oos.oos` | single 时填两端 metrics；WF 时可为 `null` 或填 **最后一窗**（文档约定：WF 填最后一窗 OOS，避免前端旧逻辑空读崩溃） |

### 5.2 徽章文案（沿用 Product 短标签）

| 条件 | 主文案 | 悬停 |
|---|---|---|
| `enabled=false` | `OOS·未切` | 原因 note |
| WF / single | `OOS·` + `verdict`（如 `OOS·通过`） | `windows_summary` + `oos_ratio=30%`；WF 可前缀 `滚动·` |
| 非「通过」 | 警告色 | 同左 |

**不上**独立短标 `OOS·滚动`（与 W46-1「不上 `成本·网格`」同一产品口味）；模式差悬停区分。

### 5.3 与主回测结果区关系

| 场景 | 主结果区 `oos` |
|---|---|
| 仅跑普通 `/backtest` | 可无 `oos` 或仅服务端附 `mode=single` 轻量块（**非本切片必做**） |
| 跑 WF 面板成功 | 面板自带完整响应；可将 `oos`（final）**投影**到主区 EvalBadges，**不覆盖**净值曲线（净值仍属主回测） |
| 与成本网格同页 | 成本徽章仍跟主回测/`grid_pick`；OOS 徽章跟 WF final；互不改对方 source |

### 5.4 配置写回（产品「`oos_windows`」）

前端本地（或后续研究卡）可保存：

```json
{
  "oos_windows": {
    "mode": "walk_forward",
    "oos_ratio": 0.3,
    "n_windows": 3,
    "scheme": "expanding",
    "metric": "sharpe"
  }
}
```

本切片**不强制**新后端配置表；请求体即单一配置源。扫描/评分二期再共用。

---

## 6. 落地序与验收清单

### 6.1 前置

- W46-1 成本敏感网格已合 main（Planner 已确认）。
- Product **冻结**本文后 → Planner 拉 Desk Doing。

### 6.2 PR 切片建议

1. **`walk_forward.py` 切窗 + verdict 聚合纯函数** + 单测（不碰 DB）。  
2. **Schema + 同步 API**：循环 `_run_single`/`_run_portfolio`；强制 `legacy_single`；护栏 `n_windows≤6`。  
3. **前端** `WalkForwardPanel` 挂 `BacktestPage` 第三块；表列：窗序、IS/OOS 区间、主度量、verdict；头栏 final + legacy。  
4. **EvalBadges** 读扩展 `oos`（悬停 `windows_summary`）。  
5. **（可选）** async；组合策略 feature 提示。

**停**：不改 Optimize 嵌套 WF、不改 W46-1、不推远程、不开实盘。

### 6.3 可测验收清单（产品对齐）

- [ ] `mode=single`（或 `n_windows=1`）切点与现寻优公式一致（同 bars、同 `oos_ratio`）
- [ ] `oos_ratio` 默认 0.3；`0` → `enabled=false` / `未启用`
- [ ] `walk_forward` 默认 3 窗；IS→OOS 时间顺序无泄漏
- [ ] 每窗含 metrics + verdict；响应含 `legacy_single`
- [ ] `final_verdict = stricter(agg, legacy)`；存在「agg 通过但 legacy 过拟合 → final 过拟合」用例
- [ ] 任一窗符号相反 → 总评不得为「通过」
- [ ] 不返回每窗 equity_curve；`n_windows>6` → 400
- [ ] 徽章主文案 `OOS·{final}`；保留 `oos_ratio`；悬停含多窗摘要
- [ ] 挂载 `#btopt` 单次回测页第三块；不改成本网格行为
- [ ] 既有 `test_oos_split.py` / `test_cost_model.py` 仍绿
- [ ] 硬闸门：无实盘、无密钥、无 push

---

## 7. 风险与取舍

| 风险 | 缓解 | 回滚 |
|---|---|---|
| 多窗 ×2 次回测耗时 | 默认 3 窗；上限 6；长任务 async；不落曲线 | 面板默认改 `single` |
| 早期窗 IS 短 → 大量「样本不足」 | `min_is_bars=60`；汇总允许不足窗不计数进「通过」分母 | 降 `n_windows` 或提高起点 |
| 策略 verdict 与因子 IC 判定不完全同构 | 文档写清；符号相反仍共用「过拟合」语义 | 仅策略面板使用本裁判 |
| 用户以为 WF 替代主净值 | UI：净值块=主回测；WF 块标题「滚动验证（不改上方净值）」 | — |
| 与 W46-1 同页抢高 | 成本块保持；OOS 块在其下；小屏可折叠 | — |
| 误把 Optimize 网格做成 WF | 非目标写死；Avoid 组合爆炸 | — |

### 取舍说明

| 选择 | 理由 |
|---|---|
| 独立 `/walk-forward` 而非改每次 `/backtest` | 耗时可控；主路径不被强制放大 |
| expanding 先于 sliding | 实现短、对照 Qlib ROLL_EX、总门禁易含 legacy 单次 |
| final 取严于 legacy | 满足产品「总门禁 ≥ 现单次 OOS」 |
| 保留 `oos_ratio` 字段与 single mode | 兼容寻优/因子心智与徽章 |
| 不做寻优嵌套 WF | Product/Planner 非目标；爆炸组合 |
| 策略窗用 sharpe/excess 而非伪 t | 回测路径无 IC 序列；避免假精度 |

---

## 8. 附录：路径与现状速查

| 主题 | 路径 |
|---|---|
| 本契约 | `/workspace/docs-design-W46-2-walk-forward.md` |
| 产品 Ready | `/workspace/docs-next-batch-product-priority.md` |
| W46-1 成本网格 | `/workspace/docs-design-W46-1-cost-sensitivity.md` |
| 总设计 OOS/徽章 | `/workspace/docs-design-P0-P2-eval-factor-cost.md` §3.3 / §5.2 / §7 |
| REBUILD M3-3 | `quant-latest/docs/REBUILD-2026-10.md` |
| 因子单次 OOS | `backend/app/services/factor_mining.py` `_is_oos_split` |
| 因子 OOS 测试 | `backend/tests/test_oos_split.py` |
| 寻优单次 OOS | `schemas.OptimizeRequest.oos_ratio`；`routers/strategy.py` `_optimize_core` |
| 单次回测 | `POST /strategy/backtest`；`BacktestPage.tsx` |
| 路由壳 | `#btopt` → `BacktestOptimizePage` → 单次回测 Tab |
| 成本默认 | `cost_defaults` / W45-2 |

---

*文档结束。Product 冻结后 Desk 按 §6 开工；W46-3 今日稳健摘要另文。*
