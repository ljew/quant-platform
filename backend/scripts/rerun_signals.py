#!/usr/bin/env python
"""重跑历史信号（M1-1 复权口径修复后）—— 独立脚本，不依赖后端进程。

用途：修复 runner.py 的复权口径 bug 后，把 signal_daily / signal_actions
按新口径整段重算，可选重算镜像组合 portfolio_state。

为什么不走 API：backfill 是后端后台线程，进程被 launchd 重启会中断
（2026-10-08 首次尝试只跑 1 天即中断）。本脚本前台执行、可中断续跑。

用法：
    python scripts/rerun_signals.py --days 250 --dry-run   # 预演不写库
    python scripts/rerun_signals.py --days 250             # 实际重跑
    python scripts/rerun_signals.py --days 250 --resume    # 断点续跑
    python scripts/rerun_signals.py --days 1 --with-portfolio
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import select                                  # noqa: E402

from app.database import SessionLocal                          # noqa: E402
from app.models import ResearchRun, SignalDaily               # noqa: E402
from app.services.xq.data import load_panel                    # noqa: E402
from app.services.xq.runner import panel_asof, run_from_panel  # noqa: E402

DUCKDB = BACKEND.parent / "data" / "quant.duckdb"
# 面板回看保留天数：与 panel_asof 的 keep_days 配套，260 足够所有策略预热
KEEP_DAYS = 260


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=250, help="重跑最近 N 个交易日")
    ap.add_argument("--lookback", type=int, default=560,
                    help="面板载入回看窗口（需覆盖最长预热：RPS 120日 / 缠论 120根）")
    ap.add_argument("--dry-run", action="store_true", help="只跑不写库")
    ap.add_argument("--with-portfolio", action="store_true", help="重算镜像组合")
    ap.add_argument("--resume", action="store_true", help="跳过已有信号的日期")
    args = ap.parse_args()

    t0 = time.time()
    print(f"载入面板（lookback={args.lookback}）…", flush=True)
    panel = load_panel(DUCKDB, lookback=args.lookback)
    if panel.df.empty:
        print("面板为空：K 线数据缺失", file=sys.stderr)
        return 1
    latest = panel.as_of or panel.latest
    print(f"面板 {len(panel.df):,} 行 · {panel.df['symbol'].nunique():,} 只 · 截至 {latest}",
          flush=True)

    trade_dates = sorted(d for d in {
        (x if isinstance(x, date) else x.date()) for x in panel.df["date"]
    } if d <= latest)[-args.days:]

    if args.resume:
        db0 = SessionLocal()
        try:
            have = {r[0] for r in db0.execute(
                select(SignalDaily.date).distinct()).all()}
        finally:
            db0.close()
        n0 = len(trade_dates)
        trade_dates = [d for d in trade_dates if d not in have]
        print(f"断点续跑：跳过 {n0 - len(trade_dates)} 天已处理", flush=True)

    print(f"待处理 {len(trade_dates)} 个交易日："
          f"{trade_dates[0]} ~ {trade_dates[-1]}", flush=True)

    ok = fail = 0
    for i, d in enumerate(trade_dates, 1):
        view = panel_asof(panel, d, keep_days=KEEP_DAYS)
        db = SessionLocal()
        try:
            if args.dry_run:
                res = run_from_panel(db, view, run_id=None)
                db.rollback()          # 预演：不落库
                print(f"[{i}/{len(trade_dates)}] {d} DRY-RUN "
                      f"actions={res.get('actions')}", flush=True)
            else:
                run = ResearchRun(trigger="rerun_script", status="RUNNING",
                                  data_date=d, gate_status="none")
                db.add(run)
                db.commit()
                db.refresh(run)
                res = run_from_panel(db, view, run_id=run.id)
                run.status = "SUCCESS"
                run.finished_at = datetime.now()
                db.commit()
                print(f"[{i}/{len(trade_dates)}] {d} "
                      f"picks={res.get('picks')} actions={res.get('actions')}",
                      flush=True)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            fail += 1
            db.rollback()
            print(f"[{i}/{len(trade_dates)}] {d} 失败: {exc}", flush=True)
            if fail >= 3:
                print("连续 3 天失败，中止", file=sys.stderr)
                break
        finally:
            db.close()

        if i % 10 == 0:
            print(f"  … {i}/{len(trade_dates)}，用时 {time.time()-t0:.0f}s", flush=True)

    print(f"\n完成：成功 {ok} 天，失败 {fail} 天，用时 {time.time()-t0:.0f}s")

    if args.with_portfolio and not args.dry_run and fail == 0:
        print("\n重算镜像组合…", flush=True)
        from app.services.xq.portfolio import run_portfolio
        db = SessionLocal()
        try:
            out = run_portfolio(db, str(DUCKDB))
            print("组合结果:", {k: v for k, v in out.items()
                                if k in ("ok", "error", "final_equity",
                                         "total_return", "n_trades")})
        finally:
            db.close()

    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
