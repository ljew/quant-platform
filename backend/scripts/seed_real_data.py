"""种子脚本：拉取真实行情灌入本地库（用于首次演示 / 沙箱环境）。

优先 akshare（真实历史全量），不可用时自动回退 westock CLI（腾讯接口，最近 N 日）。
运行：
  PYTHONPATH=.. python scripts/seed_real_data.py
"""
from __future__ import annotations

from datetime import datetime

from app.database import SessionLocal, init_db
from app.models import Stock, KlineDaily
from app.services import data_source, ingestion

FAVORITES = [
    ("sh600519", "贵州茅台"), ("sz300750", "宁德时代"),
    ("sh601318", "中国平安"), ("sz000858", "五粮液"),
    ("sh600036", "招商银行"), ("sh600276", "恒瑞医药"),
    ("sz000001", "平安银行"), ("sh601012", "隆基绿能"),
]

LIMIT = 750  # 约 3 年日K


def main():
    init_db()
    db = SessionLocal()
    total_rows = 0
    try:
        for sym, name in FAVORITES:
            # 写入/更新股票基础信息
            stock = db.query(Stock).filter(Stock.symbol == sym).first()
            if not stock:
                market = "sh" if sym.startswith("sh") else "sz"
                db.add(Stock(symbol=sym, name=name, market=market,
                             raw_code=sym[2:], industry=None, list_date=None))
            # 拉取日K
            print(f"拉取 {sym} {name} 日K (limit={LIMIT}) ...", end=" ", flush=True)
            rows = data_source.get_daily_kline(
                sym, start_date=datetime(2015, 1, 1).date(), limit=LIMIT
            )
            if rows:
                ingestion.upsert_kline(db, rows, sym, "qfq")
                total_rows += len(rows)
                print(f"OK {len(rows)} 条, 最新 {rows[-1]['trade_date']}")
            else:
                print("无数据")
        db.commit()
        print(f"\n完成。共写入 {total_rows} 条 K 线，{len(FAVORITES)} 只标的。")
    finally:
        db.close()


if __name__ == "__main__":
    main()
    from app.services.duckdb_sync import sync_after_seed

    sync_after_seed(["kline_daily", "stocks"])
