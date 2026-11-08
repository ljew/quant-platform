"""W46-1 / M3-7 成本敏感网格。

展开 commission×slippage（或 multipliers）网格，循环调用既有回测入口汇总指标；
不复制成交逻辑，不返回每格 equity_curve / trades。

契约：docs/DESIGN-W46-1-cost-sensitivity.md
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from fastapi import HTTPException

from app.core.cost_defaults import COMMISSION, SLIPPAGE
from app.core.eval_profile import build_cost_profile
from app.schemas import (
    CostGridCell,
    CostSensitivityAxes,
    CostSensitivityRequest,
    CostSensitivityResult,
    CostSensitivitySummary,
)

# §3.2 默认刻度（必须含万2.5+万1）
DEFAULT_COMMISSIONS = [0.00015, 0.00025, 0.00030, 0.00050]
DEFAULT_SLIPPAGES = [0.0000, 0.0001, 0.0002, 0.0005]
DEFAULT_MULTIPLIERS = [1.0, 2.0]

MAX_GRID_CELLS = 16
MAX_MULTIPLIERS = 8

DEFAULT_C = COMMISSION  # 0.00025
DEFAULT_S = SLIPPAGE    # 0.0001


@dataclass(frozen=True)
class CellSpec:
    commission: float
    slippage: float
    multiplier: float | None
    is_default: bool
    in_axes: bool = True  # False = summary-only 临时格（如加倍未覆盖时）


Runner = Callable[[float, float], Any]
"""runner(commission, slippage) → 引擎结果对象（含 total_return/sharpe/...）。"""


def _approx(a: float, b: float, tol: float = 1e-12) -> bool:
    return abs(float(a) - float(b)) <= tol


def resolve_axes(req: CostSensitivityRequest) -> tuple[str, list[CellSpec], CostSensitivityAxes]:
    """展开扫描格；保证默认格 (万2.5, 万1) 必含且 is_default。"""
    mode = (req.mode or "grid").strip().lower()
    if mode not in ("grid", "multipliers"):
        raise HTTPException(status_code=400, detail=f"未知 mode: {req.mode}（仅 grid|multipliers）")

    if mode == "multipliers":
        base_c = DEFAULT_C if req.base_commission is None else float(req.base_commission)
        base_s = DEFAULT_S if req.base_slippage is None else float(req.base_slippage)
        mults = list(req.multipliers) if req.multipliers is not None else list(DEFAULT_MULTIPLIERS)
        if not mults:
            raise HTTPException(status_code=400, detail="multipliers 不能为空")
        if len(mults) > MAX_MULTIPLIERS:
            raise HTTPException(
                status_code=400,
                detail=f"multipliers 上限 {MAX_MULTIPLIERS}，当前 {len(mults)}",
            )
        specs: list[CellSpec] = []
        for m in mults:
            c = base_c * float(m)
            s = base_s * float(m)
            is_def = _approx(float(m), 1.0) and _approx(c, DEFAULT_C) and _approx(s, DEFAULT_S)
            # ×1 相对 base==defaults 时标默认；若 base 非 defaults，仍以数值匹配 defaults
            if _approx(c, DEFAULT_C) and _approx(s, DEFAULT_S):
                is_def = True
            specs.append(CellSpec(c, s, float(m), is_def, in_axes=True))
        if not any(sp.is_default for sp in specs):
            specs.append(CellSpec(DEFAULT_C, DEFAULT_S, None, True, in_axes=True))
        # 仅保留一格 is_default
        specs = _uniq_default(specs)
        axes = CostSensitivityAxes(commissions=None, slippages=None, multipliers=list(mults))
        return mode, specs, axes

    # grid
    commissions = list(req.commissions) if req.commissions is not None else list(DEFAULT_COMMISSIONS)
    slippages = list(req.slippages) if req.slippages is not None else list(DEFAULT_SLIPPAGES)
    if not commissions or not slippages:
        raise HTTPException(status_code=400, detail="commissions/slippages 不能为空")
    n = len(commissions) * len(slippages)
    # 自定义轴若缺默认，稍后追加；先按笛卡尔积计上限（追加后也可能超，但契约按积上限）
    if n > MAX_GRID_CELLS:
        raise HTTPException(
            status_code=400,
            detail=f"网格格数上限 {MAX_GRID_CELLS}（当前 {len(commissions)}×{len(slippages)}={n}）",
        )
    specs = []
    has_default = False
    for c in commissions:
        for s in slippages:
            is_def = _approx(c, DEFAULT_C) and _approx(s, DEFAULT_S)
            if is_def:
                has_default = True
            specs.append(CellSpec(float(c), float(s), None, is_def, in_axes=True))
    if not has_default:
        specs.append(CellSpec(DEFAULT_C, DEFAULT_S, None, True, in_axes=True))
        # 追加后若超过上限仍允许（契约：缺默认必须追加）；轴展示用原始轴 + 保证单元格含默认
        if len(specs) > MAX_GRID_CELLS + 1:
            raise HTTPException(
                status_code=400,
                detail=f"网格格数上限 {MAX_GRID_CELLS}（含强制默认格后 {len(specs)}）",
            )
    specs = _uniq_default(specs)
    axes = CostSensitivityAxes(
        commissions=[float(x) for x in commissions],
        slippages=[float(x) for x in slippages],
        multipliers=None,
    )
    return mode, specs, axes


def _uniq_default(specs: list[CellSpec]) -> list[CellSpec]:
    """同一 (c,s) 去重；确保恰好一格 is_default（优先数值匹配 defaults）。"""
    seen: dict[tuple[float, float], CellSpec] = {}
    for sp in specs:
        key = (round(sp.commission, 12), round(sp.slippage, 12))
        prev = seen.get(key)
        if prev is None:
            seen[key] = sp
        else:
            # 合并：保留 is_default / in_axes 真值
            seen[key] = CellSpec(
                sp.commission,
                sp.slippage,
                sp.multiplier if sp.multiplier is not None else prev.multiplier,
                prev.is_default or sp.is_default,
                prev.in_axes or sp.in_axes,
            )
    out = list(seen.values())
    # 归一 is_default：仅 defaults 那格
    normed: list[CellSpec] = []
    for sp in out:
        is_def = _approx(sp.commission, DEFAULT_C) and _approx(sp.slippage, DEFAULT_S)
        normed.append(CellSpec(sp.commission, sp.slippage, sp.multiplier, is_def, sp.in_axes))
    if not any(sp.is_default for sp in normed):
        normed.append(CellSpec(DEFAULT_C, DEFAULT_S, None, True, in_axes=True))
    return normed


def extract_metrics(result: Any) -> dict[str, float | int]:
    """从引擎结果提取最小指标集；不带 equity_curve/trades。"""
    total_return = float(getattr(result, "total_return", 0.0) or 0.0)
    annual_return = float(getattr(result, "annual_return", 0.0) or 0.0)
    sharpe = float(getattr(result, "sharpe", 0.0) or 0.0)
    max_drawdown = float(getattr(result, "max_drawdown", 0.0) or 0.0)
    trade_count = int(getattr(result, "trade_count", 0) or 0)
    final_equity = float(getattr(result, "final_equity", 0.0) or 0.0)
    excess = getattr(result, "excess_return", None)
    if excess is None:
        # 单标的引擎常未汇总 excess：用净值点 benchmark 推
        eq = getattr(result, "equity_curve", None) or []
        first = next((p for p in eq if getattr(p, "benchmark", 0)), None)
        last = next((p for p in reversed(eq) if getattr(p, "benchmark", 0)), None)
        if first and last and first.benchmark:
            bench_total = last.benchmark / first.benchmark - 1
            excess = total_return - bench_total
        else:
            excess = 0.0
    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "excess_return": float(excess or 0.0),
        "trade_count": trade_count,
        "final_equity": final_equity,
    }


def _find_cell(
    cells: Iterable[CostGridCell],
    c: float,
    s: float,
) -> CostGridCell | None:
    for cell in cells:
        if _approx(cell.commission, c) and _approx(cell.slippage, s):
            return cell
    return None


def build_summary(
    cells: list[CostGridCell],
    *,
    double_excess: float | None,
) -> CostSensitivitySummary:
    """§4.3 summary 公式。"""
    default = next((c for c in cells if c.is_default), None)
    if default is None:
        default = _find_cell(cells, DEFAULT_C, DEFAULT_S)
    default_excess = float(default.excess_return) if default is not None else 0.0
    default_ok = default is not None and default_excess > 0
    if double_excess is None:
        # 网格内找 (2c, 2s)
        if default is not None:
            dbl = _find_cell(cells, default.commission * 2, default.slippage * 2)
            double_excess = float(dbl.excess_return) if dbl is not None else None
    double_pos = bool(double_excess is not None and double_excess > 0)
    fragile = bool(default_ok and (double_excess is not None) and double_excess <= 0)
    note = "×2 相对默认格：commission/slippage 均×2 后 excess_return 仍 > 0"
    if fragile:
        note = "仅低摩擦成立：默认格超额>0，但成本翻倍后超额≤0"
    elif double_excess is None:
        note = "未能计算加倍格超额（无默认格或加倍跑失败）"
    elif not default_ok:
        note = "默认格超额≤0；加倍验收不适用"
    return CostSensitivitySummary(
        default_cell_ok=default_ok,
        double_cost_excess_positive=double_pos,
        fragile=fragile,
        note=note,
    )


def run_cost_sensitivity(
    req: CostSensitivityRequest,
    runner: Runner,
) -> CostSensitivityResult:
    """展开网格 → 逐格 runner → 汇总；必要时临时加跑加倍格（可不计入轴）。

    本切片不做 async；长区间/组合策略请自行控制或后续加 /async。
    """
    mode, specs, axes = resolve_axes(req)
    metric = (req.metric or "excess_return").strip()
    if metric not in ("excess_return", "sharpe", "total_return"):
        raise HTTPException(status_code=400, detail=f"未知 metric: {req.metric}")

    cells: list[CostGridCell] = []
    metrics_by_key: dict[tuple[float, float], dict[str, float | int]] = {}

    for sp in specs:
        key = (round(sp.commission, 12), round(sp.slippage, 12))
        if key not in metrics_by_key:
            raw = runner(sp.commission, sp.slippage)
            metrics_by_key[key] = extract_metrics(raw)
        m = metrics_by_key[key]
        cells.append(
            CostGridCell(
                commission=sp.commission,
                slippage=sp.slippage,
                multiplier=sp.multiplier,
                is_default=sp.is_default,
                total_return=float(m["total_return"]),
                annual_return=float(m["annual_return"]),
                sharpe=float(m["sharpe"]),
                max_drawdown=float(m["max_drawdown"]),
                excess_return=float(m["excess_return"]),
                trade_count=int(m["trade_count"]),
                final_equity=float(m["final_equity"]),
            )
        )

    # summary 加倍格：相对默认 (c,s) → (2c, 2s)
    default_cell = next((c for c in cells if c.is_default), None)
    double_excess: float | None = None
    if default_cell is not None:
        dc, ds = default_cell.commission * 2, default_cell.slippage * 2
        existing = _find_cell(cells, dc, ds)
        if existing is not None:
            double_excess = float(existing.excess_return)
        else:
            key = (round(dc, 12), round(ds, 12))
            try:
                if key not in metrics_by_key:
                    raw = runner(dc, ds)
                    metrics_by_key[key] = extract_metrics(raw)
                double_excess = float(metrics_by_key[key]["excess_return"])
            except Exception:  # noqa: BLE001
                double_excess = None

    summary = build_summary(cells, double_excess=double_excess)
    base = build_cost_profile(DEFAULT_C, DEFAULT_S, source="default")
    return CostSensitivityResult(
        mode=mode,
        metric=metric,
        base_cost_profile=base,
        axes=axes,
        cells=cells,
        summary=summary,
        strategy_key=req.strategy,
        symbol=req.symbol,
        start=req.start,
        end=req.end,
    )
