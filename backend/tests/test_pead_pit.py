"""W45-3 / P0-2：PEAD 截面 as-of + 与回测量纲对齐（raw_pp，不除 100）。"""
from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path

from app.core.strategies.multi_factor import _pit_earnings_surprise
from app.services.factor_mining import _es_asof, _es_map, _es_timeline


def _snaps_from_tl(tl, sym):
    return [{"report_date": rd, "profit_yoy": py} for rd, py in (tl.get(sym) or [])]


def test_es_asof_hides_future_reports():
    """早期截面看不到后期报告期 surprise。"""
    tl = {
        "sh600000": [
            (date(2018, 12, 31), 10.0),
            (date(2019, 12, 31), 20.0),
            (date(2020, 12, 31), 50.0),  # 后期报告，不得泄漏到 2019 截面
        ]
    }
    early = _es_asof(tl, "sh600000", date(2019, 6, 30))
    # 仅 2018 一期可见 → <2 期 → None
    assert early is None

    mid = _es_asof(tl, "sh600000", date(2020, 6, 30))
    # 可见 2018+2019 → 20 - 10 = 10；看不到 2020
    assert mid == 10.0

    late = _es_asof(tl, "sh600000", date(2021, 1, 1))
    # 三期：50 - mean(10,20) = 35
    assert late == 35.0


def test_es_asof_matches_pit_earnings_surprise():
    """同输入下 _es_asof == _pit_earnings_surprise。"""
    tl = {
        "sz000001": [
            (date(2017, 12, 31), 5.0),
            (date(2018, 12, 31), 8.0),
            (date(2019, 6, 30), 12.0),
            (date(2019, 12, 31), 3.0),
        ]
    }
    for as_of in (date(2018, 6, 1), date(2019, 1, 15), date(2019, 7, 1), date(2020, 1, 1)):
        got = _es_asof(tl, "sz000001", as_of)
        expect = _pit_earnings_surprise(_snaps_from_tl(tl, "sz000001"), as_of.isoformat())
        assert got == expect, f"as_of={as_of}: {got} != {expect}"


def test_scale_is_raw_pp_not_div100():
    """挖矿路径量纲为原始百分点差，不得 /100。"""
    tl = {
        "sh600519": [
            (date(2020, 12, 31), 100.0),
            (date(2021, 12, 31), 130.0),
        ]
    }
    v = _es_asof(tl, "sh600519", date(2022, 1, 1))
    assert v == 30.0  # 若误 /100 会得到 0.3
    assert abs(v) > 1.0


def test_es_map_returns_timeline_not_scalar():
    """_es_map / _es_timeline 返回时序列表，禁止再当静态 float 用。"""
    assert _es_timeline is _es_map

    class _Dummy:
        def execute(self, *_a, **_k):
            class R:
                def all(self):
                    return [
                        ("sh600000", date(2019, 12, 31), 10.0),
                        ("sh600000", date(2020, 12, 31), 20.0),
                    ]
            return R()

    assert _es_map(_Dummy(), []) == {}
    tl = _es_map(_Dummy(), ["sh600000"])
    assert isinstance(tl["sh600000"], list)
    assert tl["sh600000"][0][0] == date(2019, 12, 31)
    # 静态旧口径会直接给 float；这里必须是时序
    assert not isinstance(tl["sh600000"], float)


def test_mining_source_has_no_div100_on_pead_path():
    """源码护栏：factor_mining PEAD 路径不再出现 /100。"""
    src = Path(__file__).resolve().parents[1] / "app" / "services" / "factor_mining.py"
    text = src.read_text(encoding="utf-8")
    # 锁定 _es_asof 函数体
    start = text.index("def _es_asof")
    end = text.index("\ndef ", start + 1)
    body = text[start:end]
    assert "/100" not in body
    assert "_pit_earnings_surprise" in body


def test_etl_source_no_div100_and_asof_param():
    """etl._earnings_surprise_latest 去掉 /100，且接受 as_of。"""
    src = Path(__file__).resolve().parents[1] / "app" / "services" / "etl.py"
    text = src.read_text(encoding="utf-8")
    start = text.index("def _earnings_surprise_latest")
    end = text.index("\ndef ", start + 1)
    body = text[start:end]
    assert "/100" not in body
    assert "as_of" in body
    assert "_pit_earnings_surprise" in body
    # 日频截面调用传入 trade_date
    assert "_earnings_surprise_latest(db, sym, as_of=trade_date)" in text


def test_gp_uses_es_asof_not_static_get():
    """factor_gp 截面注入走 _es_asof，禁止 es_map.get(sym) 静态值。"""
    src = Path(__file__).resolve().parents[1] / "app" / "services" / "factor_gp.py"
    text = src.read_text(encoding="utf-8")
    assert "es_map.get(sym)" not in text
    assert "_es_asof(" in text
    assert "esv=_es_asof" in text or "esv=_es_asof(" in text


def test_mining_corr_and_mine_use_asof():
    """挖矿主路径与相关矩阵均用 _es_asof。"""
    src = Path(__file__).resolve().parents[1] / "app" / "services" / "factor_mining.py"
    text = src.read_text(encoding="utf-8")
    assert "esv=es_map.get(sym)" not in text
    # 主路径已改成 ctx.es_map，不能把变量名写死；只要两处都走 _es_asof 就满足意图
    assert text.count("esv=_es_asof(") >= 2


def test_pit_earnings_surprise_still_raw_pp():
    """回测真相源本身不除 100（防回归）。"""
    snaps = [
        {"report_date": date(2019, 12, 31), "profit_yoy": 10.0},
        {"report_date": date(2020, 12, 31), "profit_yoy": 25.0},
    ]
    v = _pit_earnings_surprise(snaps, "2021-01-01")
    assert v == 15.0
    src = inspect.getsource(_pit_earnings_surprise)
    assert "/100" not in src
