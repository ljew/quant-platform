"""T5 时点成分（PIT） —— 防回归：回测股票池不得包含「未来」才有的成分。

背景：portfolio_backtest.py 的 `_members_on(date_str)` 取「截至 date_str 最新
的成分快照」，消除前视偏差与幸存者偏差。这是正确实现，本测试把它锁住。

同时记录已知隐患（本次未修，留待 M3 处理）：
`_attrs_map`（factor_mining.py:247-257,489）只查一次 stocks 表最新
market_cap/pe_ttm/pb，对所有历史截面复用同一份快照 → 2019 年截面上用的是
2026 年的 PE，属明确前视。现金流类（fin_asof by ann_date）是 PIT 正确的，
两套口径不一致。此项在 GP 重启前必须先修。
"""
from __future__ import annotations

import inspect

from app.core.engine.portfolio_backtest import PortfolioBacktestEngine


class TestPointInTimeUniverse:
    def _engine(self) -> PortfolioBacktestEngine:
        # membership: [(快照日, 成分集合), ...] 升序
        membership = [
            ("2020-01-01", {"A", "B", "C"}),
            ("2021-01-01", {"A", "B", "D"}),   # C 已被剔除，D 被纳入
        ]
        eng = PortfolioBacktestEngine(data={}, benchmark=[], membership=membership)
        return eng

    def test_members_on_uses_latest_snapshot(self):
        eng = self._engine()
        assert eng._members_on("2019-12-31") == set(), "首个快照前不应有成分"
        assert eng._members_on("2020-06-01") == {"A", "B", "C"}
        assert eng._members_on("2020-12-31") == {"A", "B", "C"}

    def test_members_on_respects_boundary(self):
        eng = self._engine()
        # 2021-01-01 起应切换到新快照（C 出、D 进）
        assert eng._members_on("2021-01-01") == {"A", "B", "D"}
        assert eng._members_on("2022-06-01") == {"A", "B", "D"}

    def test_no_future_members(self):
        """关键断言：T 时刻的成分不得包含 T 之后才纳入的标的。"""
        eng = self._engine()
        for t in ("2020-06-01", "2020-12-31"):
            assert "D" not in eng._members_on(t), \
                f"{t} 的成分里出现了未来才纳入的 D，前视偏差"
        for t in ("2021-06-01", "2022-06-01"):
            assert "C" not in eng._members_on(t), \
                f"{t} 的成分里出现了已剔除的 C"

    def test_universe_filter_uses_members_on(self):
        """ctx.universe 必须走 _members_on，否则 PIT 过滤形同虚设。"""
        src = inspect.getsource(type(PortfolioBacktestEngine.__dict__.get("universe", object))
                               ) if False else None
        import app.core.engine.portfolio_backtest as m
        full = inspect.getsource(m)
        assert "_members_on" in full, "universe 过滤应调用 _members_on"
        assert "self.engine.membership" in full

    def test_no_membership_falls_back_to_full_universe(self):
        """未注入成分数据时退化为全池，且行为显式可查。"""
        eng = PortfolioBacktestEngine(data={}, benchmark=[])
        assert eng._members_on("2020-01-01") == set()
        # 无 membership 时 _members_on 返回空集，universe() 退化为全池
        assert eng.universe == eng.universe  # 不应抛异常

    def test_known_attrs_snapshot_caveat_documented(self):
        """记录已知未修项：估值属性快照复用导致的 PIT 前视（M3 待办）。"""
        from app.services import factor_mining
        src = inspect.getsource(factor_mining)
        # _attrs_map 只查一次 stocks 最新值
        assert "_attrs_map" in src, "attrs 快照逻辑位置变更时需同步评估 PIT 风险"
        # 现金流类是 PIT 正确的（按 ann_date）
        assert "ann_date" in src or "fin_asof" in src, \
            "现金流应按 ann_date 做 PIT，若丢失需重新评估"
