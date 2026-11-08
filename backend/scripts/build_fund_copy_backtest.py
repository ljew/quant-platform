#!/usr/bin/env python3
"""抄作业 · 季度版：用「披露 + 结转」的完整组合，而不是只有 35% 的回归隐含部分。

为什么换口径
------------
上一版用 `fund_regression_holdings.bayes_w`，它只覆盖季报看不见的那 35%，
样本只有 16 个半年期，且组合系统性偏高 β（1.36）。

`data/holdings/by_fund/*.csv` 有每个基金每个季度的**完整组合**：
- Q2/Q4（半年报/年报）：disclosed 约 100%
- Q1/Q3（季报）：disclosed 约 35%（前十大）+ carried 约 40%（从上次全披露结转）≈ 72–78%
这正是「抄作业者在实盘能拿到的组合」——不是研究近似，是可执行输入。
季频 4 期/年，样本从 16 期提到约 40 期。

口径
----
- carried 是「假设还拿着」，实盘抄作业也必须这么假设，所以这里用它没问题；
  但**归因类结论（Brinson）仍然只用 disclosed**，两者不混。
- 建仓日按披露期限推后：Q1/Q3 后 15 个交易日，Q2 后 45，Q4 后 60。
"""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
import numpy as np
import pandas as pd

from pathlib import Path

from _fund_stats import tstat

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
OUT = DATA / "research"
OUT.mkdir(parents=True, exist_ok=True)

DEV = DATA / "quant_dev.db"
RES = DATA / "fund_research.db"
BYFUND = DATA / "holdings" / "by_fund"


LAG = {"0331": 15, "0630": 45, "0930": 15, "1231": 60}
TOPN = 50
STOCK_COST = 0.13
FUND_COST = 0.65


def calendar() -> list[str]:
    c = sqlite3.connect(f"file:{DEV}?mode=ro", uri=True)
    d = [r[0] for r in c.execute("select distinct trade_date from kline_daily order by 1")]
    c.close()
    return d


def month_nav() -> pd.DataFrame:
    c = sqlite3.connect(f"file:{DEV}?mode=ro", uri=True)
    d = pd.read_sql("select base_code, ym, adj_nav from fund_nav_month_d", c,
                    dtype={"base_code": str})
    c.close()
    d["base_code"] = d.base_code.str.zfill(6)
    return d.pivot_table(index="base_code", columns="ym", values="adj_nav",
                         aggfunc="last").sort_index(axis=1)


def _to_tdx(x: str) -> str | None:
    if not isinstance(x, str) or "." not in x:
        return None
    pre = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(x.split(".")[-1].upper())
    return f"{pre}{x[:6]}" if pre else None


def main():
    cal = calendar()
    P = month_nav()

    # 用到的季度（有持仓分片的）
    pers = set()
    for f in BYFUND.glob("*.csv"):
        break
    # 直接从文件名扫太慢，先从一个文件拿期次全集
    sample = next(BYFUND.glob("*.csv"))
    with open(sample, newline="", encoding="utf-8") as fh:
        pers = {r["end_date"] for r in csv.DictReader(fh)}
    pers = sorted(p for p in pers if p[4:] in LAG)

    def build_of(p: str) -> str | None:
        iso = f"{p[:4]}-{p[4:6]}-{p[6:]}"
        later = [d for d in cal if d > iso]
        k = LAG[p[4:]]
        return later[k - 1] if len(later) > k else None

    pers = [p for p in pers if build_of(p)]
    # 个股行情从 2017-06 起
    pers = [p for p in pers if build_of(p) >= "2017-09-01"]
    print(f"季度期次 {len(pers)} 个：{pers[0]} ~ {pers[-1]}")

    # 每期选基：过去 12 个月收益分位 >= 95%
    sel_map, pool_size = {}, {}
    for p in pers:
        anchor = f"{p[:4]}-{p[4:6]}"
        cols = [x for x in P.columns if x <= anchor]
        if len(cols) < 13:
            continue
        tr = ((P[cols[-1]] / P[cols[-13]]) - 1) * 100
        tr = tr.dropna()
        if len(tr) < 300:
            continue
        sel_map[p] = set(tr[tr >= tr.quantile(0.95)].index)
        pool_size[p] = len(tr)
    pers = [p for p in pers if p in sel_map]
    print(f"可用于选基的期次 {len(pers)}；平均可选池 "
          f"{int(np.mean([pool_size[p] for p in pers]))} 只")

    # ---- 流式扫分片：分别累计「选中基金」与「全体基金」的股票权重 ----
    acc_sel = defaultdict(float)
    acc_all = defaultdict(float)
    n_cnt: dict[str, int] = defaultdict(int)
    files = sorted(BYFUND.glob("*.csv"))
    print(f"扫 {len(files)} 个基金分片 ...")
    for i, path in enumerate(files, 1):
        code = path.stem.split(".")[0]
        rows = defaultdict(list)
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                ed = r["end_date"]
                if ed not in sel_map:
                    continue
                rows[ed].append((r["symbol"], float(r["weight"] or 0)))
        for ed, recs in rows.items():
            tot = sum(w for _, w in recs)
            if tot < 20:            # 覆盖太低，归一化会失真
                continue
            picked = code in sel_map[ed]
            for sym, w in recs:
                t = _to_tdx(sym)
                if not t:
                    continue
                wn = w / tot
                acc_all[(ed, t)] += wn
                if picked:
                    acc_sel[(ed, t)] += wn
            n_cnt[ed] += 1
        if i % 1500 == 0:
            print(f"  {i}/{len(files)} ...", flush=True)

    print(f"累计键：选中 {len(acc_sel):,} / 全体 {len(acc_all):,}")

    # ---- 组组合并算收益 ----
    def top_port(acc, ed, n=TOPN):
        items = [(s, w / n_cnt[ed]) for (e, s), w in acc.items() if e == ed]
        items.sort(key=lambda x: -x[1])
        return items[:n]

    def price_at(boundary, symbols, cache):
        if boundary in cache:
            return cache[boundary]
        idx = cal.index(boundary)
        win = cal[max(0, idx - 10): idx + 1]
        c = sqlite3.connect(f"file:{DEV}?mode=ro", uri=True)
        ph = ",".join("?" * len(win))
        d = pd.read_sql(
            f"""select k.symbol, k.trade_date, k.close*a.adj_factor as hfq
                from kline_daily k join adj_factor_daily a
                  on k.symbol=a.symbol and k.trade_date=a.trade_date
                where k.trade_date in ({ph})""", c, params=win)
        c.close()
        d = d[d.symbol.isin(symbols)].sort_values("trade_date").groupby("symbol").last()
        cache[boundary] = d.hfq.to_dict()
        return cache[boundary]

    def fund_nav_at(boundary, codes, cache):
        if boundary in cache:
            return cache[boundary]
        idx = cal.index(boundary)
        win = [w.replace("-", "") for w in cal[max(0, idx - 10): idx + 1]]
        c = sqlite3.connect(f"file:{RES}?mode=ro", uri=True)
        ph = ",".join("?" * len(win))
        d = pd.read_sql(f"select ts_code, nav_date, adj_nav from fund_nav_daily "
                        f"where nav_date in ({ph})", c, params=win)
        c.close()
        d = d.assign(base_code=d.ts_code.str[:6])
        d = d[d.base_code.isin(codes)].sort_values("nav_date").groupby("base_code").last()
        cache[boundary] = d.adj_nav.to_dict()
        return cache[boundary]

    pc, fc, nc = {}, {}, {}   # 价格 / 净值 分开缓存，别共用
    rows = []
    for i in range(len(pers) - 1):
        p, nxt = pers[i], pers[i + 1]
        b, s = build_of(p), build_of(nxt)
        if not b or not s:
            continue
        port = top_port(acc_sel, p)
        bench = top_port(acc_all, p)
        syms = {x[0] for x in port} | {x[0] for x in bench}
        p0, p1 = price_at(b, syms, pc), price_at(s, syms, fc)

        def ret_of(items):
            v = [(p1[k] / p0[k] - 1) * 100 for k, _ in items if k in p0 and k in p1 and p0[k] > 0]
            return float(np.mean(v)) if v else np.nan

        r_port, r_bench = ret_of(port), ret_of(bench)
        sel = sel_map[p]
        n0, n1 = fund_nav_at(b, sel, nc), fund_nav_at(s, sel, nc)
        fr = [((n1[k] / n0[k]) - 1) * 100 for k in sel if k in n0 and k in n1 and n0[k] > 0]
        r_fund = float(np.mean(fr)) if fr else np.nan

        rows.append({"period": p, "build": b, "sell": s, "n_funds": len(sel),
                     "n_pool": pool_size[p], "n_covered": n_cnt[p],
                     "port": r_port, "bench": r_bench, "fund": r_fund,
                     "expo": float(sum(w for _, w in port) * 100)})
        print(f"  {p}→{nxt}  抄持仓 {r_port:7.2f}%  抄全市场 {r_bench:7.2f}%  "
              f"买基金 {r_fund:7.2f}%  覆盖 {n_cnt[p]}")

    D = pd.DataFrame(rows).dropna(subset=["port", "bench", "fund"])
    if not len(D):
        print("无有效样本")
        return
    D["ex_bench"] = D.port - D.bench
    D["ex_fund"] = D.port - D.fund

    # β 调整 alpha
    x, y = D.fund.values, D.port.values
    n = len(x)
    b1, a1 = np.polyfit(x, y, 1)
    res = y - (a1 + b1 * x)
    s2 = (res ** 2).sum() / (n - 2)
    se = np.sqrt(s2 * (1 / n + x.mean() ** 2 / ((x - x.mean()) ** 2).sum()))
    t_alpha = a1 / se
    r2 = 1 - (res ** 2).sum() / ((y - y.mean()) ** 2).sum()

    def s_of(v, cost=0.0):
        a = np.asarray(v, dtype=float) - cost
        tv, pv, nn = tstat(a)
        return {"mean": round(float(a.mean()), 2), "t": round(float(tv), 2),
                "p": round(float(pv), 3), "pos": int((a > 0).sum()), "n": int(nn),
                "median": round(float(np.median(a)), 2)}

    res_out = {
        "freq": "quarterly", "periods": len(D), "span": f"{D.period.iloc[0]}~{D.period.iloc[-1]}",
        "build_range": f"{D.build.iloc[0]}~{D.sell.iloc[-1]}",
        "topn": TOPN, "stock_cost": STOCK_COST, "fund_cost": FUND_COST,
        "port_gross": s_of(D.port), "port_net": s_of(D.port, STOCK_COST),
        "bench_net": s_of(D.bench, STOCK_COST), "fund_net": s_of(D.fund, FUND_COST),
        "sel_effect": s_of(D.ex_bench), "vs_fund_raw": s_of(D.ex_fund),
        "beta": round(float(b1), 3), "alpha": round(float(a1), 2),
        "alpha_t": round(float(t_alpha), 2), "r2": round(float(r2), 3),
        "avg_pool": int(D.n_pool.mean()), "avg_sel": int(D.n_funds.mean()),
        "avg_covered": int(D.n_covered.mean()),
        "yearly": [{"period": r.period, "port": round(r.port, 2), "bench": round(r.bench, 2),
                    "fund": round(r.fund, 2), "exc": round(r.port - r.bench, 2)}
                   for r in D.itertuples()],
    }
    (OUT / "res_copy_q.json").write_text(json.dumps(res_out, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    D.to_csv(OUT / "res_copy_q_periods.csv", index=False)

    print("\n" + "=" * 72)
    print(f"【抄作业 · 季度版】{len(D)} 期 {res_out['span']}")
    print("=" * 72)
    for k, lab in [("port_net", "抄 Top5% 全组合(净)"), ("bench_net", "抄全市场组合(净)"),
                   ("fund_net", "直接买 Top5% 基金(净)")]:
        v = res_out[k]
        print(f"  {lab:<22} {v['mean']:+7.2f}%/期  t={v['t']:+6.2f}  "
              f"为正 {v['pos']}/{v['n']}  中位 {v['median']:+.2f}%")
    e, f = res_out["sel_effect"], res_out["vs_fund_raw"]
    print(f"\n  选股效应（抄Top5% − 抄全市场） {e['mean']:+7.2f}%/期  t={e['t']:+6.2f}  "
          f"为正 {e['pos']}/{e['n']}")
    print(f"  抄 − 买基金（未调整）          {f['mean']:+7.2f}%/期  t={f['t']:+6.2f}  "
          f"为正 {f['pos']}/{f['n']}")
    print(f"  β={res_out['beta']} R²={res_out['r2']}  α={res_out['alpha']}%/期 t={res_out['alpha_t']}")
    print(f"  平均可选池 {res_out['avg_pool']} 只 / 选中 {res_out['avg_sel']} 只 / "
          f"有持仓 {res_out['avg_covered']} 只")


if __name__ == "__main__":
    main()
