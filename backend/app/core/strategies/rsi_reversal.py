"""RSI 反转策略（均值回归）。

核心逻辑：
- RSI 跌破超卖线（默认 30）→ 认为短期超跌，买入（持仓 100%）；
- RSI 突破超买线（默认 70）→ 认为短期超涨，卖出（清仓）；
- 处于中间区 → 保持现有仓位，不频繁交易。

与「双均线 / 动量」等趋势类策略互补：趋势策略在单边市表现好，
反转策略在震荡市表现好。典型参数 period=14, oversold=30, overbought=70。
"""
from __future__ import annotations

from app.core.engine.base_strategy import StandardStrategy


class RSIReversalStrategy(StandardStrategy):
    params = {"period": 14, "oversold": 30, "overbought": 70}

    def init(self, ctx) -> None:
        self.overbought = float(ctx.params.get("overbought", 70))
        self.oversold = float(ctx.params.get("oversold", 30))

    def on_bar(self, ctx, bar) -> None:
        period = int(ctx.params.get("period", 14))
        rsi = ctx.rsi(period)
        if rsi is None:
            return  # 数据不足，观望
        if rsi < self.oversold:
            reason = (f"RSI{period}={rsi:.1f} < 超卖线 {self.oversold}，短期超跌，"
                      f"均值回归反弹概率上升，买入。")
            ctx.order_target_percent(1.0, "RSI超卖", reason)
        elif rsi > self.overbought:
            reason = (f"RSI{period}={rsi:.1f} > 超买线 {self.overbought}，短期超涨，"
                      f"回落风险上升，卖出。")
            ctx.order_target_percent(0.0, "RSI超买", reason)
        # 中间区：维持当前仓位（不调仓，避免频繁交易）
