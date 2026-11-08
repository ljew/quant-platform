"""W45-4 / P1-1：成长三因子（roe / revenue_yoy / profit_yoy）截面 as-of。

口径：FundamentalsHistory.report_date ≤ 截面日取最近一期；
禁止复用 stocks 最新快照。与 PEAD 同 residual（非 ann_date）。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from app.core.strategies.multi_factor import _pit_fund_attrs
from app.services.factor_mining import (
    _apply_fund_asof,
    _fund_asof,
    _fund_timeline,
)


def test_pit_fund_attrs_hides_future_reports():
    """早期截面看不到后期财报的 roe/yoy。"""
    snaps = [
        {"report_date": date(2018, 12, 31), "roe": 10.0, "revenue_yoy": 5.0, "profit_yoy": 8.0},
        {"report_date": date(2019, 12, 31), "roe": 12.0, "revenue_yoy": 7.0, "profit_yoy": 9.0},
        {"report_date": date(2020, 12, 31), "roe": 99.0, "revenue_yoy": 99.0, "profit_yoy": 99.0},
    ]
    early = _pit_fund_attrs(snaps, "2019-06-30")
    assert early == {"roe": 10.0, "revenue_yoy": 5.0, "profit_yoy": 8.0}
    assert early["roe"] != 99.0

    mid = _pit_fund_attrs(snaps, "2020-06-30")
    assert mid["roe"] == 12.0
    assert mid["profit_yoy"] == 9.0

    late = _pit_fund_attrs(snaps, "2021-01-01")
    assert late["roe"] == 99.0


def test_pit_fund_attrs_empty_before_first_report():
    snaps = [
        {"report_date": date(2020, 12, 31), "roe": 15.0, "revenue_yoy": 3.0, "profit_yoy": 4.0},
    ]
    assert _pit_fund_attrs(snaps, "2019-01-01") == {}
    assert _pit_fund_attrs([], "2021-01-01") == {}
    assert _pit_fund_attrs(None, "2021-01-01") == {}  # type: ignore[arg-type]


def test_fund_asof_matches_pit_and_differs_from_stock_latest():
    """与「最新 Stock」故意不同时，as-of 取旧值。"""
    tl = {
        "sh600000": [
            (date(2018, 12, 31), {"roe": 8.0, "revenue_yoy": 1.0, "profit_yoy": 2.0}),
            (date(2019, 12, 31), {"roe": 11.0, "revenue_yoy": 4.0, "profit_yoy": 6.0}),
        ]
    }
    # stocks 最新快照假装是 2025 极值
    stock_latest = {"roe": 50.0, "revenue_yoy": 50.0, "profit_yoy": 50.0, "industry": "银行"}

    asof = _fund_asof(tl, "sh600000", date(2019, 6, 1))
    assert asof == {"roe": 8.0, "revenue_yoy": 1.0, "profit_yoy": 2.0}
    assert asof["roe"] != stock_latest["roe"]

    # 对齐真相源
    snaps = [{"report_date": rd, **fields} for rd, fields in tl["sh600000"]]
    assert asof == _pit_fund_attrs(snaps, "2019-06-01")


def test_apply_fund_asof_clears_stock_when_no_history():
    """无 FundamentalsHistory 时不得保留 Stock 最新值。"""
    a = {"roe": 20.0, "revenue_yoy": 30.0, "profit_yoy": 40.0, "pe_ttm": 12.0, "industry": "食品"}
    _apply_fund_asof(a, {}, "sh600519", date(2020, 1, 1))
    assert a["roe"] is None
    assert a["revenue_yoy"] is None
    assert a["profit_yoy"] is None
    assert a["pe_ttm"] == 12.0  # 估值字段不动（归 valuation PIT）
    assert a["industry"] == "食品"


def test_apply_fund_asof_overlays_when_history_exists():
    tl = {
        "sz000001": [
            (date(2019, 12, 31), {"roe": 9.5, "revenue_yoy": 2.5, "profit_yoy": 3.5}),
        ]
    }
    a = {"roe": 99.0, "revenue_yoy": 99.0, "profit_yoy": 99.0, "pb": 1.2}
    _apply_fund_asof(a, tl, "sz000001", date(2020, 6, 1))
    assert a["roe"] == 9.5
    assert a["revenue_yoy"] == 2.5
    assert a["profit_yoy"] == 3.5
    assert a["pb"] == 1.2


def test_fund_timeline_shape_from_dummy_db():
    class _Dummy:
        def execute(self, *_a, **_k):
            class R:
                def all(self):
                    return [
                        ("sh600000", date(2019, 12, 31), 10.0, 5.0, 8.0),
                        ("sh600000", date(2020, 12, 31), 12.0, None, 9.0),
                        ("sz000001", date(2020, 6, 30), None, None, None),  # 全空跳过
                    ]
            return R()

    assert _fund_timeline(_Dummy(), []) == {}
    tl = _fund_timeline(_Dummy(), ["sh600000", "sz000001"])
    assert "sh600000" in tl
    assert len(tl["sh600000"]) == 2
    assert tl["sh600000"][0][1]["roe"] == 10.0
    assert "revenue_yoy" not in tl["sh600000"][1][1]  # None 不写入
    assert "sz000001" not in tl


def test_multi_factor_source_uses_pit_fund_attrs():
    """回测路径：multi_factor 用 _pit_fund_attrs，禁止 attrs.get 取成长三字段。"""
    src = Path(__file__).resolve().parents[1] / "app" / "core" / "strategies" / "multi_factor.py"
    text = src.read_text(encoding="utf-8")
    assert "def _pit_fund_attrs" in text
    assert "fund_attrs = _pit_fund_attrs" in text
    # 注意：fund_attrs.get("roe") 含子串 attrs.get("roe")，必须整词匹配
    assert 'attrs.get("roe")' not in text.replace("fund_attrs.get", "FUND.get")
    assert 'attrs.get("revenue_yoy")' not in text.replace("fund_attrs.get", "FUND.get")
    assert 'attrs.get("profit_yoy")' not in text.replace("fund_attrs.get", "FUND.get")
    assert '"roe": fund_attrs.get("roe")' in text


def test_mining_and_gp_apply_fund_asof():
    """挖矿 / GP 截面注入走 _apply_fund_asof。"""
    mining = (Path(__file__).resolve().parents[1] / "app" / "services" / "factor_mining.py").read_text(
        encoding="utf-8"
    )
    assert "_apply_fund_asof(a, fund_tl, sym, snap)" in mining
    assert "fund_tl=fund_tl" in mining  # corr 透传

    gp = (Path(__file__).resolve().parents[1] / "app" / "services" / "factor_gp.py").read_text(
        encoding="utf-8"
    )
    assert "_apply_fund_asof" in gp
    assert '"fund_tl": fund_tl' in gp or "'fund_tl': fund_tl" in gp


def test_etl_cross_section_applies_fund_asof():
    src = (Path(__file__).resolve().parents[1] / "app" / "services" / "etl.py").read_text(
        encoding="utf-8"
    )
    assert "_apply_fund_asof(attrs, fund_tl, sym, trade_date)" in src
