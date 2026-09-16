"""移动止损诊断：触发分布 + 止损后是否卖错。

回答两个问题：
  1. 166 次移动止损分布在哪些年份？平均持有多久？自峰值回撤多少？
  2. 止损后该股后续 20/60 日怎么走？如果是反弹，说明阈值太紧被洗出。

用法：
  python scripts/diag_trailing.py [--strategy jk001] [--start 2019-01-02] [--end 2026-09-11]
"""
from __future__ import annotations

import argparse
import collections
import logging
import statistics
import sys

logging.disable(logging.CRITICAL)

from app.database import SessionLocal, init_db          # noqa: E402
from app.core.strategies.registry import STRATEGY_REGISTRY  # noqa: E402
from app.routers.strategy import _run_portfolio          # noqa: E402
from app.schemas import BacktestRequest                  # noqa: E402


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
    p = dict(meta["default_params"])

    # —— 在触发点记录明细（date, symbol, 峰值回撤, 持有天数, 现价, 成本）
    trigger: list[tuple] = []

    cls = meta["cls"]
    orig = cls._check_stop_loss

    def patched(self, ctx, date):
        for sym in list(ctx.positions().keys()):
            try:
                px = ctx.price(sym)
                if not px:
                    continue
                cost = ctx.cost(sym)
                if cost <= 0:
                    continue
                hd = self._hold_days.get(sym, 0)
                trail = (self.ladder_trailing_pct
                         if (self.enable_ladder_stop and hd >= self.ladder_hold_days)
                         else self.trailing_stop_pct)
                peak = self._peak.get(sym, cost)
                if px / cost - 1 <= -self.stop_loss_pct:
                    trigger.append((date, sym, "固定", hd, 0.0, px, cost))
                elif peak > cost and px / peak - 1 <= -trail:
                    trigger.append((date, sym, "移动", hd, px / peak - 1, px, cost))
            except Exception:  # noqa: BLE001
                pass
        return orig(self, ctx, date)

    cls._check_stop_loss = patched
    req = BacktestRequest(symbol=meta.get("index_symbol", "sh000300"),
                          start=args.start, end=args.end, strategy=args.strategy,
                          params=p, initial_cash=1_000_000, commission=0.0003,
                          slippage=args.slippage, adj="qfq")
    r = _run_portfolio(db, req, meta, p)
    cls._check_stop_loss = orig

    print(f"策略 {args.strategy}  {args.start} ~ {args.end}  滑点 {args.slippage*100:.1f}%")
    print(f"收益 {r.total_return*100:+.2f}%  回撤 {r.max_drawdown*100:.2f}%  成交 {len(r.trades)} 笔")
    print(f"止损触发合计 {len(trigger)} 次（固定 {sum(1 for t in trigger if t[2]=='固定')} / "
          f"移动 {sum(1 for t in trigger if t[2]=='移动')}）")

    if not trigger:
        return 0

    # —— 分布 1：按年
    print("\n① 触发次数按年:")
    byyear = collections.Counter(t[0][:4] for t in trigger)
    for y in sorted(byyear):
        print(f"   {y}: {byyear[y]:>4}")

    # —— 分布 2：持有天数 / 自峰值回撤
    hds = [t[3] for t in trigger]
    dds = [t[4] for t in trigger if t[2] == "移动"]
    print(f"\n② 触发时持有天数: 中位 {statistics.median(hds):.0f} 日, "
          f"均值 {statistics.mean(hds):.1f}, 最小 {min(hds)}, 最大 {max(hds)}")
    if dds:
        print(f"   移动止损触发时自峰值回撤: 中位 {statistics.median(dds)*100:.2f}%, "
              f"最深 {min(dds)*100:.2f}%")
    short = sum(1 for t in trigger if t[3] < 10)
    print(f"   其中持有 <10 日（走前段 {p.get('trailing_stop_pct')} 阈值）: {short} 次 "
          f"({short/len(trigger)*100:.0f}%)")

    # —— 分布 3：止损后是否卖错（查后续 20/60 日价格）
    from app.services.duckdb_store import get_stock_bars_batch  # noqa: E402
    syms = sorted({t[1] for t in trigger})
    bars = get_stock_bars_batch(syms, "qfq", args.start, args.end)
    series: dict[str, list[tuple[str, float]]] = {
        s: [(b["date"], float(b["close"])) for b in v if b.get("close")]
        for s, v in (bars or {}).items()
    }

    def _norm(d: str) -> str:
        return d.replace("-", "")

    print("\n③ 止损后该股走势（衡量是否卖错）:")
    for horizon in (20, 60):
        rets = []
        for date, sym, kind, hd, dd, px, cost in trigger:
            seq = series.get(sym) or []
            key = _norm(date)
            idx = None
            for i, (d, _) in enumerate(seq):
                if _norm(d) > key:
                    idx = i
                    break
            if idx is None or idx + horizon >= len(seq):
                continue
            future = seq[idx + horizon][1]
            rets.append(future / px - 1)
        if rets:
            win = sum(1 for x in rets if x > 0)
            print(f"   后 {horizon:>2} 日: 样本 {len(rets):>4}, 平均 {statistics.mean(rets)*100:+6.2f}%, "
                  f"中位 {statistics.median(rets)*100:+6.2f}%, 上涨占比 {win/len(rets)*100:.0f}%")

    # —— 分布 4：止损后是否被重新买回（同标的再次买入）
    reentries = 0
    for date, sym, kind, *_ in trigger:
        for t in (r.trades or []):
            if t.symbol == sym and t.date > date and t.side in ("buy", "BUY"):
                reentries += 1
                break
    print(f"\n④ 止损后又被重新买回的标的数: {reentries} / {len(trigger)} "
          f"({reentries/len(trigger)*100:.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
