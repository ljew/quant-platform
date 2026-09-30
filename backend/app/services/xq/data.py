"""面板数据适配层：一次载入全市场 N 日行情，供全部选股策略共享。

设计要点（与 Sequoia-X 原实现的口径对应关系）：
- Sequoia 的 ``stock_daily`` 里 close=后复权、close_raw=不复权。本平台是
  ``kline_daily``（未复权原价）+ ``adj_factor_daily``（tushare 累计复权因子），
  因此这里现算 hfq 列：``c_hfq = close * adj_factor``（复权基准是常数倍，
  对均线/比值/波动率类指标无影响；涨停/跌停/阳线判定用**未复权原价**才准确）。
- 一次 SQL 载入全市场最近 N 个交易日（约 5200 只 × 260 天 ≈ 135 万行），
  8 个策略共享同一份 DataFrame —— 比原实现逐股循环取数快一个量级。
- 股票名称从 SQLite stocks 表取（跨库不能 join），用于 ST 过滤与展示。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

# 默认回看窗口：RPS 需要 120 日、日线均线需要 60 日，260 个交易日足够覆盖
DEFAULT_LOOKBACK = 260


class Panel:
    """全市场行情长表（按 symbol, date 升序）+ 常用切片。"""

    def __init__(self, df: pd.DataFrame, names: dict[str, str], as_of: date | None):
        self.df = df
        self.names = names
        self.as_of = as_of
        # 统一为 datetime.date（列已转 object，max() 直接返回 date；兼容 Timestamp）
        mx = df["date"].max() if len(df) else None
        self.latest: date | None = (
            mx.date() if isinstance(mx, pd.Timestamp) else mx
        )
        self._dates_cache: list | None = None

    def date_index(self) -> list:
        """升序唯一交易日（缓存）。

        回填按日切片时会被反复调用，逐次对百万行面板做 unique() 是纯浪费。
        """
        if self._dates_cache is None:
            self._dates_cache = sorted(self.df["date"].unique().tolist())
        return self._dates_cache

    @property
    def latest_df(self) -> pd.DataFrame:
        """最新一个交易日的截面。"""
        return self.df[self.df["date"] == self.latest]

    def st_symbols(self) -> set[str]:
        """ST/*ST 风险警示股集合（规则 blacklist_st：任何分组都不出现）。"""
        return {s for s, n in self.names.items() if n and "ST" in n.upper()}

    def name_of(self, symbol: str) -> str:
        return self.names.get(symbol, "")


def load_panel(
    duckdb_path: str | Path,
    lookback: int = DEFAULT_LOOKBACK,
    as_of: date | None = None,
    include_bj: bool = False,
) -> Panel:
    """载入面板。

    Args:
        duckdb_path: quant.duckdb 路径（BASE_DIR/data/quant.duckdb）。
        lookback: 回看交易日数。
        as_of: 历史重演日期（time_travel 语义：截断到该日，策略零改动重演）。
        include_bj: 是否包含北交所（平台约定默认剔除）。
    """
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        cutoff_sql = ""
        params: list = []
        if as_of is not None:
            cutoff_sql = "WHERE trade_date <= ?"
            params.append(as_of)
        params.append(lookback)  # limit ? 在 SQL 末尾
        dates = con.execute(
            f"select distinct trade_date from kline_daily {cutoff_sql} "
            "order by trade_date desc limit ?",
            params,
        ).fetchall()
        if not dates:
            return Panel(pd.DataFrame(), {}, as_of)
        date_list = [d[0] for d in dates]
        date_strs = ",".join(f"'{d.isoformat()}'" for d in date_list)

        bj_filter = "" if include_bj else " and k.symbol not like 'bj%'"
        df = con.execute(
            f"""
            select k.symbol, k.trade_date as date,
                   k.open, k.high, k.low, k.close,
                   k.volume, k.amount, f.adj_factor
            from kline_daily k
            join adj_factor_daily f
              on f.symbol = k.symbol and f.trade_date = k.trade_date
            where k.trade_date in ({date_strs}){bj_filter}
            order by k.symbol, k.trade_date
            """
        ).fetchdf()
    finally:
        con.close()

    if df.empty:
        return Panel(df, {}, as_of)

    # ⚠️ 必须转成 python date（object 列）：pandas 2.x 下 datetime64 序列与
    # datetime.date 比较恒为 False（静默！），会让所有「最新交易日」切片变空。
    df["date"] = df["date"].dt.date
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    # hfq 列：复权因子为 tushare 累计值，直接相乘即后复权价
    df["o_hfq"] = df["open"] * df["adj_factor"]
    df["h_hfq"] = df["high"] * df["adj_factor"]
    df["l_hfq"] = df["low"] * df["adj_factor"]
    df["c_hfq"] = df["close"] * df["adj_factor"]
    # ⚠️ kline_daily.amount 单位是**千元**（tushare daily 原样入库），这里统一换算成元，
    # 否则「成交额 > 1 亿」这类阈值永远不触发（turtle 曾因此 240 天 0 命中）
    df["amount"] = df["amount"] * 1000.0

    # 股票名称（SQLite，跨库不能 join）
    import sqlite3

    names: dict[str, str] = {}
    try:
        scon = sqlite3.connect(str(Path(duckdb_path).parent / "quant_dev.db"))
        try:
            for sym, nm in scon.execute("select symbol, name from stocks"):
                names[sym] = nm or ""
        finally:
            scon.close()
    except Exception:
        pass

    return Panel(df, names, as_of)
