#!/usr/bin/env python3
"""抄「回归隐含持仓」回测 —— 用穿透数据，而不是残缺的披露前十大。

为什么这个口径比之前强
----------------------
1. 旧版只能拿到「披露的 53% 持仓」，2020 年后每期都被 offset 截断；
   现在 `fund_regression_holdings.bayes_w` 给出**每只基金完整的隐含持仓**
   （披露 39% + 回归隐含 35% ≈ 股票仓位 74%），每期 6000+ 只基金。
2. 旧版要等年报（次年 3 月底），前视风险 1 个季度；回归隐含持仓由
   **日净值 + 个股收益**反推，理论上 t+1 就能算。但 `bayes_w` 的贝叶斯先验
   用了披露值（季报 15 个工作日内公布），所以建仓日一律推到
   **报告期后第 15 个交易日**，把先验的前视也堵掉。
3. 持有期 6 个月（季报节奏），比原来 1 年样本多 16 期。

口径
----
- 选基：报告期 t 的**过去 12 个月**收益分位 ≥95%
- 组合：选中基金的隐含持仓先各自归一化，再等权平均，取权重最大的 50 只等权买入
- 基准：**全部基金**按同样方法构造的 50 只（隔离「选基」这件事本身）
- 对照：直接买这些基金本身（同期净值收益）
- 成本：股票双边 0.13%/期；基金申赎 0.65%/期
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

LAG_DAYS = 15          # 报告期后第 15 个交易日建仓
TOPN = 50              # 组合持股数
STOCK_COST = 0.13      # 股票双边成本 %
FUND_COST = 0.65       # 基金申赎成本 %


def calendar() -> list[str]:
    c = sqlite3.connect(f"file:{DEV}?mode=ro", uri=True)
    d = [r[0] for r in c.execute(
        "select distinct trade_date from kline_daily where trade_date>='2017-06-01' order by 1")]
    c.close()
    return d


def periods() -> list[str]:
    c = sqlite3.connect(f"file:{RES}?mode=ro", uri=True)
    d = [r[0] for r in c.execute(
        """select end_date from fund_regression_holdings
           group by end_date having count(distinct ts_code)>=1000
             and (substr(end_date,5,4)='0331' or substr(end_date,5,4)='0930')
           order by end_date""")]
    c.close()
    return d


def build_dates(cal: list[str], pers: list[str]) -> dict[str, str]:
    """每个报告期 → 建仓交易日（报告期后第 LAG_DAYS 个交易日）。"""
    out = {}
    for p in pers:
        iso = f"{p[:4]}-{p[4:6]}-{p[6:]}"
        later = [d for d in cal if d > iso]
        if len(later) > LAG_DAYS:
            out[p] = later[LAG_DAYS - 1]
    return out


def fund_trailing_return(anchor_ym: str) -> pd.Series:
    """报告期所在月末往前 12 个月的收益（%）。"""
    c = sqlite3.connect(f"file:{DEV}?mode=ro", uri=True)
    d = pd.read_sql("select base_code, ym, adj_nav from fund_nav_month_d", c, dtype={"base_code": str})
    c.close()
    d["base_code"] = d.base_code.str.zfill(6)
    P = d.pivot_table(index="base_code", columns="ym", values="adj_nav", aggfunc="last")
    P = P.sort_index(axis=1)
    cols = [x for x in P.columns if x <= anchor_ym]
    if len(cols) < 13:
        return pd.Series(dtype=float)
    end, start = cols[-1], cols[-13]
    return ((P[end] / P[start]) - 1) * 100


def _to_tdx(x: str) -> str | None:
    """持仓表用 tushare 代码（300267.SZ），行情表用通达信代码（sz300267）。"""
    if not isinstance(x, str) or "." not in x:
        return None
    pre = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(x.split(".")[-1].upper())
    return f"{pre}{x[:6]}" if pre else None


def holdings(period: str, codes: set[str] | None) -> pd.DataFrame:
    c = sqlite3.connect(f"file:{RES}?mode=ro", uri=True)
    q = "select ts_code, symbol, bayes_w from fund_regression_holdings where end_date=? and bayes_w>0"
    d = pd.read_sql(q, c, params=[period])
    c.close()
    if codes is not None:
        d = d[d.ts_code.isin(codes)]
    d = d.assign(sym=d.symbol.map(_to_tdx))
    return d.dropna(subset=["sym"])


def expo_of(period: str, codes: set[str]) -> float:
    """选中基金当期的平均股票暴露（beta_total，%）。用于仓位对齐。"""
    c = sqlite3.connect(f"file:{RES}?mode=ro", uri=True)
    d = pd.read_sql(
        "select ts_code, beta_total from fund_regression_holdings_meta where end_date=?",
        c, params=[period])
    c.close()
    if not len(d):
        return np.nan
    d = d[d.ts_code.isin(codes)]
    b = d.beta_total.dropna()
    return float(b.mean()) if len(b) else np.nan


def agg_portfolio(d: pd.DataFrame, topn: int) -> pd.Series:
    """每只基金权重归一化后等权平均，合并到股票层。"""
    if not len(d):
        return pd.Series(dtype=float)
    s = d.groupby("ts_code").bayes_w.transform("sum")
    d = d.assign(w=d.bayes_w / s)
    n_fund = d.ts_code.nunique()
    g = d.groupby("sym").w.sum() / n_fund
    return g.sort_values(ascending=False).head(topn)


def price_at(cal: list[str], boundary: str, symbols: set[str], cache: dict) -> dict:
    """边界的「当日或之前最近一个交易日」后复权价。"""
    if boundary in cache:
        return cache[boundary]
    idx = cal.index(boundary)
    win = cal[max(0, idx - 10): idx + 1]
    c = sqlite3.connect(f"file:{DEV}?mode=ro", uri=True)
    ph = ",".join("?" * len(win))
    q = f"""select k.symbol, k.trade_date, k.close*a.adj_factor as hfq
            from kline_daily k join adj_factor_daily a
              on k.symbol=a.symbol and k.trade_date=a.trade_date
            where k.trade_date in ({ph})"""
    d = pd.read_sql(q, c, params=win)
    c.close()
    if not len(d):
        cache[boundary] = {}
        return {}
    d = d[d.symbol.isin(symbols)]
    d = d.sort_values("trade_date").groupby("symbol").last()
    cache[boundary] = d.hfq.to_dict()
    return cache[boundary]


def fund_nav_at(cal: list[str], boundary: str, codes: set[str], cache: dict) -> dict:
    if boundary in cache:
        return cache[boundary]
    idx = cal.index(boundary)
    win = cal[max(0, idx - 10): idx + 1]
    win8 = [w.replace("-", "") for w in win]
    c = sqlite3.connect(f"file:{RES}?mode=ro", uri=True)
    ph = ",".join("?" * len(win8))
    q = f"select ts_code, nav_date, adj_nav from fund_nav_daily where nav_date in ({ph})"
    d = pd.read_sql(q, c, params=win8)
    c.close()
    if not len(d):
        cache[boundary] = {}
        return {}
    d = d[d.ts_code.isin(codes)]
    d = d.sort_values("nav_date").groupby("ts_code").last()
    cache[boundary] = d.adj_nav.to_dict()
    return cache[boundary]


def main():
    cal = calendar()
    pers = periods()
    bd = build_dates(cal, pers)
    pers = [p for p in pers if p in bd and p >= "20170930"]
    print(f"报告期 {len(pers)} 个：{pers[0]} ~ {pers[-1]}")
    print("建仓日:", {p: bd[p] for p in pers[:4]}, "...")

    rows = []
    all_syms: set[str] = set()
    plan = []
    for i in range(len(pers) - 1):
        p, nxt = pers[i], pers[i + 1]
        tr = fund_trailing_return(f"{p[:4]}-{p[4:6]}")
        if tr.empty:
            continue
        h = holdings(p, None)
        if not len(h):
            continue
        h = h.assign(base_code=h.ts_code.str[:6])
        present = tr.reindex(h.base_code.unique()).dropna()
        if len(present) < 200:
            continue
        thr = present.quantile(0.95)
        sel_codes = set(present[present >= thr].index)
        sel = set(h.ts_code[h.base_code.isin(sel_codes)].unique())
        plan.append((p, nxt, sel, len(present)))
        all_syms |= set(h.sym.unique())
        print(f"  {p}: 可选基金 {len(present):5d}  选中 {len(sel):4d}  "
              f"门槛 {thr:6.2f}%  建仓 {bd[p]} → {bd[nxt]}")

    print(f"\n股票池合计 {len(all_syms):,} 只，开始算收益…")
    pcache, fcache = {}, {}
    for p, nxt, sel, n_pool in plan:
        b, s = bd[p], bd[nxt]
        h_all = holdings(p, None)
        h_sel = h_all[h_all.ts_code.isin(sel)]
        if not len(h_sel):
            continue
        port = agg_portfolio(h_sel, TOPN)
        bench = agg_portfolio(h_all, TOPN)
        syms = set(port.index) | set(bench.index)
        p0 = price_at(cal, b, syms, pcache)
        p1 = price_at(cal, s, syms, fcache)

        def ret_of(w: pd.Series) -> float:
            ok = [(p1[k] / p0[k] - 1) * 100 for k in w.index
                  if k in p0 and k in p1 and p0[k] > 0]
            return float(np.mean(ok)) if ok else np.nan

        r_port, r_bench = ret_of(port), ret_of(bench)
        # 直接买基金
        n0 = fund_nav_at(cal, b, sel, fcache if False else {})
        n1 = fund_nav_at(cal, s, sel, {})
        fr = [((n1[k] / n0[k]) - 1) * 100 for k in sel if k in n0 and k in n1 and n0[k] > 0]
        r_fund = float(np.mean(fr)) if fr else np.nan

        # 仓位对齐：组合是 100% 股票，基金只有 beta_total% 的股票暴露。
        # 不放大就直接比，等于用满仓去打半仓 —— 牛市必胜、熊市必败，测的是仓位不是选股。
        r_expo = r_port * (expo / 100) if (expo := expo_of(p, sel)) else np.nan

        rows.append({"period": p, "build": b, "sell": s, "n_funds": len(sel),
                     "n_pool": n_pool, "port": r_port, "bench": r_bench,
                     "fund": r_fund, "expo": expo, "port_matched": r_expo,
                     "n_stock": len(port), "w_sum": float(port.sum() * 100)})
        print(f"  {p}→{nxt}  抄持仓 {r_port:7.2f}%  抄全市场 {r_bench:7.2f}%  "
              f"买基金 {r_fund:7.2f}%")

    D = pd.DataFrame(rows).dropna(subset=["port", "bench"])
    if not len(D):
        print("无有效样本")
        return
    D["ex_vs_bench"] = D.port - D.bench
    D["ex_vs_fund"] = D.port - D.fund
    D["ex_fund_vs_bench"] = D.fund - D.bench

    def summ(s, cost=0.0):
        a = (s - cost).values
        tv, pv, n = tstat(a)
        per_year = 2  # 每期 6 个月
        ann = (np.prod(1 + a / 100) ** (per_year / len(a)) - 1) * 100
        return {"mean": round(float(a.mean()), 2), "t": round(float(tv), 2),
                "p": round(float(pv), 3), "pos": int((a > 0).sum()), "n": int(n),
                "ann": round(float(ann), 2),
                "median": round(float(np.median(a)), 2),
                "min": round(float(a.min()), 2), "max": round(float(a.max()), 2)}

    # 稳健性：剔除最大一期后还剩多少（防止结论被单期绑架）
    def loo(s):
        a = np.asarray(s, dtype=float)
        i = int(np.argmax(np.abs(a)))
        rest = np.delete(a, i)
        tv, _, _ = tstat(rest)
        return {"dropped": D.period.iloc[i], "dropped_val": round(float(a[i]), 2),
                "mean": round(float(rest.mean()), 2), "t": round(float(tv), 2),
                "pos": int((rest > 0).sum()), "n": len(rest)}

    res = {
        "periods": len(D), "span": f"{D.period.iloc[0]}~{D.period.iloc[-1]}",
        "avail_range": f"{D.build.iloc[0]}~{D.sell.iloc[-1]}",
        "stock_cost": STOCK_COST, "fund_cost": FUND_COST, "topn": TOPN, "lag_days": LAG_DAYS,
        "port_gross": summ(D.port), "port_net": summ(D.port, STOCK_COST),
        "bench_gross": summ(D.bench), "bench_net": summ(D.bench, STOCK_COST),
        "fund_net": summ(D.fund, FUND_COST), "fund_gross": summ(D.fund),
        "sel_effect": summ(D.port - D.bench, 0),
        "selection_loo": loo(D.port - D.bench),
        "ex_fund_net": summ(D.port - D.fund, 0),
        "ex_fund_matched": summ(D.port_matched - D.fund, 0),
        "expo_mean": round(float(D.expo.mean()), 1),
        "beta_vs_fund": round(float(np.polyfit(D.fund.values, D.port.values, 1)[0]), 2),
        "corr_vs_fund": round(float(np.corrcoef(D.fund.values, D.port.values)[0, 1]), 3),
        "yearly": [{"period": r.period, "port": round(r.port, 2), "bench": round(r.bench, 2),
                    "fund": round(r.fund, 2), "exc": round(r.port - r.bench, 2),
                    "expo": round(r.expo, 1), "n": int(r.n_funds)} for r in D.itertuples()],
        "avg_stocks": round(float(D.n_stock.mean()), 1),
        "avg_funds": round(float(D.n_funds.mean()), 1),
        "avg_pool": round(float(D.n_pool.mean()), 1),
    }

    print("\n" + "=" * 76)
    print("【抄隐含持仓 · 6 个月持有期】")
    print("=" * 76)
    for k, lab in [("port_gross", "抄 Top5% 持仓(毛)"), ("port_net", "抄 Top5% 持仓(净)"),
                   ("bench_net", "抄全市场持仓(净)"), ("fund_net", "直接买 Top5% 基金(净)")]:
        v = res[k]
        print(f"  {lab:<22} 均值 {v['mean']:+7.2f}%/期  t={v['t']:+6.2f}  "
              f"为正 {v['pos']}/{v['n']}  年化 {v['ann']:+6.2f}%")
    print(f"\n  【选股效应】抄Top5% − 抄全市场   {res['sel_effect']['mean']:+7.2f}%/期  "
          f"t={res['sel_effect']['t']:+6.2f}  为正 {res['sel_effect']['pos']}/{res['sel_effect']['n']}  "
          f"p={res['sel_effect']['p']:.3f}")
    lo = res["selection_loo"]
    print(f"     剔除最大期 {lo['dropped']}（{lo['dropped_val']:+.1f}%）后 "
          f"{lo['mean']:+.2f}%/期 t={lo['t']:+.2f} 为正 {lo['pos']}/{lo['n']}")
    d = res["ex_fund_net"]
    print(f"\n  【抄 vs 买】不做仓位对齐        {d['mean']:+7.2f}%/期  t={d['t']:+6.2f}  "
          f"为正 {d['pos']}/{d['n']}")
    m = res["ex_fund_matched"]
    print(f"  【抄 vs 买】按股票暴露对齐后    {m['mean']:+7.2f}%/期  t={m['t']:+6.2f}  "
          f"为正 {m['pos']}/{m['n']}")
    print(f"  选中基金平均股票暴露 {res['expo_mean']}% | 组合作为基金的比例 β={res['beta_vs_fund']} "
          f"相关 {res['corr_vs_fund']}")
    (OUT / "res_copy_holdings.json").write_text(json.dumps(res, ensure_ascii=False, indent=1),
                                                encoding="utf-8")
    D.to_csv(OUT / "res_copy_holdings_periods.csv", index=False)
    print(f"\n平均持股 {res['avg_stocks']} 只 / 平均选中基金 {res['avg_funds']} 只 "
          f"/ 平均可选池 {res['avg_pool']} 只")


if __name__ == "__main__":
    main()
