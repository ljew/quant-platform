"""统一取价服务：未复权原价 + 复权因子 → 按需构造复权价。

为什么需要单独一层
-----------------
kline_daily 自全A 建仓起只存**未复权原始价**（`adj='none'`），复权因子单独存
adj_factor_daily。任何需要「复权后价格」的地方都应经过这里，而不是各自写 SQL ——
复权基准的选择会直接影响因子值与回测结果，散落各处的实现迟早会不一致，
而口径不一致导致的偏差极难发现（数值看似合理，只是系统性偏移）。

三种口径
-------
hfq   后复权：price = raw × factor / factor_first
      **因子计算与回测内部一律用它**。历史值不随未来分红送股变动，可复现。
qfq   前复权：price = raw × factor / factor_latest
      与当前真实价格量级可比，用于展示与「距成本价多少」这类判断。
none  原始价：不做调整，含除权跳变。仅用于展示真实成交价。

一个容易踩的坑
-------------
前复权价会随新的复权事件**回溯变动**：同一个 2020 年的日期，在 2024 年查和
2026 年查得到的价格不同。所以前复权只适合展示，**不适合作为回测与因子的输入**
（跨期结果无法复现）。研究口径一律用后复权。
"""
from __future__ import annotations

import os

import duckdb
import pandas as pd

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
DUCKDB_DB = os.path.join(_PROJECT_ROOT, "data", "quant.duckdb")

ADJUSTS = ("hfq", "qfq", "none")


def connect(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(DUCKDB_DB, read_only=read_only)


def load_long(symbols: list[str] | None, sd: str, ed: str,
              columns: tuple[str, ...] = ("close",),
              con: duckdb.DuckDBPyConnection | None = None) -> pd.DataFrame:
    """读长表：symbol, trade_date, <columns...>, adj_factor。

    symbols 传 None 表示全市场（慎用，约 900 万行）。
    """
    own = con is None
    con = con or connect()
    try:
        sym_filter = ""
        params: list = []
        if symbols:
            ph = ",".join(["?"] * len(symbols))
            sym_filter = f" and k.symbol in ({ph})"
            params.extend(symbols)
        sel = ", ".join(f"k.{c}" for c in columns)
        q = f"""
            select k.symbol, k.trade_date, {sel}, a.adj_factor
            from kline_daily k
            left join adj_factor_daily a
              on a.symbol = k.symbol and a.trade_date = k.trade_date
            where k.trade_date between ? and ?{sym_filter}
            order by k.symbol, k.trade_date
        """
        return con.execute(q, [sd, ed, *params]).fetch_df()
    finally:
        if own:
            con.close()


def _apply_adjust(df: pd.DataFrame, adjust: str,
                  cols: tuple[str, ...]) -> pd.DataFrame:
    """就地折算：raw × factor / base。base 逐标的取首日（hfq）或末日（qfq）因子。"""
    df = df.sort_values(["symbol", "trade_date"]).copy()
    f = df["adj_factor"]
    # 因子缺失（新股/脏数据）用同标的邻近值顶替，再全缺则视为 1.0（等于不复权）
    f = f.groupby(df["symbol"]).ffill()
    f = f.groupby(df["symbol"]).bfill()
    df["adj_factor"] = f.fillna(1.0)

    if adjust == "none":
        return df

    g = df.groupby("symbol")["adj_factor"]
    base = g.transform("last") if adjust == "qfq" else g.transform("first")
    ratio = df["adj_factor"] / base.replace(0, pd.NA)
    for c in cols:
        df[c] = df[c] * ratio
    return df


def adjusted_wide(symbols: list[str] | None, sd: str, ed: str,
                  adjust: str = "hfq",
                  columns: tuple[str, ...] = ("close",),
                  con: duckdb.DuckDBPyConnection | None = None,
                  ) -> dict[str, pd.DataFrame]:
    """返回 {列名: 交易日×标的 宽表}。默认只返回 close。

    宽表是因子计算的常用形态：行是交易日，列是标的。
    """
    if adjust not in ADJUSTS:
        raise ValueError(f"adjust 只能是 {ADJUSTS}，收到 {adjust}")
    df = load_long(symbols, sd, ed, columns=columns, con=con)
    if df.empty:
        return {c: pd.DataFrame() for c in columns}
    df = _apply_adjust(df, adjust, columns)
    df["trade_date"] = df["trade_date"].astype(str)
    out = {}
    for c in columns:
        out[c] = df.pivot(index="trade_date", columns="symbol",
                          values=c).sort_index()
    return out


def close_matrix(symbols: list[str] | None, sd: str, ed: str,
                 adjust: str = "hfq",
                 con: duckdb.DuckDBPyConnection | None = None) -> pd.DataFrame:
    """最常用入口：交易日×标的 的复权收盘价矩阵。"""
    return adjusted_wide(symbols, sd, ed, adjust=adjust,
                         columns=("close",), con=con)["close"]


def returns_matrix(symbols: list[str] | None, sd: str, ed: str,
                   adjust: str = "hfq",
                   con: duckdb.DuckDBPyConnection | None = None) -> pd.DataFrame:
    """日收益率矩阵（简单收益）。复权后计算，不含除权跳变。"""
    c = close_matrix(symbols, sd, ed, adjust=adjust, con=con)
    return c.pct_change() if len(c) else c
