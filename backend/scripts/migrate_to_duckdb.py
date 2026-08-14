#!/usr/bin/env python3
"""SQLite → DuckDB 分析数据迁移脚本（完整版架构：分析库用 DuckDB）。

用法：
    cd backend && PYTHONPATH=$(pwd) python scripts/migrate_to_duckdb.py [--dry-run]

全量迁移：幂等（重跑重建），行数零差异校验。
核心逻辑在 app/services/duckdb_sync.py，seed 脚本写 SQLite 后也会自动调用。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.duckdb_sync import (  # noqa: E402
    ANALYTIC_TABLES,
    DUCKDB_DB,
    SQLITE_DB,
    sync_all,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="仅打印计划，不执行")
    args = ap.parse_args()
    if args.dry_run:
        print("计划同步的表:", ANALYTIC_TABLES)
        print(f"源: {SQLITE_DB}  目标: {DUCKDB_DB}")
        return
    if not os.path.exists(SQLITE_DB):
        sys.exit(f"SQLite 库不存在: {SQLITE_DB}")
    print(f"同步 {len(ANALYTIC_TABLES)} 张分析表: {SQLITE_DB} -> {DUCKDB_DB}")
    result = sync_all()
    total = sum(n for _, n in result.values())
    print("-" * 60)
    print(f"完成，DuckDB 分析库共 {total:,} 行 -> {DUCKDB_DB}")


if __name__ == "__main__":
    main()
