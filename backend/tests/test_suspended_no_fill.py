"""停牌不可成交 —— 防回归：停牌日只有 ffill 定价，没有真实成交。

背景：`portfolio_backtest.py` 的 `_last_close` 在停牌日保持停牌前收盘价
（ffill 定价，用于**估值**，这是正确的）；但撮合时若不校验「当日是否有真实
行情」，卖出停牌股也能按旧价成交 → 回测凭空获得流动性，净值系统性偏乐观。

实盘侧 `app/live/target.py` 早已明确「停牌卖不出，持仓保留」，回测侧此前
没有对应约束。本测试把该约束锁住，同时防止拦截过度。

口径说明：只拦**成交**，不拦**估值**。停牌期间持仓仍按 ffill 价计入净值。
"""
from __future__ import annotations

from app.core.engine.portfolio_backtest import PortfolioBacktestEngine

D1, D2, D3, D4 = "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"


def _bars(dates: list[str], px: float) -> list[dict]:
    return [{"date": d, "close": float(px), "high": float(px)} for d in dates]


def _data() -> dict:
    """B 在 D3 停牌（当日无 bar）；A 全程正常。"""
    return {
        "A": _bars([D1, D2, D3, D4], 10.0),
        "B": _bars([D1, D2, D4], 20.0),
    }


def _bench() -> list[dict]:
    return _bars([D1, D2, D3, D4], 100.0)


class _BuyThenSell:
    """D1 买入 B 半仓；sell_date 当天尝试清仓。"""

    sell_date: str = D3

    def init(self, ctx) -> None:
        pass

    def rebalance(self, ctx, date: str) -> None:
        if date == D1:
            ctx.order_target_percent("B", 0.5)
        elif date == self.sell_date:
            ctx.order_target_percent("B", 0.0)


def _engine(**kw) -> PortfolioBacktestEngine:
    opts = dict(initial_cash=1_000_000.0, warmup=0, rebalance_period=1)
    opts.update(kw)
    return PortfolioBacktestEngine(data=_data(), benchmark=_bench(), **opts)


def _run(sell_date: str) -> PortfolioBacktestEngine:
    cls = type("_S", (_BuyThenSell,), {"sell_date": sell_date})
    eng = _engine()
    eng.run(cls, {})
    return eng


class TestSuspendedNoFill:
    def test_suspended_sell_blocked(self):
        """D2 信号在 D3 停牌日开盘执行：卖出必须被拦截。"""
        eng = _run(D2)
        assert eng.positions["B"] > 0, "停牌日卖出了实盘卖不掉的持仓"

    def test_tradable_sell_filled(self):
        """D3 信号在 D4 复牌日成交：正常卖出必须成交。"""
        eng = _run(D3)
        assert eng.positions["B"] == 0, "正常交易日卖出被误拦"

    def test_tradable_set_rebuilt_daily(self):
        """_tradable_today 每日重建：D3 不含 B，复牌后恢复。"""
        eng = _engine()
        captured: dict[str, set] = {}
        orig = eng._order_target

        def spy(ctx, symbol, target_shares, signal_type="", signal_reason=""):
            captured.setdefault(ctx.date, set()).update(eng._tradable_today)
            return orig(ctx, symbol, target_shares, signal_type, signal_reason)

        eng._order_target = spy
        cls = type("_S", (_BuyThenSell,), {"sell_date": D2})
        eng.run(cls, {})

        assert "B" not in captured.get(D3, set()), \
            "停牌日 B 不应出现在可成交集合里"
        assert "A" in captured.get(D3, set()), "正常标的应始终可成交"
        assert eng._tradable_today == {"A", "B"}, "复牌日应恢复可成交"

    def test_estimate_still_uses_ffill(self):
        """只拦成交不拦估值：停牌期间持仓仍按 ffill 价计入净值。"""
        eng = _run(D2)
        assert eng.positions["B"] > 0
        assert eng._last_close["B"] == 20.0, "停牌标的应保留 ffill 定价用于估值"

    def test_buy_suspended_blocked(self):
        """D2 买入信号在 D3 停牌日执行，不得建仓。"""
        eng = _engine(rebalance_period=1)

        class _BuyBOnD3(_BuyThenSell):
            def rebalance(self, ctx, date):
                if date == D2:
                    ctx.order_target_percent("B", 0.5)

        eng.run(_BuyBOnD3, {})
        assert eng.positions["B"] == 0, "停牌日不应买得进（无真实成交）"
