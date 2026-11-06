"""DuckDB 只读分析存储层（完整版架构：分析库用 DuckDB）。

设计：SQLite 保留业务写表（backtests/paper_*/strategies），DuckDB 承载
读密集分析表（kline_daily 133万行 / index_kline_daily / fundamentals_history /
stocks / index_membership）。

**短连接模式**：DuckDB 文件级锁——常驻连接会阻止 seed/同步脚本写入
（后端运行中 sync 报 "Conflicting lock"）。故每次查询临时开/关只读连接，
连接成本毫秒级；组合回测用批量接口（一次连接取全部股票），性能不降。

接入方式：回测/模拟盘/因子研究的 K线、指数、基本面、成分快照读取优先走本层，
返回与既有 SQLAlchemy 路径一致的 list[dict]；文件缺失/表不存在返回空，调用方
自动降级 SQLite/在线（优雅降级，不破坏现有链路）。

迁移/同步脚本：backend/scripts/migrate_to_duckdb.py、app/services/duckdb_sync.py
"""
from __future__ import annotations

import os
from typing import Optional

import duckdb

# .../quant-platform/backend/app/services/duckdb_store.py → 项目根
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DUCKDB_PATH = os.path.join(_PROJECT_ROOT, "data", "quant.duckdb")

_table_exists_cache: set[str] | None = None


def _connect() -> Optional[duckdb.DuckDBPyConnection]:
    """临时只读连接（用完即关）。文件不存在/打开失败返回 None（走降级路径）。"""
    if not os.path.exists(DUCKDB_PATH):
        return None
    try:
        return duckdb.connect(DUCKDB_PATH, read_only=True)
    except Exception:  # noqa: BLE001
        return None


def _table_exists(tbl: str) -> bool:
    """表存在性检查（缓存结果，避免每次查询打 information_schema）。"""
    global _table_exists_cache
    if _table_exists_cache is None:
        conn = _connect()
        if conn is None:
            _table_exists_cache = set()
            return False
        try:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
            _table_exists_cache = {r[0] for r in rows}
        except Exception:  # noqa: BLE001
            _table_exists_cache = set()
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return tbl in _table_exists_cache


def _query(sql: str, params: tuple = ()) -> list[dict]:
    conn = _connect()
    if conn is None:
        return []
    try:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _iso(d) -> str:
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


def _to_bar_dict(r: dict, adj_factor: float | None = None) -> dict:
    d = {
        "symbol": r["symbol"],
        "date": _iso(r["trade_date"]),
        "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
        "close": float(r["close"]), "volume": float(r["volume"]), "amount": float(r["amount"]),
    }
    if adj_factor is not None:
        d["_f"] = adj_factor
    return d


def _adjust_bars(bars: list[dict], adj: str) -> list[dict]:
    """按复权口径折算价格（就地修改并返回）。

    库内存的是**未复权原始价**（全A 建仓后的统一口径），复权在读取时实时完成
    —— 单一事实源，避免「存了什么口径就只能用什么口径」。

    实际折算规则统一在 app.services.adjust（与因子计算链路共用同一份实现，
    防止 bars 路径与 series 路径口径漂移）。
    """
    from app.services.adjust import apply_adjust_bars

    return apply_adjust_bars(bars, adj)


def _adj_join_sql() -> tuple[str, str]:
    """复权因子 JOIN 片段。表缺失时降级为不复权（NULL 因子 → ratio 恒为 1）。"""
    if _table_exists("adj_factor_daily"):
        return ("LEFT JOIN main.adj_factor_daily a "
                "  ON a.symbol = k.symbol AND a.trade_date = k.trade_date ",
                "a.adj_factor")
    return "", "NULL AS adj_factor"


def get_stock_bars(symbol: str, adj: str, sd, ed) -> list[dict]:
    """个股日K（与 SQLAlchemy KlineDaily 路径同结构）。sd/ed 为 date 或 ISO 字符串。

    库内存的是未复权原价，adj 由 adj_factor_daily 实时折算 —— 调用方无需关心存储口径。
    """
    if not _table_exists("kline_daily"):
        return []
    join_sql, fac_col = _adj_join_sql()
    rows = _query(
        "SELECT k.symbol, k.trade_date, k.open, k.high, k.low, k.close, "
        f"       k.volume, k.amount, {fac_col} "
        "FROM main.kline_daily k "
        f"{join_sql}"
        "WHERE k.symbol=? AND k.trade_date BETWEEN ? AND ? "
        "ORDER BY k.trade_date",
        (symbol, sd, ed),
    )
    return _adjust_bars([_to_bar_dict(r, r.get("adj_factor")) for r in rows], adj)


def get_stock_bars_batch(symbols: list[str], adj: str, sd, ed) -> dict[str, list[dict]]:
    """批量个股日K（组合回测用）：一次连接/一次查询取全部股票，按 symbol 分组。

    返回 {symbol: [bars...]}；DuckDB 无数据的股票不在结果中（调用方走降级兜底）。
    复权基准**逐标的**计算 —— 跨标的取首/末因子会把不同股票的因子混在一起。
    """
    if not symbols or not _table_exists("kline_daily"):
        return {}
    join_sql, fac_col = _adj_join_sql()
    conn = _connect()
    if conn is None:
        return {}
    out: dict[str, list[dict]] = {}
    try:
        rows = conn.execute(
            "SELECT k.symbol, k.trade_date, k.open, k.high, k.low, k.close, "
            f"       k.volume, k.amount, {fac_col} "
            "FROM main.kline_daily k "
            f"{join_sql}"
            "WHERE k.symbol IN (SELECT unnest(?)) AND k.trade_date BETWEEN ? AND ? "
            "ORDER BY k.symbol, k.trade_date",
            [list(symbols), sd, ed],
        ).fetchall()
        cols = ["symbol", "trade_date", "open", "high", "low", "close",
                "volume", "amount", "adj_factor"]
        for r in rows:
            d = dict(zip(cols, r))
            out.setdefault(d["symbol"], []).append(
                _to_bar_dict(d, d.get("adj_factor")))
        for sym in out:
            out[sym] = _adjust_bars(out[sym], adj)
        return out
    except Exception:  # noqa: BLE001
        return out
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def get_index_bars(symbol: str, sd, ed) -> list[dict]:
    """指数日K（与 IndexKlineDaily 路径同结构）。"""
    if not _table_exists("index_kline_daily"):
        return []
    rows = _query(
        "SELECT symbol, trade_date, open, high, low, close, volume, amount "
        "FROM main.index_kline_daily "
        "WHERE symbol=? AND trade_date BETWEEN ? AND ? "
        "ORDER BY trade_date",
        (symbol, sd, ed),
    )
    return [_to_bar_dict(r) for r in rows]


def get_fundamentals_history(symbol: str) -> list[dict]:
    """多报告期基本面时序（PEAD 用），按 report_date 升序。"""
    if not _table_exists("fundamentals_history"):
        return []
    rows = _query(
        "SELECT symbol, report_date, roe, revenue_yoy, profit_yoy "
        "FROM main.fundamentals_history WHERE symbol=? ORDER BY report_date",
        (symbol,),
    )
    return [
        {
            "symbol": r["symbol"],
            "report_date": _iso(r["report_date"]),
            "roe": r["roe"], "revenue_yoy": r["revenue_yoy"], "profit_yoy": r["profit_yoy"],
        }
        for r in rows
    ]


def get_stocks() -> list[dict]:
    """全市场股票基础信息（stocks 表）。"""
    if not _table_exists("stocks"):
        return []
    rows = _query(
        "SELECT symbol, name, market, raw_code, industry, list_date, market_cap, pe_ttm, pb, roe, "
        "revenue_yoy, profit_yoy FROM main.stocks"
    )
    return [
        {
            "symbol": r["symbol"], "name": r["name"], "market": r["market"],
            "raw_code": r["raw_code"], "industry": r["industry"],
            "list_date": (_iso(r["list_date"]) if r["list_date"] is not None else None),
            "market_cap": r["market_cap"], "pe_ttm": r["pe_ttm"], "pb": r["pb"],
            "roe": r["roe"], "revenue_yoy": r["revenue_yoy"], "profit_yoy": r["profit_yoy"],
        }
        for r in rows
    ]


def count(table: str) -> int:
    """取表行数（诊断用）。"""
    if not _table_exists(table):
        return 0
    conn = _connect()
    if conn is None:
        return 0
    try:
        return int(conn.execute(f"SELECT count(*) FROM main.{table}").fetchone()[0])
    except Exception:  # noqa: BLE001
        return 0
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
