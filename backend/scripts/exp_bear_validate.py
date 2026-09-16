"""jk001 空头判据改进：样本内 / 样本外 / 全段 三段验证。

组合：
  origin+0  原版：score<=-2 才 bearish，且要等调仓日才清仓
  ma+0      补 MA 空头排列判据，但仍等调仓日清仓
  ma+1      补判据 + 看空当日即时清仓（推荐）
"""
from __future__ import annotations

import itertools
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)

from app.database import SessionLocal, init_db            # noqa: E402
from app.core.strategies.registry import STRATEGY_REGISTRY  # noqa: E402
from app.routers.strategy import _run_portfolio            # noqa: E402
from app.schemas import BacktestRequest                    # noqa: E402

COMBOS = [("origin", 0), ("ma", 0), ("ma", 1)]
SEGS = [
    ("样本内 2019-2024", "2019-01-02", "2024-12-20", 0.0),
    ("样本外 2025-2026", "2024-12-23", "2026-09-11", 0.0),
    ("全段 2019-2026", "2019-01-02", "2026-09-11", 0.0),
    ("全段(含0.2%滑点)", "2019-01-02", "2026-09-11", 0.002),
]


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="jk001", help="jk001 / jk002")
    ap.add_argument("--quick", action="store_true",
                    help="只跑 origin+0 与 ma+1 两组、只跑样本内/样本外两段（全A池很慢时用）")
    args = ap.parse_args()
    key = args.strategy

    init_db()
    db = SessionLocal()
    meta = STRATEGY_REGISTRY[key]
    base = dict(meta["default_params"])

    print(f"策略 {key}  基准 {meta.get('index_symbol')}  池 {base.get('pool_mode')}")
    print(f"{'区间':<18} {'判据':>7} {'即时':>4} | {'收益':>8} {'回撤':>8} "
          f"{'夏普':>6} {'Calmar':>7} {'基准':>8} {'超额':>8}")
    print("-" * 88)
    segs = SEGS[:2] if args.quick else SEGS
    combos = [("origin", 0), ("ma", 1)] if args.quick else COMBOS
    for seg_name, start, end, slip in segs:
        for mode, imm in combos:
            p = dict(base)
            p.update({"trend_bear_mode": mode, "bear_immediate": imm})
            try:
                req = BacktestRequest(symbol=meta.get("index_symbol", "sh000300"),
                                      start=start, end=end, strategy=key, params=p,
                                      initial_cash=1_000_000, commission=0.0003,
                                      slippage=slip, adj="qfq")
                r = _run_portfolio(db, req, meta, p)
                calmar = (r.total_return or 0) / abs(r.max_drawdown or 1)
                exc = (r.total_return or 0) - (r.benchmark_total_return or 0)
                print(f"{seg_name:<18} {mode:>7} {imm:>4} | "
                      f"{(r.total_return or 0) * 100:>7.2f}% "
                      f"{(r.max_drawdown or 0) * 100:>7.2f}% {r.sharpe or 0:>6.3f} "
                      f"{calmar:>7.2f} {(r.benchmark_total_return or 0) * 100:>7.2f}% "
                      f"{exc * 100:>+7.2f}pp")
            except Exception as e:  # noqa: BLE001
                print(f"{seg_name:<18} {mode:>7} {imm:>4} | ERROR {e}")
        print("-" * 88)
    return 0


if __name__ == "__main__":
    sys.exit(main())
