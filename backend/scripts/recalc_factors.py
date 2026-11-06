#!/usr/bin/env python
"""用新口径重算存量因子（B2，2026-10-08）。

背景：
  B2 把 DEFAULT_STEP 从 20（月频）改为 5（周频），IC 截面数提升约 4 倍；
  同时 B1 让估值因子走 PIT 时序（`_attrs_asof`）。库里的 37 条记录是
  **旧口径**（月频 + 最新快照估值）算出来的，其 IC / 评级不可直接沿用。

策略：**不覆盖旧记录，追加新记录**。原因：
  · `factor_mine_results` 是 append-only 的挖掘流水，保留历史才可追溯
  · 每条 result_json 里已存n_periods / forward_days，可区分口径
  · 加 `recalc_20261008` 标记便于筛选

用法：
    # 预演（只算不写库，看耗时与结果变化）
    python scripts/recalc_factors.py --dry-run

    # 正式重算（默认 400 天区间，与原挖掘同口径）
    python scripts/recalc_factors.py

    # 指定区间（截面更多，耗时更长）
    python scripts/recalc_factors.py --start 2021-01-01 --end 2026-09-30
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

RECALC_TAG = "recalc_20261008"   # B2 新口径标记


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="", help="起始日 ISO（空=与原挖掘同默认 400 天）")
    ap.add_argument("--end", default="", help="结束日 ISO（空=今日）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写库")
    ap.add_argument("--limit", type=int, default=0, help="最多重算 N 个表达式（0=全部）")
    ap.add_argument("--save", action="store_true",
                    help="写入库（默认只报告，不落库——避免误污染挖掘流水）")
    args = ap.parse_args()

    from app.database import SessionLocal
    from app.models import FactorMineResult
    from app.services.factor_mining import (
        DEFAULT_FORWARD, DEFAULT_STEP, mine_factor, resolve_range,
    )

    # ── 收集存量表达式（去重）──
    db = SessionLocal()
    try:
        seen: dict[str, str] = {}
        for name, expr in db.query(
            FactorMineResult.name, FactorMineResult.expr
        ).all():
            if expr and expr not in seen:
                seen[expr] = name
        #已带新标记的跳过（幂等：重跑不重复追加）
        already = set()
        for (rj,) in db.query(FactorMineResult.result_json).all():
            if rj and isinstance(rj, str) and RECALC_TAG in rj:
                d = json.loads(rj)
                if d.get("expr") in seen:
                    already.add(d["expr"])
        todo = [(e, n) for e, n in seen.items() if e not in already]
        if args.limit:
            todo = todo[: args.limit]
        print(f"存量表达式 {len(seen)} 个，已是新口径 {len(already)} 个，待重算 {len(todo)} 个")

        if not todo:
            print("无需重算")
            return 0

        # ── 逐个重算 ──
        rows = []
        t_all = time.time()
        for i, (expr, old_name) in enumerate(todo, 1):
            t0 = time.time()
            r = mine_factor(db, expr, name=f"{old_name}", start=args.start, end=args.end)
            el = time.time() - t0
            if not r.get("ok"):
                print(f"[{i}/{len(todo)}] {old_name[:20]:22s} 失败: {r.get('error')} ({el:.0f}s)")
                continue

            # 取旧记录做对照
            old = db.query(
                FactorMineResult.ic_mean, FactorMineResult.icir, FactorMineResult.result_json
            ).filter(FactorMineResult.expr == expr).order_by(
                FactorMineResult.id.desc()).first()
            old_ic = old[0] if old else None
            old_np = None
            if old and isinstance(old[2], str):
                try:
                    old_np = json.loads(old[2]).get("n_periods")
                except Exception:
                    pass

            r["recalc_tag"] = RECALC_TAG
            r["recalc_note"] = (
                f"B2 周频(step={DEFAULT_STEP},forward={DEFAULT_FORWARD}) + "
                f"B1 估值PIT；对照旧口径 n_periods={old_np}"
            )
            r["old_ic_mean"] = old_ic
            rows.append((expr, r))
            flag = "" if old_ic is None else f" (旧 ic={old_ic:+.4f})"
            print(f"[{i}/{len(todo)}] {old_name[:20]:22s} "
                  f"ic={r['ic_mean']:+.4f} t={r['t_stat']:+.2f} "
                  f"n={r['n_periods']:3d} {r['rating']}{flag} ({el:.0f}s)")

            if args.save and not args.dry_run:
                db.add(FactorMineResult(
                    name=r["name"], expr=expr, rating=r["rating"],
                    ic_mean=r["ic_mean"], icir=r["icir"],
                    result_json=json.dumps(r, ensure_ascii=False),
                    created_at=__import__("datetime").datetime.now(),
                ))
                db.commit()

        el_all = time.time() - t_all
        print(f"\n完成：成功 {len(rows)}/{len(todo)}，用时 {el_all/60:.1f} 分钟")
        if not args.save:
            print("（未落库，加 --save 才会写入 factor_mine_results）")

        # ── 汇总对比 ──
        if rows:
            print(f"\n=== 新旧口径对比（前 10）===")
            print(f"{'因子':22s} {'旧IC':>9s} {'新IC':>9s} {'变化':>9s} {'旧n':>5s} {'新n':>5s}")
            for expr, r in rows[:10]:
                oi = r.get("old_ic_mean")
                ni = r["ic_mean"]
                d = f"{(ni-oi)/abs(oi)*100:+.0f}%" if oi else "-"
                onp = (r.get("recalc_note", "").split("n_periods=")[-1]
                       if "n_periods=" in r.get("recalc_note", "") else "?")
                print(f"{r['name'][:20]:22s} {oi if oi is not None else 0:+9.4f} "
                      f"{ni:+9.4f} {d:>9s} {onp:>5s} {r['n_periods']:5d}")
            pos_old = sum(1 for e, r in rows if (r.get("old_ic_mean") or 0) > 0)
            pos_new = sum(1 for e, r in rows if r["ic_mean"] > 0)
            print(f"\n正 IC 占比：旧口径 {pos_old}/{len(rows)} → 新口径 {pos_new}/{len(rows)}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())