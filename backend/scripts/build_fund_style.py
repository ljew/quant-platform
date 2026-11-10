#!/usr/bin/env python3
"""基金风格穿透：持仓 → 个股 → 风格因子。

与行业穿透互补
--------------
行业穿透回答「拿什么行业」，风格穿透回答「拿什么风格」。两者都从**持仓**出发，
不是从净值反推 —— 所以可解释、可追溯到具体股票。

五个风格因子（Barra 简化）
--------------------------
  size        ln(总市值)        大盘为正
  value      -ln(PB)            低估值为正（PB 越低越便宜）
  momentum   过去 60 日复权收益（跳过最近 5 日，避开短期反转效应）
  volatility 过去 60 日年化波动  高波动为正
  quality    ROE                 高盈利为正

两层标准化
----------
1. 个股层：横截面 1%/99% 缩尾后 z-score，让不同量纲的因子可比
2. 基金层：对全市场基金再 z 一次 —— 于是 z=0 表示「与同行中位持平」，
   z=+1 表示「比 68% 的同行更偏这个风格」。跨基金、跨期都能直接比较。

口径纪律
--------
disclosed（真实披露）与 carried（补齐的影子持仓）**分开统计**：
  w_disc  真实披露权重占比 —— Q1/Q3 只有前十大，这个值会很低
  z_full  含补齐的风格值（覆盖完整，但混入了上期结构）
  z_disc  只用真实披露的风格值（干净，但前十大有偏：偏大市值）
两者差异大 → 说明补齐部分对风格结论影响大，页面上要提示。

产物
----
  fund_research.db: fund_style_exposure / fund_style_summary
  data/research/fund_style_progress.json（进度，供前端展示）

用法
----
  python scripts/build_fund_style.py --periods 6     # 近 6 个报告期
  python scripts/build_fund_style.py --sample 300    # 小样本验证
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
RES_DB = DATA / "fund_research.db"
DEV_DB = DATA / "quant_dev.db"
BY_FUND = DATA / "holdings" / "by_fund"
PROGRESS = DATA / "research" / "fund_style_progress.json"

FACTORS = ("size", "value", "momentum", "volatility", "quality")
MOM_DAYS = 60          # 动量回看窗口
MOM_SKIP = 5           # 跳过最近 N 日
MIN_WEIGHT = 0.01      # 持仓权重低于这个值（%）不参与，避免尾部噪声
MIN_COVER = 20.0       # 覆盖率低于 20% 不给风格标签
MIN_NAV = 5000.0       # 规模低于 5000 万不给风格标签（迷你基金打新会让风格失真）


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def to_wind(sym: str) -> str | None:
    """600000.SH → sh600000；港股/无后缀异常返回 None。"""
    if not sym:
        return None
    code, _, mkt = sym.partition(".")
    mkt = mkt.upper()
    if mkt == "SH":
        return f"sh{code}"
    if mkt == "SZ":
        return f"sz{code}"
    if mkt == "BJ":
        return f"bj{code}"
    if mkt == "HK":
        return None
    if len(code) == 6 and code.isdigit():
        if code[0] == "6":
            return f"sh{code}"
        if code[0] in "03":
            return f"sz{code}"
        if code[0] in "489":
            return f"bj{code}"
    return None


# ---------------------------------------------------------------- 个股因子
def stock_factors(dev: sqlite3.Connection) -> pd.DataFrame:
    """返回 symbol × 5 因子的原值（未标准化）。"""
    log("读复权行情 ...")
    last = dev.execute("SELECT MAX(trade_date) FROM kline_daily").fetchone()[0]
    px = pd.read_sql(
        """SELECT k.symbol, k.trade_date, k.close, f.adj_factor
           FROM kline_daily k LEFT JOIN adj_factor_daily f
             ON f.symbol=k.symbol AND f.trade_date=k.trade_date
           WHERE k.trade_date <= ? AND k.trade_date >= date(?, '-120 day')
           ORDER BY k.symbol, k.trade_date""", dev, params=(last, last))
    if px.empty:
        raise SystemExit("行情为空，无法计算动量")
    px["adj_close"] = px["close"] * px["adj_factor"].fillna(1.0)

    log(f"  行情 {len(px):,} 行 / {px.symbol.nunique():,} 只 / 截至 {last}")
    rows = []
    for sym, g in px.groupby("symbol", sort=False):
        c = g["adj_close"].to_numpy(dtype=float)
        if len(c) < 40:
            continue
        ret = np.diff(c) / c[:-1]
        ret = ret[np.isfinite(ret) & (np.abs(ret) < 0.25)]   # 剔除异常跳变
        if len(ret) < 30:
            continue
        mos = len(c) - 1
        seg = c[: max(0, mos - MOM_SKIP)]
        mom = (seg[-1] / seg[0] - 1.0) * 100 if len(seg) > 1 else np.nan
        v = np.diff(seg) / seg[:-1]
        v = v[np.isfinite(v) & (np.abs(v) < 0.25)]
        vol = float(np.std(v)) * math.sqrt(252) * 100 if len(v) > 5 else np.nan
        rows.append((sym, mom, vol))
    q = pd.DataFrame(rows, columns=["symbol", "momentum", "volatility"])
    log(f"  动量/波动：{len(q):,} 只")

    fund = pd.read_sql("SELECT symbol, market_cap, pb, roe FROM stocks", dev)
    fund["size"] = np.log(fund["market_cap"].where(fund["market_cap"] > 0))
    fund["value"] = -np.log(fund["pb"].where(fund["pb"] > 0))
    fund["quality"] = fund["roe"]
    q = q.merge(fund[["symbol", "size", "value", "quality"]], on="symbol", how="left")
    return q


def zscore(s: pd.Series, clip: float = 0.01) -> pd.Series:
    """缩尾 + 标准化。"""
    v = s.astype(float)
    if v.notna().sum() < 10:
        return v * np.nan
    lo, hi = v.quantile(clip), v.quantile(1 - clip)
    v = v.clip(lo, hi)
    sd = float(v.std())
    return (v - v.mean()) / sd if sd > 1e-9 else v * np.nan


# ---------------------------------------------------------------- 基金规模
def fund_scale(min_end: str) -> dict[tuple[str, str], float]:
    """从披露明细反推基金规模（万元）。

    ratio = mkv / nav × 100 ⇒ nav = Σmkv / (Σratio/100)。
    季报只披露前十大，但 ratio 本身就是「占净值比」，分子分母同口径，公式照样成立。

    同一记录在多份 CSV 里重复时，Σmkv 与 Σratio 同比例放大，相除结果不变 ——
    所以这里刻意不做去重，省掉一份千万级明细的内存。
    """
    files = [DATA / "holdings" / "holdings_raw.csv"]
    files += sorted(p for p in (DATA / "holdings").glob("holdings_*.csv")
                    if p.name != "holdings_raw.csv")
    agg: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
    t0 = time.time()
    n = 0
    for fp in files:
        if not fp.exists():
            continue
        with open(fp, newline="") as f:
            for r in csv.DictReader(f):
                ed = r["end_date"]
                if ed < min_end:
                    continue
                try:
                    mkv = float(r["mkv"] or 0)
                    ratio = float(r["stk_mkv_ratio"] or 0)
                except ValueError:
                    continue
                if mkv <= 0 or ratio <= 0:
                    continue
                a = agg[(r["ts_code"], ed)]
                a[0] += mkv
                a[1] += ratio
                n += 1
    out = {k: (v[0] / (v[1] / 100.0) / 1e4 if v[1] > 0 else 0.0)
           for k, v in agg.items()}
    log(f"  反推规模：{len(out):,} 个截面（扫 {n:,} 行 / {time.time()-t0:.0f}s）")
    return out


def equity_universe(dev: sqlite3.Connection) -> set[str]:
    """权益类基金白名单（股票型 + 混合型）。

    不能靠股票仓位过滤：可转债基金转股后披露的持仓全是股票，仓位一样是 100%，
    但它的「风格」是转债条款驱动的，与权益基金不可比。必须用基金类型。
    """
    rows = dev.execute(
        "SELECT ts_code FROM fund_basic WHERE fund_type IN ('股票型','混合型')")
    return {r[0] for r in rows}


# ---------------------------------------------------------------- 持仓汇总
def load_holdings(min_end: str, sample: int = 0) -> pd.DataFrame:
    """扫 by_fund/*.csv，返回明细（含 source）。"""
    files = sorted(glob.glob(str(BY_FUND / "*.csv")))
    if sample:
        files = files[:sample]
    log(f"扫描持仓 {len(files):,} 个文件（end_date >= {min_end}）...")
    rec: list[tuple] = []
    t0 = time.time()
    for i, fp in enumerate(files, 1):
        try:
            with open(fp, newline="") as f:
                for r in csv.DictReader(f):
                    ed = r["end_date"]
                    if ed < min_end:
                        continue
                    try:
                        w = float(r["weight"] or 0)
                    except ValueError:
                        continue
                    if w < MIN_WEIGHT:
                        continue
                    rec.append((Path(fp).stem, ed, r["symbol"],
                                w, r["source"] or "?"))
        except Exception:
            continue
        if i % 2000 == 0:
            log(f"  {i:,}/{len(files):,}  已收 {len(rec):,} 行  {time.time()-t0:.0f}s")
    df = pd.DataFrame(rec, columns=["ts_code", "end_date", "symbol", "weight", "source"])
    log(f"  明细 {len(df):,} 行 / {df.ts_code.nunique():,} 只基金 / "
        f"{df.end_date.nunique()} 个期次 / {time.time()-t0:.0f}s")
    return df


# ---------------------------------------------------------------- 主计算
def build(df: pd.DataFrame, sf: pd.DataFrame,
          scale: dict[tuple[str, str], float] | None = None,
          base_period: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    # 个股因子标准化（横截面，全市场口径）
    z = sf.copy()
    for f in FACTORS:
        z[f] = zscore(z[f])
    zmap = {f: dict(zip(z.symbol, z[f])) for f in FACTORS}

    df["wsym"] = df["symbol"].map(to_wind)
    df = df[df["wsym"].notna()]
    log(f"  可映射到 A 股股票：{len(df):,} 行 / "
        f"{df.wsym.nunique():,} 只股票")

    # 逐 (基金, 期次) 加权。同时算两份：
    #   full     含 carried 补齐 —— 覆盖完整，但会受到补齐参数影响
    #   disclosed 只用真实披露   —— 事实驱动，跨期比较不受算法改动干扰
    out: list[dict] = []
    for (code, ed), g in df.groupby(["ts_code", "end_date"], sort=False):
        w_tot = float(g.weight.sum())
        if w_tot <= 0:
            continue
        syms = g.wsym.to_numpy()
        ws = g.weight.to_numpy()
        dmsk = (g.source == "disclosed").to_numpy()
        row = {"ts_code": code, "end_date": ed, "w_cover": w_tot,
               "nav_wan": (scale or {}).get((code, ed))}
        for f in FACTORS:
            mp = zmap[f]
            num = wsum = dnum = dwsum = 0.0
            for i, (sym, w) in enumerate(zip(syms, ws)):
                v = mp.get(sym)
                if v is None or not np.isfinite(v):
                    continue
                num += w * v
                wsum += w
                if dmsk[i]:
                    dnum += w * v
                    dwsum += w
            row[f] = num / wsum if wsum > 0 else np.nan
            row[f"d_{f}"] = dnum / dwsum if dwsum > 0 else np.nan
        row["w_disc"] = float(g.loc[dmsk, "weight"].sum())
        row["n_holdings"] = int(len(g))
        out.append(row)
    res = pd.DataFrame(out)
    # 统一成 base_code（纯 6 位），与 fund_industry_exposure / fund_hold_coverage 对齐，
    # 否则跨表 JOIN 会因为 .OF 后缀全部落空。
    res["base_code"] = res["ts_code"].str.split(".").str[0]
    log(f"  基金截面 {len(res):,} 个")

    # 第二层：固定基准标准化。
    # 关键 —— 若每期各自 z 一次，两期基准不同，跨期「漂移」里混入的是基准位移，
    # 不是风格变化。故统一用最新期（样本最全）的均值/标准差做基准，所有期次共用，
    # 这样 z 的差值才真正可比。
    #
    # base_period：当日截面必须指定，否则每天用自己的横截面标准化，
    # 今天和昨天的 z 基准不同，日间变化里混进的全是基准位移。
    latest = base_period or res.end_date.max()
    base: dict[str, tuple[float, float]] = {}
    for f in FACTORS:
        s = res.loc[res.end_date == latest, f].dropna()
        base[f] = (float(s.mean()) if len(s) else 0.0, float(s.std()) or 1.0)
    for f in FACTORS:
        m, sd = base[f]
        res[f"z_{f}"] = (res[f] - m) / sd
        res[f"z_d_{f}"] = (res[f"d_{f}"] - m) / sd
    res.attrs["base"] = base
    log("  标准化基准（" + str(latest) + "）：" +
        "  ".join(f"{f} μ={base[f][0]:+.2f} σ={base[f][1]:.2f}" for f in FACTORS))

    # 风格标签（四分象限）
    def tag(r):
        # 迷你基金（如 107 万规模的 ETF 联接）打新获配会让风格彻底失真，
        # 与其贴个"大盘成长"的假标签，不如显式标为规模过小。
        nav = r.get("nav_wan")
        if nav is not None and nav < MIN_NAV:
            return "规模过小"
        if not np.isfinite(r.get("z_size", np.nan)) or not np.isfinite(r.get("z_value", np.nan)):
            return "未定"
        big = r["z_size"] >= 0
        val = r["z_value"] >= 0
        return ("大盘" if big else "小盘") + ("价值" if val else "成长")

    res["style_tag"] = res.apply(tag, axis=1)
    return res, z


def persist(res: pd.DataFrame, sf: pd.DataFrame, top_n: int = 3) -> dict:
    con = sqlite3.connect(RES_DB, timeout=90)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS fund_style_exposure (
        base_code TEXT, end_date TEXT, factor TEXT,
        raw REAL, z REAL, w_cover REAL,
        PRIMARY KEY (base_code, end_date, factor))""")
    con.execute("""CREATE TABLE IF NOT EXISTS fund_style_summary (
        base_code TEXT, end_date TEXT,
        z_size REAL, z_value REAL, z_momentum REAL, z_volatility REAL, z_quality REAL,
        style_tag TEXT, w_cover REAL, w_disc REAL, n_holdings INTEGER,
        computed_at TEXT,
        PRIMARY KEY (base_code, end_date))""")
    # 只用披露部分的风格（跨期比较不受补齐算法影响），兼容旧表结构
    for f in FACTORS:
        try:
            con.execute(f"ALTER TABLE fund_style_summary ADD COLUMN z_d_{f} REAL")
        except sqlite3.OperationalError:
            pass
    try:
        con.execute("ALTER TABLE fund_style_summary ADD COLUMN nav_wan REAL")
    except sqlite3.OperationalError:
        pass

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    zcols = [f"z_{f}" for f in FACTORS] + [f"z_d_{f}" for f in FACTORS]
    exp_rows, sum_rows = [], []
    for r in res.itertuples(index=False):
        for f in FACTORS:
            zv = getattr(r, f"z_{f}", None)
            rv = getattr(r, f, None)
            if zv is None or not np.isfinite(zv):
                continue
            exp_rows.append((r.base_code, r.end_date, f, round(float(rv), 4),
                             round(float(zv), 4), round(float(r.w_cover), 2)))
        vals = []
        for c in zcols:
            v = getattr(r, c, None)
            vals.append(round(float(v), 4) if v is not None and np.isfinite(v) else None)
        sum_rows.append((
            r.base_code, r.end_date, *vals,
            r.style_tag, round(float(r.w_cover), 2), round(float(r.w_disc), 2),
            int(r.n_holdings), now,
            round(float(r.nav_wan), 1) if getattr(r, "nav_wan", None) else None))

    con.execute("DELETE FROM fund_style_exposure")
    con.execute("DELETE FROM fund_style_summary")
    con.executemany("INSERT OR REPLACE INTO fund_style_exposure VALUES (?,?,?,?,?,?)", exp_rows)
    cols_sql = ", ".join(
        ["base_code", "end_date"] + zcols +
        ["style_tag", "w_cover", "w_disc", "n_holdings", "computed_at", "nav_wan"])
    ph = ",".join("?" * (2 + len(zcols) + 6))
    con.executemany(
        f"INSERT OR REPLACE INTO fund_style_summary ({cols_sql}) VALUES ({ph})", sum_rows)
    con.execute("CREATE INDEX IF NOT EXISTS idx_style_sum_ed ON fund_style_summary(end_date)")
    con.commit()

    # 进度快照（前端展示用）
    prog = {
        "computed_at": now,
        "periods": sorted(res.end_date.unique().tolist()),
        "n_funds": int(res.ts_code.nunique()),
        "n_sections": int(len(res)),
        "stock_universe": int(len(sf)),
    }
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS.write_text(json.dumps(prog, ensure_ascii=False, indent=2), encoding="utf-8")
    con.close()
    return prog


# 结构衰减曲线，与 build_fund_penetration.py 同源（由 826 只基金 / 13,545 对
# 历史「完整披露期 → 之后各期前十大」的行业结构秩相关标定）。
_DECAY_POINTS = ((0, 1.0), (90, 0.50), (180, 0.47), (270, 0.31), (365, 0.235))


def structure_decay(gap: int) -> float:
    g = max(0, int(gap))
    last_g, last_v = _DECAY_POINTS[-1]
    if g >= last_g:
        return last_v * (0.5 ** ((g - last_g) / 180.0))
    for (g0, v0), (g1, v1) in zip(_DECAY_POINTS, _DECAY_POINTS[1:]):
        if g0 <= g <= g1:
            f = (g - g0) / (g1 - g0) if g1 > g0 else 0.0
            return v0 + (v1 - v0) * f
    return 1.0


def persist_nowcast(res: pd.DataFrame, as_of: str, keep_days: int = 7) -> dict:
    """把「截至 as_of」的风格截面写进 fund_style_nowcast。

    与 summary 分表存放：summary 是「按报告期的历史事实」，是漂移分析的地基；
    nowcast 是推算值，混进去会让「这条到底是披露的还是推的」再也分不清。
    """
    from datetime import datetime as _dt
    sub = res[res.end_date == res.groupby("ts_code")["end_date"].transform("max")].copy()
    if not len(sub):
        log("当日风格截面为空，跳过落库")
        return {}
    sub["stale_days"] = [
        abs((_dt.strptime(as_of, "%Y%m%d") - _dt.strptime(e, "%Y%m%d")).days)
        for e in sub.end_date
    ]
    sub["confidence"] = [round(structure_decay(s), 4) for s in sub.stale_days]

    zcols = [f"z_{f}" for f in FACTORS] + [f"z_d_{f}" for f in FACTORS]
    con = sqlite3.connect(RES_DB, timeout=90)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS fund_style_nowcast (
        as_of_date TEXT NOT NULL, base_code TEXT NOT NULL, ts_code TEXT,
        z_size REAL, z_value REAL, z_momentum REAL, z_volatility REAL, z_quality REAL,
        z_d_size REAL, z_d_value REAL, z_d_momentum REAL, z_d_volatility REAL, z_d_quality REAL,
        style_tag TEXT, w_cover REAL, w_disc REAL, n_holdings INTEGER,
        nav_wan REAL, data_end_date TEXT, stale_days INTEGER, confidence REAL,
        PRIMARY KEY (as_of_date, base_code))""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_fsn_base ON fund_style_nowcast(base_code,as_of_date)")
    con.execute("DELETE FROM fund_style_nowcast WHERE as_of_date=?", (as_of,))
    cols = ["base_code", "ts_code"] + zcols + [
        "style_tag", "w_cover", "w_disc", "n_holdings", "nav_wan",
        "end_date", "stale_days", "confidence"]
    # res 里叫 end_date（数据期次），表里叫 data_end_date 以示与 as_of_date 区分
    sql_cols = ["data_end_date" if c == "end_date" else c for c in cols]
    rows = []
    for r in sub[cols].itertuples(index=False, name=None):
        rows.append((as_of,) + tuple(
            round(float(v), 4) if isinstance(v, float) else v for v in r))
    con.executemany(
        f"INSERT OR REPLACE INTO fund_style_nowcast (as_of_date,{','.join(sql_cols)}) "
        f"VALUES ({','.join('?' * (len(cols) + 1))})", rows)

    cut = (_dt.strptime(as_of, "%Y%m%d") - timedelta(days=keep_days)).strftime("%Y%m%d")
    con.execute("DELETE FROM fund_style_nowcast WHERE as_of_date < ?", (cut,))
    con.commit()
    con.close()

    meta = {
        "as_of_date": as_of,
        "n_funds": int(len(sub)),
        "stale_median": int(sub.stale_days.median()),
        "conf_median": round(float(sub.confidence.median()), 4),
        "tag_top": sub.style_tag.value_counts().head(3).to_dict(),
    }
    log(f"当日风格落库 {meta['n_funds']:,} 行：{as_of} / "
        f"数据滞后中位 {meta['stale_median']} 天 / 置信度中位 {meta['conf_median']} / "
        f"标签 {meta['tag_top']}")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--periods", type=int, default=6, help="回溯报告期数（默认 6 期）")
    ap.add_argument("--sample", type=int, default=0, help="只扫前 N 个持仓文件")
    ap.add_argument("--nowcast", action="store_true",
                    help="产出「截至 as_of」的当日风格截面，写 fund_style_nowcast，"
                         "不覆盖 fund_style_summary")
    ap.add_argument("--as-of", dest="as_of", default="",
                    help="截面日 YYYYMMDD，默认今天。仅在 --nowcast 下生效")
    ap.add_argument("--keep-days", type=int, default=7, help="当日截面保留天数（默认 7）")
    args = ap.parse_args()

    dev = sqlite3.connect(f"file:{DEV_DB}?mode=ro", uri=True, timeout=60)
    res = sqlite3.connect(f"file:{RES_DB}?mode=ro", uri=True, timeout=60)
    eds = [r[0] for r in res.execute(
        "SELECT DISTINCT end_date FROM fund_hold_coverage ORDER BY end_date DESC")]
    res.close()
    # 只保留法定报告期，避免零星入库日期（如 20260929）挤占期次
    eds = [e for e in eds if e[4:] in ("0331", "0630", "0930", "1231")]
    if not eds:
        raise SystemExit("fund_hold_coverage 为空，先跑 build_fund_penetration.py")
    min_end = eds[min(args.periods, len(eds)) - 1]
    log(f"报告期范围 {min_end} ~ {eds[0]}（共 {min(args.periods, len(eds))} 期）")

    sf = stock_factors(dev)
    eq = equity_universe(dev)
    dev.close()
    log(f"权益类基金白名单 {len(eq):,} 只（股票型 + 混合型）")
    scale = fund_scale(min_end)
    df = load_holdings(min_end, args.sample)
    if df.empty:
        raise SystemExit("无持仓明细")
    before = df.ts_code.nunique()
    df = df[df.ts_code.isin(eq)]
    log(f"  类型过滤：{before:,} → {df.ts_code.nunique():,} 只（剔除债券/货币/REITs）")

    if args.nowcast:
        # 标准化基准固定在最新标准报告期，而不是「今天这批基金自己」：
        # 每天各标准一次的话，z 的基准天天变，日间差异里混的是基准位移。
        as_of = args.as_of or datetime.now().strftime("%Y%m%d")
        out, _ = build(df, sf, scale, base_period=eds[0])
        persist_nowcast(out, as_of, keep_days=args.keep_days)
        return

    out, _ = build(df, sf, scale)
    prog = persist(out, sf)

    log("=" * 62)
    log(f"完成：{prog['n_funds']:,} 只基金 / {prog['n_sections']:,} 个截面 / "
        f"股票池 {prog['stock_universe']:,} 只")
    # 「落库 N 行」是调度器 _parse_rows 认的措辞，别改成「明细」——
    # 那会把它读成输入明细的 273 万行。
    log(f"落库 {prog['n_sections']:,} 行风格截面")
    latest = out[out.end_date == out.end_date.max()]
    log(f"最新期 {latest.end_date.iloc[0]}：{len(latest):,} 只")
    for f in FACTORS:
        s = latest[f"z_{f}"].dropna()
        if len(s):
            log(f"  {f:<11} 中位 {s.median():+.2f}  p10 {s.quantile(.1):+.2f}  p90 {s.quantile(.9):+.2f}")
    log("风格标签分布：" + str(latest.style_tag.value_counts().to_dict()))
    nv = latest.nav_wan.dropna()
    if len(nv):
        small = int((nv < MIN_NAV).sum())
        log(f"  规模：中位 {nv.median():,.0f} 万  p10 {nv.quantile(.1):,.0f} 万  "
            f"低于 {MIN_NAV:,.0f} 万的 {small:,} 只")


if __name__ == "__main__":
    main()
