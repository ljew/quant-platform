#!/usr/bin/env python3
"""补历史个股日K + 复权因子（tushare 按交易日拉全市场）。

为什么需要
----------
平台 kline_daily 只覆盖到最近若干年（例如 2019-01-02 起），而策略回测要拿
260+ 个交易日做因子预热。数据起点不够 → 回测要么开头被迫空仓，要么只能从更晚的年份起跑，
无法与聚宽等外部平台的完整区间对齐。

为什么按「交易日」而不是按「股票」拉
------------------------------------
tushare 对 daily / adj_factor 都提供按 trade_date 的全市场批量接口，
**1 次调用 = 全市场 3000~5000 行**；反过来逐股调用会撞逐股限速（历史上有过静默断供的事故）。
所以这里对所有标的做 `date → 全市场行情` 的循环，调用次数 = 交易日数，与股票数无关。

用法（backend/ 下）
------------------
    PYTHONPATH=$(pwd) python scripts/backfill_history_kline.py                 # 补到默认起点 2017-06-01
    PYTHONPATH=$(pwd) python scripts/backfill_history_kline.py --start 2016-01-01
    PYTHONPATH=$(pwd) python scripts/backfill_history_kline.py --start 2017-06-01 --end 2018-12-31 --sleep 0.35
    PYTHONPATH=$(pwd) python scripts/backfill_history_kline.py --dry-run        # 只看计划，不落库

特性
----
- 幂等：已存在的 (symbol, trade_date) 直接跳过，中断后可原样重跑
- 只补平台已覆盖的标的（避免把大量早已退市的老票引进来污染全市场口径）
- 进度按批次打印；结束后自动同步 DuckDB
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime

sys.path.insert(0, ".")  # backend 目录

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.sqlite import insert as sqlite_insert  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import AdjFactorDaily, IndexKlineDaily, KlineDaily  # noqa: E402

DEFAULT_START = "2017-06-01"      # 默认补到足以支撑 2019 起的全区间回测
BENCH_INDEX = "sh000300"          # 用指数交易日做交易日历（本地全历史，最可靠）


def _trading_days(db, sd: str, ed: str) -> list[str]:
    """指数交易日当作交易日历。"""
    rows = db.execute(
        select(IndexKlineDaily.trade_date)
        .where(IndexKlineDaily.symbol == BENCH_INDEX)
        .where(IndexKlineDaily.trade_date >= sd)
        .where(IndexKlineDaily.trade_date <= ed)
        .order_by(IndexKlineDaily.trade_date)
    ).scalars().all()
    # trade_date 在 SQLite 里可能读出 date 对象，统一转 "YYYY-MM-DD" 字符串
    return [str(r)[:10] for r in rows]


def _known_symbols(db) -> set[str]:
    """平台已覆盖的标的集合（不引入新票）。"""
    rows = db.execute(select(KlineDaily.symbol).distinct()).scalars().all()
    return set(rows)


def _existing_adj(db, days: list[str]) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for i in range(0, len(days), 60):
        chunk = days[i:i + 60]
        for s, d in db.execute(
            select(AdjFactorDaily.symbol, AdjFactorDaily.trade_date)
            .where(AdjFactorDaily.trade_date.in_(chunk))
        ).all():
            out.add((s, str(d)[:10]))
    return out


def _plat_symbol(ts_code: str) -> str:
    """tushare 000001.SZ → 平台 sh600519/sz000001 格式。"""
    c = str(ts_code)
    if "." not in c:
        return c
    code, mkt = c.split(".", 1)
    return ("sh" if mkt.upper().startswith("SH") else "sz") + code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default="2018-12-31")
    ap.add_argument("--sleep", type=float, default=0.35, help="每次接口调用后的间隔秒数")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-duckdb", action="store_true")
    ap.add_argument("--limit-days", type=int, default=0, help="只跑前 N 个交易日（调试用）")
    args = ap.parse_args()

    from app.services import etl  # 复用现成的 tushare 封装与限流经验

    init_db()
    db = SessionLocal()
    days = _trading_days(db, args.start, args.end)
    if args.limit_days:
        days = days[:args.limit_days]
    if not days:
        print(f"[x] 交易日历为空（{args.start}~{args.end}）")
        return 1

    known = _known_symbols(db)
    print(f"[i] 交易日 {len(days)} 天（{days[0]} ~ {days[-1]}），平台已覆盖标的 {len(known)} 只")
    print(f"[i] 预计接口调用 {len(days) * 2} 次，休眠 {args.sleep}s/次 "
          f"≈ 最少 {len(days) * 2 * args.sleep / 60:.1f} 分钟")
    if args.dry_run:
        return 0

    have_adj = _existing_adj(db, days)
    wrote_k, wrote_a = 0, 0
    t0 = time.time()

    for n, dstr in enumerate(days, 1):
        dy = date.fromisoformat(dstr)
        try:
            df = etl._fetch_daily_batch(dy)
        except Exception as e:  # noqa: BLE001
            print(f"  [err] daily {dstr}: {str(e)[:80]}")
            time.sleep(2.0)
            continue
        time.sleep(args.sleep)
        try:
            adf = etl._fetch_adj_batch(dy)
        except Exception as e:  # noqa: BLE001
            print(f"  [err] adj {dstr}: {str(e)[:80]}")
            adf = None
        time.sleep(args.sleep)

        # —— 复权因子：全市场一次性写，历史因子不再变化 ——
        if adf is not None and len(adf):
            rows = []
            for _, r in adf.iterrows():
                sym = _plat_symbol(str(r["ts_code"]))
                if sym not in known:
                    continue
                if (sym, dstr) in have_adj:
                    continue
                try:
                    # trade_date 列是 Date 类型，必须给 date 对象而不是字符串
                    rows.append({"symbol": sym, "trade_date": dy,
                                 "adj_factor": float(r["adj_factor"])})
                except (TypeError, ValueError):
                    continue
            for i in range(0, len(rows), 2000):
                stmt = sqlite_insert(AdjFactorDaily).values(rows[i:i + 2000])
                stmt = stmt.on_conflict_do_update(
                    index_elements=["symbol", "trade_date"],
                    set_={"adj_factor": stmt.excluded.adj_factor},
                )
                db.execute(stmt)
            db.commit()
            wrote_a += len(rows)

        # —— 日K：只写未复权原价，复权由 adj_factor 读取时折算 ——
        if df is not None and len(df):
            bars: dict[str, list[dict]] = {}
            for _, r in df.iterrows():
                sym = _plat_symbol(str(r["ts_code"]))
                if sym not in known:
                    continue
                try:
                    o, h = float(r["open"]), float(r["high"])
                    lo, c = float(r["low"]), float(r["close"])
                except (TypeError, ValueError):
                    continue
                if not c or c <= 0:
                    continue
                bars.setdefault(sym, []).append({
                    "trade_date": dstr,
                    "open": round(o, 3), "high": round(h, 3),
                    "low": round(lo, 3), "close": round(c, 3),
                    "volume": int(float(r["vol"] or 0)),
                    "amount": float(r["amount"] or 0.0),
                })
            for sym, rows in bars.items():
                try:
                    from app.services import ingestion
                    # upsert_kline 在某些幂等分支返回 None，这里兜底成 0
                    wrote_k += ingestion.upsert_kline(db, rows, sym, "none") or 0
                except Exception as e:  # noqa: BLE001
                    print(f"  [err] upsert {sym} {dstr}: {str(e)[:70]}")
            db.commit()

        if n % 20 == 0 or n == len(days):
            el = time.time() - t0
            print(f"  [{n}/{len(days)}] {dstr}  日K+{wrote_k}  因子+{wrote_a}  "
                  f"已用 {el/60:.1f}min  预计剩余 {el/n*(len(days)-n)/60:.1f}min")

    print(f"[✓] 回填完成：日K {wrote_k} 行，复权因子 {wrote_a} 行")

    if not args.no_duckdb:
        print("[i] 同步 DuckDB 分析库（893 万行量级，需要几分钟）…")
        t = time.time()
        from app.services import duckdb_sync
        duckdb_sync.sync_after_seed()
        print(f"[✓] DuckDB 同步完成，用时 {(time.time()-t)/60:.1f}min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
