"""研究链路 PIT、重叠标签与生产门禁回归测试。"""
from __future__ import annotations

import math
from datetime import date

from app.services.factor_lab_gate import check_enable_gate
from app.services.factor_mining import (
    _eligible_symbol,
    _is_oos_split,
    _oos_metrics,
    _pit_symbols,
    _walk_forward_report,
)


def _dates(n: int) -> list[str]:
    return [f"2024-{i // 28 + 1:02d}-{i % 28 + 1:02d}" for i in range(n)]


def _stable(n: int, mu: float = 0.06) -> list[float]:
    return [mu + 0.02 * math.sin(i * 2.399) for i in range(n)]


def _valid_report() -> dict:
    return {
        "ok": True,
        "rating": "优秀",
        "expr": "roc(c_m, 20)",
        "oos": {"enabled": True, "verdict": "通过", "n": 29,
                "ic_mean": 0.04, "t_stat": 2.4, "nw_lags": 3},
        "walk_forward": {"verdict": "通过", "n_folds": 3},
        "universe_pit": {"mode": "pit_index_membership", "lifecycle_filter": True},
        "valuation_pit": {"mode": "asof"},
    }


class TestPITUniverse:
    def test_membership_uses_only_visible_snapshot(self):
        timeline = {
            "000300": [
                (date(2024, 1, 31), {"sh600000"}),
                (date(2024, 2, 29), {"sh600000", "sz000001"}),
            ]
        }
        assert _pit_symbols(timeline, date(2024, 2, 1)) == {"sh600000"}
        assert _pit_symbols(timeline, date(2024, 3, 1)) == {"sh600000", "sz000001"}

    def test_lifecycle_excludes_future_and_delisted_stocks(self):
        lifecycle = {
            "future": (date(2025, 1, 1), None),
            "delisted": (date(2020, 1, 1), date(2024, 2, 1)),
            "active": (date(2020, 1, 1), None),
        }
        snap = date(2024, 3, 1)
        assert not _eligible_symbol(lifecycle, "future", snap)
        assert not _eligible_symbol(lifecycle, "delisted", snap)
        assert _eligible_symbol(lifecycle, "active", snap)


class TestOverlappingLabels:
    def test_split_purges_is_and_embargoes_oos(self):
        values = _stable(100)
        dates = _dates(100)
        report = _is_oos_split(dates, values, forward=20, step=5)
        assert report["purged_is"] == 4
        assert report["embargoed_oos"] == 1
        assert report["is"]["n"] == 66
        assert report["oos"]["n"] == 29
        assert dates.index(report["boundary_date"]) == 69
        assert dates.index(report["split_date"]) == 65
        assert dates.index(report["oos_start"]) == 71

    def test_newey_west_t_is_reported_with_naive_t(self):
        metrics = _oos_metrics(_stable(40), nw_lags=3)
        assert metrics["nw_lags"] == 3
        assert metrics["t_stat"] is not None
        assert metrics["t_stat_naive"] is not None
        assert metrics["t_stat"] != metrics["t_stat_naive"]

    def test_walk_forward_stays_inside_is(self):
        values = _stable(66)
        dates = _dates(66)
        report = _walk_forward_report(dates, values)
        assert report["n_folds"] == 3
        assert report["verdict"] == "通过"
        assert all(f["test_end"] <= dates[65] for f in report["folds"])
        assert report["overall"]["n"] < 66


class TestProductionGate:
    def test_valid_report_passes(self):
        assert check_enable_gate(_valid_report()) == []

    def test_small_oos_cannot_pass_by_verdict_alone(self):
        report = _valid_report()
        report["oos"]["n"] = 8
        assert any("≥20" in reason for reason in check_enable_gate(report))

    def test_walk_forward_is_mandatory(self):
        report = _valid_report()
        report["walk_forward"] = {"verdict": "未通过", "n_folds": 3}
        assert any("walk-forward" in reason for reason in check_enable_gate(report))

    def test_current_constituents_are_rejected(self):
        report = _valid_report()
        report["universe_pit"] = {"mode": "current_union", "lifecycle_filter": False}
        assert any("历史成分" in reason for reason in check_enable_gate(report))

    def test_latest_valuation_fallback_is_rejected(self):
        report = _valid_report()
        report["valuation_pit"] = {"mode": "fallback"}
        assert any("前视" in reason for reason in check_enable_gate(report))
