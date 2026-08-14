"""把核心指数（基准指数）的历史日K线拉入 index_kline_daily 表。

用法：
  python scripts/seed_index_kline.py --all          # 拉全部默认指数
  python scripts/seed_index_kline.py --symbol sh000906 sh000300  # 指定

说明：
- 直接用 akshare stock_zh_index_daily（返回全量历史，非 tushare 近段子集），
  以保证首次入库即覆盖完整历史，之后回测/模拟盘读库即可离线运行。
- 按 (symbol, trade_date) 去重，可重复运行补数据。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from sqlalchemy import select

# 允许以 `python scripts/seed_index_kline.py` 直接运行
sys.path.insert(0, ".")

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import IndexKlineDaily  # noqa: E402
import app.services.data_source as ds  # noqa: E402

# 常用基准指数（新浪格式 symbol）
DEFAULT_INDICES = [
    "sh000906",  # 中证800
    "sh000300",  # 沪深300
    "sh000905",  # 中证500
    "sh000852",  # 中证1000
    "sh000001",  # 上证指数
    "sz399001",  # 深证成指
    "sz399006",  # 创业板指
    "sh000016",  # 上证50
]


def seed_one(symbol: str, session) -> int:
    ak = ds._require_ak()
    df = ak.stock_zh_index_daily(symbol=symbol)
    if df is None or df.empty:
        print(f"  [skip] {symbol}: 无数据")
        return 0
    existing = {
        r[0]
        for r in session.execute(
            select(IndexKlineDaily.trade_date).where(IndexKlineDaily.symbol == symbol)
        ).all()
    }
    objs = []
    for _, r in df.iterrows():
        d = r["date"]
        if isinstance(d, str):
            d = date.fromisoformat(d)
        if d in existing:
            continue
        objs.append(
            IndexKlineDaily(
                symbol=symbol,
                trade_date=d,
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=int(float(r.get("volume", 0) or 0)),
                amount=float(r.get("amount", 0) or 0),
            )
        )
    if objs:
        session.bulk_save_objects(objs)
        session.commit()
    print(f"  [ok] {symbol}: 新增 {len(objs)} 行（累计 {len(existing) + len(objs)}）")
    return len(objs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed index daily kline into index_kline_daily")
    parser.add_argument("--symbol", nargs="*", help="指定指数 symbol，如 sh000906")
    parser.add_argument("--all", action="store_true", help="拉取全部默认指数")
    args = parser.parse_args()

    init_db()
    session = SessionLocal()
    try:
        syms = args.symbol if args.symbol else (DEFAULT_INDICES if args.all else DEFAULT_INDICES)
        total = 0
        for s in syms:
            print(f"seeding {s} ...")
            total += seed_one(s, session)
        print(f"\n完成：共新增 {total} 行指数行情。")
    finally:
        session.close()


if __name__ == "__main__":
    main()
