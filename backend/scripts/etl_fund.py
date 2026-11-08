"""基金 / 基金经理数据入库 CLI。

    python scripts/etl_fund.py                 # 全量：basic + manager + 近 5 日净值
    python scripts/etl_fund.py --only nav --nav-days 10
    python scripts/etl_fund.py --only basic

表由 models 声明，`init_db()` 会自动建（首次跑不需要手工 DDL）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, init_db  # noqa: E402
from app.services.fund_data import (sync_all, sync_basic, sync_manager,  # noqa: E402
                                    sync_nav)


def main() -> int:
    ap = argparse.ArgumentParser(description="基金/基金经理入库")
    ap.add_argument("--only", choices=["basic", "manager", "nav"], default=None,
                    help="只跑其中一类；缺省全跑")
    ap.add_argument("--nav-days", type=int, default=5, help="净值回看自然日数")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        if args.only == "basic":
            out = {"basic": sync_basic(db)}
        elif args.only == "manager":
            out = {"manager": sync_manager(db)}
        elif args.only == "nav":
            out = {"nav": sync_nav(db, days=args.nav_days)}
        else:
            out = sync_all(db, nav_days=args.nav_days)
    finally:
        db.close()

    print(json.dumps(out, ensure_ascii=False))
    return 0 if not out.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
