"""涨跌停幅度按板块区分 —— 防回归：回测与实盘必须共用同一份规则。

背景：回测引擎曾用固定 `chg >= 0.095` 一刀切，而实盘 `live/guard.py` 早已按
板块区分（主板 10% / ST 5% / 创业板科创板 20% / 北交所 30%）。两侧口径不一致
造成双向扭曲：

- 创业板/科创板涨到 +9.5% 就被判「买不进」→ 误杀（实际 +15% 仍可买）
- ST 涨到 +5% 已封板，9.5% 阈值不触发 → 漏杀（回测买到了买不到的票）

本测试锁三件事：① 分板块幅度正确；② 实盘与回测取同一份实现；
③ 引擎不再出现固定 9.5% 阈值。
"""
from __future__ import annotations

import inspect

import pytest

from app.core.engine.portfolio_backtest import PortfolioBacktestEngine
from app.core.rules.limit import LIMIT_BUFFER, limit_pct, limit_trigger

D1, D2, D3 = "2024-01-02", "2024-01-03", "2024-01-04"


def _bars(prices: dict[str, float]) -> list[dict]:
    return [{"date": d, "close": float(p), "high": float(p)} for d, p in prices.items()]


def _data() -> dict:
    """主板与创业板同幅上涨 +12%：主板应被拦（>9.5%），创业板应放行（<19.5%）。"""
    return {
        "sh600001": _bars({D1: 10.0, D2: 11.2, D3: 11.2}),
        "sz300001": _bars({D1: 10.0, D2: 11.2, D3: 11.2}),
    }


def _bench() -> list[dict]:
    return _bars({D1: 100.0, D2: 100.0, D3: 100.0})


class _BuyBothOnD2:
    def init(self, ctx) -> None:
        pass

    def rebalance(self, ctx, date: str) -> None:
        if date == D2:
            ctx.order_target_percent("sh600001", 0.5)
            ctx.order_target_percent("sz300001", 0.5)


class TestLimitPct:
    @pytest.mark.parametrize("symbol,name,expected", [
        ("sh600001", "", 0.10),      # 主板
        ("sz000001", "", 0.10),      # 主板（深）
        ("sz300001", "", 0.20),      # 创业板
        ("sh688001", "", 0.20),      # 科创板
        ("bj430047", "", 0.30),      # 北交所
        ("sh600001", "*ST某某", 0.05),  # ST 优先于板块
        ("sz300001", "ST创业", 0.05),   # ST 优先于创业板
    ])
    def test_band_by_board(self, symbol, name, expected):
        assert limit_pct(symbol, name) == expected

    def test_trigger_has_buffer(self):
        """触发阈值 = 法定幅度 − 0.5pp（封板价附近量能不足 + 浮点容差）。"""
        assert limit_trigger("sh600001") == pytest.approx(0.10 - LIMIT_BUFFER)
        assert limit_trigger("sz300001") == pytest.approx(0.20 - LIMIT_BUFFER)

    def test_matches_live_guard(self):
        """实盘 guard 与 core 规则必须同源——不许再分裂成两套。"""
        from app.live.guard import limit_pct as live_pct

        cases = [
            ("sh600001", ""), ("sz000001", ""), ("sz300001", ""),
            ("sh688001", ""), ("bj430047", ""), ("sh600001", "*ST某某"),
        ]
        for sym, name in cases:
            assert live_pct(sym, name) == limit_pct(sym, name), \
                f"{sym}/{name} 实盘与回测涨跌停幅度不一致"


class TestEngineBand:
    def test_chinext_allowed_at_12pct(self):
        """创业板 +12% 不该被拦（法定 20%），否则误杀上涨标的。"""
        eng = PortfolioBacktestEngine(
            data=_data(), benchmark=_bench(), warmup=0, rebalance_period=1,
        )
        eng.run(_BuyBothOnD2, {})
        assert eng.positions["sz300001"] > 0, "创业板 +12% 被误判为涨停买不进"

    def test_main_board_blocked_at_12pct(self):
        """主板 +12% 必然封板，必须拦。"""
        eng = PortfolioBacktestEngine(
            data=_data(), benchmark=_bench(), warmup=0, rebalance_period=1,
        )
        eng.run(_BuyBothOnD2, {})
        assert eng.positions["sh600001"] == 0, "主板 +12% 应判涨停买不进"


class TestNoHardcodedThreshold:
    def test_portfolio_engine_uses_shared_rule(self):
        import app.core.engine.portfolio_backtest as m

        src = inspect.getsource(m)
        assert "limit_trigger(" in src, "组合引擎应调用 core/rules.limit 的 limit_trigger"
        assert "0.095" not in src, "组合引擎残留固定 9.5% 阈值"

    def test_single_asset_engine_uses_shared_rule(self):
        import app.core.engine.backtest_engine as m

        src = inspect.getsource(m)
        assert "limit_trigger(self.symbol)" in src, \
            "单标的引擎应按 constructor 传入的 symbol 取幅度"
        assert "0.095" not in src, "单标的引擎残留固定 9.5% 阈值"
