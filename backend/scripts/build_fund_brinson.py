#!/usr/bin/env python3
"""Brinson 归因：Top5% 基金的超额，有多少是行业押注，多少是选股。

口径选择（这条决定了结果可不可信）
----------------------------------
`fund_industry_exposure` 里 Q1/Q3 的 `disclosed_w` 只覆盖到净值的约 55%
（季报只披露前十大），Q2/Q4（半年报/年报）才是 100% 全披露。
把 55% 重新归一化到 100% 会把「没披露的 45%」按比例硬摊到已知行业上，
凭空放大第一大行业 —— 所以这里**只用 Q2/Q4**。

披露时滞按监管要求设：
- 半年报（6/30）→ 8/31 前，取报告期后 45 个交易日
- 年报（12/31）  → 次年 3/31 前，取 60 个交易日
持有到下一个建仓点（约 6 个月），共 20 个区间。

分解
----
    实际超额 = Σ(w_top − w_all)·r_行业  +  残差
               └── 行业配置效应 ──┘      └ 选股 ┘
"""
from __future__ import annotations

import json
import sqlite3
import numpy as np
import pandas as pd

from pathlib import Path

from _fund_stats import tstat

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
OUT = DATA / "research"
OUT.mkdir(parents=True, exist_ok=True)

RES = DATA / "fund_research.db"
DEV = DATA / "quant_dev.db"

LAG = {"0331": 15, "0630": 45, "0930": 15, "1231": 60}


def calendar() -> list[str]:
    c = sqlite3.connect(f"file:{DEV}?mode=ro", uri=True)
    d = [r[0] for r in c.execute(
        "select distinct trade_date from kline_daily order by 1")]
    c.close()
    return d


def periods() -> list[str]:
    """只用 Q2/Q4：全披露，且每期基金数 ≥1000。"""
    c = sqlite3.connect(f"file:{RES}?mode=ro", uri=True)
    d = [r[0] for r in c.execute(
        """select end_date from fund_industry_exposure
           where substr(end_date,5,4) in ('0630','1231')
           group by end_date having count(distinct base_code)>=1000
           order by end_date""")]
    c.close()
    return d


def build_date(cal: list[str], p: str) -> str | None:
    iso = f"{p[:4]}-{p[4:6]}-{p[6:]}"
    later = [d for d in cal if d > iso]
    k = LAG[p[4:]]
    return later[k - 1] if len(later) > k else None


def trailing_ret(anchor_ym: str) -> pd.Series:
    c = sqlite3.connect(f"file:{DEV}?mode=ro", uri=True)
    d = pd.read_sql("select base_code, ym, adj_nav from fund_nav_month_d", c,
                    dtype={"base_code": str})
    c.close()
    d["base_code"] = d.base_code.str.zfill(6)
    P = d.pivot_table(index="base_code", columns="ym", values="adj_nav", aggfunc="last").sort_index(axis=1)
    cols = [x for x in P.columns if x <= anchor_ym]
    if len(cols) < 13:
        return pd.Series(dtype=float)
    return ((P[cols[-1]] / P[cols[-13]]) - 1) * 100


def main():
    cal = calendar()
    pers = [p for p in periods() if build_date(cal, p)]
    print(f"报告期（Q2/Q4 全披露）{len(pers)} 个：{pers[0]} ~ {pers[-1]}")

    rows, ind_rows = [], []
    for i in range(len(pers) - 1):
        p, nxt = pers[i], pers[i + 1]
        b, s = build_date(cal, p), build_date(cal, nxt)
        tr = trailing_ret(f"{p[:4]}-{p[4:6]}")
        if tr.empty:
            continue

        c = sqlite3.connect(f"file:{RES}?mode=ro", uri=True)
        ex = pd.read_sql(
            "select base_code, industry, total_w from fund_industry_exposure where end_date=?",
            c, params=[p], dtype={"base_code": str})
        c.close()
        ex["base_code"] = ex.base_code.str.zfill(6)
        pool = tr.reindex(ex.base_code.unique()).dropna()
        if len(pool) < 300:
            continue
        sel = set(pool[pool >= pool.quantile(0.95)].index)

        W = ex.pivot_table(index="base_code", columns="industry", values="total_w", aggfunc="sum")
        W = W.div(W.sum(axis=1), axis=0) * 100        # 归一到 100%
        w_top = W.reindex(list(sel)).mean()
        w_all = W.mean()

        # 行业收益
        b8, s8 = b.replace("-", ""), s.replace("-", "")
        c = sqlite3.connect(f"file:{RES}?mode=ro", uri=True)
        ir = pd.read_sql(
            """select industry, ret from sw_industry_daily
               where trade_date>? and trade_date<=? order by industry, trade_date""",
            c, params=[b8, s8])
        c.close()
        R = ir.groupby("industry").ret.apply(lambda x: (np.prod(1 + x / 100) - 1) * 100)

        common = [x for x in w_top.index if x in R.index]
        alloc = float(((w_top[common] - w_all[common]) / 100 * R[common]).sum())

        # 基金实际收益
        c = sqlite3.connect(f"file:{RES}?mode=ro", uri=True)
        nv = pd.read_sql(
            "select ts_code, nav_date, adj_nav from fund_nav_daily where nav_date in (?,?)",
            c, params=[b8, s8])
        c.close()
        if not len(nv):
            continue
        nv["base_code"] = nv.ts_code.str[:6]
        P = nv.pivot_table(index="base_code", columns="nav_date", values="adj_nav", aggfunc="last").dropna()
        rr = (P[s8] / P[b8] - 1) * 100
        r_top = float(rr.reindex(list(sel)).dropna().mean())
        r_all = float(rr.reindex(list(pool.index)).dropna().mean())
        actual = r_top - r_all

        rows.append({"period": p, "build": b, "sell": s, "n_pool": len(pool),
                     "n_sel": len(sel), "r_top": r_top, "r_all": r_all,
                     "actual": actual, "alloc": alloc, "select": actual - alloc})
        for ind in common:
            ind_rows.append({"period": p, "industry": ind,
                             "w_top": float(w_top[ind]), "w_all": float(w_all[ind]),
                             "active": float(w_top[ind] - w_all[ind]),
                             "ret": float(R[ind])})
        print(f"  {p}→{nxt}  实际 {actual:+6.2f}%  行业配置 {alloc:+6.2f}%  "
              f"选股 {actual-alloc:+6.2f}%  (池 {len(pool)}, 选 {len(sel)})")

    D = pd.DataFrame(rows)
    if not len(D):
        print("无有效样本")
        return
    I = pd.DataFrame(ind_rows)

    def s(x):
        tv, pv, n = tstat(x)
        return {"mean": round(float(np.mean(x)), 2), "t": round(float(tv), 2),
                "p": round(float(pv), 3), "pos": int((np.array(x) > 0).sum()), "n": int(n)}

    ov = (I.groupby("industry").agg(active=("active", "mean"), ret=("ret", "mean"))
          .sort_values("active", ascending=False))

    res = {
        "periods": len(D), "span": f"{D.build.iloc[0]}~{D.sell.iloc[-1]}",
        "note": "仅用 Q2/Q4 全披露行业权重；持有约 6 个月；配置效应=Σ(w_top−w_all)×r_行业",
        "actual": s(D.actual), "alloc": s(D.alloc), "select": s(D.select),
        "avg_pool": round(float(D.n_pool.mean()), 1),
        "avg_sel": round(float(D.n_sel.mean()), 1),
        "share_alloc": round(float(D.alloc.mean() / D.actual.mean() * 100), 1) if D.actual.mean() else None,
        "yearly": [{"period": r.period, "actual": round(r.actual, 2),
                    "alloc": round(r.alloc, 2), "select": round(r.select, 2),
                    "n": int(r.n_pool)} for r in D.itertuples()],
        "overweight": [{"industry": k, "active": round(float(v.active), 2),
                        "ret": round(float(v.ret), 2)}
                       for k, v in ov.head(6).iterrows()],
        "underweight": [{"industry": k, "active": round(float(v.active), 2),
                         "ret": round(float(v.ret), 2)}
                        for k, v in ov.tail(6).iterrows()],
    }
    (OUT / "res_brinson.json").write_text(json.dumps(res, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    D.to_csv(OUT / "res_brinson_periods.csv", index=False)

    print("\n" + "=" * 72)
    print("【行业配置 vs 选股】Top5% − 全部基金，半年期")
    print("=" * 72)
    for k, lab in [("actual", "实际超额"), ("alloc", "行业配置效应"), ("select", "选股（残差）")]:
        v = res[k]
        print(f"  {lab:<12} {v['mean']:+6.2f}%/期  t={v['t']:+5.2f}  "
              f"为正 {v['pos']}/{v['n']}  p={v['p']:.3f}")
    print(f"\n  配置效应占实际超额 {res['share_alloc']}%")
    print("  超配前 6:", [(o["industry"], o["active"]) for o in res["overweight"]])
    print("  低配前 6:", [(o["industry"], o["active"]) for o in res["underweight"]])


if __name__ == "__main__":
    main()
