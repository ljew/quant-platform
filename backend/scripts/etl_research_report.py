"""券商研报入库 CLI。

    python scripts/etl_research_report.py --days 30    # 回补一个月
    python scripts/etl_research_report.py              # 默认最近 7 天

表由 models 声明，`init_db()` 自动建。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, init_db  # noqa: E402
from app.services.research_report import sync  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="券商研报入库")
    ap.add_argument("--days", type=int, default=7, help="回看自然日数")
    ap.add_argument("--qtype", type=int, default=0, help="0=个股 1=行业 2=策略")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        n = sync(db, days=args.days, qtype=args.qtype)
    finally:
        db.close()
    print(json.dumps({"inserted": n, "days": args.days, "qtype": args.qtype},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
