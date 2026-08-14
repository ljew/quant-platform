"""DuckDB 只读分析存储层（完整版架构：分析库用 DuckDB）。

设计：SQLite 保留业务写表（backtests/paper_*/strategies），DuckDB 承载
读密集分析表（kline_daily 133万行 / index_kline_daily / fundamentals_history / stocks）。

接入方式：回测/模拟盘/因子研究的 K线、指数、基本面读取优先走本层（列式+向量化），
返回与既有 SQLAlchemy 路径完全一致的 list[dict] 结构；DuckDB 文件缺失或表不存在时
返回空，调用方自动回退 SQLite/在线路径（优雅降级，不破坏现有链路）。

迁移脚本：backend/scripts/migrate_to_duckdb.py
"""
from __future__ import annotations

import datetime
import os
import threading
from typing import Optional

import duckdb

# .../quant-platform/backend/app/services/duckdb_store.py → 项目根
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DUCKDB_PATH = os.path.join(_PROJECT_ROOT, "data", "quant.duckdb")

_lock = threading.Lock()
_conn: Optional[duckdb.DuckDBPyConnection] = None
_tables_checked = False
_table_exists_cache: set[str] = set()


def _get_conn() -> Optional[duckdb.DuckDBPyConnection]:
    """惰性单例只读连接；DuckDB 文件不存在时返回 None（走降级路径）。"""
    global _conn
    if _conn is not None:
        return _conn
    if not os.path.exists(DUCKDB_PATH):
        return None
    with _lock:
        if _conn is None:
            try:
                _conn = duckdb.connect(DUCKDB_PATH, read_only=True)
            except Exception:  # noqa: BLE001
                _conn = None
    return _conn


def _table_exists(tbl: str) -> bool:
    """缓存表存在性检查，避免每次查询都打 information_schema。"""
    global _tables_checked
    conn = _get_conn()
    if conn is None:
        return False
    if not _tables_checked:
        try:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
            _table_exists_cache.update(r[0] for r in rows)
        except Exception:  # noqa: BLE001
            pass
        _tables_checked = True
    return tbl in _table_exists_cache


def _query(sql: str, params: tuple = ()) -> list[dict]:
    conn = _get_conn()
    if conn is None:
        return []
    try:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        return []


def _iso(d) -> str:
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


def get_stock_bars(symbol: str, adj: str, sd, ed) -> list[dict]:
    """个股日K（与 SQLAlchemy KlineDaily 路径同结构）。sd/ed 为 date 或 ISO 字符串。"""
    if not _table_exists("kline_daily"):
        return []
    rows = _query(
        "SELECT symbol, trade_date, open, high, low, close, volume, amount "
        "FROM main.kline_daily "
        "WHERE symbol=? AND adj=? AND trade_date BETWEEN ? AND ? "
        "ORDER BY trade_date",
        (symbol, adj, sd, ed),
    )
    return [
        {
            "symbol": r["symbol"],
            "date": _iso(r["trade_date"]),
            "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
            "close": float(r["close"]), "volume": float(r["volume"]), "amount": float(r["amount"]),
        }
        for r in rows
    ]


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
    return [
        {
            "symbol": r["symbol"],
            "date": _iso(r["trade_date"]),
            "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
            "close": float(r["close"]), "volume": float(r["volume"]), "amount": float(r["amount"]),
        }
        for r in rows
    ]


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
    try:
        conn = _get_conn()
        if conn is None:
            return 0
        return int(conn.execute(f"SELECT count(*) FROM main.{table}").fetchone()[0])
    except Exception:  # noqa: BLE001
        return 0
