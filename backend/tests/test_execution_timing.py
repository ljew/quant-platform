"""统一成交时点：信号日决策，下一交易日开盘成交。"""
from __future__ import annotations

import inspect

import pytest

from app.core.cost_defaults import SLIPPAGE
from app.core.engine.backtest_engine import BacktestEngine
from app.core.engine.portfolio_backtest import PortfolioBacktestEngine

D1, D2 = "2026-01-05", "2026-01-06"


def _bars() -> list[dict]:
    return [
        {"date": D1, "symbol": "sh600000", "open": 9.5,
         "high": 10.2, "low": 9.4, "close": 10.0, "volume": 1e6},
        {"date": D2, "symbol": "sh600000", "open": 11.0,
         "high": 12.2, "low": 10.8, "close": 12.0, "volume": 1e6},
    ]


class _BuyOnFirstBar:
    def init(self, ctx) -> None:
        pass

    def on_bar(self, ctx, bar) -> None:
        if ctx.index == 0:
            ctx.buy()


class TestExecutionTiming:
    def test_single_asset_fills_next_day_open(self):
        eng = BacktestEngine(_bars(), limit_enabled=False)
        eng.run(_BuyOnFirstBar, {})
        assert len(eng.trades) == 1
        trade = eng.trades[0]
        assert trade.date == D2
        assert trade.price == pytest.approx(11.0 * (1 + SLIPPAGE))
        assert trade.price != pytest.approx(10.0 * (1 + SLIPPAGE))

    def test_last_bar_signal_does_not_invent_fill(self):
        eng = BacktestEngine(_bars()[:1], limit_enabled=False)
        eng.run(_BuyOnFirstBar, {})
        assert eng.trades == []
        assert eng.shares == 0

    def test_portfolio_fills_next_day_open(self):
        class BuyOnD1:
            def init(self, ctx) -> None:
                pass

            def rebalance(self, ctx, date: str) -> None:
                if date == D1:
                    ctx.order_target_percent("sh600000", 1.0)

        eng = PortfolioBacktestEngine(
            data={"sh600000": _bars()}, benchmark=_bars(),
            warmup=0, rebalance_period=1, limit_enabled=False,
        )
        eng.run(BuyOnD1, {})
        assert len(eng.trades) == 1
        assert eng.trades[0].date == D2
        assert eng.trades[0].price == pytest.approx(11.0 * (1 + SLIPPAGE))

    def test_mirror_portfolio_remains_next_day_open(self):
        from app.services.xq import portfolio
        src = inspect.getsource(portfolio)
        assert "pending_buys" in src and "pending_sells" in src
        assert '_px(d, sym, "open")' in src
