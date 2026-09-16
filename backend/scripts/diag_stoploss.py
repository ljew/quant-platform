"""jk001 止损诊断：搞清楚 stop_loss_pct 改了为什么结果完全不变。

扫互不影响的 0.08~0.20 五档结果完全一致，两种可能：
  A. 持仓浮亏从未达到阈值 → 止损是冗余逻辑（策略特性，不是 bug）
  B. ctx.cost()/ctx.price() 取不到值导致 continue → 死代码（真 bug）

本脚本统计：成本价缺失率、每日组合最深浮亏分布、各阈值下的理论触发次数。
"""
from __future__ import annotations

import argparse
import logging
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)

from app.database import SessionLocal, init_db            # noqa: E402
from app.core.strategies.registry import STRATEGY_REGISTRY  # noqa: E402
from app.routers.strategy import _run_portfolio            # noqa: E402
from app.schemas import BacktestRequest                    # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="jk001")
    ap.add_argument("--start", default="2019-01-02")
    ap.add_argument("--end", default="2026-09-11")
    ap.add_argument("--slippage", type=float, default=0.002)
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    meta = STRATEGY_REGISTRY[args.strategy]
    base = dict(meta["default_params"])
    cls = meta["cls"]

    worst_all: list[float] = []      # 每日组合内最深浮亏
    worst_pos: list[float] = []      # 每日组合内最大浮盈
    no_cost = [0]
    tot = [0]
    missing_px = [0]
    pos_cnt: list[int] = []

    o_stop = cls._check_stop_loss

    def stop(self, ctx, date):
        ws, wp = 0.0, 0.0
        positions = ctx.positions()
        n = 0
        for sym in list(positions.keys()):
            tot[0] += 1
            px = ctx.price(sym)
            if not px:
                missing_px[0] += 1
                continue
            cost = ctx.cost(sym)
            if cost is None or cost <= 0:
                no_cost[0] += 1
                continue
            n += 1
            r = px / cost - 1
            ws = min(ws, r)
            wp = max(wp, r)
        if n:
            worst_all.append(ws)
            worst_pos.append(wp)
            pos_cnt.append(n)
        return o_stop(self, ctx, date)

    cls._check_stop_loss = stop
    try:
        req = BacktestRequest(symbol=meta.get("index_symbol", "sh000300"),
                              start=args.start, end=args.end,
                              strategy=args.strategy, params=base,
                              initial_cash=1_000_000, commission=0.0003,
                              slippage=args.slippage, adj="qfq")
        r = _run_portfolio(db, req, meta, base)
    finally:
        cls._check_stop_loss = o_stop

    print(f"策略 {args.strategy}  区间 {args.start}~{args.end}  滑点 {args.slippage}")
    print(f"止损累计巡检 {tot[0]} 次(CI)  无价格 {missing_px[0]}  成本价缺失/<=0 {no_cost[0]}")
    print(f"有成本价的持仓检查 {len(worst_all)} 日  日均持仓 {statistics.mean(pos_cnt):.1f} 只")

    if not worst_all:
        print("没有任何有效样本 → 止损是死代码")
        return 1

    s = sorted(worst_all)
    def q(p):
        return s[min(len(s) - 1, int(len(s) * p))]
    print("\n每日『组合内最深浮亏』分布（负值=亏损）:")
    print(f"  最差 {min(s)*100:7.2f}%   P1 {q(0.01)*100:7.2f}%   P5 {q(0.05)*100:7.2f}%   "
          f"P25 {q(0.25)*100:6.2f}%   中位 {statistics.median(s)*100:6.2f}%")
    w = sorted(worst_pos)
    print(f"每日『组合内最大浮盈』: 中位 {statistics.median(w)*100:6.2f}%   最好 {max(w)*100:7.2f}%")

    print("\n按 fixed stop_loss_pct 阈值统计『会被触发的交易日数』:")
    for th in (0.05, 0.08, 0.10, 0.12, 0.15, 0.20):
        # 阈值 <= -th 的天数（>=1 只个股触发）
        cnt = sum(1 for x in worst_all if x <= -th)
        # 权益加权粗略估算：触发只数无法精确，用天数占比
        print(f"  -{th*100:4.0f}%: {cnt:>5} 日 / {len(worst_all)} 日  ({cnt/len(worst_all)*100:5.2f}%)")

    print(f"\n该次回测结果: 收益 {(r.total_return or 0)*100:.2f}%  "
          f"回撤 {(r.max_drawdown or 0)*100:.2f}%  夏普 {r.sharpe or 0:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
