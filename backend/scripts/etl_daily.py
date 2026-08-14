#!/usr/bin/env python3
"""ETL 每日数据管道（设计 v1.0：tushare 采集 → 增量入库 → 因子计算落库）。

用法（backend/ 下）：
    PYTHONPATH=$(pwd) python scripts/etl_daily.py [--no-duckdb] [--symbols 600519,000858]
由 app/core/data_scheduler.py 每交易日 17:00 自动调用。

流程：
  E   tushare：指数日K增量 / 股票属性 / 核心池个股日K增量（前复权）
  T   核心池最新交易日截面 14 因子（factor_library 表达式引擎）
  L   SQLite（kline_daily/index_kline_daily/stocks/factor_daily）→ DuckDB 同步
"""
from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("etl_daily")

from app.services.etl import run_etl  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-duckdb", action="store_true", help="跳过 DuckDB 同步")
    ap.add_argument("--symbols", default="", help="指定股票池（逗号分隔原始代码），默认核心指数成分并集")
    args = ap.parse_args()

    universe = None
    if args.symbols:
        from app.database import SessionLocal
        from app.services.data_source import normalize_symbol

        db = SessionLocal()
        universe = []
        for c in args.symbols.split(","):
            c = c.strip().zfill(6)
            if c:
                sym, _ = normalize_symbol(c)
                universe.append(sym)
        db.close()

    def prog(p: float, msg: str) -> None:
        print(f"  [{p * 100:5.1f}%] {msg}")

    print("ETL 开始（tushare 采集 → 增量入库 → 因子落库）…")
    stats = run_etl(universe=universe, progress=prog, no_duckdb=args.no_duckdb)
    print("-" * 60)
    print(f"完成：核心池 {stats.get('universe', '-')} 只 | 指数K线 +{stats['index_kline']} 行 "
          f"| 个股K线 +{stats['kline']} 行 | 属性 {stats['attrs']} 只")
    print(f"     因子截面 {stats['factors']} 条（{stats.get('factor_date', '-')}）"
          + (" | DuckDB 已同步" if not args.no_duckdb else " | 跳过 DuckDB"))


if __name__ == "__main__":
    main()
