"""W45-lab：horizons API 汇总 + 注册表/toggle 门禁。"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services.factor_lab_gate import (
    check_enable_gate,
    expr_depends_on_es,
    load_mine_report,
)


def _report(**kw):
    base = {
        "ok": True,
        "expr": "roc(c_m, 20)",
        "rating": "优秀",
        "ic_mean": 0.03,
        "icir": 0.5,
        "t_stat": 2.5,
        "n_periods": 40,
        "oos": {"verdict": "通过", "enabled": True, "n": 30,
                "t_stat": 2.5, "ic_mean": 0.03, "nw_lags": 3},
        "walk_forward": {"verdict": "通过", "n_folds": 3},
        "universe_pit": {"mode": "pit_index_membership", "lifecycle_filter": True},
        "pead_pit": {"mode": "asof", "scale": "raw_pp"},
        "valuation_pit": {"mode": "asof"},
        "cost_profile": {"commission": 0.00025},
    }
    base.update(kw)
    return base


class TestExprDepends:
    def test_plain(self):
        assert not expr_depends_on_es("roc(c_m, 20)")

    def test_es(self):
        assert expr_depends_on_es("earnings_surprise - roc(c_m, 5)")

    def test_pead_token(self):
        assert expr_depends_on_es("pead + 1")


class TestGate:
    def test_pass(self):
        assert check_enable_gate(_report()) == []

    def test_reject_overfit(self):
        reasons = check_enable_gate(_report(oos={"verdict": "过拟合"}, rating="弱"))
        assert any("OOS" in r for r in reasons)

    def test_reject_rating(self):
        reasons = check_enable_gate(_report(rating="弱", oos={"verdict": "通过"}))
        assert any("评级" in r for r in reasons)

    def test_reject_legacy_pead(self):
        reasons = check_enable_gate(
            _report(expr="earnings_surprise", pead_pit={"mode": "legacy_static"}),
            expr="earnings_surprise",
        )
        assert any("legacy_static" in r for r in reasons)

    def test_missing_report(self):
        reasons = check_enable_gate(None)
        assert reasons and "缺少" in reasons[0]


class TestLoadMineReport:
    def test_by_source_id(self):
        db = MagicMock()
        row = SimpleNamespace(
            id=7, rating="可用", expr="roc(c_m, 10)",
            result_json=json.dumps(_report(rating="可用")),
        )
        db.get.return_value = row
        r = load_mine_report(db, source_id=7)
        assert r is not None and r["rating"] == "可用"
        db.get.assert_called_once()


class TestHorizonsApi:
    def test_horizons_aggregates(self):
        from app.routers.factor import HorizonsPayload, mine_horizons

        calls = []

        def fake_mine(db, expr, name="自定义因子", start="", end="",
                      groups=5, forward=20, **kw):
            calls.append(forward)
            return _report(forward_days=forward, ic_mean=0.01 * forward,
                           icir=0.4, t_stat=2.0)

        db = MagicMock()
        payload = HorizonsPayload(
            expr="roc(c_m, 20)", name="t",
            start="2024-01-01", end="2025-01-01",
            forwards=[5, 10, 20, 40],
        )
        with patch("app.routers.factor.mine_factor", side_effect=fake_mine):
            out = mine_horizons(payload, db)
        assert out["ok"] is True
        assert [h["horizon"] for h in out["horizons"]] == [5, 10, 20, 40]
        assert calls == [5, 10, 20, 40]
        assert all("ic_mean" in h and "oos" in h for h in out["horizons"])


class TestRegistryGate:
    def test_toggle_enabled_rejected_integration(self):
        from app.routers.factor import registry_toggle

        db = MagicMock()
        row = SimpleNamespace(
            id=1, status="candidate", expr="roc(c_m, 20)", source_id=9, name="x",
        )
        db.get.return_value = row

        def boom(*a, **k):
            raise ValueError("OOS 须通过，当前=过拟合")

        with patch("app.services.factor_lab_gate.assert_enable_allowed", side_effect=boom):
            # registry_toggle imports assert_enable_allowed inside body — patch module before call
            import app.services.factor_lab_gate as gate
            with patch.object(gate, "assert_enable_allowed", side_effect=boom):
                with pytest.raises(HTTPException) as ei:
                    registry_toggle(1, db)
        assert ei.value.status_code == 400
        assert "OOS" in str(ei.value.detail)
        assert row.status == "candidate"  # unchanged

    def test_toggle_enabled_ok(self):
        from app.routers.factor import registry_toggle
        import app.services.factor_lab_gate as gate

        db = MagicMock()
        row = SimpleNamespace(
            id=1, status="candidate", expr="roc(c_m, 20)", source_id=9, name="x",
        )
        db.get.return_value = row

        with patch.object(gate, "assert_enable_allowed", return_value=_report()):
            out = registry_toggle(1, db)
        assert out["status"] == "enabled"
        assert row.status == "enabled"
        db.commit.assert_called()

    def test_register_rejected(self):
        from app.routers.factor import RegistryPayload, registry_register
        import app.services.factor_lab_gate as gate

        db = MagicMock()
        # no existing
        db.execute.return_value.scalar.return_value = None
        payload = RegistryPayload(name="f1", expr="roc(c_m, 20)", source_id=3)

        with patch.object(gate, "assert_enable_allowed",
                          side_effect=ValueError("评级须为优秀/可用，当前=弱")):
            with pytest.raises(HTTPException) as ei:
                registry_register(payload, db)
        assert ei.value.status_code == 400

    def test_register_ok(self):
        from app.routers.factor import RegistryPayload, registry_register
        import app.services.factor_lab_gate as gate

        db = MagicMock()
        db.execute.return_value.scalar.return_value = None

        def _add(obj):
            obj.id = 42
            obj.status = "candidate"

        db.add.side_effect = _add
        db.refresh.side_effect = lambda r: None
        payload = RegistryPayload(name="f1", expr="roc(c_m, 20)", source_id=3)

        with patch.object(gate, "assert_enable_allowed", return_value=_report()):
            out = registry_register(payload, db)
        assert out["ok"] is True
        assert out["id"] == 42
        assert out["status"] == "candidate"


class TestGpStillOff:
    def test_gp_default_off(self):
        import os
        assert os.getenv("QUANT_GP_ENABLED", "0") in ("0", "", "false", "False")
        from app.services import factor_gp
        assert factor_gp.GP_ENABLED is False
