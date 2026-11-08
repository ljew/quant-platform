"""回补研报历史（按月拉，供「盈利预测修正」因子使用）。

为什么需要：`sync()` 默认只拉最近几天，而算 EPS 修正至少要「当前 90 天」和
「之前 90 天」两段，也就是半年前的数据。库里原来只有 30 天（1867 篇）。

实测东财 reportapi 单月约 1000 篇、0.23 秒/页，59 个月约 2~3 分钟。
**不要并发** —— 同域名并发容易触发限流（公告接口那次就是并发 10 被封）。

用法：
    docker exec quant-backend python /app/scripts/backfill_research_report.py
    docker exec quant-backend python /app/scripts/backfill_research_report.py --from 2023-01
"""
import argparse
import calendar
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal  # noqa: E402
from app.services.research_report import sync  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="按月回补券商研报")
    ap.add_argument("--from", dest="start", default="2022-01",
                    help="起始月份 YYYY-MM（默认 2022-01）")
    ap.add_argument("--to", dest="end", default="",
                    help="结束月份 YYYY-MM（默认当月）")
    args = ap.parse_args()

    y0, m0 = (int(x) for x in args.start.split("-"))
    today = date.today()
    if args.end:
        y1, m1 = (int(x) for x in args.end.split("-"))
    else:
        y1, m1 = today.year, today.month

    db = SessionLocal()
    total = 0
    months = (y1 - y0) * 12 + (m1 - m0) + 1
    i = 0
    try:
        y, m = y0, m0
        while (y, m) <= (y1, m1):
            i += 1
            begin = date(y, m, 1)
            last = calendar.monthrange(y, m)[1]
            end = date(y, m, last)
            if end > today:
                end = today
            try:
                n = sync(db, start=begin, end=end)
                total += n
                print(f"[{i}/{months}] {y}-{m:02d}  {n:5d} 篇  累计 {total:6d}",
                      flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[{i}/{months}] {y}-{m:02d}  失败：{str(e)[:90]}", flush=True)
            m += 1
            if m > 12:
                y, m = y + 1, 1
    finally:
        db.close()

    print(f"\n完成：累计写入 {total} 篇")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
