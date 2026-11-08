#!/usr/bin/env python3
"""用最新日频口径重跑 5 个基金策略，并把「抄隐含持仓」也登记为一条回测。

与旧版差在数据源：
    旧：panel_equity.csv（年末截面，含 19.5% 混进来的被动指数，样本 9251）
    新：fund_nav_year_d（日频→年末，已剔除纯被动）

输出 res_strategies.json，再由 apply_strategies.py 写回 backtests 表。
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

DEV = DATA / "quant_dev.db"

YRS = list(range(2015, 2026))
TEN = list(range(2016, 2026))
N_HOLD = 20
COST = 0.0065


def load():
    con = sqlite3.connect(f"file:{DEV}?mode=ro", uri=True)
    nv = pd.read_sql("select base_code, year, adj_nav from fund_nav_year_d", con,
                     dtype={"base_code": str})
    fm = pd.read_sql("select base_code, group_tag from fund_master", con, dtype={"base_code": str})
    con.close()
    for d in (nv, fm):
        d["base_code"] = d.base_code.str.zfill(6)
    fm = fm[fm.group_tag == "active"].drop_duplicates("base_code")
    P = nv.pivot_table(index="base_code", columns="year", values="adj_nav", aggfunc="first")
    P.columns = [int(c) for c in P.columns]
    R = P.pct_change(axis=1) * 100
    R = R.iloc[:, 1:].mask(lambda x: (x > 300) | (x < -80))
    R = R.reindex(fm.base_code.values)
    std = R.std(axis=1)
    return R[std >= 8]


def lowvol(t, ret, lookback=3):
    cols = [c for c in range(t - lookback + 1, t + 1) if c in ret.columns]
    if len(cols) < lookback:
        return []
    s = ret.loc[:, cols].std(axis=1)
    ok = s.dropna()
    return list(ok.nsmallest(N_HOLD).index) if len(ok) >= N_HOLD else []


def run(RET, rule):
    curve, picks = [], {}
    for t in range(2015, 2025):
        if t not in RET.columns or t + 1 not in RET.columns:
            continue
        rk = RET[t].rank(pct=True)
        if rule == "top5":
            sel = list(rk[rk >= 0.95].index)
        elif rule == "p10_30":
            sel = list(rk[(rk >= 0.70) & (rk < 0.90)].index)
        elif rule == "p30_50":
            sel = list(rk[(rk >= 0.50) & (rk < 0.70)].index)
        elif rule == "lowvol3y":
            sel = lowvol(t, RET)
        elif rule == "lowvol_ex_bot":
            sel = [c for c in lowvol(t, RET) if rk.get(c, 1) >= 0.30]
        else:
            raise ValueError(rule)
        if len(sel) < 5:
            continue
        sel = sel[:N_HOLD] if len(sel) > N_HOLD else sel
        nxt = RET[t + 1].reindex(sel).dropna()
        if not len(nxt):
            continue
        picks[t + 1] = {"n": len(nxt), "ret": float(nxt.mean() - COST * 100)}
        curve.append({"year": t + 1, "ret": float(nxt.mean() - COST * 100)})
    return curve, picks


def stats(curve, bench):
    if not curve:
        return None
    C = pd.DataFrame(curve)
    eq = (1 + C.ret / 100).cumprod()
    n = len(C)
    total = float(eq.iloc[-1] - 1)
    ann = float(eq.iloc[-1] ** (1 / n) - 1)
    peak = eq.cummax()
    mdd = float((eq / peak - 1).min())
    sd = float(C.ret.std(ddof=1))
    sharpe = float(ann * 100 / sd) if sd else 0.0
    exc = (C.ret - C.year.map(bench)).values
    tv, pv, _ = tstat(exc)
    return {"total": round(total, 4), "ann": round(ann, 4), "mdd": round(mdd, 4),
            "sharpe": round(sharpe, 3), "std": round(sd, 2),
            "pos": int((exc > 0).sum()), "tot": n,
            "exc_mean": round(float(exc.mean()), 2), "exc_t": round(float(tv), 2),
            "exc_p": round(float(pv), 3),
            "curve": [{"year": int(r.year), "equity": round(float(e), 6)}
                      for r, e in zip(C.itertuples(), eq)]}


def main():
    RET = load()
    print(f"可投池 {len(RET):,} 只主动权益（日频口径，已剔除纯被动）")
    ben = {y: float(RET[y].mean() - COST * 100) for y in TEN if y in RET.columns}
    bench_g = float(np.prod([1 + ben[y] / 100 for y in sorted(ben)]))
    bench_eq = bench_g - 1

    rules = [("fund_top5_chase", "top5", "追冠军：上年收益分位≥95%，取20只等权"),
             ("fund_p10_30", "p10_30", "第二梯队：上年分位10-30%，取20只等权"),
             ("fund_p30_50", "p30_50", "中游：上年分位30-50%，取20只等权"),
             ("fund_lowvol3y", "lowvol3y", "低波动：过去3年年度收益标准差最小20只等权"),
             ("fund_lowvol_ex_bot", "lowvol_ex_bot", "低波动+排除后30%")]

    res = {}
    print(f"\n基准（全池等权，扣成本）累计 {bench_eq*100:.1f}%，"
          f"年化 {(bench_g**(1/len(ben))-1)*100:.2f}%")
    print(f"\n{'策略':<22}{'年化':>8}{'累计':>9}{'超额':>8}{'t':>7}{'MDD':>8}{'胜年':>7}")
    for key, rule, desc in rules:
        curve, picks = run(RET, rule)
        s = stats(curve, ben)
        if not s:
            continue
        s.update({"key": key, "desc": desc, "rule": rule})
        res[key] = s
        print(f"{key:<22}{s['ann']*100:>7.2f}%{s['total']*100:>8.1f}%"
              f"{s['exc_mean']:>+7.2f}%{s['exc_t']:>7.2f}{s['mdd']*100:>7.1f}%"
              f"{s['pos']:>4}/{s['tot']:<3}")

    res["_bench"] = {"total": round(bench_eq, 4),
                     "ann": round(bench_g ** (1 / len(ben)) - 1, 4),
                     "yearly": ben, "n_pool": int(len(RET))}
    (OUT / "res_strategies.json").write_text(json.dumps(res, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    print("\n已存 res_strategies.json")


if __name__ == "__main__":
    main()
