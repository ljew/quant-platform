"""MACD + 布林带策略（完整版 SDK 示例：新指标库 + ctx.risk 软风控）。

逻辑：
- MACD 金叉（DIF 上穿 DEA）且价格在布林中轨上方 → 满仓做多；
- MACD 死叉（DIF 下穿 DEA）→ 清仓；
- 任何时刻触发软风控（回撤/单日亏损/仓位上限）→ 强制清仓（signal_type=risk_forced）。

典型参数：fast=12, slow=26, signal=9, boll_period=20,
max_drawdown_limit=0.15（15% 回撤止损）, position_limit=0.0（关闭）。
"""
from __future__ import annotations

from app.core.engine.base_strategy import StandardStrategy


class MacdBollStrategy(StandardStrategy):
    params = {
        "fast": 12, "slow": 26, "signal": 9,
        "boll_period": 20, "boll_k": 2.0,
        "max_drawdown_limit": 0.15, "daily_loss_limit": 0.0, "position_limit": 0.0,
    }

    def init(self, ctx):
        pass

    def on_bar(self, ctx, bar):
        # ① 软风控优先：越线立即清仓
        if ctx.risk.breached:
            if ctx.engine.shares > 0:
                ctx.sell("risk_forced", f"风控触发：{ctx.risk.reason}")
            return

        fast = int(ctx.params.get("fast", 12))
        slow = int(ctx.params.get("slow", 26))
        signal = int(ctx.params.get("signal", 9))
        bp = int(ctx.params.get("boll_period", 20))
        bk = float(ctx.params.get("boll_k", 2.0))

        dif, dea, hist = ctx.macd(fast, slow, signal)
        mid, up, lo = ctx.boll(bp, bk)
        if dif is None or dea is None or mid is None:
            return  # 数据不足

        price = float(bar["close"])
        prev_hist = None
        if ctx.index > 0:
            # 用截至上一根 bar 的 MACD 判断金叉/死叉（避免用到当前 bar 的未来信息）
            prev_closes = ctx.closes()[:-1]
            if len(prev_closes) >= slow + signal:
                pdif, pdea, phist = _macd_last(prev_closes, fast, slow, signal)
                prev_hist = phist

        cross_up = prev_hist is not None and prev_hist <= 0 and hist is not None and hist > 0
        cross_dn = prev_hist is not None and prev_hist >= 0 and hist is not None and hist < 0

        if cross_up and price >= mid:
            ctx.buy("macd_boll", f"MACD金叉(hist {hist:.3f})且价在布林中轨上方({price:.2f}>={mid:.2f})")
        elif cross_dn:
            ctx.sell("macd_boll", f"MACD死叉(hist {hist:.3f})")
        elif price < lo and ctx.engine.shares > 0:
            # 跌破布林下轨视为趋势破位，离场
            ctx.sell("boll_break", f"跌破布林下轨 {lo:.2f}（现价 {price:.2f}）")


def _macd_last(closes, fast, slow, signal):
    """计算给定收盘序列的最后一根 MACD (dif, dea, hist)。"""
    from app.core.engine import indicators as ind

    dif, dea, hist = ind.macd(closes, fast, slow, signal)
    return dif[-1], dea[-1], hist[-1]
