"""双均线趋势策略：快线上穿/高于慢线时持仓，否则空仓。

典型参数：fast=5, slow=20（日线）。
适合趋势行情；震荡市可能频繁止损（whipsaw），属正常特性。
"""
from __future__ import annotations

from app.core.engine.base_strategy import StandardStrategy


class DualMAStrategy(StandardStrategy):
    params = {"fast": 5, "slow": 20}

    def init(self, ctx):
        pass

    def on_bar(self, ctx, bar):
        fast_p = int(ctx.params.get("fast", 5))
        slow_p = int(ctx.params.get("slow", 20))
        fast = ctx.sma(fast_p)
        slow = ctx.sma(slow_p)
        if fast is None or slow is None:
            return  # 数据不足，观望
        price = float(bar["close"])
        if fast > slow:
            gap = (fast / slow - 1.0) * 100
            reason = (f"双均线多头：快线 MA{fast_p}={fast:.2f} 上穿慢线 MA{slow_p}={slow:.2f}"
                      f"（乖离 +{gap:.2f}%），短期趋势强于长期，持仓。")
            ctx.order_target_percent(1.0, "均线多头", reason)
        else:
            gap = (fast / slow - 1.0) * 100
            reason = (f"双均线空头：快线 MA{fast_p}={fast:.2f} 下穿/低于慢线 MA{slow_p}={slow:.2f}"
                      f"（乖离 {gap:.2f}%），趋势转弱/向下，空仓。")
            ctx.order_target_percent(0.0, "均线空头", reason)
