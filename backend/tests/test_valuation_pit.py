"""T6 估值 PIT 时序 —— 防回归：估值因子不得用「最新快照」复用于历史截面。

Bug 背景（B1，2026-10-08）：
  `stocks` 表的 pe_ttm / pb / market_cap 是**最新一列快照**，`_attrs_map` 查一次
  后复用于所有历史截面 → 2019 年截面用的是 2026 年的 PE，属**明确前视偏差**。
  实测茅台 2019-01-10 pe_ttm=24.42 / pb=7.59，与今日值完全不同。

修复：
  新增 `stock_valuation_daily` 表（symbol × trade_date × pe/pb/mv），
  `_attrs_timeline` 载入时序，`_attrs_asof` 按截面日二分取「≤ 截面日的最近一条」。

本测试**不需要数据库**——`_attrs_asof` 是纯函数，用构造数据验证 as-of 语义。
"""
from __future__ import annotations

from datetime import date

from app.services.factor_mining import _attrs_asof


def _tl() -> dict[str, list]:
    """模拟某只股票 4 个交易日的估值时序（升序，与 SQL ORDER BY 一致）。"""
    return {
        "sh600519": [
            (date(2019, 1, 10), {"pe_ttm": 24.42, "pb": 7.59, "market_cap": 7772.9}),
            (date(2020, 6, 15), {"pe_ttm": 58.31, "pb": 15.02, "market_cap": 12100.0}),
            (date(2024, 3, 1), {"pe_ttm": 22.18, "pb": 6.90, "market_cap": 16500.0}),
            (date(2026, 9, 30), {"pe_ttm": 19.87, "pb": 5.42, "market_cap": 18200.0}),
        ]
    }


class TestValuationPIT:
    def test_exact_date_match(self):
        v = _attrs_asof(_tl(), "sh600519", date(2020, 6, 15))
        assert v["pe_ttm"] == 58.31

    def test_between_dates_uses_earlier(self):
        """截面日在两个交易日之间 → 取「≤ 该日」的最近一条（不能取未来）。"""
        v = _attrs_asof(_tl(), "sh600519", date(2020, 6, 20))
        assert v["pe_ttm"] == 58.31, "应取 2020-06-15，不得取 2024-03-01"

    def test_no_future_leak(self):
        """核心断言：2019 年的截面绝不能拿到 2026 年的估值。"""
        v = _attrs_asof(_tl(), "sh600519", date(2019, 1, 10))
        assert v["pe_ttm"] == 24.42, "2019 截面拿到了未来估值 → 前视 bug 复现"
        assert v["pb"] == 7.59
        assert v["market_cap"] == 7772.9

    def test_before_first_date_returns_none(self):
        """早于首个数据点 → 返回 None（调用方保留旧口径），不猜未来值。"""
        assert _attrs_asof(_tl(), "sh600519", date(2018, 12, 31)) is None

    def test_after_last_date_uses_last(self):
        v = _attrs_asof(_tl(), "sh600519", date(2027, 1, 1))
        assert v["pe_ttm"] == 19.87

    def test_unknown_symbol_returns_none(self):
        assert _attrs_asof(_tl(), "sz000001", date(2020, 1, 1)) is None

    def test_empty_timeline_returns_none(self):
        assert _attrs_asof({}, "sh600519", date(2020, 1, 1)) is None

    def test_values_differ_enough_to_be_detectable(self):
        """构造数据必须能区分新旧口径，否则测试形同虚设。

        若实现退回「最新快照」，2019 截面会拿到 19.87 而非 24.42——
        本测试通过与否由此保证。
        """
        tl = _tl()
        old = _attrs_asof(tl, "sh600519", date(2019, 1, 10))["pe_ttm"]
        latest = tl["sh600519"][-1][1]["pe_ttm"]
        assert old != latest, "构造数据有误：新旧口径应给出不同值"


class TestStaticCheck:
    def test_attrs_map_no_longer_primary_for_valuation(self):
        """factor_mining 必须提供 _attrs_timeline / _attrs_asof 两个 PIT 函数。"""
        from app.services import factor_mining
        assert hasattr(factor_mining, "_attrs_timeline")
        assert hasattr(factor_mining, "_attrs_asof")

    def test_gp_also_uses_pit(self):
        """GP 侧有同一个 bug，必须一并修复。"""
        import inspect

        from app.services import factor_gp
        src = inspect.getsource(factor_gp)
        assert "_attrs_timeline" in src, "GP 未接入估值 PIT 时序"
        assert "_attrs_asof" in src, "GP 未按截面日取估值"

    def test_model_table_exists(self):
        from app.models import StockValuationDaily
        assert StockValuationDaily.__tablename__ == "stock_valuation_daily"

    def test_attrs_map_docstring_warns(self):
        """_attrs_map 的 docstring 必须说明它不含 PIT 估值，避免后人误用。"""
        import inspect

        from app.services.factor_mining import _attrs_map
        doc = inspect.getdoc(_attrs_map) or ""
        assert "PIT" in doc or "时序" in doc, "_attrs_map 应有口径警示说明"

    def test_backfill_script_exists(self):
        from pathlib import Path

        import app
        backend = Path(app.__file__).resolve().parent.parent
        assert (backend / "scripts" / "backfill_valuation.py").exists(), \
            "回填脚本缺失"