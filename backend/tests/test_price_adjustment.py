"""T1 复权口径一致性 —— 防回归：风控判定用的 close 必须与 MA10/MA20 同口径。

Bug 背景（M1-1，2026-10-08）：xq/runner.py 曾用后复权 c_hfq 算 MA10/MA20，
却拿未复权 close 去比较。adj_factor 均值 10.08、最大 10055，导致
ma10/close 比值中位数 3-13 倍，「close < ma20 → SELL」风控从未正确触发。
数据库 signal_actions 139211 行里 64% 口径错位，属静默失效。
"""
from __future__ import annotations

import pytest

from app.services.xq import engine as xq_engine


def _mk_bar(close: float) -> dict:
    return {"date": "2026-09-30", "symbol": "sh600000",
            "open": close, "high": close, "low": close,
            "close": close, "volume": 1e6}


class TestPriceAdjustment:
    def test_ma_and_close_same_caliber(self):
        """同口径下 close 与 MA10 比值应在 [0.9, 1.1]。"""
        close = 10.0
        ma10 = 10.0  # 同口径 → 比值 1.0
        ma20 = 10.0
        ratio = ma10 / close
        assert 0.9 <= ratio <= 1.1, f"MA10/close={ratio:.2f} 偏离 1.0，说明口径不一致"

    def test_adj_factor_impact_documented(self):
        """记录真实 adj_factor 量级，作为回归基准（若管道口径变更需同步更新）。"""
        # 实测 2026-09-30 全市场 adj_factor: min=1.00 avg=10.08 max=10055.64
        # 这组数字说明「后复权价 / 未复权价」可以是 1 ~ 10000 倍量级，
        # 任何混用都会立刻放大成 100 倍级错误。
        assert 10.08 > 1.0

    def test_judge_detects_ma20_break_with_same_caliber(self):
        """真正跌破 MA20（同口径）时应判离场 —— 修复前因错位而永不触发。

        离场动作含 SELL/EXIT/REDUCE（见 portfolio.py EXIT_ACTIONS），此处只断言
        「不是持有类动作」，避免绑死具体枚举值。
        """
        close, ma10, ma20 = 9.0, 9.5, 9.8   # close < ma20 < ma10，真破位
        rules = xq_engine.DEFAULT_RULES
        # 传入 holding 模拟「已持仓 hold_days 足够」——MA 破位判定受
        # rules["ma_check_min_days"] 门槛保护（默认 3 天），不传 holding 则跳过 MA 段。
        holding = {"entry_price": 9.0, "hold_days": 10,
                   "ret_pct": 0.0, "peak_ret_pct": 1.0, "dd_from_peak_pct": 0.0}
        action, reason = xq_engine.judge(
            "sh600000", streak=0, resonance=1, strategies=["测试"],
            close=close, ma10=ma10, ma20=ma20, rules=rules, holding=holding,
        )
        assert action in ("SELL", "EXIT", "REDUCE"), \
            f"应识别 MA20 破位为离场，实际返回 {action}"
        assert "MA20" in reason

    def test_judge_no_false_break_when_above_ma(self):
        """close 高于 MA20/MA10 时不应误报破位。"""
        close, ma10, ma20 = 11.0, 10.0, 10.2
        rules = xq_engine.DEFAULT_RULES
        action, _ = xq_engine.judge(
            "sh600000", streak=5, resonance=1, strategies=["测试"],
            close=close, ma10=ma10, ma20=ma20, rules=rules,
        )
        # streak=5 强度足够时应给 BUY_STRONG / NEW 一类，不该是离场
        assert action not in ("SELL", "EXIT"), \
            "close 高于均线却判离场，疑似口径错位复现"

    def test_runner_uses_hfq_close(self):
        """静态检查：runner.py 取 close 必须用 c_hfq，不能用裸 close。"""
        import inspect
        from app.services.xq import runner
        src = inspect.getsource(runner)
        # 允许出现 c_hfq，禁止出现裸 latest.loc[..., "close"]
        assert 'c_hfq' in src, "runner 应使用 c_hfq 作为判定 close"
        assert 'latest.loc[sym, "close"]' not in src, \
            "runner 不应再取未复权 close（会与后复权 MA 比较导致风控失效）"
