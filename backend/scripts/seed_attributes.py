"""刷新 stocks 表的行业 / 市值 / 估值截面快照（依赖 tushare）。

用法（在 backend/ 目录下）：
    PYTHONPATH=$(pwd) python scripts/seed_attributes.py

数据来源：tushare stock_basic（行业）+ daily_basic（总市值/PE_TTM/PB，最近交易日）。
结果写入 stocks 表的 industry / market_cap / pe_ttm / pb 字段，
供指数增强策略的行业/市值中性化与估值因子使用。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import init_db
from app.services import ingestion


def main() -> None:
    init_db()
    n = ingestion.update_stock_attributes()
    print(f"已更新 {n} 只股票的基本面截面属性")


if __name__ == "__main__":
    main()
    from app.services.duckdb_sync import sync_after_seed

    sync_after_seed(["stocks"])
