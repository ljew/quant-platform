# W46-3 今日稳健摘要（轻量）· 产品落地要点

> 作者：Quant Desk · 2026-11-08 Asia/Shanghai  
> 定稿：`/workspace/docs-next-batch-product-priority.md` §2 W46-3（无独立长契约；可不另等 Architect）  
> 硬闸门：不开实盘；密钥不进源码；本地 commit 不 push；不新开驾驶舱枢纽。

## 挂载

| 项 | 值 |
|---|---|
| 页面 | `TodayPage`（今日观点） |
| 路由/hash | `#today`（默认首页 hub=`today` / sub=`today`） |
| 组件 | `TodayRobustnessCard`（「稳健摘要」） |
| 位置 | 「一句话观点」下、热点简报上 |

## 内容

1. **数据新鲜度**：只读 `GET /monitor/dataflow` 的 `gold.kline_latest` / `gold.factor_latest`（及行数）；无数据则空态，不伪造。
2. **最近评估徽章**：`GET /strategy/backtests?limit=5` → 最近一条 `GET /strategy/backtests/{id}`，渲染现有 `EvalBadges`（`cost_profile` / `valuation_pit` / `pead_pit` / `oos`）。无历史 → 空态文案。
3. **OOS/成本告警入口**：`deriveRobustnessAlerts`（前后端同口径）链到 `#btopt`（成本网格 / 滚动 OOS）或数据中心 `#datasys`。

## 非目标

模拟盘大改；新开枢纽；实盘；改 W46-1/2 契约行为。

## 测试

- `backend/tests/test_today_robustness.py`（告警推导）
- `npm run build`（前端类型/打包）
