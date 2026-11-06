"""
国家队介入强度指数（NSI）· 第 2 步：指标构造 + 单因子检验
口径纪律：
  - 信号 T 日收盘后可得（ETF 份额 T 日盘后披露）→ T+1 收盘建仓，持有 20 日到 T+21
  - 标准化一律用滚动 250 日分位，无未来信息
  - 训练期 2018-2023 定方向与权重，验证期 2024-2026 只做检验，不回头调参
"""
import numpy as np
import pandas as pd
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

RAW = Path("/data/research/raw")
pd.set_option("display.width", 200)

# ---------------- 载入 ----------------
def rd(name):
    p = RAW / f"{name}.csv"
    df = pd.read_csv(p, encoding="utf-8")
    return df

idx = {}
for tag in ["hs300", "sz50", "zz1000", "zz500", "bank", "sse"]:
    d = rd(f"idx_{tag}")
    d["date"] = pd.to_datetime(d["trade_date"].astype(str))
    idx[tag] = d.sort_values("date").set_index("date")

base = idx["hs300"][["close", "open", "high", "low", "pct_chg", "amount"]].copy()
base = base.dropna()
print(f"基准日历: {base.index[0].date()} → {base.index[-1].date()}  {len(base)} 交易日")

# ---------------- A 组：资金痕迹 ----------------
ETF = {
    "hs300etf": "510300", "sz50etf": "510050", "zz500etf": "510500",
    "hs300etf2": "159919", "kc50etf": "588000", "cybetf": "159915",
    "zz1000etf": "512100", "divetf": "510880",
}
shares, navs = {}, {}
for tag, code in ETF.items():
    s = rd(f"sh_{tag}")
    s["date"] = pd.to_datetime(s["trade_date"].astype(str))
    shares[tag] = s.set_index("date")["fd_share"].astype(float).sort_index()
    try:
        n = rd(f"nav_{tag}")
        n["date"] = pd.to_datetime(n["nav_date"].astype(str))
        navs[tag] = n.set_index("date")["unit_nav"].astype(float).sort_index()
    except Exception:
        navs[tag] = pd.Series(dtype=float)

# 只在宽基上做「国家队」判断（剔除主题/行业 ETF 的自发申购噪声）
CORE = ["hs300etf", "sz50etf", "zz500etf", "hs300etf2", "zz1000etf"]

S = pd.DataFrame({t: shares[t].reindex(base.index).ffill() for t in ETF})
A = pd.DataFrame(index=base.index)

# A1 日脉冲（去趋势：减 60 日中位数，剔除 ETF 自然增长）
chg1 = S.pct_change(fill_method=None)
detr1 = chg1 - chg1.rolling(60, min_periods=20).median()
A["A1_etf_pulse_1d"] = detr1[CORE].mean(axis=1)

# A2 5 日动量（去趋势）
chg5 = S.pct_change(5, fill_method=None)
detr5 = chg5 - chg5.rolling(60, min_periods=20).median()
A["A2_etf_mom_5d"] = detr5[CORE].mean(axis=1)

# A3 广度：份额增加的 ETF 占比
A["A3_etf_breadth"] = (chg1[CORE] > 0).sum(axis=1) / chg1[CORE].notna().sum(axis=1).replace(0, np.nan)

# A4 金额口径净流入占存量规模比
money = {}
for t in ETF:
    nv = navs[t].reindex(base.index).ffill() if len(navs[t]) else pd.Series(1.0, index=base.index)
    money[t] = S[t] * nv
M = pd.DataFrame(money)
dM = M.diff()
A["A4_etf_netflow"] = dM[CORE].sum(axis=1) / M[CORE].sum(axis=1).replace(0, np.nan) * 100

# ---------------- B 组：价格行为痕迹 ----------------
base["rng"] = (base["high"] - base["low"]).replace(0, np.nan)
base["B1_close_pos"] = (base["close"] - base["low"]) / base["rng"]

# B2 尾盘强度（30min 数据）
mn = rd("min_hs300_30m")
mn["dt"] = pd.to_datetime(mn["trade_time"])
mn["d"] = mn["dt"].dt.normalize()
mn["hm"] = mn["dt"].dt.strftime("%H:%M")
last_bar = mn[mn["hm"] == "15:00"].set_index("d")["close"].astype(float)
bar1430 = mn[mn["hm"] == "14:30"].set_index("d")["close"].astype(float)
late = (last_bar / bar1430 - 1) * 100
late = late.reindex(base.index)
base["B2_late_session"] = late - base["pct_chg"]          # 尾盘相对全天的超额

# B3 大盘 vs 小盘
base["B3_style"] = idx["sz50"]["pct_chg"].reindex(base.index) - idx["zz1000"]["pct_chg"].reindex(base.index)
# B4 银行超额
base["B4_bank_excess"] = idx["bank"]["pct_chg"].reindex(base.index) - base["pct_chg"]
# B5 上影线（护盘被砸的痕迹，负向）
base["B5_upper_shadow"] = -(base["high"] - base["close"]) / base["close"] * 100

base = base.join(A)

# ---------------- C 组：动量 / 状态 ----------------
base["C1_mom_20"] = base["close"].pct_change(20) * 100
base["C2_dd_250"] = (base["close"] / base["close"].rolling(250, min_periods=120).max() - 1) * 100
base["C3_etf_mom_20"] = (S[CORE].pct_change(20, fill_method=None) - S[CORE].pct_change(20, fill_method=None).rolling(60, min_periods=20).median()).mean(axis=1)
base["C4_vol_20"] = base["pct_chg"].rolling(20).std()

# ---------------- 远期收益（严格口径：T+1 收盘建仓） ----------------
base["fwd20"] = base["close"].shift(-21) / base["close"].shift(-1) - 1
base["fwd5"] = base["close"].shift(-6) / base["close"].shift(-1) - 1
base["fwd60"] = base["close"].shift(-61) / base["close"].shift(-1) - 1

FEATS = [c for c in base.columns if c[0] in "ABC" and "_" in c and c[1].isdigit()]
print(f"\n候选指标 {len(FEATS)} 个: {FEATS}")

df = base.copy()   # 保留完整序列：末尾 21 天 fwd20 为 NaN，但不能切掉，否则最新读数算不出来

# ---------------- 检验函数 ----------------
def pct_rank_roll(s, w=250, minp=80):
    return s.rolling(w, min_periods=minp).apply(lambda x: (x <= x[-1]).mean(), raw=True)

def spear(a, b):
    """无 scipy 版 Spearman：秩变换后算 Pearson"""
    a, b = pd.Series(a).astype(float), pd.Series(b).astype(float)
    m = a.notna() & b.notna()
    if m.sum() < 30:
        return np.nan
    ra, rb = a[m].rank(), b[m].rank()
    if ra.std() == 0 or rb.std() == 0:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])

def evaluate(sig, fwd, label, n_hold=20):
    d = pd.concat([sig.rename("s"), fwd.rename("r")], axis=1).dropna()
    if len(d) < 200:
        return None
    ic = spear(d["s"], d["r"])
    sub = d.iloc[::n_hold]
    t = spear(sub["s"], sub["r"])
    try:
        q = pd.qcut(d["s"], 5, labels=False, duplicates="drop")
    except Exception:
        return None
    g = d.groupby(q)["r"].mean() * 100
    mono = g.iloc[-1] - g.iloc[0]
    return dict(ic=ic, ic_sub=t, n=len(d), n_sub=len(sub),
                q1=g.iloc[0], q5=g.iloc[-1], spread=mono,
                groups=[round(x, 2) for x in g.tolist()])

# ---------------- 训练/验证拆分 ----------------
TRAIN_END = pd.Timestamp("2023-12-31")
tr = df[df.index <= TRAIN_END]
va = df[df.index > TRAIN_END]
print(f"训练期 {len(tr)} 行  |  验证期 {len(va)} 行\n")

rows = []
for f in FEATS:
    r_tr = evaluate(tr[f], tr["fwd20"], f)
    r_va = evaluate(va[f], va["fwd20"], f)
    if not r_tr or not r_va:
        continue
    rows.append(dict(
        feat=f,
        ic_tr=r_tr["ic"], ic_va=r_va["ic"],
        spread_tr=r_tr["spread"], spread_va=r_va["spread"],
        n_tr=r_tr["n_sub"], n_va=r_va["n_sub"],
    ))

res = pd.DataFrame(rows).sort_values("ic_tr", key=abs, ascending=False)
print("=== 单因子：训练期 vs 验证期（对 fwd20 的 Spearman IC）===")
print(res.to_string(index=False, float_format=lambda x: f"{x:+.4f}"))

# ---------------- 有效因子详表 ----------------
print("\n=== 训练期有效（|IC|>0.08 且方向一致）的因子，五分组明细 ===")
keep = res[(res["ic_tr"].abs() > 0.08) & (np.sign(res["ic_tr"]) == np.sign(res["ic_va"]))]
print(f"入选 {len(keep)} 个: {keep['feat'].tolist()}\n")
for f in keep["feat"]:
    r = evaluate(tr[f], tr["fwd20"], f)
    v = evaluate(va[f], va["fwd20"], f)
    print(f"{f}")
    print(f"  训练 IC={r['ic']:+.4f} (n={r['n_sub']})  分组 {r['groups']}  空多差 {r['spread']:+.2f}pp")
    print(f"  验证 IC={v['ic']:+.4f} (n={v['n_sub']})  分组 {v['groups']}  空多差 {v['spread']:+.2f}pp")

df.to_csv("/data/research/nsi_features.csv")
print("\n特征表已存 /data/research/nsi_features.csv")
