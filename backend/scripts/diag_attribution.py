#!/usr/bin/env python3
"""策略跑输归因：把「策略收益」拆成「选股能力」与「市场结构」两部分。

回答三个问题
------------
1. **选股是不是比随机差？** —— 用蒙特卡洛抽 N 只等权组合（一万次），
   看策略收益落在随机分布的第几百分位。落在 P10 就别再调参了，是选股本身没 alpha。
2. **池子里哪些板块在贡献/拖累？** —— 全池按板块拆中位/均值收益。
   2025-2026 年 A 股 alpha 高度集中在科创板右尾，主板中位数只有个位数。
3. **策略实际买的是什么？** —— 每次调仓的板块占比、最常入选名单。
   （曾用它证伪「北交所混入池子」的假设：实际 0 人次入选。）

用法
----
    cd backend
    PYTHONPATH=$(pwd) python scripts/diag_attribution.py --strategy jk002
    PYTHONPATH=$(pwd) python scripts/diag_attribution.py --strategy jk001 \\
        --start 2025-01-02 --end 2026-09-11 --slippage 0.002

    # 只看结构（不跑回测，秒出）
    ... --skip-backtest
"""
from __future__ import annotations

import argparse
import logging
import random
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)

from app.database import SessionLocal, init_db                      # noqa: E402
from app.core.strategies.registry import STRATEGY_REGISTRY          # noqa: E402
from app.routers.strategy import _run_portfolio                     # noqa: E402
from app.schemas import BacktestRequest                             # noqa: E402

MC_ROUNDS = 10000


def board_of(sym: str) -> str:
    if sym.startswith("bj"):
        return "北交所"
    if sym.startswith("sh688"):
        return "科创板"
    if sym.startswith("sz30"):
        return "创业板"
    if sym.startswith("sh60") or sym.startswith("sz00"):
        return "主板"
    return "其他"


def load_pool_returns(start: str, end: str, min_days: int = 200) -> dict[str, float]:
    """区间内每只标的的区间收益（首日收盘 → 末日收盘）。

    只保留区间内至少 min_days 个交易日的标的，剔除上市不足 / 长期停牌者。
    """
    from app.services.duckdb_store import DUCKDB_PATH, _connect
    con = _connect()
    if con is None:
        raise SystemExit(f"DuckDB 打不开：{DUCKDB_PATH}")
    try:
        rows = con.execute(
            f"""
            WITH b AS (
              SELECT symbol, arg_min(close, trade_date) AS c0,
                     arg_max(close, trade_date) AS c1, count(*) AS n
              FROM kline_daily
              WHERE trade_date BETWEEN DATE '{start}' AND DATE '{end}'
              GROUP BY symbol
            )
            SELECT symbol, c1 / c0 - 1 FROM b WHERE c0 > 0 AND n >= {min_days}
            """
        ).fetchall()
    finally:
        con.close()
    return {r[0]: r[1] for r in rows}


def report_structure(ret: dict[str, float], start: str, end: str, top_n: int) -> None:
    vals = list(ret.values())
    if not vals:
        print("池子为空")
        return
    dec = statistics.quantiles(vals, n=10)      # dec[0]=P10 … dec[8]=P90
    qrt = statistics.quantiles(vals, n=4)       # qrt[0]=P25 … qrt[2]=P75
    print(f"\n=== 全池个股收益分布（{start}~{end}，{len(vals)} 只）===")
    print(f"  中位数 {statistics.median(vals)*100:+.2f}%   均值 {statistics.fmean(vals)*100:+.2f}%")
    print(f"  P10 {dec[0]*100:+.2f}%   P25 {qrt[0]*100:+.2f}%   "
          f"P75 {qrt[2]*100:+.2f}%   P90 {dec[8]*100:+.2f}%")
    print(f"  正收益占比 {sum(1 for v in vals if v > 0)/len(vals)*100:.1f}%")

    # 蒙特卡洛：随机 top_n 只等权（对照「策略选股」）
    syms = list(ret)
    if len(syms) <= top_n:
        return
    random.seed(42)
    sims = sorted(statistics.fmean(ret[s] for s in random.sample(syms, top_n))
                  for _ in range(MC_ROUNDS))
    d = statistics.quantiles(sims, n=10)
    print(f"\n=== 随机 {top_n} 只等权组合（{MC_ROUNDS} 次模拟）对照 ===")
    print(f"  中位数 {statistics.median(sims)*100:+.2f}%   均值 {statistics.fmean(sims)*100:+.2f}%")
    print(f"  P10 {d[0]*100:+.2f}%   P25 {statistics.quantiles(sims, n=4)[0]*100:+.2f}%   "
          f"P75 {statistics.quantiles(sims, n=4)[2]*100:+.2f}%   P90 {d[8]*100:+.2f}%")

    grp: dict[str, list[float]] = {}
    for s, r in ret.items():
        grp.setdefault(board_of(s), []).append(r)
    print("\n=== 板块结构 ===")
    print(f"{'板块':<8}{'只数':>7}{'占比':>9}{'中位收益':>11}{'均值收益':>11}")
    for b, rs in sorted(grp.items(), key=lambda kv: -len(kv[1])):
        print(f"{b:<8}{len(rs):>7}{len(rs)/len(ret)*100:>8.1f}%"
              f"{statistics.median(rs)*100:>10.1f}%{statistics.fmean(rs)*100:>10.1f}%")
    return sims


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="jk002")
    ap.add_argument("--start", default="2025-01-02")
    ap.add_argument("--end", default="2026-09-11")
    ap.add_argument("--slippage", type=float, default=0.0)
    ap.add_argument("--overrides", action="append", default=[],
                    help="覆盖默认参数 key=value，可重复")
    ap.add_argument("--skip-backtest", action="store_true")
    args = ap.parse_args()

    key = args.strategy
    if key not in STRATEGY_REGISTRY:
        print(f"未注册策略 {key}")
        return 1
    meta = STRATEGY_REGISTRY[key]
    params = dict(meta["default_params"])
    for kv in args.overrides:
        k, _, v = kv.partition("=")
        try:
            v = int(v)
        except ValueError:
            try:
                v = float(v)
            except ValueError:
                pass
        params[k] = v

    ret = load_pool_returns(args.start, args.end)
    sims = report_structure(ret, args.start, args.end, int(params.get("stock_num", 30)))

    if args.skip_backtest:
        return 0

    # 跑策略回测，顺带记录每次调仓的实际选股
    import app.core.strategies.jk_series as jk
    rec: list[tuple[str, list[str]]] = []
    orig = jk.JKFactorStrategy._select
    if hasattr(jk.JKFactorStrategy, "_select"):
        def patched(self, ctx, filtered, fdata, date):     # noqa: ANN001
            out = orig(self, ctx, filtered, fdata, date)
            rec.append((date, list(out)))
            return out
        jk.JKFactorStrategy._select = patched

    init_db()
    db = SessionLocal()
    req = BacktestRequest(symbol=meta.get("index_symbol", "sh000300"),
                          start=args.start, end=args.end, strategy=key, params=params,
                          initial_cash=1_000_000, commission=0.0003,
                          slippage=args.slippage, adj="qfq")
    r = _run_portfolio(db, req, meta, params)

    print(f"\n=== {key} 回测（滑点 {args.slippage:.2%}）===")
    print(f"  收益 {r.total_return*100:+.2f}%  回撤 {r.max_drawdown*100:.2f}%  "
          f"夏普 {r.sharpe:.3f}  基准 {r.benchmark_total_return*100:+.2f}%  "
          f"超额 {(r.total_return - r.benchmark_total_return)*100:+.2f}pp  成交 {r.trade_count}")
    if sims:
        pct = sum(1 for x in sims if x < r.total_return) / len(sims) * 100
        print(f"  该收益落在随机 {int(params.get('stock_num', 30))} 只组合分布的第 {pct:.0f} 百分位"
              f"{'  ← 低于 P25，选股没跑赢随机' if pct < 25 else ''}")

    if rec:
        bc: Counter = Counter()
        freq: Counter = Counter()
        for _d, picks in rec:
            for s in picks:
                bc[board_of(s)] += 1
                freq[s] += 1
        tot = sum(bc.values()) or 1
        print(f"\n=== 调仓选股画像（{len(rec)} 次调仓）===")
        for b, c in bc.most_common():
            print(f"  {b:<6} {c:>5} 人次  {c/tot*100:>5.1f}%")
        print("  最常入选：" + "、".join(f"{s}({c}次)" for s, c in freq.most_common(10)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
