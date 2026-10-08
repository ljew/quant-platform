"""T3 成本模型 —— 防回归：回测成本必须是真实 A 股成本（含印花税）。

Bug 背景（M1-3，2026-10-08）：主引擎 backtest_engine / portfolio_backtest
默认 commission=0.0003、slippage=0.0、且完全没有印花税；而 xq/portfolio.py
含万2.5 佣金 + 分向滑点 + 万5 印花税。同平台两套成本口径，主引擎显著更乐观
（实测 238/248 条回测用 slippage=0）。修复后统一为：
  佣金万2.5 · 滑点万1（分向）· 印花税万5（仅卖出）· 单笔最低佣金 5 元。
"""
from __future__ import annotations

import inspect

from app.core.engine.backtest_engine import BacktestEngine
from app.core.engine.portfolio_backtest import PortfolioBacktestEngine


def _bars(n: int = 60) -> list[dict]:
    """构造一段温和上涨的日 K，close 从 10 线性到 12。"""
    out = []
    for i in range(n):
        c = 10.0 + (2.0 * i / (n - 1))
        out.append({"date": f"2026-01-{i + 1:02d}", "symbol": "sh600000",
                    "open": c, "high": c, "low": c, "close": c, "volume": 1e6})
    return out


class TestCostModel:
    def test_defaults_are_realistic(self):
        eng = BacktestEngine(_bars())
        assert eng.commission == 0.00025, f"佣金应为万2.5，实为 {eng.commission}"
        assert eng.slippage > 0, "滑点不应为 0（旧实现零滑点过于乐观）"
        assert eng.stamp_tax == 0.0005, f"印花税应为万5，实为 {eng.stamp_tax}"
        assert eng.min_commission == 5.0
        assert eng.risk_free_rate == 0.025, "无风险利率应为 2.5%"

    def test_portfolio_defaults_match(self):
        """组合引擎与单标的引擎成本口径必须一致（M1-3 的核心目标）。"""
        eng = PortfolioBacktestEngine(data={}, benchmark=[])
        assert eng.commission == BacktestEngine(_bars()).commission
        assert eng.slippage == BacktestEngine(_bars()).slippage
        assert eng.stamp_tax == BacktestEngine(_bars()).stamp_tax

    def _ctx(self, eng: BacktestEngine, i: int = 0):
        """构造一个已 set_bar 的 ctx（_execute 记录成交时需读 ctx.bar）。"""
        from app.core.engine.backtest_engine import StrategyContext
        ctx = StrategyContext(eng, {})
        ctx.set_bar(eng.bars[i], i)
        return ctx

    def test_buy_includes_commission_and_slippage(self):
        """买入后现金应被 notional + commission 占用，且成交价被滑点推高。

        规模须超过 rebalance_tol（默认 0.5% 净值）门槛，否则订单被过滤。
        """
        eng = BacktestEngine(_bars(), initial_cash=10_000_000)
        price = 10.0
        eng._execute(self._ctx(eng), 10_000, price)
        assert eng.shares == 10_000
        spent = 10_000_000 - eng.cash
        assert spent > 100_000, f"买入支出 {spent} 应高于裸额（滑点+佣金）"
        assert eng.avg_cost > price, f"avg_cost={eng.avg_cost} 应含滑点与佣金"

    def test_stamp_tax_only_on_sell(self):
        """印花税只在卖出收取：买入不扣、卖出扣。"""
        src = inspect.getsource(BacktestEngine._execute)
        assert "stamp = notional * self.stamp_tax if not is_buy else 0.0" in src, \
            "印花税应仅对卖出收取"
        assert "self.cash -= notional + commission" in src, "买入不应扣印花税"
        assert "self.cash += notional - commission - stamp" in src, "卖出应扣印花税"

    def test_sell_pays_stamp_tax(self):
        """卖出现金回流应少于裸卖出额（佣金+印花税）。"""
        eng = BacktestEngine(_bars(), initial_cash=10_000_000)
        eng._execute(self._ctx(eng), 10_000, 10.0)   # 先建仓
        cash_after_buy = eng.cash
        eng._execute(self._ctx(eng), -10_000, 12.0)  # 卖出
        inflow = eng.cash - cash_after_buy
        assert inflow < 120_000, f"卖出回流 {inflow} 应小于裸额 120000（佣金+印花税）"
        assert eng.shares == 0

    def test_min_commission_floor(self):
        eng = BacktestEngine(_bars())
        assert eng.min_commission == 5.0, "小单也应收最低 5 元佣金"

    def test_sharpe_subtracts_risk_free(self):
        """夏普必须减无风险利率（M1-5）。"""
        src = inspect.getsource(BacktestEngine)
        assert "rf_daily = self.risk_free_rate / 252.0" in src
        assert "sharpe = ((m - rf_daily) / std)" in src, "夏普应减 rf 日收益"

    def test_portfolio_sharpe_subtracts_risk_free(self):
        src = inspect.getsource(PortfolioBacktestEngine)
        assert "rf_daily = self.risk_free_rate / 252.0" in src
