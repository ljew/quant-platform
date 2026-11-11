"""SQLite → DuckDB 分析表同步服务。

seed 脚本写 SQLite 后调用 sync_all()，把读密集分析表增量同步到 DuckDB，
避免手动重跑 migrate_to_duckdb.py（数据管道自动化，设计 v1.0 目标）。

策略：全量重建（DuckDB 列式写入 133 万行约 1~2s，seed 为低频操作，可靠幂等）。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import duckdb

logger = logging.getLogger("quant.duckdb_sync")

# 与 duckdb_store 同路径基准
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SQLITE_DB = os.path.join(_PROJECT_ROOT, "data", "quant_dev.db")
DUCKDB_DB = os.path.join(_PROJECT_ROOT, "data", "quant.duckdb")

# 无扩展路径的分块大小：20 万行 × 约 10 列 ≈ 100~200MB 峰值，容器 5.5G 够用
CHUNK_ROWS = 200_000

ANALYTIC_TABLES = [
    "kline_daily",
    "index_kline_daily",
    "fundamentals_history",
    "stocks",
    "index_membership",
    "factor_daily",
    # 2026-09 扩池到全A 时新增：回测取价依赖复权因子，因子检验依赖现金流原始字段。
    # 不进 DuckDB 的话，评估模块每次都要回 SQLite 查，失去列存优势。
    "adj_factor_daily",
    "financials_raw",
]

INDEXES = {
    "kline_daily": ["symbol", "trade_date"],
    "index_kline_daily": ["symbol", "trade_date"],
    "fundamentals_history": ["symbol", "report_date"],
    "stocks": ["symbol"],
    "index_membership": ["index_code", "trade_date"],
    "factor_daily": ["symbol", "trade_date"],
    "adj_factor_daily": ["symbol", "trade_date"],
    "financials_raw": ["symbol", "ann_date"],
}


# SQLite 声明类型 → DuckDB 类型。顺序有讲究：DATETIME 必须排在 DATE 前面。
_TYPE_RULES = (
    ("INT", "BIGINT"),
    ("REAL", "DOUBLE"),
    ("FLOA", "DOUBLE"),
    ("DOUB", "DOUBLE"),
    ("DEC", "DOUBLE"),
    ("NUM", "DOUBLE"),
    ("BOOL", "BOOLEAN"),
    ("DATETIME", "TIMESTAMP"),
    ("TIMESTAMP", "TIMESTAMP"),
    ("DATE", "DATE"),
    ("CHAR", "VARCHAR"),
    ("CLOB", "VARCHAR"),
    ("TEXT", "VARCHAR"),
)


def _duck_type(decl: str) -> str:
    d = (decl or "").upper()
    for key, typ in _TYPE_RULES:
        if key in d:
            return typ
    return "VARCHAR"


def _has_sqlite_ext(con) -> bool:
    try:
        con.execute("LOAD sqlite;")
        return True
    except Exception:  # noqa: BLE001
        return False


def _sync_via_ext(con, tables, verbose: bool) -> dict[str, tuple[int, int]]:
    """走 sqlite 扩展（最快，列存直搬）。"""
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
    return result


def _sync_via_python(con, tables, verbose: bool) -> dict[str, tuple[int, int]]:
    """不依赖任何 DuckDB 扩展：sqlite3 分块读 → pandas → INSERT。

    为什么要有这条：容器里 `/root/.duckdb/extensions/*/linux_arm64/` 是空目录，
    而 `INSTALL sqlite` 在容器内会挂死（extensions.duckdb.org 整站 404/403）。
    2026-11-11 就是因此让分析库静默冻结了 26 小时：步骤显示「完成 0 行」，绿的。

    类型不靠推断，直接按 SQLite 的声明类型建表 + 显式 CAST —— 否则日期列会被
    pandas 读成字符串、整列退化成 VARCHAR，下游按日期比较的查询全部失准。
    """
    import sqlite3

    import pandas as pd

    src = sqlite3.connect(f"file:{SQLITE_DB}?mode=ro", uri=True, timeout=30)
    result: dict[str, tuple[int, int]] = {}
    try:
        for tbl in tables:
            info = src.execute(f'PRAGMA table_info("{tbl}")').fetchall()
            if not info:
                raise RuntimeError(f"SQLite 里没有表 {tbl}")
            schema = [(r[1], _duck_type(r[2])) for r in info]
            cols_def = ", ".join(f'"{n}" {t}' for n, t in schema)
            cols_cast = ", ".join(f'CAST("{n}" AS {t})' for n, t in schema)

            con.execute(f"DROP TABLE IF EXISTS main.{tbl}")
            con.execute(f"CREATE TABLE main.{tbl} ({cols_def})")

            moved = 0
            for df in pd.read_sql_query(f'SELECT * FROM "{tbl}"', src, chunksize=CHUNK_ROWS):
                if df.empty:
                    continue
                con.register("__chunk", df)
                try:
                    con.execute(f'INSERT INTO main.{tbl} SELECT {cols_cast} FROM "__chunk"')
                finally:
                    con.unregister("__chunk")
                moved += len(df)

            src_n = src.execute(f'SELECT count(*) FROM "{tbl}"').fetchone()[0]
            if src_n != moved:
                raise RuntimeError(f"{tbl} 行数不一致: sqlite={src_n} 搬运={moved}")
            cols = INDEXES.get(tbl)
            if cols:
                con.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{tbl}_{'_'.join(cols)} ON main.{tbl} ({', '.join(cols)})"
                )
            result[tbl] = (src_n, moved)
            if verbose:
                print(f"  [duckdb_sync] {tbl:26s} {src_n:>10,} 行 -> DuckDB OK (python)")
        con.execute("CHECKPOINT;")
        return result
    finally:
        src.close()


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
        # 🔴 必须先关掉自动安装：容器访问 extensions.duckdb.org 是整站 403，
        # DuckDB 会为 INSTALL sqlite 反复重试，**整条管道就挂在这一步**（实测挂 30 分钟、
        # 线程杀不掉，DuckDB 写锁还留在进程里，之后所有读 DuckDB 的接口一起降级）。
        # 关掉后 LOAD 立刻报「扩展不存在」，可以马上转 python 搬运路径。
        con.execute("SET autoinstall_known_extensions=false; SET autoload_known_extensions=false;")
        if _has_sqlite_ext(con):
            return _sync_via_ext(con, tables, verbose)
        logger.warning("DuckDB sqlite 扩展不可用，改用 sqlite3+pandas 分块搬运")
        return _sync_via_python(con, tables, verbose)
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass


def sync_after_seed(only: Optional[list[str]] = None) -> None:
    """seed 脚本末尾的推荐入口：同步失败仅告警，不阻断 seed 主流程。

    数据始终安全保存在 SQLite，可稍后手动 `python scripts/migrate_to_duckdb.py` 补齐。
    """
    try:
        sync_all(only=only, verbose=True)
    except Exception as e:  # noqa: BLE001
        print(f"  [duckdb_sync] 同步失败（不影响 SQLite 数据，可稍后手动补齐）: {e}")
