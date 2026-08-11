"""均线金叉/死叉策略：基于 EMA 快慢线，仅在「交叉」发生时交易。

- 金叉（快线上穿慢线）→ 买入；
- 死叉（快线下穿慢线）→ 卖出。

相比「双均线趋势」（每根 bar 都按相对位置调仓），本策略只在交叉事件触发，
交易次数更少、对震荡市更稳健。
"""
from __future__ import annotations

from app.core.engine.base_strategy import StandardStrategy


class MACrossStrategy(StandardStrategy):
    params = {"fast": 5, "slow": 20}

    def init(self, ctx):
        self._prev_golden = None  # None=未知, True=金叉状态, False=死叉状态
        self._prev_fast = None
        self._prev_slow = None

    def on_bar(self, ctx, bar):
        fast_p = int(ctx.params.get("fast", 5))
        slow_p = int(ctx.params.get("slow", 20))
        fast = ctx.ema(fast_p)
        slow = ctx.ema(slow_p)
        if fast is None or slow is None:
            return
        golden = fast > slow
        if self._prev_golden is None:
            # 首根有效 bar：若已处于金叉状态则直接建仓；否则仅记录状态
            self._prev_golden = golden
            self._prev_fast, self._prev_slow = fast, slow
            if golden:
                reason = f"开盘即金叉：EMA{fast_p}={fast:.2f} > EMA{slow_p}={slow:.2f}，直接建仓。"
                ctx.buy("金叉买入", reason)
            return
        # 仅在状态切换（交叉）时下单
        if golden and not self._prev_golden:
            reason = (f"金叉：EMA{fast_p} 由 {self._prev_fast:.2f} 上穿 EMA{slow_p}"
                      f"（{slow:.2f}），短期均线上穿长期均线，趋势转多，买入。")
            ctx.buy("金叉买入", reason)
        elif not golden and self._prev_golden:
            reason = (f"死叉：EMA{fast_p} 由 {self._prev_fast:.2f} 下穿 EMA{slow_p}"
                      f"（{slow:.2f}），短期均线下穿长期均线，趋势转空，卖出。")
            ctx.sell("死叉卖出", reason)
        self._prev_golden = golden
        self._prev_fast, self._prev_slow = fast, slow
