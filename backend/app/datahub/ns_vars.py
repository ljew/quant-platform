"""统一变量命名空间构造（全平台单一口径）。

窗口定义（与 multi_factor 语义注释对齐）：
  c_m = 126 收盘（动量）
  c_r = 26  收盘（反转）
  c_v = 61  收盘（波动/偏度）
  c_b = 126 收盘（Beta/特异度）
  c_t = 121 收盘（尾部最大回撤）
  mkt_b = 与 c_b 等长的基准序列

GP 探索 / 单因子检验 / ETL 生产 三端共用本函数，
保证"检验时的表达式含义 == 注册后每日计算的含义"。
"""
from __future__ import annotations

from typing import Callable


def make_ns(seg: list[float], mkt_all: list[float], attrs: dict,
            news: float | None = None, esv=None) -> dict:
    seg = list(seg)
    if not seg:
        return {}

    def tail(n: int) -> list[float]:
        return seg[-n:] if len(seg) >= n else seg

    cm = tail(126)
    cb = tail(126)
    mb = tail_of(mkt_all, len(cb)) if mkt_all else []
    return {
        "c_m": cm,
        "c_r": tail(26),
        "c_v": tail(61),
        "c_b": cb,
        "c_t": tail(121),
        "mkt_b": mb,
        "pe_ttm": attrs.get("pe_ttm"),
        "pb": attrs.get("pb"),
        "market_cap": attrs.get("market_cap"),
        "roe": attrs.get("roe"),
        "revenue_yoy": attrs.get("revenue_yoy"),
        "profit_yoy": attrs.get("profit_yoy"),
        "earnings_surprise": esv if esv is not None else attrs.get("_es"),
        "news_senti": news,
        "industry": attrs.get("industry"),
    }


def tail_of(xs: list[float], n: int) -> list[float]:
    return xs[-n:] if len(xs) >= n else list(xs)


def lookup_recent(hist: dict, asof, max_days: int = 3):
    """按日期查找最近 ≤max_days 自然日的值均值（news_senti 等）。"""
    if not hist:
        return None
    vals = []
    for d, v in hist.items():
        gap = (asof - d).days
        if 0 <= gap <= max_days:
            vals.append(v)
    return (sum(vals) / len(vals)) if vals else None


def news_lookup_factory(hist: dict) -> Callable:
    def f(sym: str, asof):
        h = hist.get(sym)
        return lookup_recent(h, asof) if h else None
    return f
