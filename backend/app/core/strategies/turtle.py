"""唐奇安通道突破（海龟）策略。

经典海龟交易法则的简化版，属于「突破追涨」型趋势跟随：
- 入场：收盘价创 N 日新高（突破上轨）→ 买入，满仓；
- 离场：收盘价创 M 日新低（跌破下轨）→ 卖出，清仓。

使用当根 bar 的 high/low 计算通道上下轨（标准 Donchian 通道）。
N（entry）通常取 20、M（exit）取 10。长周期能捕捉大趋势、过滤噪音；
短周期更灵敏但交易更频繁。
"""
from __future__ import annotations

from app.core.engine.base_strategy import StandardStrategy


class TurtleStrategy(StandardStrategy):
    params = {"entry": 20, "exit": 10}

    def init(self, ctx) -> None:
        pass

    def on_bar(self, ctx, bar) -> None:
        entry = int(ctx.params.get("entry", 20))
        exit_n = int(ctx.params.get("exit", 10))
        idx = ctx.index
        bars = ctx.bars
        if idx < entry:
            return  # 数据不足，观望
        # 上轨：最近 entry 根 K 线的最高价；下轨：最近 exit 根 K 线的最低价
        hh = max(bars[i]["high"] for i in range(idx - entry + 1, idx + 1))
        ll = min(bars[i]["low"] for i in range(idx - exit_n + 1, idx + 1))
        price = float(bar["close"])
        if price >= hh:
            reason = (f"突破上轨：收盘价 {price:.2f} 创 {entry} 日新高（上轨 {hh:.2f}），"
                      f"趋势突破，追涨买入。")
            ctx.order_target_percent(1.0, "突破买入", reason)
        elif price <= ll:
            reason = (f"跌破下轨：收盘价 {price:.2f} 创 {exit_n} 日新低（下轨 {ll:.2f}），"
                      f"趋势破位，离场卖出。")
            ctx.order_target_percent(0.0, "破位卖出", reason)
