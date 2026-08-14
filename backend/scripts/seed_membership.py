#!/usr/bin/env python3
"""预拉取指数成分股时点快照（index_membership）落库缓存。

背景：回测/模拟盘在首次运行时若本地无 membership 快照会在线拉取，导致
首次较慢且依赖在线源。本脚本把核心指数的全历史月度快照一次性入库，
之后回测完全离线、结果可复现。

用法（backend/ 目录下）：
    PYTHONPATH=$(pwd) python scripts/seed_membership.py
    PYTHONPATH=$(pwd) python scripts/seed_membership.py --index 000906,000300
    PYTHONPATH=$(pwd) python scripts/seed_membership.py --start 2021-01-01 --end 2026-08-01

数据来源：tushare index_weight（免费 token 可回溯至 2019 年）。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

sys.path.insert(0, ".")  # backend 目录

from app.database import init_db, SessionLocal  # noqa: E402
from app.services.membership_store import get_membership  # noqa: E402

DEFAULT_INDEXES = ["000906", "000300", "000905", "000852", "000001", "399006"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default=",".join(DEFAULT_INDEXES), help="指数代码（逗号分隔）")
    ap.add_argument("--start", default="2020-01-01", help="起始日期")
    ap.add_argument("--end", default=date.today().isoformat(), help="结束日期")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    indexes = [i.strip() for i in args.index.split(",") if i.strip()]
    sd = date.fromisoformat(args.start)
    ed = date.fromisoformat(args.end)
    for code in indexes:
        snaps = get_membership(db, code, sd, ed)
        total = sum(len(s) for _, s in snaps)
        print(f"  {code}: {len(snaps)} 个月度快照 / 共 {total} 条成分记录（{sd}~{ed}）")
    db.close()
    print("完成。")


if __name__ == "__main__":
    main()
