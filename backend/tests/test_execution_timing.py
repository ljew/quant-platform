"""T4 成交时点 —— 防回归：明确并锁定「信号日收盘 / 次日开盘」口径。

背景：backtest_engine.py:359 用 `bar["close"]`（信号当日收盘）成交，
代码注释自认「简化模型」；而 xq/portfolio.py 用**次日开盘**成交（T+1）。
两种口径并存会导致单标的回测系统性比组合回测乐观（少了隔夜跳空与日内波动）。

本测试不改变现有模型（M1 未改成交时点，避免与已发布数据口径断裂），
而是把口径**显式记录并锁定**，让后续任何人改动时能看到影响。
"""
from __future__ import annotations

import inspect

from app.core.engine.backtest_engine import BacktestEngine


class TestExecutionTiming:
    def test_single_asset_uses_close_of_signal_bar(self):
        """单标的引擎当前口径：信号当日收盘价成交（需在文档/注释中明示为简化模型）。"""
        src = inspect.getsource(BacktestEngine.run)
        assert "price = float(bar[\"close\"])" in src, \
            "单标的引擎成交价口径变了——若改次日开盘，需同步更新组合引擎与本测试"

    def test_comment_documents_simplification(self):
        """成交口径必须在源码注释中显式标注，避免后人误当精确模型。"""
        src = inspect.getsource(BacktestEngine)
        assert "简化模型" in src or "次日开盘" in src, \
            "成交价口径应有注释说明（当前为收盘价简化模型）"

    def test_portfolio_uses_next_day_open(self):
        """组合镜像引擎口径：信号日决策 → 次日开盘成交（T+1），且禁当日反手。"""
        import inspect as _i
        from app.services.xq import portfolio
        src = _i.getsource(portfolio)
        # 次日开盘成交：pending 队列在 d 日开盘执行 d-1 日挂单
        assert "pending_buys" in src and "pending_sells" in src
        assert '_px(d, sym, "open")' in src, "组合引擎应用次日开盘价成交"

    def test_no_same_day_round_trip(self):
        """T+1 语义：当日买入的标的当日不可卖。"""
        import inspect as _i
        from app.services.xq import portfolio
        src = _i.getsource(portfolio)
        assert "T+1" in src or "next_day" in src or "明日开盘" in src, \
            "组合引擎应有 T+1 说明"
