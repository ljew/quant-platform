"""W46-3 今日稳健摘要 · 告警推导验收。"""
from __future__ import annotations

from datetime import date

from app.services.today_robustness import days_ago_shanghai, derive_robustness_alerts


class TestDaysAgo:
    def test_none_and_basic(self):
        assert days_ago_shanghai(None, today=date(2026, 11, 8)) is None
        assert days_ago_shanghai("2026-11-08", today=date(2026, 11, 8)) == 0
        assert days_ago_shanghai("2026-11-05", today=date(2026, 11, 8)) == 3
        assert days_ago_shanghai("2026-11-01", today=date(2026, 11, 8)) == 7


class TestAlerts:
    def test_empty_eval_points_to_btopt(self):
        a = derive_robustness_alerts(has_eval=False, today=date(2026, 11, 8))
        assert any(x["tab"] == "btopt" and x["level"] == "info" for x in a)
        assert not any("OOS·" in x["text"] for x in a)

    def test_stale_freshness_warns_datasys(self):
        a = derive_robustness_alerts(
            kline_latest="2026-10-01",
            factor_latest="2026-11-08",
            has_eval=False,
            today=date(2026, 11, 8),
        )
        assert any(x["tab"] == "datasys" and x["level"] == "warn" for x in a)

    def test_oos_fail_and_grid_pick(self):
        a = derive_robustness_alerts(
            has_eval=True,
            oos={"enabled": True, "verdict": "过拟合"},
            cost_profile={"source": "grid_pick", "commission": 0.00025, "slippage": 0.0001},
            today=date(2026, 11, 8),
        )
        assert any("OOS·过拟合" in x["text"] and x["tab"] == "btopt" for x in a)
        assert any("grid_pick" in x["text"] for x in a)

    def test_fragile_regime(self):
        a = derive_robustness_alerts(
            has_eval=True,
            oos={"enabled": True, "verdict": "通过", "fragile_regime": True},
            cost_profile={"source": "default"},
            today=date(2026, 11, 8),
        )
        assert any("脆弱" in x["text"] for x in a)

    def test_pass_with_default_cost_soft_tip(self):
        a = derive_robustness_alerts(
            has_eval=True,
            oos={"enabled": True, "verdict": "通过"},
            cost_profile={"source": "default", "commission": 0.00025, "slippage": 0.0001},
            kline_latest="2026-11-07",
            factor_latest="2026-11-07",
            today=date(2026, 11, 8),
        )
        assert not any(x["level"] == "warn" for x in a)
        assert any("成本敏感网格" in x["text"] for x in a)
