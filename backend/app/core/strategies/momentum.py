"""动量策略：基于 N 日收益率（ROC）追涨杀跌。

- ROC > 阈值 → 买入（动量向上）；
- ROC <= 阈值 → 卖出（动量转弱）。

典型参数：lookback=20, threshold=0.0。
属趋势跟随的另一种表达，与均线类互补。
"""
from __future__ import annotations

from app.core.engine.base_strategy import StandardStrategy


class MomentumStrategy(StandardStrategy):
    params = {"lookback": 20, "threshold": 0.0}

    def init(self, ctx):
        pass

    def on_bar(self, ctx, bar):
        lookback = int(ctx.params.get("lookback", 20))
        threshold = float(ctx.params.get("threshold", 0.0))
        roc = ctx.roc(lookback)
        if roc is None:
            return
        if roc > threshold:
            reason = (f"动量向上：{lookback}日收益率 ROC={roc*100:.2f}% > 阈值 {threshold*100:.2f}%，"
                      f"价格动能为正，持仓/买入。")
            ctx.order_target_percent(1.0, "动量向上", reason)
        else:
            reason = (f"动量转弱：ROC={roc*100:.2f}% ≤ 阈值 {threshold*100:.2f}%，"
                      f"动能衰竭，空仓。")
            ctx.order_target_percent(0.0, "动量转弱", reason)
