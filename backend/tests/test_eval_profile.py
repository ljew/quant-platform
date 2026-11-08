"""W45-badge / W45-5：评估口径徽章字段组装。"""
from __future__ import annotations

from app.core.cost_defaults import COMMISSION, MIN_COMMISSION, RISK_FREE_RATE, SLIPPAGE, STAMP_TAX
from app.core.eval_profile import (
    build_cost_profile,
    build_eval_profile_for_backtest,
    build_eval_profile_for_mining,
    build_eval_profile_for_optimize,
    build_eval_profile_for_paper,
    cost_source_from_request,
)
from app.schemas import BacktestRequest, OptimizeRequest
from app.routers.paper import PaperTaskCreate


class TestCostProfile:
    def test_defaults_match_cost_defaults(self):
        p = build_cost_profile()
        assert p["commission"] == COMMISSION == 0.00025
        assert p["slippage"] == SLIPPAGE == 0.0001
        assert p["stamp_tax"] == STAMP_TAX
        assert p["min_commission"] == MIN_COMMISSION
        assert p["risk_free_rate"] == RISK_FREE_RATE
        assert p["source"] == "default"
        assert "万2.5" in p["label"]

    def test_user_source_when_explicit(self):
        p = build_cost_profile(0.0003, 0.0, source="user")
        assert p["commission"] == 0.0003
        assert p["slippage"] == 0.0
        assert p["source"] == "user"


class TestRequestSource:
    def test_backtest_omit_is_default(self):
        r = BacktestRequest(
            symbol="sh600000", start="2020-01-01", end="2020-12-31", strategy="chan",
        )
        assert cost_source_from_request(r) == "default"
        prof = build_eval_profile_for_backtest(r, multi_asset=False)
        assert prof["cost_profile"]["source"] == "default"
        assert prof["cost_profile"]["commission"] == 0.00025
        assert prof["valuation_pit"]["mode"] == "n/a"
        assert prof["pead_pit"]["mode"] == "n/a"

    def test_backtest_explicit_is_user(self):
        r = BacktestRequest(
            symbol="sh600000", start="2020-01-01", end="2020-12-31", strategy="chan",
            commission=0.0003, slippage=0.0,
        )
        assert cost_source_from_request(r) == "user"
        assert build_eval_profile_for_backtest(r, multi_asset=False)["cost_profile"]["source"] == "user"

    def test_multi_asset_marks_valuation_fallback(self):
        r = BacktestRequest(
            symbol="sh000906", start="2020-01-01", end="2020-12-31", strategy="multi_factor",
        )
        prof = build_eval_profile_for_backtest(r, multi_asset=True)
        assert prof["valuation_pit"]["mode"] == "fallback"  # W45-5
        assert prof["pead_pit"]["mode"] == "asof"
        assert prof["pead_pit"]["scale"] == "raw_pp"

    def test_optimize_defaults(self):
        r = OptimizeRequest(
            symbol="sh600000", start="2020-01-01", end="2020-12-31",
            strategy="chan", param_ranges={"fast": [5, 10]},
        )
        prof = build_eval_profile_for_optimize(r)
        assert prof["cost_profile"]["commission"] == 0.00025
        assert prof["valuation_pit"]["mode"] == "n/a"

    def test_paper_create_defaults(self):
        r = PaperTaskCreate(name="t", strategy_key="chan", symbols="sh600000")
        prof = build_eval_profile_for_paper(r, multi_asset=False)
        assert prof["cost_profile"]["commission"] == 0.00025
        assert prof["cost_profile"]["source"] == "default"
        assert prof["valuation_pit"]["mode"] == "n/a"


class TestMiningProfile:
    def test_asof_when_timeline_loaded(self):
        prof = build_eval_profile_for_mining(
            valuation_mode="asof", valuation_coverage=0.92,
        )
        assert prof["valuation_pit"]["mode"] == "asof"
        assert prof["valuation_pit"]["coverage"] == 0.92
        assert prof["pead_pit"]["mode"] == "asof"
        assert prof["pead_pit"]["scale"] == "raw_pp"
        assert prof["cost_profile"]["commission"] == 0.00025

    def test_fallback_when_no_timeline(self):
        """W45-5：估值 PIT 回退必须显式 fallback，不得静默。"""
        prof = build_eval_profile_for_mining(
            valuation_mode="fallback",
            valuation_coverage=0.0,
            valuation_note="stock_valuation_daily 无数据",
        )
        assert prof["valuation_pit"]["mode"] == "fallback"
        assert prof["valuation_pit"]["note"]
