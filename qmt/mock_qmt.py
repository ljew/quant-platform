#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地 mock QMT 环境，跑通策略入口（无需 QMT 账号）。

它验证的是入口层，不是算法层（算法已由 verify_parity 全市场验证过）：
  · get_market_data_ex 的返回结构解析对不对
  · 持仓/资金读取、状态 KV 读写是否闭环
  · 从候选到订单的整条链路会不会崩

用法（quant-platform 根目录）：
    python qmt/mock_qmt.py                     # 默认最近一个已定稿交易日、300 只
    python qmt/mock_qmt.py --limit 0           # 全市场
    python qmt/mock_qmt.py --date 2026-10-28

⚠️ 通过 mock 只说明「代码逻辑没崩」，不说明「QMT 里能跑」。
   真实环境必须验证三件事：handlebar 是否按预期触发、passorder 参数是否被接受、
   save_py_key 是否能读写。这三条 mock 模拟不了。
"""
from __future__ import print_function

import argparse
import os
import sqlite3
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import strategy as st  # noqa: E402

DB = os.path.join(ROOT, "data", "quant_dev.db")


class MockContextInfo(object):
    """最小可用的 ContextInfo 替身。"""

    def __init__(self, con, date, symbols):
        self.con = con
        self.date = date
        self.symbols = symbols
        self.order_log = []

    def set_universe(self, _):
        pass

    def get_bar_trading_day(self):
        return self.date

    def get_stock_list_in_sector(self, _name):
        # 真实 QMT 返回 600000.SH 这类格式，mock 必须一致，否则测不出格式转换 bug
        return [s[2:] + "." + s[:2].upper() for s in self.symbols]

    def get_market_data_ex(self, fields, stock_list, period=None, count=260,
                           dividend_type="back", fill_data=True):
        out = {}
        q = ("select k.symbol, k.trade_date, k.open, k.high, k.low, k.close, k.volume,"
             "       a.adj_factor from kline_daily k join adj_factor_daily a"
             "  on a.symbol=k.symbol and a.trade_date=k.trade_date"
             " where k.trade_date <= ? and k.symbol in (%s)"
             " order by k.symbol, k.trade_date" % ",".join("?" * len(stock_list)))
        plain = [c.split(".")[0] for c in stock_list]
        pref = ["sh" + c if c[0] == "6" else "sz" + c for c in plain]
        rows = self.con.execute(q, [self.date] + pref).fetchall()
        bucket = {}
        for sym, d, o, h, l, c, v, adj in rows:
            sym = sym[2:] + "." + sym[:2].upper()      # 用 QMT 格式作 key
            bucket.setdefault(sym, []).append((d, o, h, l, c, v, adj))
        for sym, rs in bucket.items():
            rs = rs[-count:]
            if not rs:
                continue
            factor = (lambda x: x) if dividend_type == "none" else (lambda x: x)
            idx = [str(r[0]) for r in rs]
            k_all = (lambda x: 1.0) if dividend_type == "none" else (lambda x: x)
            rows_all = []
            for d, o, h, l, c, v, adj in rs:
                k = 1.0 if dividend_type == "none" else adj
                rows_all.append({"time": str(d), "open": o * k, "high": h * k,
                                 "low": l * k, "close": c * k, "volume": v})
            # 真实 QMT 只返回 fields 里请求的列，mock 必须一致，否则测不出「取错列」类 bug
            want = [f.lower() for f in fields]
            out[sym] = pd.DataFrame(
                [[r[w] for w in want if w in r] for r in rows_all],
                columns=[w for w in want if w in rows_all[0]] if rows_all else [],
                index=idx)
        return out

    def get_trade_detail_data(self, account, kind, what):
        if what == "account":
            return [_Acc()]
        if what == "order":
            return []          # 模拟「委托回报还没到」：PENDING 不会被清空
        return []


class _Acc(object):
    m_dAvailable = 400000.0
    m_dBalance = 400000.0


def make_globals(ctx):
    """注入 QMT 的全局函数（save_py_key / get_py_key / passorder）。"""
    store = {}

    def save_py_key(key, val):
        if len(val) > 5000:
            print("[kv] ⚠ %s 长度 %d 超过 5000 字符上限" % (key, len(val)))
        store[key] = val
        return (0, "")

    def get_py_key(key):
        return (store.get(key, ""), 0, "")

    def passorder(op, order_type, account, code, pr_type, price, volume,
                  strategy_name="", quick_trade=0, user_order_id="", ctx_=None):
        """必须与真实签名一致（11 参数），否则测不出参数错位这类问题。"""
        assert quick_trade == 2, "quickTrade 必须为 2，否则非 K 线边界下单会被静默丢弃"
        assert user_order_id, "userOrderId 不能为空（防超单字典的 key）"
        ctx.order_log.append({"op": op, "code": code, "price": price,
                              "volume": volume, "uid": user_order_id,
                              "qt": quick_trade, "strategy": strategy_name})
        return None

    st.save_py_key = save_py_key
    st.get_py_key = get_py_key
    st.passorder = passorder
    return store


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--limit", type=int, default=300)
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    if args.date:
        date = args.date
    else:
        ds = [r[0] for r in con.execute(
            "select distinct trade_date from kline_daily order by trade_date desc limit 2")]
        date = str(ds[1])            # 排除未定稿的当天

    symbols = [r[0] for r in con.execute(
        "select distinct symbol from kline_daily where trade_date = ? order by symbol",
        (date,))]
    if args.limit:
        symbols = symbols[:args.limit]

    print("mock 环境 · %s · %d 只\n" % (date, len(symbols)))
    ctx = MockContextInfo(con, date, symbols)
    store = make_globals(ctx)

    st.init(ctx)
    st.handlebar(ctx)
    st.handlebar(ctx)               # 再跑一次：应被 last_run 拦住，不重复下单

    print("\n" + "=" * 64)
    print("订单 %d 笔" % len(ctx.order_log))
    for o in ctx.order_log[:8]:
        side = "BUY" if o["op"] == 23 else "SELL"
        print("  %s %-10s %6d 股 @ %8.2f  qt=%d  uid=%s"
              % (side, o["code"], o["volume"], o["price"], o["qt"], o["uid"]))
    if len(ctx.order_log) > 8:
        print("  … 其余 %d 笔" % (len(ctx.order_log) - 8))
    print("\nKV 写入:")
    for k, v in sorted(store.items()):
        print("  %-10s %5d 字符  %s" % (k, len(v), v[:60] + ("…" if len(v) > 60 else "")))
    print("=" * 64)
    print("幂等: 两次 handlebar 共下 %d 笔（若翻倍则防重复失效）" % len(ctx.order_log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
