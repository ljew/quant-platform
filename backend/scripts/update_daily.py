#!/usr/bin/env python3
"""每日增量数据更新（数据管道，设计 v1.0：定时拉取/更新）。

聚合：
1. 股票基本面截面属性（行业/市值/估值）—— ingestion.update_stock_attributes
2. 核心指数日K 增量 —— seed_index_kline.seed_one
3. 指数成分快照补当月 —— membership_store.get_membership（缺失自动回填）
4. DuckDB 分析库同步 —— duckdb_sync.sync_after_seed

用法（backend/ 下）：
    PYTHONPATH=$(pwd) python scripts/update_daily.py [--no-duckdb]
由 app/core/data_scheduler.py 每交易日 15:30 自动调用。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

sys.path.insert(0, ".")  # backend 目录

from app.database import init_db, SessionLocal  # noqa: E402
from app.services import ingestion  # noqa: E402
from app.services import duckdb_sync  # noqa: E402

# 复用时保持 print 输出即可；索引符号表与 seed_index_kline 一致
INDEX_SYMBOLS = [
    "sh000906", "sh000300", "sh000905", "sh000852",
    "sh000001", "sz399001", "sz399006", "sh000016",
]


def update_index_kline(session) -> int:
    from scripts.seed_index_kline import seed_one

    total = 0
    for sym in INDEX_SYMBOLS:
        try:
            total += seed_one(sym, session)
        except Exception as e:  # noqa: BLE001
            print(f"  [err] {sym}: {e}")
    return total


def update_attributes() -> int:
    try:
        n = ingestion.update_stock_attributes()
        print(f"  [ok] 股票属性更新 {n} 只")
        return n
    except Exception as e:  # noqa: BLE001
        print(f"  [err] 属性更新: {e}")
        return 0


def update_membership(session) -> int:
    from app.services.membership_store import get_membership

    codes = ["000906", "000300", "000905", "000852", "399006"]
    sd = date(datetime.now().year, 1, 1)
    ed = date.today()
    total = 0
    for code in codes:
        try:
            snaps = get_membership(session, code, sd, ed)
            total += sum(len(s) for _, s in snaps)
        except Exception as e:  # noqa: BLE001
            print(f"  [err] membership {code}: {e}")
    print(f"  [ok] 成分快照更新完成（{len(codes)} 指数）")
    return total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-duckdb", action="store_true", help="跳过 DuckDB 同步")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 每日增量更新开始")
        n_attr = update_attributes()
        n_idx = update_index_kline(db)
        n_mem = update_membership(db)
        print(f"  属性 {n_attr} 只 | 指数K线 +{n_idx} 行 | 成分快照 {n_mem} 条")
        if not args.no_duckdb:
            print("  同步 DuckDB…")
            duckdb_sync.sync_after_seed()
        print("每日增量更新完成 ✓")
    finally:
        db.close()


if __name__ == "__main__":
    main()
