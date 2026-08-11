"""布林带均值回归策略。

核心逻辑（基于截至当前 bar 的收盘价序列）：
- 中轨 = N 日移动平均（mid）；
- 上轨 = mid + k × 标准差（upper）；下轨 = mid − k × 标准差（lower）；
- 价格 < 下轨 → 超跌，买入（持仓 100%）；
- 价格 > 上轨 → 超涨，卖出（清仓）；
- 轨道之间 → 保持仓位。

属典型的均值回归（逆势）策略，适合震荡行情；单边趋势中可能反复止损。
典型参数 period=20, num_std=2.0。
"""
from __future__ import annotations

from app.core.engine.base_strategy import StandardStrategy
from app.core.engine import indicators as ind


class BollingerStrategy(StandardStrategy):
    params = {"period": 20, "num_std": 2.0}

    def init(self, ctx) -> None:
        pass

    def on_bar(self, ctx, bar) -> None:
        period = int(ctx.params.get("period", 20))
        k = float(ctx.params.get("num_std", 2.0))
        closes = ctx.closes()
        mid = ind.sma(closes, period)
        sd = ind.stddev(closes, period)
        mid_v = mid[-1] if mid else None
        sd_v = sd[-1] if sd else None
        if mid_v is None or sd_v is None:
            return
        upper = mid_v + k * sd_v
        lower = mid_v - k * sd_v
        price = float(bar["close"])
        if sd_v > 0:
            z = (price - mid_v) / sd_v
        else:
            z = 0.0
        if price < lower:
            reason = (f"布林下轨：价 {price:.2f} < 下轨 {lower:.2f}"
                      f"（中轨 {mid_v:.2f} {k}σ），偏离 {z:.2f}σ，超跌，均值回归买入。")
            ctx.order_target_percent(1.0, "下轨买入", reason)
        elif price > upper:
            reason = (f"布林上轨：价 {price:.2f} > 上轨 {upper:.2f}"
                      f"（中轨 {mid_v:.2f} {k}σ），偏离 +{z:.2f}σ，超涨，回落卖出。")
            ctx.order_target_percent(0.0, "上轨卖出", reason)
