"""政策原文采集 CLI。

    python scripts/etl_policy_doc.py --days 3 --limit 12

依赖 DeepSeek 搜索（`/v1/tools/search` + `/v1/tools/fetch`），
每条约 0.01 元，一天十几条不到两毛钱。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal  # noqa: E402
from app.services.policy_doc import collect  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="政策原文采集")
    ap.add_argument("--days", type=int, default=3, help="回看快讯天数")
    ap.add_argument("--limit", type=int, default=12, help="本轮最多处理几条政策")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        stat = collect(db, days=args.days, limit=args.limit)
    finally:
        db.close()
    print(json.dumps(stat, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
