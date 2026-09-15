"""jk001 回撤归因：定位最大回撤区间，看当时策略的状态（趋势档位/仓位/持仓数）。

用来判断「回撤该从哪治」：是仓位太高？趋势信号失灵？还是选股 beta 太大？
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

KEY = "jk001"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-02")
    ap.add_argument("--end", default="2024-12-20")
    ap.add_argument("--slippage", type=float, default=0.0)
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    meta = STRATEGY_REGISTRY[KEY]
    base = dict(meta["default_params"])

    holder, rec = {}, []
    import app.routers.strategy as S
    Orig = S.PortfolioBacktestEngine

    class Spy(Orig):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            holder["e"] = self

    S.PortfolioBacktestEngine = Spy

    cls = meta["cls"]
    o_init, o_ob, o_reb = cls.init, cls.on_bar, cls.rebalance

    def init(self, ctx):
        o_init(self, ctx)
        holder["s"] = self

    def ob(self, ctx, date):
        res = o_ob(self, ctx, date)
        _snap(date)
        return res

    def reb(self, ctx, date):
        res = o_reb(self, ctx, date)
        _snap(date)
        return res

    def _snap(date):
        e, s = holder.get("e"), holder.get("s")
        if not e or not s:
            return
        eq = e._equity_today()
        mv = sum(sh * (e._last_close.get(sy) or 0)
                 for sy, sh in e.positions.items() if sh > 0)
        rec.append({
            "date": date, "equity": eq,
            "expo": mv / eq if eq else 0.0,
            "trend": s.market_trend,
            "target": s.position_ratio,
            "n": len([1 for v in s._hold_days.items()]) or 0,
            "npos": len([x for x in e.positions.values() if x > 0]),
        })

    cls.init, cls.on_bar, cls.rebalance = init, ob, reb

    req = BacktestRequest(symbol=meta.get("index_symbol", "sh000300"),
                          start=args.start, end=args.end, strategy=KEY,
                          params=base, initial_cash=1_000_000,
                          commission=0.0003, slippage=args.slippage, adj="qfq")
    r = _run_portfolio(db, req, meta, base)

    if not rec:
        print("无记录"); return 1

    # —— 最大回撤区间 ——
    peak, peak_i, worst, wi, wj = rec[0]["equity"], 0, 0.0, 0, 0
    for i, x in enumerate(rec):
        if x["equity"] > peak:
            peak, peak_i = x["equity"], i
        dd = x["equity"] / peak - 1
        if dd < worst:
            worst, wi, wj = dd, peak_i, i

    seg = rec[wi:wj + 1]
    print(f"最大回撤 {worst * 100:.2f}%  区间 {rec[wi]['date']} → {rec[wj]['date']} "
          f"（{len(seg)} 交易日）")
    print(f"  峰值净值 {rec[wi]['equity']:,.0f} → 谷值 {rec[wj]['equity']:,.0f}")
    print(f"  区间内 平均仓位 {statistics.mean(x['expo'] for x in seg) * 100:.1f}%  "
          f"平均持仓 {statistics.mean(x['npos'] for x in seg):.1f} 只")
    tr = {}
    for x in seg:
        tr[x["trend"]] = tr.get(x["trend"], 0) + 1
    print(f"  趋势档位分布 {tr}")

    # —— 全程趋势档位分布 ——
    print("\n全程趋势档位分布（交易日数 / 该档平均仓位 / 该档平均持仓）")
    bytr = {}
    for x in rec:
        bytr.setdefault(x["trend"], []).append(x)
    for k, v in sorted(bytr.items()):
        print(f"  {k:>8}: {len(v):>5} 日  仓位 {statistics.mean(y['expo'] for y in v) * 100:5.1f}%  "
              f"持仓 {statistics.mean(y['npos'] for y in v):4.1f} 只")

    # —— 年度 ——
    print("\n年度：收益 / 最大回撤 / 平均仓位")
    byy = {}
    for x in rec:
        byy.setdefault(x["date"][:4], []).append(x)
    for y in sorted(byy):
        v = byy[y]
        pk, mdd = v[0]["equity"], 0.0
        for x in v:
            pk = max(pk, x["equity"])
            mdd = min(mdd, x["equity"] / pk - 1)
        ret = v[-1]["equity"] / v[0]["equity"] - 1
        print(f"  {y}: 收益 {ret * 100:>+7.2f}%  回撤 {mdd * 100:>7.2f}%  "
              f"仓位 {statistics.mean(z['expo'] for z in v) * 100:5.1f}%")

    print(f"\n全程 收益 {(r.total_return or 0) * 100:.2f}%  回撤 {(r.max_drawdown or 0) * 100:.2f}%  "
          f"夏普 {r.sharpe or 0:.3f}  基准 {(r.benchmark_total_return or 0) * 100:.2f}%")

    # —— 回撤 >20% 的所有时段 ——
    print("\n所有回撤超过 20% 的时段：")
    peak, peak_d = rec[0]["equity"], rec[0]["date"]
    cur_start = None
    for x in rec:
        if x["equity"] > peak:
            if cur_start:
                pass
            peak, peak_d = x["equity"], x["date"]
            cur_start = None
        dd = x["equity"] / peak - 1
        if dd <= -0.20 and cur_start is None:
            cur_start = peak_d
            print(f"  {peak_d} → {x['date']}  达到 -20%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
