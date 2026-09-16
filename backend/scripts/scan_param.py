"""jk 系列通用单参数扫描：对某个参数取多档，对比收益/回撤/夏普/Calmar。

用法：
    python scripts/scan_param.py --strategy jk001 --param neutral_position \\
        --values 0.5,0.6,0.7,0.8,0.9 --start 2019-01-02 --end 2024-12-20

    # 同时叠加其它固定覆盖（如开波动率约束）
    python scripts/scan_param.py --param vol_target --values 0.12,0.15,0.18 \\
        --set trend_bear_mode=ma
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)

from app.database import SessionLocal, init_db            # noqa: E402
from app.core.strategies.registry import STRATEGY_REGISTRY  # noqa: E402
from app.routers.strategy import _run_portfolio            # noqa: E402
from app.schemas import BacktestRequest                    # noqa: E402


def _cast(v: str):
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="jk001")
    ap.add_argument("--param", required=True)
    ap.add_argument("--values", required=True, help="逗号分隔的取值列表")
    ap.add_argument("--set", action="append", default=[],
                    help="额外的固定覆盖，格式 key=value，可重复")
    ap.add_argument("--start", default="2019-01-02")
    ap.add_argument("--end", default="2024-12-20")
    ap.add_argument("--slippage", type=float, default=0.0)
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    meta = STRATEGY_REGISTRY[args.strategy]
    base = dict(meta["default_params"])
    for kv in args.set:
        k, _, v = kv.partition("=")
        base[k] = _cast(v)

    values = [_cast(x) for x in args.values.split(",")]
    print(f"策略 {args.strategy}  区间 {args.start}~{args.end}  "
          f"滑点 {args.slippage}  扫描 {args.param}")
    print(f"{args.param:>18} | {'收益':>8} {'回撤':>8} {'夏普':>6} "
          f"{'Calmar':>7} {'基准':>8} {'超额':>8}")
    print("-" * 76)

    best = None
    for v in values:
        p = dict(base)
        p[args.param] = v
        try:
            req = BacktestRequest(symbol=meta.get("index_symbol", "sh000300"),
                                  start=args.start, end=args.end,
                                  strategy=args.strategy, params=p,
                                  initial_cash=1_000_000, commission=0.0003,
                                  slippage=args.slippage, adj="qfq")
            r = _run_portfolio(db, req, meta, p)
            calmar = (r.total_return or 0) / abs(r.max_drawdown or 1)
            exc = (r.total_return or 0) - (r.benchmark_total_return or 0)
            print(f"{str(v):>18} | {(r.total_return or 0) * 100:>7.2f}% "
                  f"{(r.max_drawdown or 0) * 100:>7.2f}% {r.sharpe or 0:>6.3f} "
                  f"{calmar:>7.2f} {(r.benchmark_total_return or 0) * 100:>7.2f}% "
                  f"{exc * 100:>+7.2f}pp", flush=True)
            if best is None or calmar > best[1]:
                best = (v, calmar)
        except Exception as e:  # noqa: BLE001
            print(f"{str(v):>18} | ERROR {e}", flush=True)

    if best:
        print("-" * 76)
        print(f"Calmar 最优档：{args.param}={best[0]}（Calmar {best[1]:.2f}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
