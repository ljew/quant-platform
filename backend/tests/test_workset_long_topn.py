"""W48-2 薄壳 WorksetMemberLongTopN：排序、权重帽、无空头、无中性、0 值不下单。"""
from __future__ import annotations

from pathlib import Path

from app.core.strategies.workset_long_topn import (
    DEFAULT_MAX_WEIGHT,
    DEFAULT_TOP_N,
    WorksetMemberLongTopN,
    peek_last_run_stats,
)


BACKEND = Path(__file__).resolve().parents[1]
SRC = (BACKEND / "app/core/strategies/workset_long_topn.py").read_text(encoding="utf-8")


class FakeEngine:
    def __init__(self, hist=None, data=None, fundamentals=None):
        self.hist = hist or {}
        self.hist_dates = {s: [f"2023-01-{i+1:02d}" for i in range(len(h))] for s, h in self.hist.items()}
        self.data = data or {}
        self.fundamentals = fundamentals or {}
        self.financials_raw = {}
        self.positions = {}
        self.bench_map = {}
        self.bench_hist = [1.0] * 200


class FakeCtx:
    def __init__(self, universe, *, hist_len=120, held=None, params=None):
        self._universe = list(universe)
        self._held = dict(held or {})
        self.orders: list[tuple[str, float]] = []
        self.reported: list[tuple] = []
        self.params = params or {
            "expr": "roc(c_m, 20)",
            "top_n": DEFAULT_TOP_N,
            "max_weight": DEFAULT_MAX_WEIGHT,
        }
        closes = [100.0 + i * 0.1 for i in range(hist_len)]
        self.engine = FakeEngine(hist={s: list(closes) for s in universe})
        # 已持仓可能不在当前池内
        for s in self._held:
            if s not in self.engine.hist:
                self.engine.hist[s] = list(closes)
                self.engine.hist_dates[s] = [f"2023-01-{i+1:02d}" for i in range(hist_len)]

    def universe(self):
        return list(self._universe)

    def history(self, symbol, n):
        h = self.engine.hist.get(symbol, [])
        if len(h) < n:
            return None
        return h[-n:]

    def history_dates(self, symbol, n):
        h = self.engine.hist_dates.get(symbol, [])
        if len(h) < n:
            return None
        return h[-n:]

    def benchmark_aligned(self, symbol):
        return [1.0] * len(self.engine.hist.get(symbol, []))

    def benchmark_history(self, n):
        return [1.0] * n

    def attribute(self, symbol, key):
        return None

    def financial(self, symbol):
        return []

    def positions(self):
        return dict(self._held)

    def report_factor(self, symbol, name, value):
        self.reported.append((symbol, name, value))

    def order_target_percent(self, symbol, pct, *a, **k):
        self.orders.append((symbol, float(pct)))


def _run(values: dict, *, held=None, hist_len=120, top_n=50, universe=None):
    univ = universe if universe is not None else list(values)
    ctx = FakeCtx(univ, hist_len=hist_len, held=held, params={
        "expr": "roc(c_m, 20)", "top_n": top_n, "max_weight": 0.05,
    })
    st = WorksetMemberLongTopN()
    st.init(ctx)
    st._eval_one = lambda ctx, sym, date, _v=values: _v.get(sym)  # noqa: ARG005
    st.rebalance(ctx, "2024-06-01")
    return st, ctx


def test_source_does_not_inherit_or_neutralize():
    assert "class WorksetMemberLongTopN(PortfolioStrategy)" in SRC
    assert "class WorksetMemberLongTopN(EnhancedFactorStrategy)" not in SRC
    assert "class WorksetMemberLongTopN(Csi800EnhancedStrategy)" not in SRC
    assert "import EnhancedFactorStrategy" not in SRC
    assert "import Csi800EnhancedStrategy" not in SRC
    assert "_neutralize_by_group" not in SRC
    assert "_select_by_industry_quota" not in SRC


def test_select_top_50_equal_weight_cap():
    values = {f"s{i:03d}": float(i) for i in range(80)}  # 最高 79..30 共 50 只
    st, ctx = _run(values, top_n=50)
    buys = {s: w for s, w in ctx.orders if w > 0}
    assert len(buys) == 50
    expected = {f"s{i:03d}" for i in range(30, 80)}
    assert set(buys) == expected
    assert all(abs(w - 0.02) < 1e-12 for w in buys.values())  # min(1/50, 0.05)
    assert all(w <= 0.05 + 1e-12 for w in buys.values())
    assert all(w >= 0 for _, w in ctx.orders)
    assert st._n_held_last == 50
    assert peek_last_run_stats()["n_held_last"] == 50


def test_less_than_50_holds_actual_and_caps_weight():
    values = {f"s{i:03d}": float(i) for i in range(10)}
    st, ctx = _run(values, top_n=50)
    buys = {s: w for s, w in ctx.orders if w > 0}
    assert len(buys) == 10
    # n<20 时 1/n=0.1 > 0.05，宁可留现金，不突破 5%
    assert all(abs(w - 0.05) < 1e-12 for w in buys.values())
    assert st._n_held_last == 10


def test_zero_valid_does_not_open_long_and_clears_held():
    values = {f"s{i:03d}": None for i in range(8)}
    held = {"s000": 100.0, "out_of_index": 50.0}
    st, ctx = _run(values, held=held, universe=list(values))
    assert st._n_valid_last == 0
    assert all(w == 0.0 for _, w in ctx.orders)
    # 已持仓（含不在池内）被清到 0；没有正权重开仓
    cleared = {s for s, w in ctx.orders if w == 0.0}
    assert "s000" in cleared
    assert "out_of_index" in cleared
    assert not any(w > 0 for _, w in ctx.orders)


def test_held_not_in_universe_is_cleared():
    values = {f"s{i:03d}": float(i) for i in range(5)}
    _, ctx = _run(values, held={"ghost": 10.0})
    ghost = [w for s, w in ctx.orders if s == "ghost"]
    assert ghost == [0.0]


def test_skip_short_history():
    univ = ["short", "long"]
    ctx = FakeCtx(univ, hist_len=120)
    ctx.engine.hist["short"] = [100.0] * 20
    ctx.engine.hist_dates["short"] = [f"2023-01-{i+1:02d}" for i in range(20)]
    st = WorksetMemberLongTopN()
    st.init(ctx)
    # 短窗应跳过，不求值成功
    assert st._eval_one(ctx, "short", "2024-06-01") is None


def test_no_negative_orders():
    values = {f"s{i:03d}": float(-i) for i in range(5)}  # 全部有限，仍只做多
    _, ctx = _run(values)
    assert all(w >= 0 for _, w in ctx.orders)
