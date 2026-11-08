"""W47-2 / M3-4：截面正交化共享模块 + 挖矿开关验收。"""
from __future__ import annotations

import inspect
import math
from unittest.mock import MagicMock, patch

import pytest

from app.services.factor_ortho import (
    MIN_CROSS_SECTION,
    ORTHO_EXPRS,
    orthogonalize_cross_section,
    ortho_basis_template,
)


class TestOrthoExprs:
    def test_five_names(self):
        assert list(ORTHO_EXPRS.keys()) == [
            "momentum", "reversal", "low_vol", "value", "size",
        ]

    def test_exprs_validate(self):
        from app.services.factor_mining import validate_expr
        for name, expr in ORTHO_EXPRS.items():
            ok, err, _ = validate_expr(expr)
            assert ok, f"{name}={expr} 校验失败: {err}"


class TestOrthogonalizeCrossSection:
    def _make_fv(self, n: int, slope: float = 2.0):
        """构造 y = slope * momentum + noise0；其余代表因子给独立列。"""
        fv = []
        bucket = {f: {} for f in ORTHO_EXPRS}
        for i in range(n):
            sym = f"S{i:03d}"
            mom = float(i)
            rev = float(i % 7)
            lv = float((i * 3) % 11)
            val = float((i * 5) % 13)
            size = float((i * 2) % 17)
            bucket["momentum"][sym] = mom
            bucket["reversal"][sym] = rev
            bucket["low_vol"][sym] = lv
            bucket["value"][sym] = val
            bucket["size"][sym] = size
            # 纯动量暴露 + 常数；残差应接近 0（数值误差内）
            y = slope * mom + 1.0
            fv.append((sym, y, 0.01 * i))
        return fv, bucket

    def test_hand_residuals_near_zero(self):
        fv, bucket = self._make_fv(40, slope=2.5)
        out = orthogonalize_cross_section(fv, bucket)
        assert out is not fv
        assert len(out) == 40
        resid = [x[1] for x in out]
        # 设计矩阵含截距+五因子，y 恰为截距+动量线性组合 → 残差≈0
        assert max(abs(r) for r in resid) < 1e-8

    def test_n_below_min_returns_same_object(self):
        fv, bucket = self._make_fv(15)
        out = orthogonalize_cross_section(fv, bucket)
        assert out is fv

    def test_empty_bucket_returns_same(self):
        fv, _ = self._make_fv(30)
        assert orthogonalize_cross_section(fv, None) is fv
        assert orthogonalize_cross_section(fv, {}) is fv

    def test_collinear_returns_same(self):
        """五列完全共线（全等于动量）→ 主元过小 → 降级。"""
        fv = []
        bucket = {f: {} for f in ORTHO_EXPRS}
        for i in range(30):
            sym = f"C{i:03d}"
            v = float(i)
            for f in ORTHO_EXPRS:
                bucket[f][sym] = v  # 全部相同 → X 列共线
            fv.append((sym, 3.0 * v + 1.0, 0.0))
        out = orthogonalize_cross_section(fv, bucket)
        assert out is fv

    def test_missing_fac_returns_same(self):
        fv, bucket = self._make_fv(30)
        del bucket["value"]
        assert orthogonalize_cross_section(fv, bucket) is fv


class TestOrthoBasisTemplate:
    def test_shape(self):
        b = ortho_basis_template(n_snaps_resid=3, n_snaps_fallback_raw=1)
        assert b["names"] == list(ORTHO_EXPRS.keys())
        assert b["exprs"] == ORTHO_EXPRS
        assert b["min_cross_section"] == MIN_CROSS_SECTION
        assert b["n_snaps_resid"] == 3
        assert b["n_snaps_fallback_raw"] == 1
        assert "note" in b


class TestMineFactorSignature:
    def test_orthogonalize_default_true(self):
        from app.services.factor_mining import mine_factor
        sig = inspect.signature(mine_factor)
        assert "orthogonalize" in sig.parameters
        assert sig.parameters["orthogonalize"].default is True


class TestRouterPayload:
    def test_mine_payload_default(self):
        from app.routers.factor import MinePayload, HorizonsPayload
        m = MinePayload(expr="roc(c_m, 20)")
        assert m.orthogonalize is True
        h = HorizonsPayload(expr="roc(c_m, 20)")
        assert h.orthogonalize is True

    def test_params_json_o_flag_isolated(self):
        """o=0 / o=1 签名不串；True 不命中无 |o= 旧缓存。"""
        from datetime import datetime
        from app.routers.factor import MinePayload, _find_existing

        db = MagicMock()
        payload = MinePayload(expr="roc(c_m, 20)", groups=5, forward=20,
                              start="2024-01-01", end="2024-06-01")

        # 模拟库里只有旧 raw 签名（无 |o=）
        old_row = MagicMock()
        old_row.id = 1
        old_row.params_json = "2024-01-01|2024-06-01|5|20"
        old_row.result_json = '{"ok": true, "ic_mean": 0.03, "groups": 5, "forward_days": 20, "ic_series": []}'
        old_row.created_at = datetime(2024, 6, 1)

        def _exec(stmt):
            # 简化：按调用次序区分；用 where 不可见，改为按侧效应计数
            res = MagicMock()
            return res

        # 更可控：直接 patch db.execute 返回序列
        calls = {"n": 0}

        def fake_execute(stmt):
            calls["n"] += 1
            res = MagicMock()
            # 第一次：精确 sig（含 o=1）→ miss
            # 若 allow_legacy=False，到此为止
            res.scalar_one_or_none.return_value = None
            res.scalars.return_value.all.return_value = []
            return res

        db.execute.side_effect = fake_execute

        sig_true = "2024-01-01|2024-06-01|5|20|o=1"
        row, cached = _find_existing(
            db, "roc(c_m, 20)", payload, sig_true,
            allow_legacy=False, legacy_sig="2024-01-01|2024-06-01|5|20",
        )
        assert row is None and cached is None
        # allow_legacy=False 时不应继续查旧签名 / 空 params
        assert calls["n"] == 1

        # o=0 允许 legacy_sig 命中
        calls["n"] = 0

        def fake_execute2(stmt):
            calls["n"] += 1
            res = MagicMock()
            if calls["n"] == 1:
                # exact o=0 miss
                res.scalar_one_or_none.return_value = None
            elif calls["n"] == 2:
                # legacy_sig hit
                res.scalar_one_or_none.return_value = old_row
            else:
                res.scalar_one_or_none.return_value = None
                res.scalars.return_value.all.return_value = []
            return res

        db.execute.side_effect = fake_execute2
        sig_false = "2024-01-01|2024-06-01|5|20|o=0"
        row2, cached2 = _find_existing(
            db, "roc(c_m, 20)", payload, sig_false,
            allow_legacy=True, legacy_sig="2024-01-01|2024-06-01|5|20",
        )
        assert row2 is old_row
        assert cached2 is not None and cached2["ok"] is True


class TestGpImportShared:
    def test_gp_uses_shared_ortho(self):
        import app.services.factor_gp as gp
        src = inspect.getsource(gp)
        assert "orthogonalize_cross_section" in src
        assert "build_ortho_cache" in src
        assert "def _orthogonalize" not in src
        # 默认仍 False
        sig = inspect.signature(gp.evaluate_candidate)
        assert sig.parameters["orthogonal"].default is False


class TestMineFactorFalsePathSource:
    def test_false_skips_cache_build(self):
        from app.services.factor_mining import mine_factor
        src = inspect.getsource(mine_factor)
        assert "if orthogonalize:" in src
        assert "build_ortho_cache" in src
        assert "ic_resid" in src
