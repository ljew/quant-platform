"""W46-2 / M3-3 walk-forward OOS 纯函数与编排验收（§6.3）。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.schemas import WalkForwardRequest
from app.services.walk_forward import (
    aggregate_verdicts,
    clamp_oos_ratio,
    judge_strategy_window,
    make_expanding_windows,
    run_walk_forward,
    single_split_index,
    stricter,
)


def _dates(n: int, start="2019-01-01") -> list[str]:
    """生成 n 个伪交易日（按自然日递推，仅作切窗下标用）。"""
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    out = []
    d = d0
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _mk(sharpe=0.0, excess=0.0, total=0.0, trades=5, n_bars=0):
    return SimpleNamespace(
        sharpe=sharpe,
        excess_return=excess,
        total_return=total,
        max_drawdown=-0.1,
        trade_count=trades,
        annual_return=0.0,
        final_equity=1.0,
        equity_curve=[],
        n_bars=n_bars,
    )


class TestClampAndSplit:
    def test_oos_ratio_default_and_clamp(self):
        assert clamp_oos_ratio(None) == 0.3
        assert clamp_oos_ratio(0.9) == 0.6
        assert clamp_oos_ratio(-1) == 0.0

    def test_single_split_matches_optimize_formula(self):
        # 与 _optimize_core：split_i = max(int(T*(1-r)), 30)；OOS>=20；T>=60
        T, r = 100, 0.3
        assert single_split_index(T, r) == max(int(T * (1 - r)), 30)
        assert single_split_index(50, 0.3) is None  # <60
        assert single_split_index(100, 0.0) is None
        # OOS 太短
        # T=40 but <60 already None; craft T=60, r small so OOS <20?
        # split = max(int(60*0.9),30)=54 → OOS=6 <20 → None。但 clamp r max 0.6 → int(60*0.4)=24 → max(24,30)=30, OOS=30 OK
        # T=60,r=0.3 → split=max(42,30)=42, OOS=18<20 → 不分
        assert single_split_index(60, 0.3) is None
        assert single_split_index(80, 0.3) == max(int(80 * 0.7), 30)


class TestExpandingWindows:
    def test_no_leak_and_cover(self):
        T, n = 200, 3
        wins = make_expanding_windows(T, n, min_is_bars=60, min_oos_bars=20)
        assert len(wins) == 3
        assert wins[0].oos_start_i == 60
        assert wins[-1].oos_end_i == T
        prev_end = 60
        for w in wins:
            assert w.is_end_i == w.oos_start_i
            assert w.oos_start_i >= prev_end
            assert w.oos_start_i < w.oos_end_i
            # IS 终点下标 == OOS 起点 → 日期严格无重叠
            assert w.is_end_i <= w.oos_start_i
            prev_end = w.oos_end_i

    def test_n_windows_1_like_tail(self):
        wins = make_expanding_windows(100, 1, min_is_bars=60)
        assert len(wins) == 1
        assert wins[0].oos_start_i == 60 and wins[0].oos_end_i == 100


class TestJudgeAndAggregate:
    def test_pass_does_not_require_same_sign(self):
        assert judge_strategy_window(-0.5, 0.1, oos_bars=30)[0] == "通过"
        assert judge_strategy_window(0.5, 0.0, oos_bars=30)[0] == "通过"

    def test_overfit_and_fail(self):
        assert judge_strategy_window(1.0, -0.2, oos_bars=30)[0] == "过拟合"
        assert judge_strategy_window(-0.1, -0.2, oos_bars=30)[0] == "未通过"
        assert judge_strategy_window(0.0, -0.2, oos_bars=30)[0] == "未通过"

    def test_insufficient(self):
        assert judge_strategy_window(1.0, 1.0, oos_bars=5)[0] == "样本不足"
        assert judge_strategy_window(None, 1.0, oos_bars=30)[0] == "样本不足"

    def test_aggregate_priority(self):
        assert aggregate_verdicts(["通过", "过拟合", "通过"]) == "过拟合"
        assert aggregate_verdicts(["通过", "未通过"]) == "未通过"
        assert aggregate_verdicts(["通过", "通过", "样本不足"]) == "通过"
        assert aggregate_verdicts(["样本不足", "样本不足"]) == "样本不足"
        assert aggregate_verdicts(["通过", "样本不足", "样本不足"]) == "通过"  # valid=1, need=1

    def test_stricter(self):
        assert stricter("通过", "过拟合") == "过拟合"
        assert stricter("未通过", "样本不足") == "未通过"
        assert stricter("通过", "未启用") == "通过"


class TestRunWalkForward:
    def test_oos_ratio_zero_disabled(self):
        dates = _dates(120)
        req = WalkForwardRequest(
            symbol="sh600000", start=dates[0], end=dates[-1], strategy="chan",
            oos_ratio=0.0, n_windows=3,
        )
        out = run_walk_forward(req, dates, lambda s, e: _mk(sharpe=1))
        assert out.oos["enabled"] is False
        assert out.oos["verdict"] == "未启用"
        assert out.summary.final_verdict == "未启用"
        assert out.windows == []

    def test_n_windows_over_limit_400(self):
        dates = _dates(200)
        req = WalkForwardRequest(
            symbol="sh600000", start=dates[0], end=dates[-1], strategy="chan",
            n_windows=7,
        )
        with pytest.raises(HTTPException) as ei:
            run_walk_forward(req, dates, lambda s, e: _mk())
        assert ei.value.status_code == 400

    def test_single_mode_one_window_and_legacy(self):
        dates = _dates(100)
        calls = []

        def runner(s, e):
            calls.append((s, e))
            return _mk(sharpe=0.5, excess=0.02, trades=3)

        req = WalkForwardRequest(
            symbol="sh600000", start=dates[0], end=dates[-1], strategy="chan",
            mode="single", oos_ratio=0.3, n_windows=3,  # n_windows ignored →1
        )
        out = run_walk_forward(req, dates, runner)
        assert out.mode == "single"
        assert len(out.windows) == 1
        assert out.legacy_single is not None
        assert out.oos["oos_ratio"] == 0.3
        # 无泄漏：IS end < OOS start（日期）
        w = out.windows[0]
        assert w.is_end < w.oos_start or w.is_end <= w.oos_start
        # 半开下标保证 is_end = dates[split-1] < dates[split]=oos_start
        assert w.is_end < w.oos_start
        # 不落 equity
        dumped = out.model_dump(by_alias=True)
        assert "equity_curve" not in dumped
        for win in dumped["windows"]:
            assert "equity_curve" not in win
            assert "is" in win

    def test_walk_forward_default_3_windows_no_leak(self):
        dates = _dates(250)

        def runner(s, e):
            return _mk(sharpe=0.4, excess=0.01, trades=4)

        req = WalkForwardRequest(
            symbol="sh600000", start=dates[0], end=dates[-1], strategy="chan",
            mode="walk_forward", n_windows=3, oos_ratio=0.3,
        )
        out = run_walk_forward(req, dates, runner)
        assert out.n_windows == 3
        assert len(out.windows) == 3
        for w in out.windows:
            assert w.is_end < w.oos_start
        assert out.legacy_single is not None
        assert out.oos["verdict"] == out.summary.final_verdict
        assert out.oos["mode"] == "walk_forward"
        assert "windows_summary" in out.oos

    def test_agg_pass_legacy_overfit_final_overfit(self):
        """§6.3：agg 通过但 legacy 过拟合 → final 过拟合。"""
        dates = _dates(240)
        split = single_split_index(len(dates), 0.3)
        assert split is not None
        legacy_oos_start = dates[split]

        def runner(s, e):
            # legacy OOS 段：IS 正、OOS 负 → 过拟合
            # 各 WF 窗 OOS：给正 sharpe → 通过
            if s == legacy_oos_start:
                return _mk(sharpe=-0.8, trades=5)  # legacy OOS
            # legacy IS 与各窗：看 end
            # 粗略：若区间覆盖接近全样本末段且 start 是首日且 end 是 legacy is end → IS 正
            if e == dates[split - 1] and s == dates[0]:
                return _mk(sharpe=1.2, trades=8)  # legacy IS
            # WF 窗 OOS 多为正
            return _mk(sharpe=0.5, trades=4)

        req = WalkForwardRequest(
            symbol="sh600000", start=dates[0], end=dates[-1], strategy="chan",
            mode="walk_forward", n_windows=3, oos_ratio=0.3, metric="sharpe",
        )
        out = run_walk_forward(req, dates, runner)
        # 多窗应多为通过（agg 通过），legacy 过拟合
        assert out.summary.legacy_verdict == "过拟合"
        assert out.summary.agg_verdict == "通过"
        assert out.summary.final_verdict == "过拟合"
        assert out.oos["verdict"] == "过拟合"
        assert out.summary.gate_ok is False

    def test_any_opposite_sign_window_blocks_pass(self):
        """任一窗过拟合 → 总评不得为通过。"""
        dates = _dates(240)
        wins = make_expanding_windows(len(dates), 3, min_is_bars=60)
        bad_oos_start = dates[wins[1].oos_start_i]

        def runner(s, e):
            if s == bad_oos_start:
                return _mk(sharpe=-1.0, trades=3)
            # IS for that window positive when end matches
            return _mk(sharpe=0.8, trades=4)

        req = WalkForwardRequest(
            symbol="sh600000", start=dates[0], end=dates[-1], strategy="chan",
            n_windows=3,
        )
        out = run_walk_forward(req, dates, runner)
        assert out.windows[1].verdict == "过拟合"
        assert out.summary.agg_verdict == "过拟合"
        assert out.summary.final_verdict == "过拟合"
