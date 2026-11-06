#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全A 历史行情拉取（Bronze 层）—— 一次性建仓脚本。

背景
----
quant 原有 kline_daily 只覆盖「核心指数成分并集」（3132 只），且存的是前复权价。
要做全A 因子有效性检验与策略评估，必须补齐三件事：

  ① 全A 标的，且含已退市股 —— 否则 2019 年的池子会凭空包含后来上市的公司，
     产生严重幸存者偏差（这是扩池研究里最容易犯的错）。
  ② 2019-2026 完整历史 —— 原库 2018 年仅 970 只、2019 仅 1011 只，逐年爬升。
  ③ 未复权价 + 复权因子 —— 与聚宽/回测标准口径一致，消除历史价随复权事件漂移。

两个设计要点
-----------
1. 按 trade_date 批量拉，不逐只拉
   pro.daily(trade_date=dt) 一次返回全市场约 5550 行（单次上限 6000，安全）。
   1880 个交易日 = 1880 次调用，而逐只拉要 5553 次。

2. 多线程 + 令牌桶
   单线程实测约 1.0 次/秒 —— 瓶颈是接口响应时间，不是限速（限速设的是 7.3 次/秒）。
   配 6 线程后 1880 天可从 31 分钟压到约 6 分钟。

输出：data/raw/market/bars_daily_alla/bars_<YYYY>.parquet（按年分片，便于增量）
      data/raw/market/stock_basic_all.parquet
"""
from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

try:
    import tushare as ts
except ImportError:
    print("需要 tushare：pip install tushare", file=sys.stderr)
    raise

# ---- 路径：项目根 = backend/scripts/../../ ----
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
OUT_DIR = os.path.join(ROOT, "data", "raw", "market", "bars_daily_alla")
BASIC_PATH = os.path.join(ROOT, "data", "raw", "market", "stock_basic_all.parquet")
os.makedirs(OUT_DIR, exist_ok=True)

TOKEN = os.environ.get(
    "TUSHARE_TOKEN",
    "d2684763c95c5f46e0cf65dee253d0559ad6ed2ec0b05b12ae557e99",
)
pro = ts.pro_api(TOKEN)

START, END = "20190101", "20260915"
RATE_PER_MIN = 400          # 令牌桶速率（低于实测上限，留安全余量）
WORKERS = 6
DAILY_COLS = ["ts_code", "trade_date", "open", "high", "low", "close",
              "pre_close", "change", "pct_chg", "vol", "amount"]


class Limiter:
    """全局令牌桶，线程安全。"""

    def __init__(self, per_min: float):
        self.interval = 60.0 / per_min
        self.lock = threading.Lock()
        self.next_t = time.time()
        self.n = 0

    def acquire(self):
        with self.lock:
            now = time.time()
            if self.next_t < now:
                self.next_t = now
            self.next_t += self.interval
            wait = self.next_t - now
            self.n += 1
        if wait > 0:
            time.sleep(wait)


LIM = Limiter(RATE_PER_MIN)


def call(fn, **kw):
    """带限速 + 重试。空结果不算失败（停牌/休市），直接返回 None。"""
    last = None
    for attempt in range(3):
        LIM.acquire()
        try:
            return fn(**kw)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.2 * (attempt + 1))
    print("   ! 放弃 %s: %s" % (kw.get("trade_date", kw.get("ts_code", "?")),
                               str(last)[:70]), flush=True)
    return None


# ============================ 1. 成分（含退市股）============================
def fetch_universe() -> pd.DataFrame:
    """三种上市状态合并 —— 含退市股是反幸存者偏差的关键。"""
    parts = []
    for st in ("L", "D", "P"):
        x = call(pro.stock_basic, exchange="", list_status=st,
                 fields="ts_code,name,list_date,delist_date,market,industry")
        if x is not None and len(x):
            x["list_status"] = st
            parts.append(x)
            print("   stock_basic %s: %d 只" % (st, len(x)), flush=True)
    sb = pd.concat(parts, ignore_index=True)
    n0 = len(sb)
    # 北交所（.BJ）剔除：流动性、涨跌幅规则、整手规则均与主板差异过大
    sb = sb[~sb.ts_code.astype(str).str.endswith(".BJ")].copy()
    print("   [成分] %d -> %d 只（剔除北交所 %d 只）" % (n0, len(sb), n0 - len(sb)),
          flush=True)
    sb.to_parquet(BASIC_PATH)
    print("   → %s" % BASIC_PATH, flush=True)
    return sb


# ============================ 2. 交易日历 ============================
def fetch_calendar() -> list[str]:
    cal = call(pro.trade_cal, exchange="SSE", start_date=START, end_date=END,
               is_open="1")
    if cal is None or not len(cal):
        raise RuntimeError("交易日历拉取失败")
    dates = sorted(cal["cal_date"].astype(str).tolist())
    print("   [日历] %d 个交易日 %s ~ %s" % (len(dates), dates[0], dates[-1]),
          flush=True)
    return dates


# ============================ 3. 按交易日拉日线（多线程）============================
def fetch_daily_multithread(dates: list[str]) -> pd.DataFrame:
    t0 = time.time()
    results: dict[str, pd.DataFrame] = {}
    done = 0
    fails: list[str] = []

    def one(dt: str):
        return dt, call(pro.daily, trade_date=dt)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(one, d) for d in dates]
        for f in as_completed(futs):
            dt, x = f.result()
            done += 1
            if x is None or not len(x):
                fails.append(dt)
            else:
                if len(x) >= 6000:
                    print("   ! %s 触 6000 上限，可能需分页" % dt, flush=True)
                results[dt] = x
            if done % 100 == 0 or done == len(dates):
                el = time.time() - t0
                eta = el / done * (len(dates) - done)
                print("   daily %d/%d  %.0fs  ETA %.1fmin  失败 %d"
                      % (done, len(dates), el, eta / 60, len(fails)), flush=True)

    if not results:
        raise RuntimeError("日线全部拉取失败 —— 检查 token / 网络")

    d = pd.concat(results.values(), ignore_index=True)
    # ⚠️ 北交所（.BJ）必须在这里同步剔除：pro.daily(trade_date=) 返回的是**全市场**，
    # 含北交所，而 fetch_universe 只过滤了成分表（stock_basic）—— 日线不过滤的话
    # Bronze 落盘会带 31 万行 bj 数据，Gold 层一并入库（2026-09-18 修）。
    n0 = len(d)
    d = d[~d.ts_code.astype(str).str.endswith(".BJ")].copy()
    if n0 != len(d):
        print("   [daily] 剔除北交所 %s 行（%s -> %s）" % (n0 - len(d), n0, len(d)),
              flush=True)
    d["trade_date"] = d["trade_date"].astype(str)
    keep = [c for c in DAILY_COLS if c in d.columns]
    missing = [c for c in DAILY_COLS if c not in d.columns]
    if missing:
        print("   ! 接口未返回列: %s" % missing, flush=True)
    d = d[keep]
    d["year"] = d["trade_date"].str[:4]

    # 按年分片落盘 —— 便于后续按年增量补齐，也避免单文件过大
    n_files = 0
    for y, g in d.groupby("year"):
        g.drop(columns=["year"]).to_parquet(
            os.path.join(OUT_DIR, "bars_%s.parquet" % y))
        n_files += 1
    print("   [daily] %s 行 × %d 只 × %d 天 → %d 个年度分片"
          % (format(len(d), ","), d.ts_code.nunique(), d.trade_date.nunique(), n_files),
          flush=True)
    if fails:
        print("   ! 无数据日 %d 个（休市/停牌/未发布）: %s"
              % (len(fails), fails[:8]), flush=True)
    return d


def main():
    t0 = time.time()
    print("=" * 78, flush=True)
    print("全A 历史行情拉取 —— %s ~ %s" % (START, END), flush=True)
    print("=" * 78, flush=True)

    print("[1/2] 成分（含退市股）", flush=True)
    sb = fetch_universe()

    print("[2/2] 交易日历 + 按交易日拉日线", flush=True)
    dates = fetch_calendar()
    fetch_daily_multithread(dates)

    el = time.time() - t0
    print("\n[完成] %.0fs（%.1f 分钟）→ %s" % (el, el / 60, OUT_DIR), flush=True)
    print("成分 %d 只（上市 %d / 退市 %d）"
          % (len(sb), (sb.list_status == "L").sum(), (sb.list_status == "D").sum()),
          flush=True)


if __name__ == "__main__":
    main()
