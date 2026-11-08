#!/usr/bin/env python3
"""样本代表性检验：日频子集 vs 全样本（用同一份旧年度收益，只换样本）。

目的：日频净值只覆盖 ~78% 的 active 基金（来源是 fund_hold_coverage）。
如果「只用日频覆盖的那批」算出来的结论和全样本差异很大，说明日频子集是有偏的，
把它当主口径就是降级 —— 这时候正确做法是保留全样本年度口径，日频只用于月度分辨率。
"""
from __future__ import annotations

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
BINS = [("top5", 0.95, 1.01), ("p5_10", 0.90, 0.95), ("p10_30", 0.70, 0.90),
        ("p30_50", 0.50, 0.70), ("p50_70", 0.30, 0.50), ("bot30", -0.01, 0.30)]
LABEL = {"top5": "前5%", "p5_10": "5-10%", "p10_30": "10-30%",
         "p30_50": "30-50%", "p50_70": "50-70%", "bot30": "后30%"}


def tier_of_rank(r):
    for name, lo, hi in BINS:
        if lo <= r < hi:
            return name
    return "bot30"


def build():
    con = sqlite3.connect(f"file:{DEV}?mode=ro", uri=True)
    fm = pd.read_sql("select base_code, group_tag from fund_master", con, dtype={"base_code": str})
    nv = pd.read_sql("select base_code, year, adj_nav from fund_nav_year", con, dtype={"base_code": str})
    con.close()
    for d in (fm, nv):
        d["base_code"] = d.base_code.str.zfill(6)
    fm = fm[fm.group_tag == "active"].drop_duplicates("base_code")
    P = nv.pivot_table(index="base_code", columns="year", values="adj_nav", aggfunc="first")
    P.columns = [int(c) for c in P.columns]
    R = P.pct_change(axis=1) * 100
    R = R.iloc[:, 1:].mask((R.iloc[:, 1:] > 300) | (R.iloc[:, 1:] < -80))
    R = R.reindex(fm.base_code.values)
    std = R.std(axis=1)
    return R[std >= 8]


def tiers(R, label):
    rows = []
    for name in [b[0] for b in BINS]:
        arr = []
        for t in range(2015, 2025):
            if t not in R.columns or t + 1 not in R.columns:
                continue
            s_t, s_n = R[t], R[t + 1]
            ok = s_t.notna() & s_n.notna()
            if ok.sum() < 30:
                continue
            tier = s_t[ok].rank(pct=True).map(tier_of_rank)
            e = (s_n[ok] - R[t + 1].median())[tier == name]
            if len(e) >= 10:
                arr.append(e.mean())
        if len(arr) < 5:
            continue
        a = np.array(arr)
        tv, _, n = tstat(a)
        rows.append({"tier": name, "mean": float(a.mean()), "t": float(tv),
                     "sd": float(a.std(ddof=1)), "pos": int((a > 0).sum()), "n": n})
    return pd.DataFrame(rows)


def main():
    R = build()
    # 日频覆盖的基金集合 = fund_nav_year_d 里出现过的基金
    con = sqlite3.connect(f"file:{DEV}?mode=ro", uri=True)
    daily = {r[0].zfill(6) for r in con.execute("select distinct base_code from fund_nav_year_d")}
    con.close()

    full = R
    sub = R.loc[R.index.intersection(daily)]
    print(f"全样本 {len(full):,} 只 / 日频子集 {len(sub):,} 只 ({len(sub)/len(full)*100:.1f}%)")
    print("\n各年样本数:")
    cmp = pd.DataFrame({"全": full.notna().sum(), "日频子集": sub.notna().sum()})
    cmp["占比%"] = (cmp["日频子集"] / cmp["全"] * 100).round(1)
    print(cmp.loc[[c for c in cmp.index if 2015 <= c <= 2025]].to_string())

    a, b = tiers(full, "全"), tiers(sub, "日频子集")
    m = a.merge(b, on="tier", suffixes=("_全", "_子"))
    print("\n" + "=" * 84)
    print("【梯队超额】全样本 vs 日频子集（同一数据源，只改样本）")
    print("=" * 84)
    print(f"  {'梯队':<8}{'全均值':>8}{'全t':>7}{'全σ':>7}   "
          f"{'子均值':>8}{'子t':>7}{'子σ':>7}{'均值差':>8}")
    for r in m.itertuples():
        print(f"  {LABEL[r.tier]:<8}{r.mean_全:>8.2f}{r.t_全:>7.2f}{r.sd_全:>7.2f}   "
              f"{r.mean_子:>8.2f}{r.t_子:>7.2f}{r.sd_子:>7.2f}{r.mean_子-r.mean_全:>+8.2f}")

    d = (m["mean_子"] - m["mean_全"]).abs()
    print(f"\n  平均绝对偏差 {d.mean():.2f}pp / 最大 {d.max():.2f}pp")
    print(f"  方向一致 {int(((m['mean_子'] > 0) == (m['mean_全'] > 0)).sum())}/{len(m)} 档")


if __name__ == "__main__":
    main()
