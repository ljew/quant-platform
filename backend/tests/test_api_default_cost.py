"""P0-3 / W45-2 护栏：API/schema 不传 commission/slippage 时，默认必须等于引擎口径。

权威成本（引擎，已修 M1-3）：
  commission = 0.00025  # 万 2.5
  slippage   = 0.0001   # 万 1
来源：app.core.engine.backtest_engine.BacktestEngine.__init__
      app.core.engine.portfolio_backtest.PortfolioBacktestEngine.__init__
      app.core.cost_defaults（W45-2 单一真相源）

缺口说明（相对 tests/test_cost_model.py）：
  test_cost_model 只断言「直接构造引擎」的默认；
  护不住 BacktestRequest / OptimizeRequest / PaperTaskCreate / ORM
  仍为 0.0003 / 0.0，前端不传时经路由灌进引擎，绕过 M1-3。

本文件：打在「默认值解析 → 将传入引擎的参数」边界。
"""
from __future__ import annotations

import inspect

import pytest

# 权威期望：与引擎构造默认对齐（勿手抄魔法数而不对照 signature）
from app.core.engine.backtest_engine import BacktestEngine
from app.core.engine.portfolio_backtest import PortfolioBacktestEngine
from app.schemas import BacktestRequest, OptimizeRequest


def _engine_cost_defaults() -> tuple[float, float]:
    sig = inspect.signature(BacktestEngine.__init__)
    commission = sig.parameters["commission"].default
    slippage = sig.parameters["slippage"].default
    assert commission == 0.00025
    assert slippage == 0.0001
    return float(commission), float(slippage)


class TestApiSchemaDefaultCost:
    """不传成本字段时，Pydantic schema 默认 == 引擎默认。"""

    def test_backtest_request_omitted_cost_matches_engine(self):
        want_c, want_s = _engine_cost_defaults()
        req = BacktestRequest(
            symbol="sh600000",
            start="2024-01-01",
            end="2024-06-30",
            strategy="dual_ma",  # key 仅占位，不跑引擎
        )
        assert req.commission == want_c, (
            f"BacktestRequest.commission 默认 {req.commission} != 引擎 {want_c} "
            f"（旧值 0.0003 会绕过 M1-3）"
        )
        assert req.slippage == want_s, (
            f"BacktestRequest.slippage 默认 {req.slippage} != 引擎 {want_s} "
            f"（旧值 0.0 会绕过 M1-3）"
        )

    def test_optimize_request_omitted_cost_matches_engine(self):
        want_c, want_s = _engine_cost_defaults()
        req = OptimizeRequest(
            symbol="sh600000",
            start="2024-01-01",
            end="2024-06-30",
            strategy="dual_ma",
            param_ranges={"fast": [5, 10], "slow": [20, 40]},
        )
        assert req.commission == want_c
        assert req.slippage == want_s

    def test_paper_task_create_omitted_cost_matches_engine(self):
        want_c, want_s = _engine_cost_defaults()
        # PaperTaskCreate 定义在 routers.paper，非 schemas.py
        from app.routers.paper import PaperTaskCreate

        req = PaperTaskCreate(name="qa-cost-guard", strategy_key="dual_ma", symbols="sh600000")
        assert req.commission == want_c
        assert req.slippage == want_s


class TestOrmColumnDefaultCost:
    """ORM 落库默认也应与引擎一致，避免「API 修了、库默认仍旧」。"""

    def test_backtest_and_paper_task_column_defaults(self):
        want_c, want_s = _engine_cost_defaults()
        from app.models import Backtest, PaperTask

        assert Backtest.__table__.c.commission.default.arg == want_c
        assert Backtest.__table__.c.slippage.default.arg == want_s
        assert PaperTask.__table__.c.commission.default.arg == want_c
        assert PaperTask.__table__.c.slippage.default.arg == want_s


class TestRouterWiresRequestCostIntoEngine:
    """静态护栏：回测/组合路径必须把 req.commission/slippage 传入引擎构造。

    不跑完整 HTTP；防止以后改成「忽略 req、却仍用 schema 旧默认」或漏传。
    """

    def test_strategy_router_passes_req_cost(self):
        from app.routers import strategy as strategy_router

        src = inspect.getsource(strategy_router)
        assert "commission=req.commission" in src
        assert "slippage=req.slippage" in src

    def test_paper_create_persists_req_cost(self):
        from app.routers import paper as paper_router

        src = inspect.getsource(paper_router.create_task)
        assert "commission=req.commission" in src
        assert "slippage=req.slippage" in src


class TestEngineDefaultsStillAligned:
    """与 test_cost_model 重叠的最小锚点：引擎双轨自身仍一致。"""

    def test_portfolio_signature_matches_single(self):
        c1, s1 = _engine_cost_defaults()
        sig_p = inspect.signature(PortfolioBacktestEngine.__init__)
        assert sig_p.parameters["commission"].default == c1
        assert sig_p.parameters["slippage"].default == s1

    def test_cost_defaults_module_is_ssot(self):
        from app.core import cost_defaults as cd

        assert cd.COMMISSION == 0.00025
        assert cd.SLIPPAGE == 0.0001
        assert cd.STAMP_TAX == 0.0005
        assert cd.MIN_COMMISSION == 5.0
        assert cd.RISK_FREE_RATE == 0.025
