#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
频率 × 成本 的净超额检验
同一信号（按期内收益排名分组），在月/季/年三种调仓频率下，扣申赎成本后的净超额
成本：单边申赎 0.65%/次（申购0.15%打1折 + 赎回0.5%）
"""
import os, sqlite3, json
import numpy as np
import pandas as pd
from pathlib import Path

from _fund_stats import t_pvalue

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
OUT = DATA / "research"
OUT.mkdir(parents=True, exist_ok=True)


FR = DATA / "fund_research.db"
QD = DATA / "quant_dev.db"
COST = 0.65  # %/次


def tstat(x):
    s = pd.Series(x).dropna()
    n = len(s)
    if n < 3:
        return np.nan, np.nan, n
    sd = s.std(ddof=1)
    if not sd:
        return np.nan, np.nan, n
    t = s.mean() / (sd / np.sqrt(n))
    return float(t), t_pvalue(t, n - 1), n


print("构建月度收益矩阵 ...")
con = sqlite3.connect(f"file:{FR}?mode=ro", uri=True)
nav = pd.read_sql("select ts_code, nav_date, adj_nav from fund_nav_daily", con,
                  dtype={"ts_code": str, "nav_date": str})
con.close()
nav["base_code"] = nav["ts_code"].str.extract(r"^(\d{6})")[0]
nav["ym"] = nav.nav_date.str[:6]
nav = nav.sort_values(["base_code", "nav_date"])
m = nav.groupby(["base_code", "ym"]).tail(1)[["base_code", "ym", "adj_nav"]]
P = m.pivot_table(index="base_code", columns="ym", values="adj_nav", aggfunc="first").sort_index(axis=1)
M = (P.pct_change(axis=1) * 100).iloc[:, 1:]
M = M.mask((M > 150) | (M < -60))

con = sqlite3.connect(f"file:{QD}?mode=ro", uri=True)
fm = pd.read_sql("select base_code, group_tag, std_ret from fund_master", con, dtype={"base_code": str})
con.close()
fm["base_code"] = fm["base_code"].str.zfill(6)
pool = [c for c in fm[(fm.group_tag == "active") & (fm.std_ret >= 8)].base_code if c in M.index]
M = M.loc[pool]
print(f"  可投池 {len(M)} 只, {M.shape[1]} 个月")

# 季度 / 年度收益（用月末净值重采样）
qm_cols = [c for c in P.columns if c[4:] in ("03", "06", "09", "12")]
Q = (P[qm_cols].pct_change(axis=1) * 100).iloc[:, 1:]
Q = Q.mask((Q > 300) | (Q < -80))
yc = [c for c in P.columns if c[4:] == "12"]
A = (P[yc].pct_change(axis=1) * 100).iloc[:, 1:]
A = A.mask((A > 500) | (A < -80))
Q = Q.reindex([c for c in pool if c in Q.index])
A = A.reindex([c for c in pool if c in A.index])
Q.columns = [f"{c[:4]}Q{(int(c[4:]) - 1) // 3 + 1}" for c in Q.columns]
A.columns = [c[:4] for c in A.columns]
print(f"  季度 {Q.shape}, 年度 {A.shape}")

BINS = [("top5", 0.95, 1.01), ("p10_30", 0.70, 0.90), ("p30_50", 0.50, 0.70), ("bot30", -0.01, 0.30)]


def tier_of(r):
    for n, lo, hi in BINS:
        if lo <= r < hi:
            return n
    return "bot30"


def run(mat, name, per_year):
    """返回该频率下各梯队的净超额（已扣成本）"""
    cols = list(mat.columns)
    out = []
    for tier_name, _, _ in BINS:
        g, net = [], []
        for i in range(len(cols) - 1):
            st, sn = mat[cols[i]], mat[cols[i + 1]]
            ok = st.notna() & sn.notna()
            if ok.sum() < 300:
                continue
            tier = st[ok].rank(pct=True).map(tier_of)
            e = (sn[ok] - sn[ok].median())[tier == tier_name]
            if len(e) >= 50:
                g.append(e.mean())
                net.append(e.mean() - COST)   # 每次调仓扣 COST，不是年化
        if len(g) < 8:
            continue
        a, b = np.array(g), np.array(net)
        tg, _, ng = tstat(a)
        tn, _, _ = tstat(b)
        out.append({"freq": name, "tier": tier_name,
                    "gross": round(float(a.mean() * per_year), 2),
                    "cost": round(float(COST * per_year), 2),
                    "net": round(float(b.mean() * per_year), 2),
                    "t_gross": round(float(tg), 2), "t_net": round(float(tn), 2),
                    "n": int(ng), "pos": int((b > 0).sum())})
    return out


print("\n" + "=" * 82)
print("【频率 × 成本】毛超额 / 成本 / 净超额（%/年）")
print("=" * 82)
allres = []
for mat, nm, py in ((M, "月度", 12), (Q, "季度", 4), (A, "年度", 1)):
    r = run(mat, nm, py)
    allres += r
    print(f"\n[{nm}调仓 · 成本 {COST*py:.2f}%/年]")
    for x in r:
        flag = " ★" if abs(x["t_net"]) >= 2 else ""
        print(f"  {x['tier']:8s} 毛 {x['gross']:+7.2f}%  成本 -{x['cost']:5.2f}%  净 {x['net']:+7.2f}%  "
              f"t(净)={x['t_net']:+5.2f}  为正 {x['pos']}/{x['n']}{flag}")

json.dump(allres, open(f"{OUT}/res_freq_cost.json", "w"), ensure_ascii=False, indent=2, default=float)
print("\n已存 res_freq_cost.json")
