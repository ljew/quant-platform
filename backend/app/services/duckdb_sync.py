"""SQLite → DuckDB 分析表同步服务。

seed 脚本写 SQLite 后调用 sync_all()，把读密集分析表增量同步到 DuckDB，
避免手动重跑 migrate_to_duckdb.py（数据管道自动化，设计 v1.0 目标）。

策略：全量重建（DuckDB 列式写入 133 万行约 1~2s，seed 为低频操作，可靠幂等）。
"""
from __future__ import annotations

import os
from typing import Optional

import duckdb

# 与 duckdb_store 同路径基准
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SQLITE_DB = os.path.join(_PROJECT_ROOT, "data", "quant_dev.db")
DUCKDB_DB = os.path.join(_PROJECT_ROOT, "data", "quant.duckdb")

ANALYTIC_TABLES = [
    "kline_daily",
    "index_kline_daily",
    "fundamentals_history",
    "stocks",
    "index_membership",
    "factor_daily",
]

INDEXES = {
    "kline_daily": ["symbol", "trade_date"],
    "index_kline_daily": ["symbol", "trade_date"],
    "fundamentals_history": ["symbol", "report_date"],
    "stocks": ["symbol"],
    "index_membership": ["index_code", "trade_date"],
    "factor_daily": ["symbol", "trade_date"],
}


def sync_all(only: Optional[list[str]] = None, verbose: bool = True) -> dict[str, tuple[int, int]]:
    """把 SQLite 分析表同步到 DuckDB（幂等）。返回 {表: (sqlite行数, duckdb行数)}。

    only 参数可指定仅同步部分表（如 seed 脚本只动了自己的表）。
    失败抛异常，调用方（seed 脚本）应捕获并仅告警，不阻断写 SQLite。
    """
    tables = [t for t in ANALYTIC_TABLES if (not only) or (t in only)]
    if not os.path.exists(SQLITE_DB):
        raise FileNotFoundError(f"SQLite 库不存在: {SQLITE_DB}")

    con = duckdb.connect(DUCKDB_DB)
    try:
        con.execute("INSTALL sqlite; LOAD sqlite;")
        con.execute(f"ATTACH '{SQLITE_DB}' AS old (TYPE sqlite);")
        result: dict[str, tuple[int, int]] = {}
        for tbl in tables:
            con.execute(f"DROP TABLE IF EXISTS main.{tbl}")
            con.execute(f"CREATE TABLE main.{tbl} AS SELECT * FROM old.{tbl}")
            old_n = con.execute(f"SELECT count(*) FROM old.{tbl}").fetchone()[0]
            new_n = con.execute(f"SELECT count(*) FROM main.{tbl}").fetchone()[0]
            if old_n != new_n:
                raise RuntimeError(f"{tbl} 行数不一致: sqlite={old_n} duckdb={new_n}")
            cols = INDEXES.get(tbl)
            if cols:
                con.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{tbl}_{'_'.join(cols)} ON main.{tbl} ({', '.join(cols)})"
                )
            result[tbl] = (old_n, new_n)
            if verbose:
                print(f"  [duckdb_sync] {tbl:26s} {old_n:>10,} 行 -> DuckDB OK")
        con.execute("CHECKPOINT;")
        con.close()
        return result
    except Exception:
        con.close()
        raise


def sync_after_seed(only: Optional[list[str]] = None) -> None:
    """seed 脚本末尾的推荐入口：同步失败仅告警，不阻断 seed 主流程。

    数据始终安全保存在 SQLite，可稍后手动 `python scripts/migrate_to_duckdb.py` 补齐。
    """
    try:
        sync_all(only=only, verbose=True)
    except Exception as e:  # noqa: BLE001
        print(f"  [duckdb_sync] 同步失败（不影响 SQLite 数据，可稍后手动补齐）: {e}")
