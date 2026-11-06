#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""导出交易日历给 Windows 看门狗用。

平台侧 data/trading_calendar.json 含 1990 年至今全部交易日（几百 KB），
看门狗只需要最近两年。每次跨年前重跑一次即可。

用法（quant-platform 根目录）：
    python3 scripts/export_calendar.py                 # 默认导出当年 + 次年
    python3 scripts/export_calendar.py --years 2026 2027
    python3 scripts/export_calendar.py --out qmt/watchdog/calendar.json
"""

import argparse
import json
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "trading_calendar.json")
DEFAULT_OUT = os.path.join(ROOT, "qmt", "watchdog", "calendar.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="*", type=int, default=None,
                    help="要导出的年份，默认当年与次年")
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    years = args.years or [date.today().year, date.today().year + 1]
    if not os.path.exists(args.src):
        print("找不到源日历：%s" % args.src, file=sys.stderr)
        return 1
    with open(args.src, encoding="utf-8") as f:
        raw = json.load(f).get("trade_dates", [])
    keep = sorted(d for d in raw if int(str(d)[:4]) in years)
    if not keep:
        print("年份 %s 在源日历里没有数据" % years, file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"trade_dates": keep, "source": "quant-platform data/trading_calendar.json",
                   "years": years}, f, ensure_ascii=False)
    print("已导出 %d 个交易日（%s）→ %s（%.1f KB）"
          % (len(keep), ", ".join(str(y) for y in years), args.out,
             os.path.getsize(args.out) / 1024.0))
    per_year = dict((y, len([d for d in keep if int(str(d)[:4]) == y])) for y in years)
    for y, n in per_year.items():
        if n < 200:
            print("⚠ %d 年只有 %d 个交易日，源日历可能还没更新到该年份"
                  "（跨年前请重跑本脚本，否则看门狗会把节假日当交易日）" % (y, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
