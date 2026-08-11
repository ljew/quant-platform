"""缠论买卖点策略（单标的，long-only）。

逻辑：每根 bar 用『截至当前的全部已确认 K 线』做缠论分析，取最新产生的买卖点信号：
- 出现 一买 / 二买 / 三买 且 当前空仓 → 买入（满仓）；
- 出现 一卖 / 二卖 / 三卖 且 当前持仓 → 卖出（清仓）。

买卖点类型（buy1/buy2/buy3/sell1/sell2/sell3）随成交记录回传，便于前端标注。
缠论信号天然低频（笔级别），与双均线、RSI 等形成互补：偏逆向/抄底与回调买点。

注意：分型需要右侧 1 根确认，因此分析用 ctx.bars[:idx+1] 时最后一根不会成为
已确认分型，信号天然带 1 根延迟，避免未来函数。
"""
from __future__ import annotations

from app.core.engine.base_strategy import StandardStrategy
from app.core.engine.chan import analyze_chan


class ChanStrategy(StandardStrategy):
    params = {"bi_gap": 4, "need_trend": 2, "use_sell": 1}

    def init(self, ctx) -> None:
        self.bi_gap = int(ctx.params.get("bi_gap", 4))
        self.need_trend = int(ctx.params.get("need_trend", 2))
        self.use_sell = int(ctx.params.get("use_sell", 1))
        self.last_idx = -1
        self.have_position = False
        self._cache_n = -1
        self._cache = None

    def on_bar(self, ctx, bar) -> None:
        # 数据量不足以形成笔与中枢时观望
        if ctx.index < self.bi_gap + 8:
            return
        bars = ctx.bars[: ctx.index + 1]
        # 每个 index 只算一次（避免重复全量计算）
        if self._cache_n != ctx.index:
            self._cache = analyze_chan(bars, self.bi_gap, self.need_trend)
            self._cache_n = ctx.index
        res = self._cache
        signals = res.get("signals") or []
        if not signals:
            return
        # 取 idx 最大的信号
        last = max(signals, key=lambda s: s["idx"])
        if last["idx"] <= self.last_idx:
            return  # 旧信号已处理
        self.last_idx = last["idx"]
        t = last["type"]
        reason = last.get("reason", "")
        if t.startswith("buy") and not self.have_position:
            ctx.order_target_percent(1.0, t, reason)
            self.have_position = True
        elif t.startswith("sell") and self.have_position:
            if self.use_sell:
                ctx.order_target_percent(0.0, t, reason)
                self.have_position = False
        # buy 但已持仓 / sell 但空仓：忽略，避免对敲
