# 技术契约：W47-1 模拟盘体验包（小步）

> 作者：Quant Architect · 2026-11-08（Asia/Shanghai）  
> 看板/产品：`/workspace/docs-next-batch-product-priority.md` §2 **W47-1** · Ready 序 **W47-1 → W47-2 → W47-3**  
> 设计锚点：`/workspace/docs-design-P0-P2-eval-factor-cost.md` §2.1 / §3.1 / §5.2–5.4 / 附录 A；W45-badge / W45-2 已合 main  
> 对照体例：`docs-design-W46-1-cost-sensitivity.md`、`docs-design-W46-2-walk-forward.md`  
> **硬闸门**：不开实盘；密钥不进源码；不擅自推远程；**本文只写设计，不改业务代码。**  
> **冻结状态（2026-11-08）**：**Product 已冻结**。通过=创建前默认成本徽章 + 口径句「与回测默认成本同契约，非券商实盘」+ detail 回显 commission/slippage/cost_profile + 四条前端校验；非目标券商/大改 UI/实盘。Planner 可立刻拉 Desk；Architect 可开写 W47-2。  
> 读码基线：本机 `~/quant-platform` tip `6470e2f`（W46-3 已合）；box 快照 `/workspace/quant-latest` 可能滞后，**以本机仓为准**。

---

## 1. 背景 / 目标 / 非目标

### 1.1 背景

- W45-2 已把 `PaperTaskCreate` / `PaperTask` ORM / 引擎默认对齐 `cost_defaults`（万2.5+万1）；W45-badge 已在 `#paper` 创建成功路径挂 `EvalBadges`；`POST /paper/tasks` 已回显 `commission`/`slippage`/`cost_profile`。
- 产品验收仍缺体验闭环：创建区**创建前**看不见同回测成本；无「非券商实盘」口径句；**详情**接口/UI 不回显费率或 `cost_profile`；创建缺省策略/标的仅靠后端 400，前端无友好提示。
- 读码现状（`~/quant-platform`）：
  - `web/src/pages/PaperPage.tsx`：创建后 `setCreateBadges` → `EvalBadges`；`PageHeader.desc` 无成本口径；`DetailPanel` 无徽章/佣金滑点 Metric。
  - `backend/app/routers/paper.py`：create 已 `**build_eval_profile_for_paper(...)`；`GET /tasks/{id}` 仅透传 `get_paper_task_detail`。
  - `backend/app/core/engine/paper_engine.py`：`get_paper_task_detail` **未**返回 `commission`/`slippage`/`cost_profile`；`Trade.commission` 写入 `PaperTrade.commission`（**不含**印花税分项）。
  - `web/src/api/client.ts`：`PaperTask` 已有可选 `cost_profile`；`PaperDetail` **无**成本字段。

### 1.2 目标

1. `#paper` **创建区与详情**强制可见与回测同一 `cost_profile`（万2.5+万1+印花税万5）徽章或等价文案。  
2. 固定一句话口径：**「与回测默认成本同契约，非券商实盘」**。  
3. 详情能看到成本相关字段回显（佣金/滑点费率 **或** 累计费用其一即可；本切片主选费率回显）。  
4. 创建摩擦小修：缺省策略/标的（及组合指数）前端提示或校验文案；**不重做布局**。

### 1.3 非目标（本切片刻意不做）

| 不做 | 归属 |
|---|---|
| 券商接入 / 实盘 / live 下单 | 硬闸门 |
| 大改持仓 UI / 成交表大改版 | Product 边界 |
| 镜像组合与模拟盘任务合并 | 非目标 |
| 新建完整「费用汇总引擎」或把印花税拆进 `PaperTrade` 新列 | 超出小步；见 §4.3 |
| 改 `cost_defaults` 数字 / 徽章附录 A 短标签语义 | W45/W46 已冻 |
| 成本敏感网格挂到 Paper | W46-1 挂 `#btopt`；本切片只读徽章 |
| 改策略 alpha / 选股权重 | Product 边界 |
| W47-2 正交化 / W47-3 多重检验 | 后续切片 |

---

## 2. 模块边界

### 2.1 后端（真实路径）

| 层 | 路径 | 本切片职责 |
|---|---|---|
| 常量 | `backend/app/core/cost_defaults.py` | **只读**；`COMMISSION=0.00025` / `SLIPPAGE=0.0001` / `STAMP_TAX=0.0005` |
| 徽章组装 | `backend/app/core/eval_profile.py` | 复用 `build_cost_profile` / `build_eval_profile_for_paper`；详情侧可对已落库 task 调 `build_cost_profile(task.commission, task.slippage)` |
| 路由 | `backend/app/routers/paper.py` | create 已回显 → 保持；**扩展** detail（及可选 list）回显成本字段 |
| 引擎详情 | `backend/app/core/engine/paper_engine.py` → `get_paper_task_detail` | **本切片新增**返回 `commission`/`slippage`/`cost_profile`（及可选累计佣金） |
| ORM | `backend/app/models.py` → `PaperTask` | 已有 `commission`/`slippage` 列；**不**加新列、不 migration |
| 成交 | `PaperTrade.commission` | 可选聚合为 `fees_paid_commission`；印花税未单列存储 |

### 2.2 前端（真实路径）

| 挂载点 | 说明 |
|---|---|
| `#paper` → `web/src/pages/PaperPage.tsx` | 创建区预览徽章 + 口径句；成功提示保留/加强；`DetailPanel` 挂 `EvalBadges` + 费率 Metric；创建前校验文案 |
| `web/src/components/EvalBadges.tsx` | **复用**；短标对齐附录 A（`成本·默认万2.5+万1` / 悬停 `label`） |
| `web/src/api/client.ts` | `PaperDetail` **本切片新增**可选 `commission`/`slippage`/`cost_profile`（及可选 `fees_paid_commission`） |

### 2.3 责任

| 角色 | 动作 |
|---|---|
| Desk | Product 冻结后实现；一次一项 Doing；约 0.5–1 Desk 日 |
| QA | §7 验收清单 |
| Architect | 本文；冲突补丁 |
| Product | 冻结文案/验收勾选 |
| Planner | 冻结后拉 Ready → Doing；不占当前 Desk Doing（契约阶段） |

---

## 3. 成本口径与文案

### 3.1 数字（与回测同一真相）

| 参数 | 值 | 来源 |
|---|---|---|
| commission | `0.00025` | `cost_defaults.COMMISSION` |
| slippage | `0.0001` | `cost_defaults.SLIPPAGE` |
| stamp_tax | `0.0005` | `cost_defaults.STAMP_TAX`（引擎卖出收取；报告写入 `cost_profile`） |
| min_commission | `5.0` | `cost_defaults` |
| risk_free_rate | `0.025` | `cost_defaults`（纸面可不展示） |

规则：创建请求**省略** `commission`/`slippage` → 必须落到上表（W45-2 已护栏）；显式传入则 `source=user`，徽章走「成本·自定义」。本切片 UI **默认不暴露**成本输入框（避免摩擦）；高级覆盖保留 API 能力即可。

### 3.2 固定口径句（产品定稿，勿改字）

> **与回测默认成本同契约，非券商实盘**

挂载建议（任选其一级联，但创建区与详情**至少各出现一次**可见）：

1. 创建表单上方/按钮旁灰字一行；  
2. `PageHeader.desc` 末尾追加同句（可缩短为半句，但全文必须在创建区可见）；  
3. 详情标题旁或徽章旁灰字。

### 3.3 徽章短标（附录 A / Product 已定）

| `cost_profile.source` | Badge 主文案 | 悬停 |
|---|---|---|
| `default` | `成本·默认万2.5+万1` | `cost_profile.label` = `万2.5+万1滑点+卖出万5印花税` |
| `user` / `grid_pick` | `成本·自定义` | `label`（`grid_pick` 可带「网格选中·」前缀，与 W46-1 一致） |

纸面默认创建 → `source=default`，与回测同契约。`EvalBadges` 现实现若写成 `成本·默认万2.5+万1` 拼接逻辑，Desk 核对与附录 A 一致即可；**本切片不改短标语义**。

---

## 4. API / 响应字段

### 4.1 已有（保持，勿回退）

**`POST /paper/tasks`**（`PaperTaskCreate`）

- 默认：`commission=COMMISSION`，`slippage=SLIPPAGE`（`routers/paper.py`）。  
- 校验（已有）：单标的空 `symbols` → 400「单标的任务需要填写标的(symbols)」；组合缺指数 → 400；未知策略 / 重名 → 400。  
- 响应（已有，保持）：

```json
{
  "ok": true,
  "id": 12,
  "commission": 0.00025,
  "slippage": 0.0001,
  "cost_profile": {
    "commission": 0.00025,
    "slippage": 0.0001,
    "stamp_tax": 0.0005,
    "min_commission": 5.0,
    "risk_free_rate": 0.025,
    "source": "default",
    "label": "万2.5+万1滑点+卖出万5印花税"
  },
  "valuation_pit": { "mode": "n/a" },
  "pead_pit": { "mode": "n/a" }
}
```

（组合任务 `valuation_pit`/`pead_pit` 按现有 `build_eval_profile_for_paper`。）

### 4.2 本切片新增：详情回显

**`GET /paper/tasks/{task_id}`** → 在现有 `get_paper_task_detail` 返回上**只增字段**：

| 字段 | 类型 | 含义 |
|---|---|---|
| `commission` | float | 任务落库费率 |
| `slippage` | float | 任务落库滑点 |
| `cost_profile` | object | 与 §5.2 / create 同 shape；`build_cost_profile(task.commission, task.slippage)`；数值等于 defaults → `source=default` |
| `fees_paid_commission` | float \| 可选 | `sum(PaperTrade.commission)`；**仅佣金累计**，不含印花税分项 |

**不做**（本切片）：新费用汇总引擎、把 stamp 回填进历史 `PaperTrade`、改成交表 schema。

若 Desk 选「只回显费率、不做累计」：可省略 `fees_paid_commission`，产品验收③仍满足（佣金/滑点其一即可）。**推荐最小实现 = 费率三字段 + `cost_profile`；累计佣金为加分项（≤10 行）**。

### 4.3 列表（可选薄改）

`GET /paper/tasks` 卡片可不挂徽章。可选回显 `commission`/`slippage` 供调试；**非验收必做**。

### 4.4 前端类型（本切片新增）

`web/src/api/client.ts` → `PaperDetail` 增：

```ts
commission?: number;
slippage?: number;
cost_profile?: CostProfile | null;
fees_paid_commission?: number; // 可选
```

`PaperTrade` 类型可补 `commission?: number`（详情表已有后端字段，前端成交表本切片**可不展示佣金列**——避免大改 UI）。

---

## 5. 前端挂载

### 5.1 创建区（强制可见）

| 元素 | 行为 |
|---|---|
| 预览徽章 | 页面加载后、**创建前**即渲染一枚基于默认 `cost_profile` 的 `EvalBadges`（可本地常量：`{ commission: 0.00025, slippage: 0.0001, stamp_tax: 0.0005, source: "default", label: "万2.5+万1滑点+卖出万5印花税" }`，或轻量 `build` 辅助；无需为预览打 API） |
| 口径句 | 紧邻预览徽章灰字：`与回测默认成本同契约，非券商实盘` |
| 创建成功 | 保持现有 `createBadges`（吃 create 响应真实 `cost_profile`）；可追加一行成功提示如「已按回测默认成本建账」+ 同口径句 |
| 布局 | 不重做 grid；在现有表单与按钮之间插入 1 行徽章+文案即可 |

### 5.2 详情区

| 元素 | 行为 |
|---|---|
| `EvalBadges` | `DetailPanel` 标题行下方挂载；`cost_profile={detail.cost_profile}`；PIT 可传 `n/a` 或不传 |
| 费率 Metric | 在净值/收益率旁增加「佣金」「滑点」或合并一枚「成本口径」Metric（如 `万2.5 / 万1`）；数据来自 `detail.commission`/`slippage` 或 `cost_profile` |
| 可选 | `fees_paid_commission` → Metric「累计佣金」；旁注「不含印花税分项」灰字 |
| 成交表 | **不强制**加佣金列（非目标：大改 UI） |

### 5.3 创建摩擦小修（具体文案）

在 `create()` 开头、调 API 前：

| 条件 | 前端提示（建议原文） |
|---|---|
| `strategies` 仍空或 `!skey` | `请先选择策略（策略列表加载中或尚未选择）` |
| `!isPortfolio && !symbols.trim()` | `请填写标的代码，例如 sh600519` |
| `isPortfolio && !indexCode.trim()` | `请填写指数代码，例如 000906` |
| `!Number.isFinite(cash) \|\| cash <= 0` | `初始资金须为正数` |

后端 400 文案保持；前端先挡一层，减少「点了没反应/只见英文堆栈」感。不改布局、不加重表单控件。

---

## 6. 与引擎关系（只读说明）

- `paper_engine._load_single` / `_load_portfolio`：`BacktestEngine` / `PortfolioBacktestEngine(..., commission=task.commission, slippage=task.slippage)`；印花税吃引擎构造默认 `stamp_tax=0.0005`（与 `cost_defaults` 一致）。  
- 纸面与回测共用撮合/成本模型（文件头注释已声明）——口径句的技术依据。  
- `Trade.commission` 仅记佣金；卖出印花税扣减现金但不进 `PaperTrade.commission` → 故累计字段命名必须诚实。

---

## 7. 门禁 / 验收清单

### 7.1 前置

- W46 齐；W45-badge 已在 `#paper`；`cost_defaults` + create 回显已存在。  
- Product **冻结**本文后 → Planner 拉 Desk Doing。

### 7.2 可勾选验收（产品对齐）

- [ ] 创建区**创建前**可见成本徽章（默认万2.5+万1）或等价文案  
- [ ] 创建区可见固定句：**与回测默认成本同契约，非券商实盘**  
- [ ] 创建成功后徽章与 create 响应 `cost_profile` 一致（`source=default` 时与回测同）  
- [ ] 详情可见 `cost_profile` 徽章 **或** 佣金/滑点费率回显（其一即可；推荐两者都有）  
- [ ] 省略成本字段创建 → 落库/引擎仍为 `0.00025`/`0.0001`（既有 `test_api_default_cost` 仍绿）  
- [ ] 缺策略/空标的（或空指数）前端有中文提示，不重做布局  
- [ ] 无券商接入、无持仓 UI 大改、无镜像组合合并、无实盘、无密钥、无 push  
- [ ] 既有 paper / cost / badge 相关测试仍绿  

### 7.3 建议补测（薄）

- 单元/API：`get_paper_task_detail`（或 TestClient GET detail）含 `commission`/`slippage`/`cost_profile`。  
- 可选：`fees_paid_commission == sum(trades.commission)`。

---

## 8. 落地顺序（约 0.5–1 Desk 日）

1. **后端详情回显**（`paper_engine.get_paper_task_detail` + 必要时 router 透传）：`commission`/`slippage`/`cost_profile`；（可选）`fees_paid_commission`。  
2. **前端类型** `PaperDetail` 对齐。  
3. **创建区**：预览徽章 + 口径句；保留成功 `createBadges`。  
4. **详情区**：`EvalBadges` + 费率 Metric。  
5. **创建校验文案**四条。  
6. **QA** 跑 §7；本地 commit，不 push。

**停**：不改成本数字、不改持仓/镜像、不开实盘、不推远程。

---

## 9. 风险与后续

| 风险 | 缓解 | 回滚 |
|---|---|---|
| 用户误以为模拟盘=券商实盘 | 固定口径句双挂载（创建+详情） | 加强 `PageHeader` |
| 详情无 `cost_profile` 时徽章空白 | 后端必填组装；前端无数据时回退本地默认徽章并灰字「费率待刷新」 | — |
| 累计佣金被当成含印花税 | 字段名 `fees_paid_commission` + UI 旁注；或本切片不做累计 | 去掉 Metric |
| box `/workspace/quant-latest` 滞后导致误读旧默认 | 以 `~/quant-platform` tip 为准；契约已写明 | — |
| 前端预览徽章与真实 create 不一致 | 预览只用 defaults；成功后再用响应覆盖 | — |

### 关键取舍

| 选择 | 理由 |
|---|---|
| 详情补 `cost_profile` 而非新费用引擎 | 验收允许「佣金/滑点」；复用 `build_cost_profile`；0.5 日可完 |
| 创建前本地预览徽章 | 不增 API；强制「创建前可见」 |
| 固定口径句不进 Badge 主文案 | 徽章跟附录 A；口径句独立灰字，避免短标膨胀 |
| 成交表不加佣金列 | 产品非目标「不大改 UI」；费率 Metric 已够验收 |
| 不做券商/镜像合并 | Product 非目标写死 |

### W47-2 指针

冻结本切片后并行起草：**W47-2 / M3-4 正交化进挖矿主流程**（`#factorlab` 残差化后再评 IC；报告 `ic_raw`/`ic_resid`；非本文件范围）。

---

## 10. 附录：路径与现状速查

| 主题 | 路径 |
|---|---|
| 本契约（box） | `/workspace/docs-design-W47-1-paper-ux.md` |
| 本契约（本机） | `~/quant-platform/docs/DESIGN-W47-1-paper-ux.md` |
| 产品 Ready | `/workspace/docs-next-batch-product-priority.md` §2 W47-1 |
| 总设计成本/徽章 | `docs/DESIGN-P0-P2-eval-factor-cost.md` §3.1 / §5 / 附录 A |
| 成本默认 | `backend/app/core/cost_defaults.py` |
| 徽章组装 | `backend/app/core/eval_profile.py` |
| Paper 路由 | `backend/app/routers/paper.py` |
| 纸面引擎 | `backend/app/core/engine/paper_engine.py` |
| ORM | `backend/app/models.py` → `PaperTask` / `PaperTrade` |
| 前端页 | `web/src/pages/PaperPage.tsx` |
| 徽章组件 | `web/src/components/EvalBadges.tsx` |
| API 客户端 | `web/src/api/client.ts` |
| 成本护栏测 | `backend/tests/test_api_default_cost.py`（及 `test_cost_model.py`） |

---

*文档结束。Product 冻结后 Desk 按 §8 开工；W47-2 正交化另文。*
