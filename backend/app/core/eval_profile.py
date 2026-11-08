"""评估口径徽章字段组装（W45-badge / W45-5）。

在回测 / 挖矿 / 寻优 / paper 成功响应写入 cost_profile / valuation_pit /
pead_pit（及扩展 oos），供前端 EvalBadges 只读展示。

设计契约：docs/DESIGN-P0-P2-eval-factor-cost.md §5.2
硬约束：只增字段；数字从 cost_defaults 取；不改 alpha。
"""
from __future__ import annotations

from typing import Any, Mapping

from app.core.cost_defaults import (
    COMMISSION,
    MIN_COMMISSION,
    RISK_FREE_RATE,
    SLIPPAGE,
    STAMP_TAX,
)

COST_LABEL = "万2.5+万1滑点+卖出万5印花税"
PEAD_ALIGNED_WITH = "multi_factor._pit_earnings_surprise"


def cost_source_from_request(req: Any) -> str:
    """按 §5.2 判定 cost_profile.source（default / user / grid_pick）。

    - commission/slippage 均未显式传入 → default
    - 显式传入且 req.cost_source ∈ {user, grid_pick} → 用该值
    - 显式传入但无 cost_source → user
    - 数值恰好等于 defaults 且非 grid_pick → 归一为 default（在 build_cost_profile 侧）
    """
    fields = getattr(req, "model_fields_set", None)
    if fields is None:
        fields = getattr(req, "__fields_set__", set()) or set()
    explicit_cost = ("commission" in fields) or ("slippage" in fields)
    if not explicit_cost:
        return "default"
    cs = getattr(req, "cost_source", None)
    if cs in ("user", "grid_pick"):
        return cs
    return "user"


def build_cost_profile(
    commission: float | None = None,
    slippage: float | None = None,
    *,
    source: str = "default",
    stamp_tax: float | None = None,
    min_commission: float | None = None,
    risk_free_rate: float | None = None,
) -> dict[str, Any]:
    """组装 cost_profile；省略参数时落到 cost_defaults。

    W46-1：允许 source=grid_pick；数值等于默认且非 grid_pick 时归一为 default；
    grid_pick 的 label 加「网格选中·」前缀（主文案仍由前端显示「成本·自定义」）。
    """
    c = COMMISSION if commission is None else float(commission)
    s = SLIPPAGE if slippage is None else float(slippage)
    src = source if source in ("default", "user", "grid_pick") else "default"
    if src != "grid_pick" and c == COMMISSION and s == SLIPPAGE:
        src = "default"
    label = COST_LABEL
    if src == "grid_pick":
        label = f"网格选中·{COST_LABEL}"
    return {
        "commission": c,
        "slippage": s,
        "stamp_tax": STAMP_TAX if stamp_tax is None else float(stamp_tax),
        "min_commission": MIN_COMMISSION if min_commission is None else float(min_commission),
        "risk_free_rate": RISK_FREE_RATE if risk_free_rate is None else float(risk_free_rate),
        "source": src,
        "label": label,
    }


def pit_na() -> dict[str, Any]:
    return {"mode": "n/a"}


def build_valuation_pit(
    mode: str,
    *,
    coverage: float | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """mode ∈ asof|fallback|unavailable|n/a。"""
    out: dict[str, Any] = {"mode": mode, "note": note}
    if coverage is not None:
        out["coverage"] = float(coverage)
    elif mode != "n/a":
        out["coverage"] = 0.0
    return out


def build_pead_pit(
    mode: str,
    *,
    scale: str = "raw_pp",
    note: str | None = None,
) -> dict[str, Any]:
    """mode ∈ asof|legacy_static|unavailable|n/a。"""
    if mode == "n/a":
        return {"mode": "n/a"}
    return {
        "mode": mode,
        "scale": scale,
        "aligned_with": PEAD_ALIGNED_WITH,
        "note": note,
    }


def build_eval_profile_for_backtest(
    req: Any,
    *,
    multi_asset: bool,
    valuation_coverage: float | None = None,
) -> dict[str, Any]:
    """回测路径：单标的技术指标 → PIT n/a；组合/多因子 → 真实 mode。

    2026-11-08 起组合回测的估值已按日频序列取值（stock_valuation_daily，每 5 日
    采一点），不再用 stocks 最新快照。`valuation_coverage` 由装配层传入 ——
    有覆盖标 asof，没覆盖才降级 fallback（显式标注，勿静默）。
    PEAD 走 fundamentals 截面 as-of（W45-3），标 asof + raw_pp。
    """
    source = cost_source_from_request(req)
    cost = build_cost_profile(req.commission, req.slippage, source=source)
    if multi_asset:
        cov = float(valuation_coverage or 0.0)
        if cov > 0.5:
            valuation = build_valuation_pit(
                "asof", coverage=cov,
                note="估值取自 stock_valuation_daily 日频序列（每 5 日一点，按调仓日取最近值）",
            )
        else:
            valuation = build_valuation_pit(
                "fallback", coverage=cov,
                note="估值日频序列缺失或覆盖不足，退回 stocks 最新快照（有前视风险）",
            )
        pead = build_pead_pit("asof", scale="raw_pp")
    else:
        valuation = pit_na()
        pead = pit_na()
    return {
        "cost_profile": cost,
        "valuation_pit": valuation,
        "pead_pit": pead,
    }


def build_eval_profile_for_optimize(req: Any) -> dict[str, Any]:
    """寻优目前走单标的网格；PIT 为 n/a，成本与请求对齐。"""
    source = cost_source_from_request(req)
    return {
        "cost_profile": build_cost_profile(req.commission, req.slippage, source=source),
        "valuation_pit": pit_na(),
        "pead_pit": pit_na(),
    }


def build_eval_profile_for_paper(req: Any, *, multi_asset: bool) -> dict[str, Any]:
    source = cost_source_from_request(req)
    cost = build_cost_profile(req.commission, req.slippage, source=source)
    if multi_asset:
        valuation = build_valuation_pit(
            "asof",
            note="创建时仅回显；实际运行时按 stock_valuation_daily 日频序列取值",
        )
        pead = build_pead_pit("asof", scale="raw_pp")
    else:
        valuation = pit_na()
        pead = pit_na()
    return {
        "cost_profile": cost,
        "valuation_pit": valuation,
        "pead_pit": pead,
    }


def build_eval_profile_for_mining(
    *,
    valuation_mode: str,
    valuation_coverage: float,
    valuation_note: str | None = None,
) -> dict[str, Any]:
    """挖矿路径：成本用默认；估值按 PIT 载入结果；PEAD 已 asof+raw_pp。"""
    return {
        "cost_profile": build_cost_profile(source="default"),
        "valuation_pit": build_valuation_pit(
            valuation_mode,
            coverage=valuation_coverage,
            note=valuation_note,
        ),
        "pead_pit": build_pead_pit("asof", scale="raw_pp"),
    }


def merge_oos(existing: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """保留既有 oos key，仅保证是 dict；无则 None。"""
    if existing is None:
        return None
    return dict(existing)
