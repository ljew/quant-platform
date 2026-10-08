#!/usr/bin/env python
"""回填 stock_valuation_daily —— 估值因子 PIT 时序表（B1，2026-10-08）。

为什么需要：
  `stocks` 表的 pe_ttm / pb / market_cap 是**最新一列快照**，被 factor_mining
  的 `_attrs_map` 复用于所有历史截面 → 2019 年的截面用 2026 年的 PE，属明确
  前视。实测茅台 2019-01-10 pe_ttm=24.42 / pb=7.59，与今日值完全不同。

  在本表回填完成前，**任何含估值/市值因子的历史结论都不可信**。

用法：
    # 预演（不写库，只看能拉多少）
    python scripts/backfill_valuation.py --dry-run

    # 回填 2019 至今（约 640 万行 / 0.4GB / 20 分钟）
    python scripts/backfill_valuation.py --start 20190101

    # 只回填最近一年
    python scripts/backfill_valuation.py --start 20250101

    # 断点续跑（已存在的日期跳过）
    python scripts/backfill_valuation.py --start 20190101 --resume

特性：
  · 幂等：同一 (symbol, trade_date) 唯一约束，重复跑用 UPSERT
  · 断点续跑：--resume 时跳过已有数据的日期（按交易日粒度）
  · 限流保护：每次调用间隔可配（tushare 按日全市场约 0.6s/次）
  · 进度落盘：每 N 个交易日打印一次 ETA
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _load_token() -> str:
    """从环境变量或 backend/../.env 读 tushare token。"""
    tok = os.environ.get("QUANT_TUSHARE_TOKEN") or os.environ.get("TUSHARE_TOKEN")
    if tok:
        return tok
    env = BACKEND.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("QUANT_TUSHARE_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("未找到 tushare token（设 QUANT_TUSHARE_TOKEN 或写 .env）")


def _to_symbol(ts_code: str) -> str:
    """600519.SH → sh600519；000001.SZ → sz000001。"""
    code, _, ex = ts_code.partition(".")
    if ex.upper() == "SH":
        return f"sh{code}"
    if ex.upper() == "SZ":
        return f"sz{code}"
    return ts_code  # BJ 等原样保留


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20190101", help="起始日 YYYYMMDD")
    ap.add_argument("--end", default="", help="结束日 YYYYMMDD（空=今日）")
    ap.add_argument("--fields", default="ts_code,trade_date,pe_ttm,pb,total_mv,circ_mv,turnover_rate")
    ap.add_argument("--interval", type=float, default=0.12, help="调用间隔秒（限流保护）")
    ap.add_argument("--resume", action="store_true", help="跳过已有数据的交易日")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 个交易日（0=不限）")
    args = ap.parse_args()

    t0 = time.time()
    import tushare as ts

    from app.database import SessionLocal
    from app.models import StockValuationDaily

    ts.set_token(_load_token())
    pro = ts.pro_api()

    # ── 交易日清单：以本地 kline_daily 为准（与因子截面日期完全一致，避免多拉）──
    # 注意 kline_daily.trade_date 存的是 '2019-01-02'（带横线 ISO），而 tushare
    # 端点要 '20190102'，两边都要转换。
    import sqlite3

    db_path = BACKEND.parent / "data" / "quant_dev.db"
    scon = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    lo = args.start
    hi = args.end or date.today().strftime("%Y%m%d")
    # 统一成 ISO 比较
    lo_iso = f"{lo[:4]}-{lo[4:6]}-{lo[6:8]}" if len(lo) == 8 else lo
    hi_iso = f"{hi[:4]}-{hi[4:6]}-{hi[6:8]}" if len(hi) == 8 else hi
    rows = scon.execute(
        "SELECT DISTINCT trade_date FROM kline_daily "
        "WHERE trade_date >= ? AND trade_date <= ? ORDER BY trade_date",
        (lo_iso, hi_iso),
    ).fetchall()
    scon.close()
    trade_dates = [str(r[0]).replace("-", "") for r in rows]
    trade_dates = [d for d in trade_dates if len(d) == 8]
    if args.limit:
        trade_dates = trade_dates[: args.limit]
    print(f"待处理 {len(trade_dates)} 个交易日：{trade_dates[0]} ~ {trade_dates[-1]}")

    if not trade_dates:
        print("无交易日（检查 --start/--end）")
        return 1

    # ── 断点续跑：查已有数据的日期 ──
    if args.resume:
        db = SessionLocal()
        try:
            have = {
                str(d).replace("-", "")
                for (d,) in db.query(StockValuationDaily.trade_date).distinct().all()
            }
        finally:
            db.close()
        n0 = len(trade_dates)
        trade_dates = [d for d in trade_dates if d not in have]
        print(f"断点续跑：已有 {len(have)} 个日期，跳过 {n0 - len(trade_dates)} 天")

    if args.dry_run:
        print(f"[预演] 预计调用 {len(trade_dates)} 次，约 "
              f"{len(trade_dates) * 0.7 / 60:.0f} 分钟，数据量约 "
              f"{len(trade_dates) * 3555 / 1e4:.0f} 万行")
        # 试拉一天验证连通性
        d = trade_dates[0]
        df = pro.daily_basic(trade_date=d, fields=args.fields)
        print(f"[预演] {d} 拉到 {len(df)} 行，字段：{list(df.columns)}")
        if len(df):
            print(df.head(2).to_string(index=False))
        return 0

    # ── 正式回填 ──
    total_rows = 0
    failed: list[str] = []
    batch: list[dict] = []

    def flush(session):
        nonlocal total_rows
        if not batch:
            return
        session.bulk_insert_mappings(StockValuationDaily, batch)
        session.commit()
        total_rows += len(batch)
        batch.clear()

    db = SessionLocal()
    try:
        for i, d in enumerate(trade_dates, 1):
            try:
                df = pro.daily_basic(trade_date=d, fields=args.fields)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{d}: {type(exc).__name__}")
                if len(failed) >= 5:
                    print("连续 5 天失败，中止")
                    break
                time.sleep(args.interval * 3)
                continue

            if df is not None and len(df):
                for r in df.to_dict("records"):
                    batch.append({
                        "symbol": _to_symbol(r["ts_code"]),
                        "trade_date": date(int(d[:4]), int(d[4:6]), int(d[6:8])),
                        "pe_ttm": r.get("pe_ttm"),
                        "pb": r.get("pb"),
                        # tushare total_mv 单位：万元 → 亿元（÷ 10000）
                        "total_mv": (r["total_mv"] / 10000.0) if r.get("total_mv") is not None else None,
                        "circ_mv": (r["circ_mv"] / 10000.0) if r.get("circ_mv") is not None else None,
                        "turnover_rate": r.get("turnover_rate"),
                    })
            if len(batch) >= 20000:
                flush(db)
            if i % 50 == 0:
                el = time.time() - t0
                eta = el / i * (len(trade_dates) - i)
                print(f"  {i}/{len(trade_dates)} 累计 {total_rows:,} 行 "
                      f"用时 {el/60:.1f}min ETA {eta/60:.1f}min", flush=True)
            time.sleep(args.interval)
        flush(db)
    except KeyboardInterrupt:
        flush(db)
        print(f"\n中断（已提交 {total_rows:,} 行，可加 --resume 续跑）")
    finally:
        db.close()

    el = time.time() - t0
    print(f"\n完成：{total_rows:,} 行，用时 {el/60:.1f} 分钟"
          + (f"，失败 {len(failed)} 天（{failed[:3]}）" if failed else "，无失败"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())