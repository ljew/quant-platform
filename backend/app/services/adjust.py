"""复权原语：未复权原价 + 复权因子 → 按需构造复权价。

**单一事实源。** 库内 kline_daily 统一存未复权原价（`adj='none'`），
复权在读取时实时完成。有三条读取链路需要复权：

    · duckdb_store.get_stock_bars / get_stock_bars_batch（回测、模拟盘）— bars 形态
    · etl.compute_factor_cross_section（ETL 每日因子）        — series 形态
    · factor_mining / factor_gp（因子挖掘与 GP）              — series 形态

过去 bars 与 series 各写一份实现，规则容易漂移（例如因子缺失时一个用 1.0、
另一个用邻近日顶替），同一天同一只股票会算出不同的复权价。本模块把
「因子缺失如何顶替」「基准取首日还是末日」这两个决定收敛到一处。

口径定义（详见 price.py）：
    hfq   后复权 = raw × factor / factor_first  —— 研究/因子/回测内部默认
    qfq   前复权 = raw × factor / factor_last   —— 展示用
    none  原始价                                  —— 真实成交价
"""
from __future__ import annotations

ADJUSTS = ("hfq", "qfq", "none")


def fill_factors(facs: list[float | None]) -> list[float]:
    """补齐缺失因子：前向填充 → 后向填充 → 仍缺则 1.0（等同不复权）。

    新股上市初期、或复权因子尚未入库时会出现缺失。直接按 1.0 处理会让该点
    相对邻近日产生一个假跳变（等于把除权当成了真实涨跌），故必须用邻近值顶替。
    """
    n = len(facs)
    out: list[float | None] = [None] * n

    last: float | None = None
    for i, f in enumerate(facs):
        if f is not None and f > 0:
            last = float(f)
        out[i] = last

    nxt: float | None = None
    for i in range(n - 1, -1, -1):
        if out[i] is None:
            out[i] = nxt
        else:
            nxt = out[i]

    return [f if f else 1.0 for f in out]


def ratio_series(facs: list[float | None], adjust: str) -> list[float] | None:
    """逐点折算比例。返回 None 表示不做调整（none / 未知口径）。"""
    if adjust in (None, "", "none"):
        return None
    f = fill_factors(facs)
    base = f[-1] if adjust == "qfq" else f[0]
    if not base:
        return None
    return [x / base for x in f]


def apply_adjust_series(raw: list[float | None], facs: list[float | None],
                        adjust: str) -> list[float | None]:
    """按口径折算价格序列（不改动入参）。"""
    r = ratio_series(facs, adjust)
    if r is None:
        return list(raw)
    return [None if x is None else x * k for x, k in zip(raw, r)]


def apply_adjust_bars(bars: list[dict], adjust: str) -> list[dict]:
    """就地折算 bars（list[dict]，含 `_f` 因子键）的 OHLC，并移除 `_f`。

    与 apply_adjust_series 同口径 —— 两处结果必须一致。
    volume / amount 不折算：量能因子取的是相对变化（vol/vol_ma），
    常数比例折算不影响；保留原始量额也更便于与真实成交额核对。
    """
    if not bars:
        return bars
    if adjust in (None, "", "none"):
        for b in bars:
            b.pop("_f", None)
        return bars

    r = ratio_series([b.get("_f") for b in bars], adjust)
    if r is None:
        for b in bars:
            b.pop("_f", None)
        return bars
    for b, k in zip(bars, r):
        for c in ("open", "high", "low", "close"):
            v = b.get(c)
            if v is not None:
                b[c] = v * k
        b.pop("_f", None)
    return bars
