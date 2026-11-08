#!/usr/bin/env python3
"""基金持续性统计 → fund_stat_cache（页面 /api/v1/fund/persistence 的数据源）。

为什么要这个脚本
----------------
「基金经理持续性」研究要的是**年度**复权净值（2015-2025 每年末一个截面），
而平台的 `fund_nav` 表是按日存近期净值（sync_nav 只回看几天），装不下 11 年历史；
`fund_basic` 的 sync_basic 默认 `market="E"`，只覆盖 3 千余只场内基金，主动权益（场外 O）不在内。
所以这里维护三张年度/任期粒度的表作补充：

    fund_master           基金主数据（含分组 tag：active / enh_quant / passive）
    fund_nav_year         年末复权净值（每只每年一行）
    fund_manager_tenure   基金-年度 → 主经理归属（solo / shared）

**不替代** fund_basic / fund_nav —— 那两张是日更近期数据，给实时场景用；
这三张是研究用的历史面板，按年更新即可。

用法（backend/ 下）
------------------
    PYTHONPATH=$(pwd) python scripts/build_fund_stats.py            # 只重算统计（读现有三张表）
    PYTHONPATH=$(pwd) python scripts/build_fund_stats.py --sync     # 先从 tushare 补数据再算

口径（不可随便改，改了所有结论都会漂）
------------------------------------
- 收益用 `adj_nav`（分红再投），年末截面相除；>300% / <-80% 视为份额折算造成的假跳变，置 NaN
- 分组按**上年**收益分位；超额 = 次年收益 − 次年**全体**中位数（不是子集中位数）
- 显著性用**年度等权序列**（n≤10）做 t 检验。同一基金跨年重复出现，用 pooled 观测会把 t 放大 5-10 倍
- 主动权益 = 股票型+混合型，剔除名称含「指数/ETF/联接」且不含「增强」的纯被动产品
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, init_db  # noqa: E402

YRS = list(range(2015, 2026))
TEN = list(range(2016, 2026))
BINS = [("top5", 0.95, 1.01), ("p5_10", 0.90, 0.95), ("p10_30", 0.70, 0.90),
        ("p30_50", 0.50, 0.70), ("p50_70", 0.30, 0.50), ("bot30", -0.01, 0.30)]
NAMES = [b[0] for b in BINS]
LABEL = {"top5": "前5%", "p5_10": "5-10%", "p10_30": "10-30%",
         "p30_50": "30-50%", "p50_70": "50-70%", "bot30": "后30%"}


# ---------- 统计工具（不依赖 scipy）----------
def _betacf(a: float, b: float, x: float, maxit: int = 300, eps: float = 3e-13) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > 1e-30 else 1e-30)
    h = d
    for m in range(1, maxit + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1.0 + aa / c
        c = c if abs(c) > 1e-30 else 1e-30
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1.0 + aa / c
        c = c if abs(c) > 1e-30 else 1e-30
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def _p_two_sided(t: float, df: int) -> float:
    if not np.isfinite(t) or df <= 0:
        return float("nan")
    x = df / (df + t * t)
    lb = math.lgamma(df / 2 + 0.5) - math.lgamma(df / 2) - math.lgamma(0.5)
    front = math.exp(lb + (df / 2) * math.log(x) + 0.5 * math.log(1 - x)) if 0 < x < 1 else 0.0
    if x < (df / 2 + 1) / (df / 2 + 0.5 + 2):
        p = front * _betacf(df / 2, 0.5, x) / (df / 2)
    else:
        p = 1.0 - front * _betacf(0.5, df / 2, 1 - x) / 0.5
    return float(min(1.0, max(0.0, p)))


def tstat(x) -> tuple[float, float, int]:
    s = pd.Series(x).dropna()
    n = len(s)
    if n < 3:
        return float("nan"), float("nan"), n
    sd = s.std(ddof=1)
    if not sd or np.isnan(sd):
        return float("nan"), float("nan"), n
    t = s.mean() / (sd / math.sqrt(n))
    return float(t), _p_two_sided(t, n - 1), n


def tier_of_rank(r: float) -> str:
    for name, lo, hi in BINS:
        if lo <= r < hi:
            return name
    return "bot30"


# ---------- 数据同步（可选）----------
def sync_from_tushare(db) -> None:
    """补 fund_master / fund_nav_year / fund_manager_tenure（一次性，按年更新）。"""
    import time

    import tushare as ts

    from app.config import settings

    token = os.environ.get("TUSHARE_TOKEN") or getattr(settings, "tushare_token", None)
    if not token:
        raise SystemExit("缺 TUSHARE_TOKEN，无法同步")
    pro = ts.pro_api(token)

    print("[1/3] fund_basic（offset 分页，单次上限 15000）")
    frames, off = [], 0
    while True:
        df = pro.fund_basic(market="O", offset=off)
        if df is None or len(df) == 0:
            break
        frames.append(df)
        if len(df) < 15000:
            break
        off += 15000
        time.sleep(0.5)
    basic = pd.concat(frames, ignore_index=True).drop_duplicates("ts_code")
    print(f"  {len(basic)} 只场外基金")

    print("[2/3] 年末复权净值截面")
    cal = pro.index_daily(ts_code="000300.SH", start_date="20141201", end_date="20261231")
    cal["trade_date"] = cal["trade_date"].astype(str)
    cal = cal.sort_values("trade_date")
    year_end = {}
    for d in cal["trade_date"]:
        year_end[d[:4]] = d
    navs = []
    for y, d in sorted(year_end.items()):
        off = 0
        while True:
            df = pro.fund_nav(nav_date=d, market="O", offset=off)
            if df is None or len(df) == 0:
                break
            df["year"] = int(y)
            navs.append(df)
            if len(df) < 10000:
                break
            off += len(df)
            time.sleep(0.3)
        print(f"  {y} {d} ok")
    nav = pd.concat(navs, ignore_index=True)
    nav["base_code"] = nav["ts_code"].str.extract(r"^(\d{6})")[0]
    nav = nav.sort_values(["base_code", "year", "ts_code"]).drop_duplicates(["base_code", "year"])

    print("[3/3] fund_manager")
    mgrs, off = [], 0
    while True:
        df = pro.fund_manager(offset=off)
        if df is None or len(df) == 0:
            break
        mgrs.append(df)
        if len(df) < 5000:
            break
        off += 5000
        time.sleep(0.5)
    mgr = pd.concat(mgrs, ignore_index=True).drop_duplicates()

    # ---- 写 fund_master ----
    b = basic.copy()
    b["base_code"] = b["ts_code"].str.extract(r"^(\d{6})")[0]
    b = b.drop_duplicates("base_code")
    name = b["name"].fillna("")
    it = b["invest_type"].fillna("")
    is_index = name.str.contains("指数|ETF|联接")
    is_enh = name.str.contains("增强") | it.str.contains("增强")
    is_quant = name.str.contains("量化|多因子|对冲|阿尔法")
    is_passive = is_index & ~is_enh
    grp = pd.Series("active", index=b.index)
    grp[(is_index & is_enh) | is_quant] = "enh_quant"
    grp[is_passive] = "passive"
    b["group_tag"] = grp

    rows = [{"base_code": r.base_code, "ts_code": r.ts_code, "name": (r["name"] or "")[:128],
             "management": (r.management if isinstance(r.management, str) else "")[:64],
             "fund_type": r.fund_type, "invest_type": r.invest_type,
             "found_date": r.found_date if isinstance(r.found_date, str) else None,
             "status": r.status, "group_tag": r.group_tag,
             "updated_at": datetime.now().isoformat(timespec="seconds")}
            for _, r in b.iterrows()]
    db.execute(text("DELETE FROM fund_master"))
    db.execute(text("""INSERT INTO fund_master
        (base_code, ts_code, name, management, fund_type, invest_type, found_date, status, group_tag, updated_at)
        VALUES (:base_code,:ts_code,:name,:management,:fund_type,:invest_type,:found_date,:status,:group_tag,:updated_at)"""), rows)
    db.commit()
    print(f"  fund_master: {len(rows)} 行")

    # ---- 写 fund_nav_year ----
    n = nav[["base_code", "year", "adj_nav", "unit_nav", "net_asset"]].copy()
    n = n[n.base_code.notna()]
    db.execute(text("DELETE FROM fund_nav_year"))
    db.execute(text("""INSERT INTO fund_nav_year (base_code, year, adj_nav, unit_nav, net_asset)
        VALUES (:base_code,:year,:adj_nav,:unit_nav,:net_asset)"""),
               n.replace({np.nan: None}).to_dict("records"))
    db.commit()
    print(f"  fund_nav_year: {len(n)} 行")

    # ---- 写 fund_manager_tenure ----
    m = mgr.copy()
    m["base_code"] = m["ts_code"].str.extract(r"^(\d{6})")[0]
    m["begin"] = pd.to_datetime(m["begin_date"], errors="coerce")
    m["end"] = pd.to_datetime(m["end_date"].astype(str).str.strip().replace({"": None, "NaT": None, "nan": None}), errors="coerce")
    m = m.merge(b[["base_code", "management"]], on="base_code", how="left")
    recs = []
    for y in YRS + [2026]:
        ys, ye = pd.Timestamp(f"{y}-01-01"), pd.Timestamp(f"{y}-12-31")
        days = (ye - ys).days
        sub = m[(m.begin.notna()) & (m.begin <= ye) & (m.end.isna() | (m.end >= ys))]
        if sub.empty:
            continue
        sub = sub.assign(s=sub.begin.clip(lower=ys), e=sub.end.fillna(ye).clip(upper=ye))
        sub = sub.assign(d=(sub.e - sub.s).dt.days.clip(lower=0))
        sub = sub[sub.d > 0]
        for code, g in sub.groupby("base_code"):
            g = g.sort_values("d", ascending=False)
            top = g.iloc[0]
            share = top.d / days
            second = (g.iloc[1].d / days) if len(g) > 1 else 0.0
            recs.append({"base_code": code, "year": y, "manager": top["name"],
                         "company": top["management"] if isinstance(top["management"], str) else "",
                         "manager_id": f'{top["name"]}|{top["management"] if isinstance(top["management"], str) else ""}',
                         "share": round(float(share), 3), "n_mgr": len(g),
                         "attr": "solo" if (share >= 0.7 and second < 0.3) else "shared"})
    db.execute(text("DELETE FROM fund_manager_tenure"))
    db.execute(text("""INSERT INTO fund_manager_tenure
        (base_code, year, manager, company, manager_id, share, n_mgr, attr)
        VALUES (:base_code,:year,:manager,:company,:manager_id,:share,:n_mgr,:attr)"""), recs)
    db.commit()
    print(f"  fund_manager_tenure: {len(recs)} 行")


# ---------- 统计 ----------
def build_stats(db) -> dict:
    ens = db.bind
    with ens.connect() as conn:
        fm = pd.read_sql(text("select base_code, group_tag, std_ret from fund_master"), conn,
                         dtype={"base_code": str})
        # 净值来源：日频折叠表优先（scripts/build_nav_year_from_daily.py 生成）。
        # 与旧年截面表在重叠区偏差为 0，换源不会改变任何年度结论；区别只在于
        # 日频表能同时给出月度面板（fund_nav_month_d），支撑更高分辨率的检验。
        try:
            nv = pd.read_sql(text("select base_code, year, adj_nav from fund_nav_year_d"), conn,
                             dtype={"base_code": str})
            src = "fund_nav_daily"
        except Exception:
            nv = pd.read_sql(text("select base_code, year, adj_nav from fund_nav_year"), conn,
                             dtype={"base_code": str})
            src = "fund_nav_year"
        print(f"  净值来源: {src} ({len(nv):,} 行)")
        ten = pd.read_sql(text("select base_code, year, manager_id, attr from fund_manager_tenure"), conn,
                          dtype={"base_code": str})
    fm["base_code"] = fm["base_code"].str.zfill(6)
    nv["base_code"] = nv["base_code"].str.zfill(6)
    ten["base_code"] = ten["base_code"].str.zfill(6)
    fm = fm[fm.group_tag.isin(["active", "enh_quant"])].drop_duplicates("base_code")

    piv = nv.pivot_table(index="base_code", columns="year", values="adj_nav", aggfunc="first")
    piv.columns = [int(c) for c in piv.columns]
    rets = piv.pct_change(axis=1) * 100.0
    rets = rets.iloc[:, 1:].mask(lambda x: (x > 300) | (x < -80))

    # 权益性确认：年度收益标准差 < 8% 视为偏债/保守型，剔除
    std = rets.std(axis=1)
    keep = fm.set_index("base_code").index.intersection(std[std >= 8].index)
    fm = fm[fm.base_code.isin(keep)]
    R = rets.reindex(fm.base_code.values)
    G = fm.set_index("base_code")["group_tag"].reindex(R.index)

    out = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "period": "2015-2025"}

    census = []
    for grp in ["active", "enh_quant"]:
        sub = R.loc[G == grp]
        base = sub[sub[2015].notna()]
        f10 = base[base[TEN].notna().sum(axis=1) == 10]
        if not len(f10):
            continue
        ann = ((1 + f10[TEN] / 100).prod(axis=1) ** 0.1 - 1) * 100
        census.append({"group": grp, "n_base": int(len(base)), "n_full10": int(len(f10)),
                       "ann_median": round(float(ann.median()), 2),
                       "ann_p90": round(float(ann.quantile(.9)), 2),
                       "ann_p99": round(float(ann.quantile(.99)), 2),
                       "ann_max": round(float(ann.max()), 2),
                       "n_ge20": int((ann >= 20).sum()), "n_ge15": int((ann >= 15).sum()),
                       "pct_ge20": round(float((ann >= 20).mean() * 100), 2)})
    out["census"] = census

    tiers, yearly = [], {}
    for grp in ["active", "enh_quant"]:
        Rg = R.loc[G == grp]
        for name in NAMES:
            arr, ys = [], []
            for t in range(2015, 2025):
                s_t, s_n = Rg[t], Rg[t + 1]
                ok = s_t.notna() & s_n.notna()
                if ok.sum() < 30:
                    continue
                tier = s_t[ok].rank(pct=True).map(tier_of_rank)
                e = (s_n[ok] - Rg[t + 1].median())[tier == name]
                if len(e) >= 10:
                    arr.append(e.mean())
                    ys.append({"year": t + 1, "exc": round(float(e.mean()), 2), "n": int(len(e))})
            if len(arr) < 5:
                continue
            a = np.array(arr)
            tv, pv, nn = tstat(a)
            tiers.append({"group": grp, "tier": name, "label": LABEL[name],
                          "mean": round(float(a.mean()), 2), "t": round(float(tv), 2),
                          "p": round(float(pv), 3), "sd": round(float(a.std(ddof=1)), 2),
                          "pos_years": int((a > 0).sum()), "n_years": int(nn)})
            if grp == "active":
                yearly[name] = ys
    out["tiers"] = tiers
    out["yearly"] = yearly

    Rg = R.loc[G == "active"]
    cnt = pd.DataFrame(0, index=NAMES, columns=NAMES, dtype=float)
    for t in range(2015, 2025):
        s_t, s_n = Rg[t], Rg[t + 1]
        ok = s_t.notna() & s_n.notna()
        if ok.sum() < 30:
            continue
        tt = s_t[ok].rank(pct=True).map(tier_of_rank)
        tn = s_n[ok].rank(pct=True).map(tier_of_rank)
        for a in NAMES:
            for b in NAMES:
                cnt.loc[a, b] += int(((tt == a) & (tn == b)).sum())
    mat = cnt.div(cnt.sum(axis=1), axis=0) * 100
    out["transition"] = {
        "keys": NAMES, "labels": [LABEL[n] for n in NAMES],
        "matrix": [[round(float(v), 1) for v in row] for row in mat.values],
        "diag": {n: round(float(mat.loc[n, n]), 1) for n in NAMES},
        "top5_to_bot30": round(float(mat.loc["top5", "bot30"]), 1),
        "bot30_stay": round(float(mat.loc["bot30", "bot30"]), 1),
    }

    # ---- 迁移概率随持有期的变化：1 / 3 / 12 个月 ----
    # 年度矩阵只有 10 个观测，"排名多久失效"这个问题在年度粒度上回答不了。
    # 月度面板有 131 期，可以把同一张矩阵在三个持有期上各算一遍，
    # 与「完全随机 = 1/6 = 16.7%」对照，看排名的信息量随期限如何衰减。
    try:
        with ens.connect() as conn:
            mv = pd.read_sql(text("select base_code, ym, adj_nav from fund_nav_month_d"), conn,
                             dtype={"base_code": str})
        mv["base_code"] = mv["base_code"].str.zfill(6)
        Mp = mv.pivot_table(index="base_code", columns="ym", values="adj_nav", aggfunc="last")
        Mp = Mp.sort_index(axis=1)
        Mr = Mp.pct_change(axis=1) * 100.0
        Mr = Mr.iloc[:, 1:]
        Mr = Mr.mask((Mr > 60) | (Mr < -40))
        Mr = Mr.reindex(R.index)          # 与年度样本同一批基金，避免口径漂移

        items = []
        for h, hname in [(1, "1 个月"), (3, "3 个月"), (12, "12 个月")]:
            cnt2 = pd.DataFrame(0, index=NAMES, columns=NAMES, dtype=float)
            pairs = 0
            cols = list(Mr.columns)
            for i in range(len(cols) - h):
                s0, s1 = Mr[cols[i]], Mr[cols[i + h]]
                ok = s0.notna() & s1.notna()
                if ok.sum() < 100:
                    continue
                t0 = s0[ok].rank(pct=True).map(tier_of_rank)
                t1 = s1[ok].rank(pct=True).map(tier_of_rank)
                for a in NAMES:
                    for b in NAMES:
                        cnt2.loc[a, b] += int(((t0 == a) & (t1 == b)).sum())
                pairs += 1
            if not pairs:
                continue
            m2 = cnt2.div(cnt2.sum(axis=1), axis=0) * 100
            items.append({
                "h": h, "name": hname, "n_pairs": pairs,
                "matrix": [[round(float(v), 1) for v in row] for row in m2.values],
                "diag_top5": round(float(m2.loc["top5", "top5"]), 1),
                "top5_to_bot30": round(float(m2.loc["top5", "bot30"]), 1),
                "bot30_stay": round(float(m2.loc["bot30", "bot30"]), 1),
                "avg_diag": round(float(np.mean([m2.loc[n, n] for n in NAMES])), 1),
                "diag_excess": round(float(np.mean([m2.loc[n, n] for n in NAMES]) - 100 / len(NAMES)), 1),
            })
        if items:
            out["transition_h"] = {"keys": NAMES, "labels": [LABEL[n] for n in NAMES],
                                   "items": items, "n_months": int(Mr.shape[1]),
                                   "random_baseline": round(100 / len(NAMES), 1)}
    except Exception as e:  # 月度表不存在时静默跳过
        print(f"  月度迁移跳过: {type(e).__name__} {e}")

    ten = ten[(ten.attr == "solo")].merge(fm[["base_code", "group_tag"]], on="base_code", how="inner")
    ten = ten[ten.group_tag == "active"]
    long = R.loc[G == "active"].reset_index().melt(id_vars="base_code", value_vars=YRS,
                                                   var_name="year", value_name="ret").dropna(subset=["ret"])
    long["year"] = long["year"].astype(int)
    long = long.merge(ten[["base_code", "year", "manager_id"]], on=["base_code", "year"], how="inner")
    mp = long.groupby(["manager_id", "year"])["ret"].median().reset_index()
    pivm = mp.pivot(index="manager_id", columns="year", values="ret")
    mgr_tiers = []
    for name in NAMES:
        arr = []
        for t in range(2015, 2025):
            if t not in pivm.columns or t + 1 not in pivm.columns:
                continue
            s_t, s_n = pivm[t], pivm[t + 1]
            ok = s_t.notna() & s_n.notna()
            if ok.sum() < 30:
                continue
            tier = s_t[ok].rank(pct=True).map(tier_of_rank)
            e = (s_n[ok] - pivm[t + 1].median())[tier == name]
            if len(e) >= 5:
                arr.append(e.mean())
        if len(arr) < 5:
            continue
        a = np.array(arr)
        tv, _, nn = tstat(a)
        mgr_tiers.append({"tier": name, "label": LABEL[name], "mean": round(float(a.mean()), 2),
                          "t": round(float(tv), 2), "sd": round(float(a.std(ddof=1)), 2),
                          "pos_years": int((a > 0).sum()), "n_years": int(nn)})
    out["manager_tiers"] = mgr_tiers
    out["manager_n"] = int(mp.manager_id.nunique())

    # ---- 规模拖累 ----
    with ens.connect() as conn:
        sc = pd.read_sql(text("select base_code, year, fd_share, scale_yi from fund_share_year"), conn,
                         dtype={"base_code": str})
    if len(sc):
        sc["base_code"] = sc["base_code"].str.zfill(6)
        sc["year"] = sc["year"].astype(int)
        Ra = R.loc[G == "active"]
        SZ = sc.pivot_table(index="base_code", columns="year", values="scale_yi",
                            aggfunc="first").reindex(Ra.index)
        SH = sc.pivot_table(index="base_code", columns="year", values="fd_share",
                            aggfunc="first").reindex(Ra.index)
        GS = SH.pct_change(axis=1) * 100
        GS = GS.mask((GS > 800) | (GS < -95))

        size_bands = []
        for lo, hi, label in [(0, 2, "<2亿"), (2, 10, "2-10亿"), (10, 30, "10-30亿"),
                              (30, 80, "30-80亿"), (80, 1e9, ">80亿")]:
            arr, pos = [], 0
            for t in range(2015, 2025):
                if t not in SZ.columns:
                    continue
                s_t, s_n = SZ[t], Ra[t + 1]
                ok = s_t.notna() & s_n.notna()
                if ok.sum() < 50:
                    continue
                msk = ok & (s_t >= lo) & (s_t < hi)
                if msk.sum() < 15:
                    continue
                e = s_n[msk] - Ra[t + 1].median()
                arr.append(e.mean())
                pos += int(e.mean() > 0)
            if len(arr) < 5:
                continue
            a = np.array(arr)
            tv, pv, nn = tstat(a)
            size_bands.append({"band": label, "mean": round(float(a.mean()), 2),
                               "t": round(float(tv), 2), "p": round(float(pv), 3),
                               "pos_years": pos, "n_years": int(nn),
                               "sd": round(float(a.std(ddof=1)), 2)})

        flow_bands = []
        for lo, hi, label in [(-1e9, -30, "净赎回>30%"), (-30, 0, "-30~0%"), (0, 30, "0~30%"),
                              (30, 100, "30~100%"), (100, 1e9, "翻倍以上")]:
            arr, pos = [], 0
            for t in range(2015, 2025):
                if t not in GS.columns:
                    continue
                g_t, s_n = GS[t], Ra[t + 1]
                ok = g_t.notna() & s_n.notna()
                if ok.sum() < 50:
                    continue
                msk = ok & (g_t >= lo) & (g_t < hi)
                if msk.sum() < 15:
                    continue
                e = s_n[msk] - Ra[t + 1].median()
                arr.append(e.mean())
                pos += int(e.mean() > 0)
            if len(arr) < 5:
                continue
            a = np.array(arr)
            tv, pv, nn = tstat(a)
            flow_bands.append({"band": label, "mean": round(float(a.mean()), 2),
                               "t": round(float(tv), 2), "p": round(float(pv), 3),
                               "pos_years": pos, "n_years": int(nn)})

        # 冠军组次年的份额增速（申购冲击是否真实存在）
        top_f, oth_f = [], []
        for t in range(2015, 2025):
            if t not in GS.columns:
                continue
            s_t, g_n = Ra[t], GS[t + 1]
            ok = s_t.notna() & g_n.notna()
            if ok.sum() < 80:
                continue
            rk = s_t[ok].rank(pct=True)
            top_f.append(g_n[ok][rk >= 0.95].median())
            oth_f.append(g_n[ok][rk < 0.95].median())
        flow_test = None
        if len(top_f) >= 5:
            a, b = np.array(top_f), np.array(oth_f)
            tv, pv, nn = tstat(a - b)
            flow_test = {"top5_median": round(float(np.median(a)), 1),
                         "others_median": round(float(np.median(b)), 1),
                         "diff_pp": round(float(np.mean(a - b)), 1),
                         "t": round(float(tv), 2), "p": round(float(pv), 3),
                         "pos_years": int(((a - b) > 0).sum()), "n_years": int(nn)}

        out["size"] = {"bands": size_bands, "flow_bands": flow_bands, "flow_test": flow_test}

        # 规模策略回测（全买，不抽样；成本 0.65%/年）
        COST = 0.0065
        pool = Ra.loc[Ra[2015].notna()].index
        bench = {y: Ra.loc[pool, y].fillna(0).mean() for y in range(2016, 2026)}
        bt = {}
        for label, lo, hi in [("2-10亿", 2, 10), (">80亿", 80, 1e9)]:
            eq, recs = 1.0, []
            for t in range(2015, 2025):
                if t not in SZ.columns:
                    continue
                s = SZ[t]
                cand = s[(s >= lo) & (s < hi)].index.intersection(Ra.index)
                if len(cand) < 5:
                    continue
                r = Ra.loc[cand, t + 1].fillna(0.0)
                net = r.mean() - COST * 100
                eq *= (1 + net / 100)
                recs.append({"year": t + 1, "exc": round(float(net - bench[t + 1]), 2), "n": len(cand)})
            if len(recs) >= 8:
                exc = np.array([x["exc"] for x in recs])
                tv, pv, nn = tstat(exc)
                bt[label] = {"ann": round(float((eq ** (1 / len(recs)) - 1) * 100), 2),
                             "excess": round(float(exc.mean()), 2), "t": round(float(tv), 2),
                             "p": round(float(pv), 3), "pos": int((exc > 0).sum()),
                             "tot": int(nn), "avg_n": round(float(np.mean([x["n"] for x in recs])), 0)}
        out["size_backtest"] = bt

    # ---- 持仓归因：Top5% 的超额来自持仓还是交易 ----
    with ens.connect() as conn:
        ha = pd.read_sql(text("select base_code, year, hold_ret, fund_ret, n_hold, cover from fund_hold_attr"),
                         conn, dtype={"base_code": str})
    if len(ha):
        ha["base_code"] = ha["base_code"].str.zfill(6)
        ha = ha[ha.base_code.isin(Ra.index)]
        rows_h = []
        for t in sorted(ha.year.unique()):
            if t not in Ra.columns:
                continue
            sub = ha[ha.year == t]
            if len(sub) < 100:
                continue
            rk = Ra[t].reindex(sub.base_code).rank(pct=True)
            top, rest = sub[rk.values >= 0.95], sub[rk.values < 0.95]
            if len(top) < 8:
                continue
            rows_h.append({"year": int(t + 1), "n_top": int(len(top)),
                           "top_hold": round(float(top.hold_ret.mean()), 2),
                           "rest_hold": round(float(rest.hold_ret.mean()), 2),
                           "top_fund": round(float(top.fund_ret.mean()), 2),
                           "rest_fund": round(float(rest.fund_ret.mean()), 2),
                           "cover": round(float(top.cover.mean()), 1)})
        if len(rows_h) >= 5:
            dh = np.array([x["top_hold"] - x["rest_hold"] for x in rows_h])
            df_ = np.array([x["top_fund"] - x["rest_fund"] for x in rows_h])
            th, ph, nh = tstat(dh)
            tf, pf2, nf = tstat(df_)
            out["hold_attr"] = {
                "yearly": rows_h,
                "hold_diff": round(float(dh.mean()), 2), "hold_t": round(float(th), 2),
                "hold_pos": int((dh > 0).sum()),
                "fund_diff": round(float(df_.mean()), 2), "fund_t": round(float(tf), 2),
                "fund_pos": int((df_ > 0).sum()),
                "gap": round(float(df_.mean() - dh.mean()), 2),
                "n_years": int(nh),
                "cover_median": round(float(ha.cover.median()), 1),
            }

    # ---- 无前视版：年报 3 月底才披露，改为次年 4/15 建仓 ----
    with ens.connect() as conn:
        hl = pd.read_sql(text("select base_code, year, lag_ret from fund_hold_attr where lag_ret is not null"),
                         conn, dtype={"base_code": str})
    if len(hl):
        hl["base_code"] = hl["base_code"].str.zfill(6)
        hl = hl[hl.base_code.isin(Ra.index)]
        rr = []
        for t in sorted(hl.year.unique()):
            if t not in Ra.columns:
                continue
            sub = hl[hl.year == t]
            if len(sub) < 100:
                continue
            rk = Ra[t].reindex(sub.base_code).rank(pct=True)
            top, rest = sub[rk.values >= 0.95], sub[rk.values < 0.95]
            if len(top) < 8:
                continue
            rr.append(float(top.lag_ret.mean() - rest.lag_ret.mean()))
        if len(rr) >= 5:
            a = np.array(rr)
            tv, pv, nn = tstat(a)
            out["hold_lag"] = {"diff": round(float(a.mean()), 2), "t": round(float(tv), 2),
                               "p": round(float(pv), 3), "pos": int((a > 0).sum()),
                               "n": int(nn),
                               "years": f"{int(hl.year.min()) + 1}-{int(hl.year.max()) + 1}"}
    return out


def main() -> int:
    init_db()
    db = SessionLocal()
    try:
        if "--sync" in sys.argv:
            sync_from_tushare(db)
        out = build_stats(db)
        db.execute(text("""CREATE TABLE IF NOT EXISTS fund_stat_cache
            (key TEXT PRIMARY KEY, payload_json TEXT, updated_at TEXT)"""))
        db.execute(text("INSERT OR REPLACE INTO fund_stat_cache VALUES ('persistence', :p, :u)"),
                   {"p": json.dumps(out, ensure_ascii=False), "u": out["generated_at"]})
        db.commit()
        print(f"fund_stat_cache 已更新：{len(out.get('tiers', []))} 个梯队, "
              f"{out.get('manager_n')} 位经理, 生成于 {out['generated_at']}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
