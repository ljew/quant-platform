"""回填指数历史成分股的日K线，使 point-in-time 回测彻底无幸存者偏差。

问题背景
--------
`seed_csi800.py` / `seed_index.py` 只拉取『当前』成分股的日K，因此数据库
`kline_daily` 里只有当下还在指数内的股票。若只用这些股票做回测，天然排除了
『曾经在指数内、后来被剔除/退市』的股票——这本身就是幸存者偏差（且方向偏乐观）。

本脚本的做法
------------
1. 用 `membership_store.get_membership` 拿到区间内每个月的指数成分快照
   （**库优先**：`index_membership` 表已按月落库；缺失月份自动在线兜底
   —— 2026-09 起 tushare 积分到位，`index_weight` 可回溯历史，兜底顺序为
   tushare → csindex → sina。请勿在业务代码里直连 tushare `index_weight`：
   统一走 membership_store 才能保证「落库缓存 + 结果可复现 + 缺月自动补」）；
2. 取窗口内『曾经入选过的全部标的』并集（含已退出者）；
3. 逐只补齐其日K线（DB 已有则跳过），写入 `kline_daily`。

跑一次之后，组合回测的 point-in-time 过滤就能覆盖完整的历史成员集合，
回测结果不再因『缺少已退出股票数据』而残留幸存者偏差。

用法
----
    cd backend
    PYTHONPATH=$(pwd) python scripts/seed_index_history.py \
        --index 000906 --index 000300 \
        --start 2018-01-01 --end 2026-08-01

不传参数时默认：000906 + 000300，区间 2018-01-01 ~ 今天。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta

from app.database import SessionLocal, init_db
from app.services import data_source, ingestion, membership_store


def backfill(index_code: str, start: date, end: date, adj: str = "qfq",
             sleep_s: float = 0.08, dry_run: bool = False) -> dict:
    print(f"\n=== 指数 {index_code}：{'[dry-run] ' if dry_run else ''}历史成员日K回填 ===")
    # PIT 快照：库优先 + 缺失月自动在线兜底（csindex/sina）
    db = SessionLocal()
    try:
        membership = membership_store.get_membership(db, index_code, start, end)
    finally:
        db.close()
    union: set = set()
    for _ds, sset in membership:
        union |= sset
    print(f"  时点快照数: {len(membership)} | 窗口内曾入选标的并集: {len(union)} 只")

    if dry_run:
        return {"index": index_code, "members": len(membership), "symbols": len(union)}

    db = SessionLocal()
    done = 0
    skipped = 0
    failed = 0
    try:
        for i, sym in enumerate(sorted(union), 1):
            try:
                existing = db.execute(
                    ingestion.KlineDaily.__table__.select().where(
                        ingestion.KlineDaily.symbol == sym,
                        ingestion.KlineDaily.adj == adj,
                    ).limit(1)
                ).first()
                if existing is not None:
                    skipped += 1
                else:
                    bars = data_source.get_stock_daily_qfq(sym, start, end)
                    if bars:
                        ingestion.upsert_kline(db, bars, sym, adj)
                        db.commit()
                        done += 1
                    else:
                        failed += 1
            except Exception as e:  # noqa: BLE001
                db.rollback()
                failed += 1
                if i % 50 == 0:
                    print(f"  [{i}/{len(union)}] 最近一只 {sym} 失败: {e}")
            if i % 100 == 0:
                print(f"  [{i}/{len(union)}] 已新增 {done} / 跳过 {skipped} / 失败 {failed}")
            time.sleep(sleep_s)
    finally:
        db.close()
    print(f"  完成: 新增 {done} 只, 已存在跳过 {skipped} 只, 失败 {failed} 只")
    return {"index": index_code, "added": done, "skipped": skipped, "failed": failed}


def main():
    ap = argparse.ArgumentParser(description="回填指数历史成分股日K（消除幸存者偏差）")
    ap.add_argument("--index", action="append", default=None,
                    help="指数代码，可多次指定（默认 000906 000300）")
    ap.add_argument("--start", default="2018-01-01", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--end", default=date.today().isoformat(), help="结束日期 YYYY-MM-DD")
    ap.add_argument("--adj", default="qfq")
    ap.add_argument("--sleep", type=float, default=0.08, help="每只股票请求间隔(秒)，避免限频")
    ap.add_argument("--dry-run", action="store_true", help="只统计标的规模，不实际拉取")
    args = ap.parse_args()

    indexes = args.index or ["000906", "000300"]
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    init_db()
    for idx in indexes:
        backfill(idx, start, end, adj=args.adj, sleep_s=args.sleep, dry_run=args.dry_run)
    print("\n全部完成。")


if __name__ == "__main__":
    main()
    from app.services.duckdb_sync import sync_after_seed

    sync_after_seed(["kline_daily"])
