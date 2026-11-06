#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""选股一致性验证：QMT 内核 vs 平台 signal_daily。

这是三层验证的第一层，也是唯一能证明「移植没走样」的一步。
不通过就不许上实盘——否则实盘跑的是另一个策略，而你以为在跑回测那个。

用法（quant-platform 根目录）：
    python qmt/verify_parity.py                 # 默认最近 10 个交易日、全部股票
    python qmt/verify_parity.py --days 15
    python qmt/verify_parity.py --limit 300     # 只验前 300 只，快速冒烟

判据：逐日逐策略的命中名单**完全一致**。
不一致时打印差集，并按「平台有终端无 / 终端有平台无」分类——
前者通常是数据窗口或复权差异，后者通常是内核逻辑 bug。
"""
from __future__ import print_function

import argparse
import os
import sqlite3
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import core  # noqa: E402
import chanlun_py36 as chan  # noqa: E402

DB = os.path.join(ROOT, "data", "quant_dev.db")
LOOKBACK = 260          # 与平台 load_panel 的默认窗口一致


def load_bars(con, symbols, end_date, lookback=LOOKBACK):
    """取每只股票 end_date 之前 lookback 根日线，算后复权价。

    复权口径：c_hfq = close × adj_factor，且必须用 **inner join** ——
    平台 data.py 用的就是 join，adj_factor 缺失的那一行会被整行丢弃。
    如果改成 left join + coalesce(1.0)，会多出一行历史，缠论分解随之改变。
    实测 sh600439 正因这 1 行差异多出买点。
    """
    # 窗口必须按「交易日」定义，不能按「行数」。
    # 停牌股在 260 个交易日内可能只有 259 行；若按行数往前补齐，窗口起点会早一天，
    # 而缠论是路径依赖算法（合并→分型→笔→线段→中枢），起点差一天结果就全变。
    # 实测正是这一处让 sh600439 多出一笔买点。
    window_start = con.execute(
        "select min(trade_date) from (select distinct trade_date from kline_daily "
        "where trade_date <= ? order by trade_date desc limit ?)",
        (end_date, lookback)).fetchone()[0]

    q = """
        select k.symbol, k.trade_date, k.open, k.high, k.low, k.close, k.volume,
               a.adj_factor
        from kline_daily k
        join adj_factor_daily a
          on a.symbol = k.symbol and a.trade_date = k.trade_date
        where k.trade_date between ? and ? and k.symbol in (%s)
        order by k.symbol, k.trade_date
    """ % ",".join("?" * len(symbols))
    rows = con.execute(q, [window_start, end_date] + list(symbols)).fetchall()

    raw = defaultdict(list)
    for sym, d, o, h, l, c, v, adj in rows:
        raw[sym].append((d, o, h, l, c, v, adj))

    out = {}
    for sym, rs in raw.items():
        if len(rs) < 120:            # 缠论需要 120 根
            continue
        bars = []
        for d, o, h, l, c, v, adj in rs:
            bars.append({
                "date": str(d).replace("-", ""),
                "open": float(o) * adj,
                "high": float(h) * adj,
                "low": float(l) * adj,
                "close": float(c) * adj,
                "volume": float(v),
                "raw_close": float(c),      # 不复权，下单用
            })
        out[sym] = bars
    return out


def platform_hits(con, dates, universe):
    """平台当日各策略命中名单：{(date, strategy): set(symbol)}。

    必须把平台结果**裁到本次验证的股票范围**内 —— 平台 signal_daily 是全市场
    5209 只的扫描结果，而 --limit 只取了一个子集。不裁就会把「范围差」误判成
    「算法差」（实测 725 vs 53，比例恰好等于 5209 vs 400）。
    """
    q = ("select date, strategy, symbol from signal_daily "
         "where date in (%s)" % ",".join("?" * len(dates)))
    out = defaultdict(set)
    uni = set(universe)
    for d, st, sym in con.execute(q, list(dates)):
        if sym in uni:
            out[(str(d), st)].add(sym)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0, help="只验前 N 只（0=全部）")
    ap.add_argument("--end", type=str, default=None)
    ap.add_argument("--include-latest", action="store_true",
                    help="把最新交易日也纳入验证。默认排除：当日 K 线可能未定稿"
                         "（实测出现过 08:00 就写入当日行的现象），两边行数不一致会误判")
    ap.add_argument("--baseline", choices=["db", "func"], default="func",
                    help="db=对比 signal_daily（会发现基准库陈旧）；"
                         "func=平台函数实时重算（判定算法是否等价，默认）")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    end = args.end
    if end is None:
        all_dates = [r[0] for r in con.execute(
            "select distinct trade_date from kline_daily order by trade_date desc limit 2")]
        end = all_dates[0] if args.include_latest else all_dates[1]
    dates = [r[0] for r in con.execute(
        "select distinct trade_date from kline_daily where trade_date <= ? "
        "order by trade_date desc limit ?", (end, args.days))]
    dates = sorted(str(d) for d in dates)
    print("验证区间: %s ~ %s（%d 个交易日）" % (dates[0], dates[-1], len(dates)))

    symbols = [r[0] for r in con.execute(
        "select distinct symbol from kline_daily where trade_date = ? order by symbol",
        (end,))]
    if args.limit:
        symbols = symbols[:args.limit]
    print("股票范围: %d 只\n" % len(symbols))

    # ── 基准：平台函数实时重算（推荐）或库里历史信号（查陈旧）──
    plat = None
    if args.baseline == "func":
        sys.path.insert(0, os.path.join(ROOT, "backend"))
        from datetime import date as _date
        from app.config import DATA_DIR as _DD
        from app.services.xq.data import load_panel as _lp
        from app.services.xq.runner import panel_asof as _pa
        from app.services.xq import strategies as _st
        print("载入平台面板（一次性）…")
        _panel = _lp(str(_DD / "quant.duckdb"), lookback=560)
        _fns = {"chan_buy": _st.chan_buy, "high_tight_flag": _st.high_tight_flag}

        def _baseline_hits(d):
            v = _pa(_panel, _date(int(d[:4]), int(d[5:7]), int(d[8:10])), keep_days=LOOKBACK)
            return {st: (set(_fns[st](v)[0]) & set(symbols)) for st in enabled}

    hits = platform_hits(con, dates, symbols)
    # 平台信号最后一天之后没有可比数据（signal_daily 由 15:35 流水线写入）
    sig_last = con.execute("select max(date) from signal_daily").fetchone()[0]
    sig_last = str(sig_last) if sig_last else ""
    enabled = ["chan_buy", "high_tight_flag"]
    total = {"chan_buy": [0, 0], "high_tight_flag": [0, 0]}   # [一致天, 总天]

    for di, date in enumerate(dates):
        bars_map = load_bars(con, symbols, date)
        today = date.replace("-", "")
        # 内核只判定「最新一根」是否为命中，所以把 bars_map 截到当日即可
        got = {s: [] for s in enabled}
        for sym, bars in bars_map.items():
            if bars[-1]["date"] != today:
                continue        # 当日停牌/无数据
            if core.chan_buy(bars, chan.analyze_chanlun, chan.Candle):
                got["chan_buy"].append(sym)
            if core.high_tight_flag(bars):
                got["high_tight_flag"].append(sym)

        if args.baseline == "db" and date > sig_last:
            print("[%d/%d] %s 平台无信号数据（signal_daily 截至 %s），跳过"
                  % (di + 1, len(dates), date, sig_last))
            continue

        base = _baseline_hits(date) if args.baseline == "func" else None
        for st in enabled:
            exp = base[st] if base is not None else hits.get((date, st), set())
            g = set(got[st])
            same = (exp == g)
            total[st][1] += 1
            if same:
                total[st][0] += 1
            else:
                miss = sorted(exp - g)[:5]
                extra = sorted(g - exp)[:5]
                print("[%d/%d] %s %-16s 平台 %3d / 内核 %3d  ✗"
                      % (di + 1, len(dates), date, st, len(exp), len(g)))
                if miss:
                    print("        平台有终端无: %s%s"
                          % (miss, " …" if len(exp - g) > 5 else ""))
                if extra:
                    print("        终端有平台无: %s%s"
                          % (extra, " …" if len(g - exp) > 5 else ""))
        if di % 5 == 4:
            print("  … 已处理 %d/%d 天" % (di + 1, len(dates)))

    print("\n" + "=" * 60)
    ok_all = True
    for st in enabled:
        ok, n = total[st]
        flag = "✅" if ok == n else "❌"
        if ok != n:
            ok_all = False
        print("%s %-16s 一致 %d/%d 天" % (flag, st, ok, n))
    print("=" * 60)
    print("结论:", "通过 —— 移植选股与平台完全一致" if ok_all
          else "不通过 —— 差异原因见上方（先查数据窗口/复权，再查内核逻辑）")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
