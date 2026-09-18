"""因子有效性检验：四池 IC 对照、年度衰减、分层单调性。

回答的核心问题
-------------
把股票池从沪深300（约 290 只）扩到全A（约 5500 只），
FCF/OE 与低波动这两个主因子还剩多少预测力？

实验设计：把「扩池」与「放开科创板」拆成两个可分离的变量
------------------------------------------------------
  沪深300      ← 基线（当前实盘口径）
  中证500      ← 已知失败参照（α 从 t=2.32 掉到 0.48）
  全A 非科创   ← 纯「扩池」效应
  全A 含科创   ← 「扩池 + 放开科创板」= 目标配置

  对照：全A非科创 vs 沪深300 → 池子效应
        全A含科创 vs 全A非科创 → 科创板效应

五条方法论（均为踩坑后固化，勿轻易改动）
--------------------------------------
1. **四池共用同一份价格数据**，只按成分快照过滤。
   否则池子差异会混入数据源差异，无法归因到池子本身。
2. **主口径锁定四池共同区间**（2022-01 起）。
   沪深300/中证500 的成分快照从 2021-11 才有，全A 却能追到 2019。
   若把全A 的 2019-2021 也拿来比，等于把两年大牛市混进对照，
   结论会被市场环境带偏而不是被池子带偏。
3. **收益标签用复权价算，不用 pct_chg**。
   pct_chg 基于未复权前收盘，除权日会出现人为跳变（实测最极端 −82%），
   且各板块分红率不同 → 系统性偏误方向不同 → 直接污染「池子效应」结论。
4. **打分因子必须在池内标准化**。
   曾用全市场标准化，导致「最终打分」的 IC 与策略实际口径不符
   —— 两个 z 值加权时，池内/全市场标准化的相对尺度不同，排序会变。
5. **必须看年度 IC**。「因子是否只在某一段有效」比均值更关键：
   低波动因子在沪深300 上就已衰减（t: 3.33 → 0.93 → −0.35 → 0.30）。
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from app.services import price

_MIN_STOCKS = 10      # 单期少于 10 只不算 IC（薄截面噪声太大）
_MIN_LAYER = 50       # 分层至少 50 只才能分 5 组

# 池子定义：index_code → (是否剔除科创板)
POOLS: dict[str, tuple[str, bool]] = {
    "沪深300": ("000300", False),
    "中证500": ("000905", False),
    "全A非科创": ("ALLA", True),
    "全A含科创": ("ALLA", False),
}

_INDEX_TABLE = {
    "000300": "000300", "000905": "000905",
    "000852": "000852", "000016": "000016", "ALLA": "ALLA",
}


# ============================ 池子 ============================
def load_pool_map(index_code: str, drop_kcb: bool,
                  con=None) -> dict[str, set[str]]:
    """读成分快照 → {trade_date: {symbol,...}}。drop_kcb 剔除 688 开头。"""
    own = con is None
    con = con or price.connect()
    try:
        df = con.execute(
            "select trade_date, symbol from index_membership where index_code = ?",
            [_INDEX_TABLE.get(index_code, index_code)]
        ).fetch_df()
    finally:
        if own:
            con.close()
    if df.empty:
        return {}
    df["trade_date"] = df["trade_date"].astype(str)
    if drop_kcb:
        df = df[~df["symbol"].str.contains("688", na=False)]
    return {t: set(g["symbol"]) for t, g in df.groupby("trade_date")}


def _pool_at(pool_map: dict[str, set[str]], t: str) -> set[str]:
    """取 t 日所属月份的成分快照（快照按月存，非月末日回退到当月）。"""
    if t in pool_map:
        return pool_map[t]
    same = [k for k in pool_map if k[:7] == t[:7]]
    return pool_map[max(same)] if same else set()


# ============================ 因子 ============================
def _zscore_row(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    """池内截面 z-score（逐行减均值除标准差，std=0 视为 NaN）。"""
    sub = df[cols]
    m = sub.mean(axis=1)
    s = sub.std(axis=1).replace(0, np.nan)
    z = sub.sub(m, axis=0).div(s, axis=0)
    return z.mean(axis=1)


def build_factors(C: pd.DataFrame, sd: str, ed: str, con=None) -> dict[str, pd.DataFrame]:
    """构造因子宽表（交易日×标的）。全部按无前视口径对齐。

    C 为复权收盘价矩阵（作为标的与交易日的基准轴）。
    """
    syms = list(C.columns)
    dates = [d for d in C.index]

    # —— 低波动：20 日收益率标准差取负（值越大越「低波动」）——
    vola = C.pct_change().rolling(20).std()
    lowvol = -vola

    # —— 财务因子：按 ann_date 无前视 ffill 到交易日 ——
    own = con is None
    con = con or price.connect()
    try:
        fin = con.execute(
            "select symbol, ann_date, roe, n_cashflow_act, capex, total_assets "
            "from financials_raw where ann_date >= ? and ann_date <= ?",
            [_dash(_shift(sd, -400)), _dash(ed)]
        ).fetch_df()
    finally:
        if own:
            con.close()

    roe = pd.DataFrame(index=C.index, columns=C.columns, dtype=float)
    fcfoe = pd.DataFrame(index=C.index, columns=C.columns, dtype=float)
    if not fin.empty:
        fin["ann_date"] = fin["ann_date"].astype(str).str.replace("-", "")
        fin = fin.dropna(subset=["ann_date"]).sort_values(["symbol", "ann_date"])
        fin["fcfoe"] = (fin["n_cashflow_act"] - fin["capex"]) / fin["total_assets"]
        didx = pd.Index([d.replace("-", "") for d in C.index])
        for col, name in (("roe", "roe"), ("fcfoe", "fcfoe")):
            panel = {}
            for sym, g in fin.groupby("symbol"):
                if sym not in C.columns:
                    continue
                s = g.set_index("ann_date")[col]
                s = s[~s.index.duplicated(keep="last")].sort_index()
                if s.notna().any():
                    panel[sym] = s.reindex(didx, method="ffill")
            if panel:
                P = pd.DataFrame(panel).reindex(didx)
                P.index = C.index
                if name == "roe":
                    roe = P
                else:
                    fcfoe = P

    # 这里返回的是**原始因子值**，不做任何标准化。
    # 合成分的池内标准化发生在 IC 计算阶段（ic_series 的 score_mode 分支）——
    # 因为「池内」要先知道当期池子成员，而那是计算 IC 时才知道的。
    return {"FCF/OE": fcfoe, "低波动": lowvol, "ROE": roe, "_C": C}


def _pool_z(df: pd.DataFrame, t: str, cols: list[str]) -> pd.Series:
    """t 日在 cols 池内的截面 z-score（index=symbol）。

    必须在**池内**标准化：策略实际面对的就是池子，若用全市场均值/标准差，
    会引入与池子成分相关的系统性偏移；两个 z 值加权成合成分时排序会改变。
    （单因子 IC 用秩相关，不受线性变换影响，故只有合成分才需要这一步。）
    """
    sub = df.loc[t, cols].astype(float)
    s = sub.std()
    if not (s and s == s and s > 0):
        return sub * np.nan
    return (sub - sub.mean()) / s


def _score_in_pool(fcf: pd.DataFrame, lowvol: pd.DataFrame,
                   t: str, cols: list[str]) -> pd.Series:
    """池内等权合成分 = 0.5·z(FCF/OE) + 0.5·z(低波动)。"""
    return 0.5 * _pool_z(fcf, t, cols) + 0.5 * _pool_z(lowvol, t, cols)


def _shift(sd: str, days: int) -> str:
    return (pd.Timestamp(sd) + pd.Timedelta(days=days)).strftime("%Y%m%d")


# ============================ 收益标签 ============================
def month_ends(dates: list[str]) -> list[str]:
    s = pd.Series(list(dates))
    return s.groupby(s.str[:7]).max().tolist()


def fwd_returns(C: pd.DataFrame, reb: list[str]) -> dict[str, pd.Series]:
    """{月末日: 下一期收益 Series}。用复权价，不含除权跳变。"""
    out = {}
    for i in range(len(reb) - 1):
        t, t2 = reb[i], reb[i + 1]
        if t in C.index and t2 in C.index:
            r = C.loc[t2] / C.loc[t] - 1
            out[t] = r.replace([np.inf, -np.inf], np.nan)
    return out


# ============================ IC ============================
def ic_series(F: pd.DataFrame, pool_map: dict[str, set[str]],
              reb: list[str], fwd: dict[str, pd.Series],
              score_mode: bool = False, lowvol: pd.DataFrame | None = None,
              ) -> list[tuple[str, float]]:
    """月频截面 Spearman IC。score_mode=True 时在池内重算合成分。"""
    out = []
    for t in reb:
        if t not in fwd:
            continue
        pool = _pool_at(pool_map, t)
        if not pool:
            continue
        r = fwd[t]
        cols = [c for c in pool if c in F.columns]
        if len(cols) < _MIN_STOCKS:
            continue
        if score_mode:
            f = _score_in_pool(F, lowvol, t, cols)
        elif t in F.index:
            f = F.loc[t].reindex(cols)
        else:
            f = pd.Series(dtype=float)
        r = r.reindex(f.index)
        m = f.notna() & r.notna()
        if m.sum() < _MIN_STOCKS:
            continue
        ic = f[m].rank().corr(r[m].rank())
        if ic == ic:
            out.append((t, float(ic)))
    return out


def stat(ics: list[tuple[str, float]]) -> dict:
    """IC 均值 / IR / t 值 / 胜率。

    t = IC均值 / IC标准差 × sqrt(n) —— 衡量「IC 是否稳定异于 0」。
    |t| < 2 时不应宣称因子有效。
    """
    if len(ics) < 5:
        return dict(ic=np.nan, ir=np.nan, t=np.nan, win=np.nan, n=len(ics))
    a = np.array([v for _, v in ics], dtype=float)
    sd = a.std(ddof=1)
    return dict(ic=float(a.mean()),
                ir=float(a.mean() / sd) if sd > 0 else np.nan,
                t=float(a.mean() / sd * np.sqrt(len(a))) if sd > 0 else np.nan,
                win=float((a > 0).mean()), n=len(a))


def run(sd: str = "20220101", ed: str = "20260915",
        pools: dict[str, tuple[str, bool]] | None = None) -> dict:
    """主入口：跑四池 × 四因子的 IC 对照 + 年度 IC + 分层单调性。"""
    pools = pools or POOLS
    print(f"[取数] {sd} ~ {ed}（复权口径 hfq）", flush=True)
    C = price.close_matrix(None, _dash(sd), _dash(ed), adjust="hfq")
    C = C.loc[:, C.notna().sum() > 60]
    print(f"[行情] {C.shape[0]} 交易日 × {C.shape[1]} 标的", flush=True)

    fx = build_factors(C, sd, ed)
    fcf, lowvol, roe = fx["FCF/OE"], fx["低波动"], fx["ROE"]
    print(f"[因子] FCF/OE 非空率 {fcf.notna().mean().mean()*100:.1f}% | "
          f"低波动 {lowvol.notna().mean().mean()*100:.1f}% | "
          f"ROE {roe.notna().mean().mean()*100:.1f}%", flush=True)

    reb = month_ends(list(C.index))
    fwd = fwd_returns(C, reb)

    combos = {
        "FCF/OE": dict(F=fcf),
        "低波动": dict(F=lowvol),
        "ROE": dict(F=roe),
        "最终打分": dict(F=fcf, score_mode=True, lowvol=lowvol),
    }

    rows, yearly, detail = [], [], {}
    for pname, (code, drop_kcb) in pools.items():
        pmap = load_pool_map(code, drop_kcb)
        if not pmap:
            print(f"  ! 池 {pname} 无成分快照，跳过", flush=True)
            continue
        ns = [len(v) for v in pmap.values()]
        print(f"[池] {pname:10s} {len(pmap)} 期 | 月均 {np.mean(ns):.0f} 只 | "
              f"{min(pmap)} ~ {max(pmap)}", flush=True)
        for fname, kw in combos.items():
            ics = ic_series(kw["F"], pmap, reb, fwd,
                            score_mode=kw.get("score_mode", False),
                            lowvol=kw.get("lowvol"))
            st = stat(ics)
            rows.append(dict(池=pname, 因子=fname, **st))
            detail[f"{pname}|{fname}"] = dict(ics)
            for y, g in pd.Series(dict(ics)).groupby(lambda x: x[:4]):
                yearly.append(dict(池=pname, 因子=fname, 年=y,
                                   **stat(list(zip(g.index, g.values)))))
            print(f"   {fname:8s} IC {st['ic']:+.4f}  t {st['t']:+5.2f}  "
                  f"胜率 {st['win']*100:3.0f}%  n={st['n']}", flush=True)

    lay = _layered(fcf, lowvol, pools, reb, fwd)
    return dict(summary=pd.DataFrame(rows), yearly=pd.DataFrame(yearly),
                layered=pd.DataFrame(lay), detail=detail)


def _layered(fcf, lowvol, pools, reb, fwd) -> list[dict]:
    """等权分 5 组的未来月均收益 + 多空（G5−G1）。"""
    out = []
    for pname, (code, drop_kcb) in pools.items():
        pmap = load_pool_map(code, drop_kcb)
        if not pmap:
            continue
        for fname, F in (("FCF/OE", fcf), ("低波动", lowvol)):
            buckets = {q: [] for q in range(1, 6)}
            for t in reb:
                if t not in fwd or t not in F.index:
                    continue
                cols = [c for c in _pool_at(pmap, t) if c in F.columns]
                f = F.loc[t].reindex(cols).dropna()
                r = fwd[t].reindex(f.index).dropna()
                f = f.reindex(r.index)
                if len(f) < _MIN_LAYER:
                    continue
                try:
                    q = pd.qcut(f.rank(method="first"), 5, labels=False) + 1
                except ValueError:
                    continue
                for k in range(1, 6):
                    v = r[q == k]
                    if len(v):
                        buckets[k].append(float(v.mean()))
            avgs = {k: (np.mean(v) if v else np.nan) for k, v in buckets.items()}
            for k in range(1, 6):
                out.append(dict(池=pname, 因子=fname, 组=f"G{k}",
                                月均收益=avgs[k]))
            out.append(dict(池=pname, 因子=fname, 组="多空G5-G1",
                            月均收益=avgs[5] - avgs[1]))
    return out


def _dash(s: str) -> str:
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and "-" not in s else s
