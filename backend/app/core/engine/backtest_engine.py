"""事件驱动回测引擎（Phase 2 核心）。

设计要点：
- 逐根 K 线驱动：每根 bar 调用一次策略 on_bar(ctx, bar)，策略通过 ctx 下单；
- 多头单一标的、按目标仓位（百分比）调仓，long-only；
- 撮合采用「下一根开盘/当前收盘」近似：本引擎使用当前 bar 收盘价成交（简化模型），
  并计入佣金与滑点；
- 输出权益曲线、成交明细与绩效归因（总收益/年化/最大回撤/夏普/胜率）；
- 与策略 SDK（StandardStrategy）共用同一套策略代码，仅运行时 ctx 不同。
- 成交记录 Trade 携带 signal_type，用于标注触发该笔的信号类型（如缠论 buy1/buy3）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, List, Optional

from app.core.engine import indicators as ind
from app.core.engine.base_strategy import Mode, StandardStrategy


@dataclass
class Trade:
    date: str
    side: str          # BUY / SELL
    price: float
    shares: float
    cash_after: float
    commission: float
    pnl: float = 0.0   # SELL 的已实现盈亏
    signal_type: str = "manual"  # 触发该笔成交的信号类型（如缠论 buy1/buy3）
    signal_reason: str = ""      # 触发该笔成交的数据支撑说明（为何买/卖）


@dataclass
class EquityPoint:
    date: str
    equity: float
    benchmark: float   # 买入持有基准


@dataclass
class BacktestResult:
    symbol: str
    start: str
    end: str
    strategy_key: str
    params: dict
    initial_cash: float
    final_equity: float
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float
    win_rate: float
    trade_count: int
    round_trips: int
    equity_curve: List[EquityPoint] = field(default_factory=list)
    trades: List[Trade] = field(default_factory=list)


class StrategyContext:
    """注入给策略的运行时上下文（回测版）。"""

    def __init__(self, engine: "BacktestEngine", params: dict):
        self.engine = engine
        self.params = params
        self.mode = Mode.BACKTEST
        self.bar: Optional[dict] = None
        self.index = -1
        self.bars = engine.bars
        self._risk_guard: Optional["RiskGuard"] = None

    def set_bar(self, bar: dict, index: int) -> None:
        self.bar = bar
        self.index = index

    # —— 下单接口（策略调用）——
    def order_target_percent(self, pct: float, signal_type: str = "manual", signal_reason: str = "") -> None:
        """目标仓位占净值的百分比（0~1，long-only）。signal_type/reason 透传信号类型与说明。"""
        self.engine.order_target_percent(self, max(0.0, min(1.0, float(pct))), signal_type, signal_reason)

    def buy(self, signal_type: str = "manual", signal_reason: str = "") -> None:
        self.order_target_percent(1.0, signal_type, signal_reason)

    def sell(self, signal_type: str = "manual", signal_reason: str = "") -> None:
        self.order_target_percent(0.0, signal_type, signal_reason)

    # —— 便捷指标（基于截至当前 bar 的收盘价序列）——
    def closes(self) -> List[float]:
        return [b["close"] for b in self.bars[: self.index + 1]]

    def highs(self) -> List[float]:
        return [b["high"] for b in self.bars[: self.index + 1]]

    def lows(self) -> List[float]:
        return [b["low"] for b in self.bars[: self.index + 1]]

    def volumes(self) -> List[float]:
        return [float(b.get("volume", 0)) for b in self.bars[: self.index + 1]]

    def _last(self, vals: List[Optional[float]]) -> Optional[float]:
        return vals[-1] if vals else None

    def sma(self, period: int) -> Optional[float]:
        return self._last(ind.sma(self.closes(), period))

    def ema(self, period: int) -> Optional[float]:
        return self._last(ind.ema(self.closes(), period))

    def roc(self, period: int) -> Optional[float]:
        return self._last(ind.roc(self.closes(), period))

    def stddev(self, period: int) -> Optional[float]:
        return self._last(ind.stddev(self.closes(), period))

    def rsi(self, period: int = 14) -> Optional[float]:
        return self._last(ind.rsi(self.closes(), period))

    def macd(self, fast: int = 12, slow: int = 26, signal: int = 9):
        """返回 (dif, dea, hist) 当前值。"""
        dif, dea, hist = ind.macd(self.closes(), fast, slow, signal)
        return self._last(dif), self._last(dea), self._last(hist)

    def boll(self, period: int = 20, k: float = 2.0):
        """返回 (mid, upper, lower) 当前值。"""
        mid, upper, lower = ind.boll(self.closes(), period, k)
        return self._last(mid), self._last(upper), self._last(lower)

    def atr(self, period: int = 14) -> Optional[float]:
        return self._last(ind.atr(self.highs(), self.lows(), self.closes(), period))

    def kdj(self, n: int = 9, m1: int = 3, m2: int = 3):
        """返回 (k, d, j) 当前值。"""
        k, d, j = ind.kdj(self.highs(), self.lows(), self.closes(), n, m1, m2)
        return self._last(k), self._last(d), self._last(j)

    def obv(self) -> Optional[float]:
        return self._last(ind.obv(self.closes(), self.volumes()))

    def mom(self, period: int = 10) -> Optional[float]:
        return self._last(ind.mom(self.closes(), period))

    def bias(self, period: int = 6) -> Optional[float]:
        return self._last(ind.bias(self.closes(), period))

    def willr(self, period: int = 14) -> Optional[float]:
        return self._last(ind.willr(self.highs(), self.lows(), self.closes(), period))

    # —— 软风控（ctx.risk，设计 v1.0：超限自动拦截）——
    @property
    def risk(self) -> "RiskGuard":
        """返回软风控对象：检查 max_drawdown_limit / daily_loss_limit / position_limit。

        用法：``if ctx.risk.breached: ctx.sell("risk_forced", ctx.risk.reason)``
        """
        if self._risk_guard is None:
            self._risk_guard = RiskGuard(self)
        return self._risk_guard


class RiskGuard:
    """单标的软风控（ctx.risk）。

    从策略参数读取上限（0/缺省=关闭），实时计算是否越线：
    - max_drawdown_limit: 自净值峰值回撤比例上限（0.15 = 15%）
    - daily_loss_limit:   单日跌幅上限（0.03 = 3%）
    - position_limit:     单标的最大仓位比例（0.3 = 30%）

    用法（策略 on_bar 内）：
        if ctx.risk.breached:
            ctx.sell("risk_forced", ctx.risk.reason)
    """

    def __init__(self, ctx: StrategyContext):
        self.ctx = ctx
        self._reason = ""

    @property
    def equity(self) -> float:
        ec = self.ctx.engine.equity_curve
        return ec[-1].equity if ec else float(self.ctx.engine.initial_cash)

    @property
    def peak(self) -> float:
        ec = self.ctx.engine.equity_curve
        return max((p.equity for p in ec), default=self.equity)

    @property
    def drawdown(self) -> float:
        peak = self.peak
        return (self.equity / peak - 1.0) if peak > 0 else 0.0

    @property
    def position_ratio(self) -> float:
        eng = self.ctx.engine
        equity = self.equity
        if equity <= 0:
            return 0.0
        price = float(self.ctx.bar["close"]) if self.ctx.bar else 0.0
        return (eng.shares * price) / equity if price > 0 else 0.0

    @property
    def breached(self) -> bool:
        p = self.ctx.params
        mdd = float(p.get("max_drawdown_limit", 0) or 0)
        dll = float(p.get("daily_loss_limit", 0) or 0)
        pl = float(p.get("position_limit", 0) or 0)
        if mdd > 0 and self.drawdown <= -mdd:
            self._reason = f"回撤 {self.drawdown:.1%} 超过上限 {mdd:.1%}"
            return True
        ec = self.ctx.engine.equity_curve
        if dll > 0 and len(ec) >= 2:
            prev = ec[-2].equity
            day_ret = self.equity / prev - 1.0 if prev > 0 else 0.0
            if day_ret <= -dll:
                self._reason = f"单日跌幅 {day_ret:.1%} 超过上限 {dll:.1%}"
                return True
        if pl > 0 and self.position_ratio > pl:
            self._reason = f"仓位 {self.position_ratio:.1%} 超过上限 {pl:.1%}"
            return True
        self._reason = ""
        return False

    @property
    def reason(self) -> str:
        return self._reason


class BacktestEngine:
    def __init__(
        self,
        bars: List[dict],
        initial_cash: float = 1_000_000.0,
        commission: float = 0.0003,
        slippage: float = 0.0,
        rebalance_tol: float = 0.005,
        min_commission: float = 5.0,
        lot_size: int = 100,
        limit_enabled: bool = True,
    ):
        # bars: 升序的日 K 线列表，字段含 open/high/low/close/volume/date
        self.bars = bars
        self.initial_cash = float(initial_cash)
        self.commission = float(commission)
        self.slippage = float(slippage)
        # 调仓容忍度：订单金额低于净值的该比例则跳过，避免残余现金造成碎单 churn
        self.rebalance_tol = float(rebalance_tol)
        # 实盘化参数
        self.min_commission = float(min_commission)  # 单笔最低佣金（元）
        self.lot_size = int(lot_size)                # 最小交易单位（A股 100 股/手）
        self.limit_enabled = bool(limit_enabled)     # 涨跌停限制（涨停买不进/跌停卖不出）
        self.prev_close = float(bars[0]["close"]) if bars else 0.0

        self.cash = self.initial_cash
        self.shares = 0.0
        self.avg_cost = 0.0

        self.trades: List[Trade] = []
        self.equity_curve: List[EquityPoint] = []
        self.round_trips: List[dict] = []
        self._current_trip_pnl = 0.0

    # —— 撮合 ——
    def order_target_percent(self, ctx: StrategyContext, pct: float, signal_type: str = "manual", signal_reason: str = "") -> None:
        if ctx.bar is None:
            return
        price = float(ctx.bar["close"])
        if price <= 0:
            return
        equity = self.cash + self.shares * price
        target_value = equity * pct
        target_shares = target_value / price
        delta = target_shares - self.shares
        self._execute(ctx, delta, price, self.prev_close, signal_type, signal_reason)

    def _execute(self, ctx: StrategyContext, delta_shares: float, price: float, prev_close: float = 0.0, signal_type: str = "manual", signal_reason: str = "") -> None:
        if abs(delta_shares) < 1e-6:
            return
        # 微小调仓过滤：订单金额占净值比例过小则跳过
        equity = self.cash + self.shares * price
        if equity > 0 and abs(delta_shares) * price < equity * self.rebalance_tol:
            return
        is_buy = delta_shares > 0
        # 涨跌停限制：涨停买不进、跌停卖不出（A股 ±10%，用 9.5% 阈值防浮点误差）
        if self.limit_enabled and prev_close and prev_close > 0:
            chg = price / prev_close - 1.0
            if is_buy and chg >= 0.095:
                return
            if (not is_buy) and chg <= -0.095:
                return
        # 最小交易单位（手）：向下取整到 lot_size 的整数倍
        if self.lot_size and self.lot_size > 1:
            signed = 1 if is_buy else -1
            delta_shares = math.floor(abs(delta_shares) / self.lot_size) * self.lot_size * signed
            if abs(delta_shares) < 1e-6:
                return
        exec_price = price * (1 + self.slippage * (1 if is_buy else -1))
        notional = abs(delta_shares) * exec_price
        commission = max(notional * self.commission, self.min_commission)
        pnl_val = 0.0

        if is_buy:
            new_shares = self.shares + delta_shares
            # 持仓成本含买入佣金
            self.avg_cost = (
                (self.avg_cost * self.shares + exec_price * delta_shares + commission)
                / new_shares
            )
            self.shares = new_shares
            self.cash -= notional + commission
            side = "BUY"
        else:
            sold = -delta_shares
            # 实现盈亏 = (成交价 - 持仓成本) * 卖出股数 - 卖出佣金
            realized = (exec_price - self.avg_cost) * sold - commission
            pnl_val = realized
            self.cash += notional - commission
            self.shares -= sold
            side = "SELL"
            # 若清仓，结束一个完整回合
            if self.shares <= 1e-9:
                self.shares = 0.0
                self._current_trip_pnl += realized
                self.round_trips.append({"pnl": self._current_trip_pnl})
                self._current_trip_pnl = 0.0
                self.avg_cost = 0.0
            else:
                self._current_trip_pnl += realized

        self.trades.append(
            Trade(
                date=ctx.bar["date"],
                side=side,
                price=round(exec_price, 4),
                shares=round(abs(delta_shares), 2),
                cash_after=round(self.cash, 2),
                commission=round(commission, 2),
                pnl=round(pnl_val, 2),
                signal_type=signal_type,
                signal_reason=signal_reason,
            )
        )

    # —— 主循环 ——
    def run(self, strategy_class: type, params: dict) -> BacktestResult:
        if not self.bars:
            raise ValueError("回测数据为空")
        strat: StandardStrategy = strategy_class()
        ctx = StrategyContext(self, params)
        strat.init(ctx)

        base_close = float(self.bars[0]["close"])
        for i, bar in enumerate(self.bars):
            # 上一根收盘价作为当日"昨收"，用于涨跌停判断
            self.prev_close = float(self.bars[i - 1]["close"]) if i > 0 else float(bar["open"])
            ctx.set_bar(bar, i)
            strat.on_bar(ctx, bar)
            price = float(bar["close"])
            equity = self.cash + self.shares * price
            benchmark = self.initial_cash * (price / base_close)
            self.equity_curve.append(
                EquityPoint(date=bar["date"], equity=round(equity, 2), benchmark=round(benchmark, 2))
            )

        return self._build_result(strategy_class, params)

    def _build_result(self, strategy_class, params: dict) -> BacktestResult:
        eq = [e.equity for e in self.equity_curve]
        n = len(eq)
        start = self.bars[0]["date"]
        end = self.bars[-1]["date"]

        total_return = (eq[-1] / eq[0] - 1) if n else 0.0
        annual_return = ((eq[-1] / eq[0]) ** (252.0 / n) - 1) if n > 1 else 0.0

        rets = [eq[i] / eq[i - 1] - 1 for i in range(1, n)] if n > 1 else []
        if rets:
            m = sum(rets) / len(rets)
            var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
            std = math.sqrt(var)
            sharpe = (m / std) * math.sqrt(252.0) if std > 0 else 0.0
        else:
            sharpe = 0.0

        peak = eq[0]
        mdd = 0.0
        for e in eq:
            if e > peak:
                peak = e
            dd = (e - peak) / peak if peak > 0 else 0.0
            if dd < mdd:
                mdd = dd

        trips = len(self.round_trips)
        wins = sum(1 for t in self.round_trips if t["pnl"] > 0)
        win_rate = (wins / trips) if trips else 0.0

        return BacktestResult(
            symbol=self.bars[0].get("symbol", ""),
            start=start,
            end=end,
            strategy_key=strategy_class.__name__,
            params=params,
            initial_cash=self.initial_cash,
            final_equity=round(eq[-1], 2),
            total_return=round(total_return, 6),
            annual_return=round(annual_return, 6),
            max_drawdown=round(mdd, 6),
            sharpe=round(sharpe, 4),
            win_rate=round(win_rate, 4),
            trade_count=len(self.trades),
            round_trips=trips,
            equity_curve=self.equity_curve,
            trades=self.trades,
        )
