"""W46-1 / M3-7 成本敏感网格验收（契约 §6.2）。

引擎用 mock runner，不打真实行情；summary / 护栏 / 默认格可测。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.cost_defaults import COMMISSION, SLIPPAGE
from app.core.eval_profile import build_cost_profile, cost_source_from_request
from app.schemas import BacktestRequest, CostSensitivityRequest
from app.services.cost_sensitivity import (
    DEFAULT_COMMISSIONS,
    DEFAULT_SLIPPAGES,
    MAX_GRID_CELLS,
    extract_metrics,
    resolve_axes,
    run_cost_sensitivity,
)


def _mk_result(*, excess: float, total: float = 0.1, sharpe: float = 0.5) -> SimpleNamespace:
    return SimpleNamespace(
        total_return=total,
        annual_return=total / 3,
        sharpe=sharpe,
        max_drawdown=-0.1,
        excess_return=excess,
        trade_count=10,
        final_equity=1_100_000.0,
        equity_curve=[],
    )


class TestResolveAxes:
    def test_default_grid_contains_wan25_wan1(self):
        req = CostSensitivityRequest(
            symbol="sh600000", start="2020-01-01", end="2020-12-31", strategy="chan",
        )
        mode, specs, axes = resolve_axes(req)
        assert mode == "grid"
        assert axes.commissions == DEFAULT_COMMISSIONS
        assert axes.slippages == DEFAULT_SLIPPAGES
        defaults = [s for s in specs if s.is_default]
        assert len(defaults) == 1
        assert abs(defaults[0].commission - 0.00025) < 1e-12
        assert abs(defaults[0].slippage - 0.0001) < 1e-12
        assert len(specs) == 16

    def test_custom_axes_missing_default_still_appends(self):
        req = CostSensitivityRequest(
            symbol="sh600000", start="2020-01-01", end="2020-12-31", strategy="chan",
            commissions=[0.00015, 0.0005],
            slippages=[0.0, 0.0005],
        )
        _, specs, axes = resolve_axes(req)
        assert (0.00025, 0.0001) not in [
            (round(c, 12), round(s, 12))
            for c in axes.commissions for s in axes.slippages
        ]
        assert any(s.is_default for s in specs)
        d = next(s for s in specs if s.is_default)
        assert abs(d.commission - COMMISSION) < 1e-12
        assert abs(d.slippage - SLIPPAGE) < 1e-12

    def test_grid_over_16_raises_400(self):
        req = CostSensitivityRequest(
            symbol="sh600000", start="2020-01-01", end="2020-12-31", strategy="chan",
            commissions=[0.0001, 0.0002, 0.0003, 0.0004, 0.0005],
            slippages=[0.0, 0.0001, 0.0002, 0.0003],
        )
        with pytest.raises(HTTPException) as ei:
            resolve_axes(req)
        assert ei.value.status_code == 400
        assert str(MAX_GRID_CELLS) in str(ei.value.detail)

    def test_multipliers_default_1_and_2(self):
        req = CostSensitivityRequest(
            symbol="sh600000", start="2020-01-01", end="2020-12-31", strategy="chan",
            mode="multipliers",
        )
        mode, specs, axes = resolve_axes(req)
        assert mode == "multipliers"
        assert axes.multipliers == [1.0, 2.0]
        assert len(specs) == 2
        m1 = next(s for s in specs if s.multiplier == 1.0)
        assert abs(m1.commission - COMMISSION) < 1e-12
        assert abs(m1.slippage - SLIPPAGE) < 1e-12
        assert m1.is_default


class TestSummaryAndRun:
    def test_fragile_when_double_collapses(self):
        """默认 excess>0，加倍 excess≤0 → fragile。"""
        def runner(c, s):
            # 默认格正超额；加倍塌陷
            if abs(c - COMMISSION) < 1e-12 and abs(s - SLIPPAGE) < 1e-12:
                return _mk_result(excess=0.05)
            if abs(c - 2 * COMMISSION) < 1e-12 and abs(s - 2 * SLIPPAGE) < 1e-12:
                return _mk_result(excess=-0.01)
            return _mk_result(excess=0.02)

        req = CostSensitivityRequest(
            symbol="sh600000", start="2020-01-01", end="2020-12-31", strategy="chan",
            mode="multipliers",
            multipliers=[1.0, 2.0],
        )
        out = run_cost_sensitivity(req, runner)
        assert out.summary.default_cell_ok is True
        assert out.summary.double_cost_excess_positive is False
        assert out.summary.fragile is True
        assert any(c.is_default for c in out.cells)
        # 不落 equity_curve
        assert all(not hasattr(c, "equity_curve") for c in out.cells)

    def test_double_cost_positive(self):
        def runner(c, s):
            return _mk_result(excess=0.04 if c <= 2 * COMMISSION else 0.01)

        req = CostSensitivityRequest(
            symbol="sh600000", start="2020-01-01", end="2020-12-31", strategy="chan",
            mode="multipliers",
        )
        out = run_cost_sensitivity(req, runner)
        assert out.summary.double_cost_excess_positive is True
        assert out.summary.fragile is False

    def test_no_equity_in_response_model(self):
        def runner(c, s):
            return _mk_result(excess=0.01)

        req = CostSensitivityRequest(
            symbol="sh600000", start="2020-01-01", end="2020-12-31", strategy="chan",
            commissions=[0.00025],
            slippages=[0.0001],
        )
        out = run_cost_sensitivity(req, runner)
        dumped = out.model_dump()
        assert "equity_curve" not in dumped
        for cell in dumped["cells"]:
            assert "equity_curve" not in cell
            assert "trades" not in cell


class TestGridPickCostSource:
    def test_grid_pick_preserved_even_if_default_values(self):
        r = BacktestRequest(
            symbol="sh600000", start="2020-01-01", end="2020-12-31", strategy="chan",
            commission=COMMISSION, slippage=SLIPPAGE, cost_source="grid_pick",
        )
        assert cost_source_from_request(r) == "grid_pick"
        p = build_cost_profile(r.commission, r.slippage, source="grid_pick")
        assert p["source"] == "grid_pick"
        assert p["label"].startswith("网格选中·")

    def test_explicit_default_values_normalize_to_default(self):
        r = BacktestRequest(
            symbol="sh600000", start="2020-01-01", end="2020-12-31", strategy="chan",
            commission=COMMISSION, slippage=SLIPPAGE,
        )
        assert cost_source_from_request(r) == "user"
        p = build_cost_profile(r.commission, r.slippage, source="user")
        assert p["source"] == "default"  # §5.2 归一

    def test_omit_is_default_badge(self):
        r = BacktestRequest(
            symbol="sh600000", start="2020-01-01", end="2020-12-31", strategy="chan",
        )
        assert cost_source_from_request(r) == "default"
        p = build_cost_profile(r.commission, r.slippage, source="default")
        assert p["source"] == "default"


class TestExtractMetrics:
    def test_excess_from_benchmark_curve(self):
        eq = [
            SimpleNamespace(benchmark=100.0, equity=100.0),
            SimpleNamespace(benchmark=110.0, equity=120.0),
        ]
        raw = SimpleNamespace(
            total_return=0.20,
            annual_return=0.1,
            sharpe=1.0,
            max_drawdown=-0.05,
            trade_count=3,
            final_equity=120.0,
            equity_curve=eq,
            # no excess_return attr
        )
        # delete if present
        m = extract_metrics(raw)
        assert abs(m["excess_return"] - (0.20 - 0.10)) < 1e-9
