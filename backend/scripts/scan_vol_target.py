"""jk001 波动率目标约束参数扫描。

背景：平台忠实满仓 89%，回撤 -42.8%；聚宽实际半仓 51.6%，回撤 -18.9%。
原版文件头 R3 建议「给总仓位加独立的波动率约束」但从未实现 —— 本脚本扫描该
约束的参数，找出回撤可接受、收益损失最小的档位。

用法：
    python scripts/scan_vol_target.py [--start 2019-01-02] [--end 2024-12-20]
                                      [--slippage 0.0] [--quick]
"""
from __future__ import annotations

import argparse
import itertools
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.disable(logging.WARNING)

from app.database import SessionLocal, init_db          # noqa: E402
from app.core.strategies.registry import STRATEGY_REGISTRY  # noqa: E402
from app.routers.strategy import _run_portfolio          # noqa: E402
from app.schemas import BacktestRequest                  # noqa: E402

KEY = "jk001"

# 扫描网格：目标波动 / 回看窗口 / 缩放下限
GRID = {
    "vol_target": [0.0, 0.10, 0.12, 0.15, 0.18, 0.22],
    "vol_lookback": [40, 60],
    "vol_min_scale": [0.30],
}


def run_one(db, base: dict, override: dict, start: str, end: str, slippage: float):
    p = dict(base)
    p.update(override)
    meta = STRATEGY_REGISTRY[KEY]
    req = BacktestRequest(symbol=meta.get("index_symbol", "sh000300"),
                          start=start, end=end, strategy=KEY, params=p,
                          initial_cash=1_000_000, commission=0.0003,
                          slippage=slippage, adj="qfq")
    r = _run_portfolio(db, req, meta, p)
    return {
        "ret": r.total_return or 0.0,
        "mdd": r.max_drawdown or 0.0,
        "sharpe": r.sharpe or 0.0,
        "bench": r.benchmark_total_return or 0.0,
        "trades": len(r.trades) if getattr(r, "trades", None) else 0,
        "calmar": (r.total_return or 0.0) / abs(r.max_drawdown or 1.0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-02")
    ap.add_argument("--end", default="2024-12-20")
    ap.add_argument("--slippage", type=float, default=0.0,
                    help="聚宽回测无滑点 → 对比时置 0（平台默认 0.002）")
    ap.add_argument("--quick", action="store_true", help="只跑 vol_lookback=60 一组")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    meta = STRATEGY_REGISTRY[KEY]
    base = dict(meta["default_params"])

    grid = dict(GRID)
    if args.quick:
        grid["vol_lookback"] = [60]

    print(f"区间 {args.start} ~ {args.end}  滑点 {args.slippage}")
    print(f"{'vol_target':>10} {'lookback':>8} {'min':>5} | {'收益':>8} {'回撤':>8} "
          f"{'夏普':>6} {'Calmar':>7} {'基准':>8} {'交易':>6}")
    print("-" * 78)

    rows = []
    combos = list(itertools.product(grid["vol_target"], grid["vol_lookback"],
                                    grid["vol_min_scale"]))
    for vt, lb, ms in combos:
        ov = {"vol_target": vt, "vol_lookback": lb, "vol_min_scale": ms}
        try:
            m = run_one(db, base, ov, args.start, args.end, args.slippage)
        except Exception as e:  # noqa: BLE001
            print(f"{vt:>10.2f} {lb:>8} {ms:>5.2f} | ERROR {e}")
            continue
        rows.append((vt, lb, ms, m))
        print(f"{vt:>10.2f} {lb:>8} {ms:>5.2f} | {m['ret'] * 100:>7.2f}% "
              f"{m['mdd'] * 100:>7.2f}% {m['sharpe']:>6.3f} {m['calmar']:>7.2f} "
              f"{m['bench'] * 100:>7.2f}% {m['trades']:>6}")

    if not rows:
        return 1
    base_row = [r for r in rows if r[0] == 0.0]
    print("-" * 78)
    if base_row:
        b = base_row[0][3]
        print(f"基线(关闭) 收益 {b['ret'] * 100:.2f}%  回撤 {b['mdd'] * 100:.2f}%  "
              f"夏普 {b['sharpe']:.3f}  Calmar {b['calmar']:.2f}")
    print("\n相对基线的变化：")
    print(f"{'vol_target':>10} {'lookback':>8} | {'收益':>8} {'回撤':>8} {'夏普':>6} {'Calmar':>7}")
    b = base_row[0][3] if base_row else None
    for vt, lb, ms, m in rows:
        if b is None:
            continue
        print(f"{vt:>10.2f} {lb:>8} | {m['ret'] * 100 - b['ret'] * 100:>+7.2f}pp "
              f"{m['mdd'] * 100 - b['mdd'] * 100:>+7.2f}pp "
              f"{m['sharpe'] - b['sharpe']:>+6.3f} {m['calmar'] - b['calmar']:>+7.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
