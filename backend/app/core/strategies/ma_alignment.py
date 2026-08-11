"""均线多头排列策略。

核心逻辑：用三条均线（短 / 中 / 长）的排列方向判断趋势强弱：
- 多头排列：短 > 中 > 长 → 上升趋势确立，持仓（100%）；
- 空头排列：短 < 中 < 长 → 下降趋势，空仓（0%）；
- 其他（缠绕/交叉中）→ 保持现有仓位，不频繁调仓。

相比「双均线」只在快/慢两线相对位置切换，本策略用三条均线，
对趋势的确认更严格，能过滤一部分假突破与震荡市的噪音。
典型参数 short=5, mid=20, long=60。
"""
from __future__ import annotations

from app.core.engine.base_strategy import StandardStrategy


class MAAlignmentStrategy(StandardStrategy):
    params = {"short": 5, "mid": 20, "long": 60}

    def init(self, ctx) -> None:
        pass

    def on_bar(self, ctx, bar) -> None:
        short_p = int(ctx.params.get("short", 5))
        mid_p = int(ctx.params.get("mid", 20))
        long_p = int(ctx.params.get("long", 60))
        s = ctx.sma(short_p)
        m = ctx.sma(mid_p)
        l = ctx.sma(long_p)
        if s is None or m is None or l is None:
            return
        if s > m > l:
            reason = (f"均线多头排列：MA{short_p}={s:.2f} > MA{mid_p}={m:.2f} > MA{long_p}={l:.2f}，"
                      f"短中长期趋势同向向上，持仓。")
            ctx.order_target_percent(1.0, "均线多头", reason)
        elif s < m < l:
            reason = (f"均线空头排列：MA{short_p}={s:.2f} < MA{mid_p}={m:.2f} < MA{long_p}={l:.2f}，"
                      f"短中长期趋势同向向下，空仓。")
            ctx.order_target_percent(0.0, "均线空头", reason)
        # 缠绕区间：维持当前仓位
