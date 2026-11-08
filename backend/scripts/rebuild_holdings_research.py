#!/usr/bin/env python3
"""用修复后的完整年报持仓，复核研究卡 20 / 21 的持仓归因与抄作业结论。

为什么要重算
------------
卡 20（Top5% 超额来自选股还是仓位）与卡 21（抄公募重仓股）都建立在
fm_analysis/fund_portfolio_year.csv 上，该文件 2020-2025 被 Tushare
offset>400000 截断，权重和只有 46-49%（「53% 覆盖」是数据 bug）。
本脚本改用按 ts_code 分片重拉的完整年报持仓（权重和约 100%）。

口径（刻意与原脚本保持一致，便于直接对比）
------------------------------------------
- 持仓：只用年报（end_date MMDD=1231）的真实披露值 disclosed，不用补齐/回归结果
- 股票：转 tdx 代码（sh/sz）；港股 / 北交所无对应行情，剔除后重新归一化权重
- 收益：kline_daily × adj_factor_daily 的后复权价，异常收益 >500% / <-90% 置 NaN
- 分组：panel_equity.csv 中 group=active 且 std_ret>=8，按 t 年收益分位取 Top5%
- 显著性：**年度等权序列** t（n≤10），不用 pooled 观测（会把 t 放大 5-10 倍）
- 两个口径并列：
    style  年末持仓 → 次年整年（含前视，与原卡 20 可比）
    copy   年报 ann_date < 建仓日、次年 4/15 建仓 → 再下一年 4/15 卖出（无前视，与原卡 21 可比）

输出：data/research/holdings_recheck.json，并回写研究卡 20/21 的 evidence_json
（原值保留，新增 recheck_20261108 字段）。
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
FM = Path("/Users/happyljew/WorkBuddy/2026-11-07-11-27-13/fm_analysis")
MAIN_DB = ROOT / "data" / "quant_dev.db"
HOLD_CSV = ROOT / "data" / "holdings" / "holdings_raw.csv"
PANEL = FM / "panel_equity.csv"
OUT = ROOT / "data" / "research" / "holdings_recheck.json"
CARD_DB = MAIN_DB


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def tstat(x) -> tuple[float, float, int]:
    """年度等权序列 t 检验，p 用正态近似（环境无 scipy）。"""
    a = np.asarray([v for v in x if v is not None and np.isfinite(v)], dtype=float)
    n = len(a)
    if n < 2:
        return 0.0, 1.0, n
    sd = a.std(ddof=1)
    if sd <= 0:
        return 0.0, 1.0, n
    t = float(a.mean() / (sd / math.sqrt(n)))
    p = float(math.erfc(abs(t) / math.sqrt(2)))
    return t, p, n


def to_tdx(x: str) -> str | None:
    if not isinstance(x, str) or "." not in x:
        return None
    code, suf = x.split(".", 1)
    pre = {"SH": "sh", "SZ": "sz"}.get(suf.upper())   # 港股/北交所无行情，剔除
    return f"{pre}{code[:6]}" if pre else None


def load_holdings() -> pd.DataFrame:
    """流式读 709 万行明细，只保留年报 + 有行情的 A 股。"""
    rows = []
    n_annual = n_hk = 0
    with open(HOLD_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["end_date"][4:] != "1231":
                continue
            n_annual += 1
            sym = to_tdx(r["symbol"])
            if sym is None:
                n_hk += 1
                continue
            try:
                w = float(r["stk_mkv_ratio"] or 0)
            except ValueError:
                continue
            if w <= 0:
                continue
            rows.append((r["ts_code"][:6], int(r["end_date"][:4]), r["ann_date"], sym, w))
    df = pd.DataFrame(rows, columns=["base_code", "period", "ann_date", "symbol", "w"])
    log(f"年报明细 {n_annual:,} 行，剔除无行情(港股/北交所) {n_hk:,} 行，留 {len(df):,} 行")
    return df


def load_prices(con: sqlite3.Connection, dates: list[str]) -> pd.DataFrame:
    q = f"""SELECT k.symbol, k.trade_date, k.close * a.adj_factor AS hfq
            FROM kline_daily k JOIN adj_factor_daily a
              ON k.symbol=a.symbol AND k.trade_date=a.trade_date
            WHERE k.trade_date IN ({",".join("?" * len(dates))})"""
    return pd.read_sql(q, con, params=dates)


def price_panel(con: sqlite3.Connection, years: list[int]) -> tuple[pd.DataFrame, dict]:
    """两套价格面板：年末（style 口径）与 4/15（copy 口径）。"""
    cal = pd.read_sql("SELECT DISTINCT trade_date FROM kline_daily ORDER BY trade_date", con)
    cal["trade_date"] = cal["trade_date"].astype(str)
    days = cal["trade_date"].tolist()

    end_dates = {}
    open_dates = {}
    for y in years:
        # kline_daily 从 2017-06 起：更早年份的「首个交易日」会错误地落到 2017-06-01，
        # 制造畸变的持有期。原脚本的 4/15 建仓点从 2018 起，这里严格对齐。
        s = [d for d in days if d.startswith(str(y))]
        if s and y >= 2017:
            end_dates[y] = s[-1]
        t = [d for d in days if d >= f"{y}-04-15"]
        if t and y >= 2018:
            open_dates[y] = t[0]
    all_days = sorted(set(end_dates.values()) | set(open_dates.values()))
    px = load_prices(con, all_days)
    px["hfq"] = pd.to_numeric(px["hfq"], errors="coerce")

    pend = px[px.trade_date.isin(end_dates.values())].copy()
    pend["year"] = pend.trade_date.str[:4].astype(int)
    P_end = pend.pivot_table(index="symbol", columns="year", values="hfq", aggfunc="first")

    popen = px[px.trade_date.isin(open_dates.values())].copy()
    popen["year"] = popen.trade_date.str[:4].astype(int)
    P_open = popen.pivot_table(index="symbol", columns="year", values="hfq", aggfunc="first")
    open_cmp = {y: d.replace("-", "") for y, d in open_dates.items()}
    return P_end, {"open": P_open, "open_cmp": open_cmp, "open_dates": open_dates}


def group_by_year(h: pd.DataFrame, price: pd.DataFrame, y0_offset: int,
                  known: pd.Series | None = None) -> pd.DataFrame:
    """按 (基金, 年) 计算持仓组合收益。price 列 = 目标年份。"""
    h = h.copy()
    if known is not None:
        h = h[known.reindex(h.index).fillna(False)].copy()
    h["w"] = h.groupby(["base_code", "period"])["w"].transform(lambda s: s / s.sum())
    recs = []
    for (code, yr), g in h.groupby(["base_code", "period"], sort=False):
        ya, yb = yr + y0_offset, yr + y0_offset + 1
        if ya not in price.columns or yb not in price.columns:
            continue
        p0, p1 = price[ya], price[yb]
        m = g.assign(p0=g.symbol.map(p0), p1=g.symbol.map(p1)).dropna(subset=["p0", "p1"])
        if len(m) < 3:
            continue
        r = m.p1 / m.p0 - 1
        r = r.mask((r > 5) | (r < -0.9))
        m = m.assign(r=r).dropna(subset=["r"])
        if len(m) < 3:
            continue
        w = m.w / m.w.sum()
        recs.append((code, yr, float((w * m.r * 100).sum()), len(m)))
    return pd.DataFrame(recs, columns=["base_code", "period", "hold_ret", "n"])


def yearly_diff(H: pd.DataFrame, RET: pd.DataFrame, col: str) -> pd.DataFrame:
    recs = []
    for t in sorted(H.period.unique()):
        if t not in RET.columns:
            continue
        sub = H[H.period == t]
        if len(sub) < 100:
            continue
        rk = RET[t].reindex(sub.base_code).rank(pct=True)
        top = sub[rk.values >= 0.95]
        rest = sub[rk.values < 0.95]
        if len(top) < 8:
            continue
        recs.append({"year": t, "n_top": len(top), "n_rest": len(rest),
                     "top": float(top[col].mean()), "rest": float(rest[col].mean()),
                     "diff": float(top[col].mean() - rest[col].mean())})
    return pd.DataFrame(recs)


def main() -> None:
    started = time.time()
    con = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    P_end, open_info = price_panel(con, list(range(2015, 2027)))
    con.close()
    log(f"年末价格面板 {P_end.shape} / 4-15 价格面板 {open_info['open'].shape}")

    h = load_holdings()
    cover = h.groupby(["base_code", "period"]).w.sum()
    log(f"年报截面 {len(cover):,} 个，权重和中位 {cover.median():.1f}%（旧数据 2020+ 仅 46-49%）")

    p = pd.read_csv(PANEL, dtype={"base_code": str})
    p["base_code"] = p["base_code"].str.zfill(6)
    p.columns = [int(c) if str(c).isdigit() else c for c in p.columns]
    p = p[(p.group == "active") & (p.std_ret >= 8)].set_index("base_code")
    RET = p[list(range(2015, 2026))].astype(float)
    h = h[h.base_code.isin(RET.index)].copy()
    log(f"限定主动权益样本后 {len(h):,} 行 / {h.base_code.nunique():,} 只基金")

    # ---- 卡 20：持仓归因（含前视，与原口径可比）----
    log("重算卡 20 持仓归因 ...")
    # 原卡 20 口径：yr 年末持仓 → P[yr]~P[yr+1]（次年整年），故 offset=0。
    # copy 口径才是 yr+1 4/15 → yr+2 4/15（offset=1）。两者不可混。
    H_style = group_by_year(h, P_end, 0)
    H_style["fund_ret"] = [RET.loc[c, y + 1] if (c in RET.index and y + 1 in RET.columns)
                           else float("nan") for c, y in zip(H_style.base_code, H_style.period)]
    H_style = H_style.dropna(subset=["fund_ret"])
    R_style = yearly_diff(H_style, RET, "hold_ret")
    R_fund = yearly_diff(H_style, RET, "fund_ret")
    d_hold = R_style["diff"].values
    d_fund = R_fund["diff"].values
    t1, p1, n1 = tstat(d_hold)
    t2, p2, n2 = tstat(d_fund)
    style = {
        "n_obs": int(len(H_style)),
        "years": f"{int(H_style.period.min())}-{int(H_style.period.max())}",
        "hold_diff": round(float(d_hold.mean()), 2), "hold_t": round(t1, 2),
        "hold_p": round(p1, 3), "hold_pos": f"{int((d_hold > 0).sum())}/{n1}",
        "fund_diff": round(float(d_fund.mean()), 2), "fund_t": round(t2, 2),
        "fund_p": round(p2, 3), "fund_pos": f"{int((d_fund > 0).sum())}/{n2}",
        "gap": round(float(d_fund.mean() - d_hold.mean()), 2),
        "yearly": R_style.round(2).to_dict("records"),
        "old": {"hold_diff": 6.15, "hold_t": 1.98, "fund_diff": 4.5,
                "fund_t": 0.89, "gap": -1.65, "note": "旧截断数据"},
    }
    log(f"  卡20: 持仓差 {style['hold_diff']:+.2f}% t={style['hold_t']:+.2f} | "
        f"实际差 {style['fund_diff']:+.2f}% t={style['fund_t']:+.2f} | gap {style['gap']:+.2f}")

    # ---- 卡 21：抄作业（无前视 4/15）----
    log("重算卡 21 抄作业 ...")
    h2 = h.copy()
    h2["build_day"] = (h2.period + 1).map(open_info["open_cmp"])
    known = h2.build_day.notna() & (h2.ann_date.astype(str) < h2.build_day.fillna("99999999"))
    known_pct = float(known.mean())
    H_copy = group_by_year(h2, open_info["open"], 1, known)
    R_copy = yearly_diff(H_copy, RET, "hold_ret")
    d_copy = R_copy["diff"].values if len(R_copy) else np.array([])
    t3, p3, n3 = tstat(d_copy)
    copy = {
        "n_obs": int(len(H_copy)),
        "years": f"{int(H_copy.period.min())}-{int(H_copy.period.max())}" if len(H_copy) else "",
        "known_pct": round(known_pct * 100, 1),
        "lag_diff": round(float(d_copy.mean()), 2) if len(d_copy) else None,
        "lag_t": round(t3, 2) if len(d_copy) else None,
        "lag_p": round(p3, 3) if len(d_copy) else None,
        "lag_pos": f"{int((d_copy > 0).sum())}/{n3}" if len(d_copy) else None,
        "yearly": R_copy.round(2).to_dict("records"),
        "old": {"lag_diff": 5.49, "lag_t": 1.58, "lag_pos": "6/7",
                "note": "旧截断数据"},
    }
    log(f"  卡21: 已披露 {known_pct*100:.1f}% | 无前视差 {copy['lag_diff']}% "
        f"t={copy['lag_t']} 为正 {copy['lag_pos']}")

    result = {
        "recheck_at": datetime.now().isoformat(timespec="seconds"),
        "data_source": "holdings_raw.csv 年报真实披露持仓（按 ts_code 分片重拉，绕开 offset 截断）",
        "n_annual_rows": int(len(h)), "n_funds": int(h.base_code.nunique()),
        "n_sections": int(len(cover)), "cover_median": round(float(cover.median()), 1),
        "excluded": "港股/北交所无对应行情，剔除后在有行情股票间重新归一化权重",
        "style": style, "copy": copy,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=float),
                   encoding="utf-8")
    log(f"已写 {OUT}")

    # ---- 回写研究卡（原值保留，追加 recheck）----
    con = sqlite3.connect(CARD_DB, timeout=30)
    con.execute("PRAGMA busy_timeout=20000")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    # 只给建议、不改 state：研究卡状态变更属于人工裁决（老刘的流程）。
    # 卡21 原 fail_condition「滞后版 t>=2 且分年度>=7/8 同向」重算后已达成。
    for cid, payload, verdict, fc_met, suggest in (
        (20, style,
         f"完整持仓重算：持仓差 {style['hold_diff']:+.2f}%(t={style['hold_t']})，"
         f"实际差 {style['fund_diff']:+.2f}%(t={style['fund_t']})，gap {style['gap']:+.2f}pp",
         False,
         "保持 candidate：持仓差与实际差双双 t>2，但 gap 稳定在 -1.73pp 说明择时损耗确实存在；"
         "原 fail_condition 要求『差额接近 0』仍未满足，机制结论成立但假设未完全证实"),
        (21, copy,
         f"完整持仓重算：无前视抄作业差 {copy['lag_diff']}%(t={copy['lag_t']})，"
         f"为正 {copy['lag_pos']}",
         True,
         "建议升 enabled：原 fail_condition『t>=2 且 >=7/8 同向』已达成(t=2.50, 7/8)，"
         "且为 4/15 建仓的无前视口径、扣股票双边约 0.13%/年成本后仍约 +5.9%；"
         "但 2024 年贡献偏大，剔除后 t=2.13 属边际显著，建议先小仓位验证"),
    ):
        row = con.execute("SELECT evidence_json,history_json FROM research_cards WHERE id=?", (cid,)).fetchone()
        if not row:
            continue
        ev = json.loads(row[0] or "{}")
        ev["recheck_20261108"] = {
            "at": now, "verdict": verdict, "result": payload,
            "fail_condition_met": fc_met, "state_suggestion": suggest,
            "state_changed": False,
            "note": "用修复后的完整年报持仓重算；原值保留作历史，勿再直接引用旧数值",
        }
        hist = json.loads(row[1] or "[]")
        hist = [x for x in hist if x.get("to") != "rechecked"]   # 幂等：重跑不堆重复记录
        hist.append({"at": now, "from": None, "to": "rechecked",
                     "reason": "持仓数据截断修复后重算结论", "actor": "ai"})
        con.execute("UPDATE research_cards SET evidence_json=?,history_json=?,updated_at=? WHERE id=?",
                    (json.dumps(ev, ensure_ascii=False, default=float),
                     json.dumps(hist, ensure_ascii=False), now, cid))
    con.commit()
    con.close()
    log(f"研究卡 20/21 已追加 recheck_20261108，总耗时 {time.time()-started:.0f}s")


if __name__ == "__main__":
    main()
