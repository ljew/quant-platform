#!/usr/bin/env python3
"""SQLite → DuckDB 分析数据迁移脚本（完整版架构：分析库用 DuckDB）。

迁移策略：
- 读密集分析表 → DuckDB（只读分析库，回测/因子研究走列式加速）：
    kline_daily / index_kline_daily / fundamentals_history / stocks
- 业务写表 → 保留 SQLite（写路径零风险，DuckDB 单写者不擅长高频 CRUD）：
    backtests / paper_tasks / paper_trades / paper_snapshots / strategies

用法：
    cd backend && PYTHONPATH=$(pwd) python scripts/migrate_to_duckdb.py [--dry-run]

设计文档映射：原「PostgreSQL(业务) + TimescaleDB(时序)」→「SQLite(业务) + DuckDB(时序/分析)」，
用户确认 pg 可替换为 duckdb。
"""
from __future__ import annotations

import argparse
import os
import sys

import duckdb

# 路径以 backend/ 为基准
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQLITE_DB = os.path.join(os.path.dirname(BACKEND_DIR), "data", "quant_dev.db")
DUCKDB_DB = os.path.join(os.path.dirname(BACKEND_DIR), "data", "quant.duckdb")

ANALYTIC_TABLES = ["kline_daily", "index_kline_daily", "fundamentals_history", "stocks"]

# 每张分析表的查询索引（symbol+date 点查 / 区间查）
ANALYTIC_INDEXES = {
    "kline_daily": ["symbol", "trade_date"],
    "index_kline_daily": ["symbol", "trade_date"],
    "fundamentals_history": ["symbol", "report_date"],
    "stocks": ["symbol"],
}


def migrate(dry_run: bool = False) -> None:
    if not os.path.exists(SQLITE_DB):
        sys.exit(f"SQLite 库不存在: {SQLITE_DB}")

    con = duckdb.connect(DUCKDB_DB)
    try:
        con.execute("INSTALL sqlite; LOAD sqlite;")
        con.execute(f"ATTACH '{SQLITE_DB}' AS old (TYPE sqlite);")

        for tbl in ANALYTIC_TABLES:
            exists = con.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
                [tbl],
            ).fetchone()
            if exists:
                # 全量重建，保证与 SQLite 一致（幂等）
                con.execute(f"DROP TABLE IF EXISTS main.{tbl}")
            con.execute(f"CREATE TABLE main.{tbl} AS SELECT * FROM old.{tbl}")

            old_n = con.execute(f"SELECT count(*) FROM old.{tbl}").fetchone()[0]
            new_n = con.execute(f"SELECT count(*) FROM main.{tbl}").fetchone()[0]
            status = "OK" if old_n == new_n else "MISMATCH!"
            print(f"  {tbl:26s} sqlite={old_n:>10,}  duckdb={new_n:>10,}  {status}")

            # 建索引
            cols = ANALYTIC_INDEXES.get(tbl)
            if cols:
                idx_name = f"idx_{tbl}_{'_'.join(cols)}"
                con.execute(
                    f"CREATE INDEX IF NOT EXISTS {idx_name} ON main.{tbl} ({', '.join(cols)})"
                )

        # 汇总信息
        total_sqlite = sum(
            con.execute(f"SELECT count(*) FROM old.{t}").fetchone()[0] for t in ANALYTIC_TABLES
        )
        total_duck = sum(
            con.execute(f"SELECT count(*) FROM main.{t}").fetchone()[0] for t in ANALYTIC_TABLES
        )
        print("-" * 64)
        print(f"分析表合计: sqlite={total_sqlite:,}  duckdb={total_duck:,}")
        if total_sqlite != total_duck:
            sys.exit("数据量不一致，请检查！")
        print(f"迁移完成 -> {DUCKDB_DB}")
        if not dry_run:
            con.execute("CHECKPOINT;")
        con.close()
    except Exception as e:  # noqa: BLE001
        con.close()
        raise


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="仅打印计划，不执行")
    args = ap.parse_args()
    if args.dry_run:
        print("计划迁移的表:", ANALYTIC_TABLES)
        print(f"源: {SQLITE_DB}  目标: {DUCKDB_DB}")
    else:
        migrate()
