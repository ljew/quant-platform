# 归档说明

本文档已**全部落地或已失效**，归档备查。原文见下方。

| 文档 | 归档原因 | 现状 |
|---|---|---|
| `REFACTOR.md` | 状态标「待确认」，写于 2026-08。核心方案（数据分层 Bronze→Silver→Gold、投研流水线、因子工厂）**已全部落地**，路线图 Phase 1-5 不再适用 | ✅ 已实施，见 `architecture.html` |
| `research-card-v0.1.md` | 提案 v0.1「未实施」，但实际已落地且**超出提案范围**：`research_cards` 表 10 张卡、`research_runs` 238 次运行，字段比提案多了 `cost_note` / `crowd_note` / `fail_condition` | ✅ 已实施并演进，见 `research-card.html` |

---

## REFACTOR.md 的实际落地情况

2026-08 提出的五个 Phase 与当前状态对照：

| Phase | 提案内容 | 落地状态 |
|---|---|---|
| Phase 1 | 数据分层（Bronze raw/ → Silver → Gold） | ✅ `data/raw` / `data/silver` / SQLite+DuckDB 双库 |
| Phase 2 | 投研流水线（采集→清洗→打分） | ✅ `services/etl.py` + data_scheduler |
| Phase 3 | 因子工厂（DSL + 表达式引擎） | ✅ `core/engine/factor_expr.py`（29 函数 / 31 变量） |
| Phase 4 | 监控与血缘 | ✅ `routers/monitor.py` + health_rules 表 |
| Phase 5 | 研究卡机制 | ✅ 且已演进，见下 |

**未落地的原始设想**：提案中提到的「多数据源热切换」「因子版本管理」等，
现以更轻的方式实现（`datahub/sources.yaml` 配置驱动）。

---

## research-card v0.1 → 实际实现

| 提案字段 | 实际表字段 | 说明 |
|---|---|---|
| （8 项核心字段） | `card_type` `key` `name` `hypothesis` `data_spec` `evidence_json` `state` `history_json` | 全部落地 |
| — | `cost_note` | **新增**：交易成本假设（提案未含） |
| — | `crowd_note` | **新增**：拥挤度/同类信号重叠（提案未含） |
| — | `fail_condition` | **新增**：证伪条件（提案未含，M3 的 PBO 思路前置） |

状态机已从提案的 4 态扩展为实现版：`观察 → 候选 → 启用 → 降级`，
由 `services/research_card_svc.py` 驱动，与周复盘联动（2026-10-07/08 四个提交）。

**当前数据**：`research_cards` 10 张卡（9 策略 + 1 因子）· `research_runs` 238 次运行。

---

**相关文档**：架构现状见 `../REBUILD-2026-10.md` 与侧边栏 `architecture.html`
