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
    """请求是否显式传了 commission/slippage → user，否则 default。"""
    fields = getattr(req, "model_fields_set", None)
    if fields is None:
        fields = getattr(req, "__fields_set__", set()) or set()
    if "commission" in fields or "slippage" in fields:
        return "user"
    return "default"


def build_cost_profile(
    commission: float | None = None,
    slippage: float | None = None,
    *,
    source: str = "default",
    stamp_tax: float | None = None,
    min_commission: float | None = None,
    risk_free_rate: float | None = None,
) -> dict[str, Any]:
    """组装 cost_profile；省略参数时落到 cost_defaults。"""
    c = COMMISSION if commission is None else float(commission)
    s = SLIPPAGE if slippage is None else float(slippage)
    src = source if source in ("default", "user") else "default"
    return {
        "commission": c,
        "slippage": s,
        "stamp_tax": STAMP_TAX if stamp_tax is None else float(stamp_tax),
        "min_commission": MIN_COMMISSION if min_commission is None else float(min_commission),
        "risk_free_rate": RISK_FREE_RATE if risk_free_rate is None else float(risk_free_rate),
        "source": src,
        "label": COST_LABEL,
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
) -> dict[str, Any]:
    """回测路径：单标的技术指标 → PIT n/a；组合/多因子 → 真实 mode。

    组合回测当前 attributes 仍读 stocks 最新快照（非 StockValuationDaily），
    故 valuation_pit=fallback（W45-5 显式标注，勿静默）。
    PEAD 走 fundamentals 截面 as-of（W45-3），标 asof + raw_pp。
    """
    source = cost_source_from_request(req)
    cost = build_cost_profile(req.commission, req.slippage, source=source)
    if multi_asset:
        valuation = build_valuation_pit(
            "fallback",
            coverage=0.0,
            note="组合回测 attributes 仍取 stocks 最新快照；估值日频 PIT 未接入本路径",
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
            "fallback",
            note="纸面组合任务沿用回测属性口径；创建时仅回显成本徽章",
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
