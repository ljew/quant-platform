"""
NSI · 第 5 步：降换手改造
协议提醒：任何能降换手/降成本的旋钮，先假定它有害。
所以每个改造都必须同时看「换手降了多少」和「信号衰减了多少」，两个数一起报。
"""
import numpy as np
import pandas as pd
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

RAW = Path("/data/research/raw")
df = pd.read_csv("/data/research/nsi_index.csv", index_col=0, parse_dates=True)
hs = pd.read_csv(RAW / "idx_hs300.csv")
hs["date"] = pd.to_datetime(hs["trade_date"].astype(str))
hs = hs.sort_values("date").set_index("date")
ret_all = hs["pct_chg"] / 100

first = df[df["NSI_cond"].notna()].index[0]
sub = df.loc[first:].copy()
ret = ret_all.reindex(sub.index)


def perf(pos, cost=0.0):
    ok = pos.notna() & ret.notna()
    r = pos[ok] * ret[ok]
    d = pos[ok].diff().abs().fillna(0)
    r = r - d * cost
    eq = (1 + r).cumprod()
    n = len(r)
    ann = eq.iloc[-1] ** (250 / n) - 1
    vol = r.std() * np.sqrt(250)
    mdd = (eq / eq.cummax() - 1).min()
    return dict(ann=ann * 100, vol=vol * 100, sharpe=ann / vol if vol else np.nan,
                mdd=mdd * 100, avgpos=pos[ok].mean() * 100,
                nturn=d.sum(), hold=n / max(d.sum(), 1e-9))


def map5(sig, levels=(1.0, 0.8, 0.6, 0.4, 0.2)):
    s = sig
    pos = pd.Series(np.nan, index=s.index)
    pos[s >= 80] = levels[0]
    pos[(s >= 60) & (s < 80)] = levels[1]
    pos[(s >= 40) & (s < 60)] = levels[2]
    pos[(s >= 20) & (s < 40)] = levels[3]
    pos[s < 20] = levels[4]
    return pos.ffill().shift(1)


def map_hyst(sig, hi=80, lo=20, p_hi=1.0, p_lo=0.3, p_mid=0.6):
    """迟滞带：只有穿越 hi/lo 才改变仓位，中间保持不变"""
    s = sig.values
    pos = np.full(len(s), np.nan)
    cur = p_mid
    for i in range(len(s)):
        if np.isnan(s[i]):
            pos[i] = np.nan
        else:
            if s[i] >= hi:
                cur = p_hi
            elif s[i] <= lo:
                cur = p_lo
            pos[i] = cur
    return pd.Series(pos, index=sig.index).shift(1)


def map3(sig, lo=30, hi=70):
    pos = pd.Series(0.6, index=sig.index)
    pos[sig >= hi] = 1.0
    pos[sig <= lo] = 0.25
    return pos.shift(1)


variants = {}
# 原始基准：5 档
for c in ["NSI_flow", "NSI_cond"]:
    variants[f"{c} 5档"] = map5(sub[c])
    # 平滑后再分档（降低抖动）
    for w in (5, 10, 20):
        variants[f"{c} 平滑{w}日+5档"] = map5(sub[c].rolling(w, min_periods=1).mean())
    variants[f"{c} 3档"] = map3(sub[c])
    variants[f"{c} 迟滞带8020"] = map_hyst(sub[c])
    variants[f"{c} 平滑10+迟滞8020"] = map_hyst(sub[c].rolling(10, min_periods=1).mean())

print("=" * 108)
print("降换手改造（0 成本 / 10bp 成本 双列）")
print("=" * 108)
rows = []
for k, p in variants.items():
    a = perf(p, 0.0)
    b = perf(p, 0.001)
    rows.append(dict(改造=k, 年化0bp=a["ann"], 夏普0bp=a["sharpe"], 回撤=a["mdd"],
                     年化10bp=b["ann"], 夏普10bp=b["sharpe"],
                     均仓=a["avgpos"], 累计换手=a["nturn"], 平均持有天=a["hold"]))
T = pd.DataFrame(rows)
print(T.to_string(index=False, float_format=lambda x: f"{x:.2f}"))

# 参照
print("\n参照：满仓基准")
p = perf(pd.Series(1.0, index=sub.index).shift(1), 0.0)
print(f"  年化 {p['ann']:.2f}%  夏普 {p['sharpe']:.2f}  回撤 {p['mdd']:.2f}%")
p46 = perf(pd.Series(0.4636, index=sub.index).shift(1), 0.0)
print(f"  固定46%仓位：年化 {p46['ann']:.2f}%  夏普 {p46['sharpe']:.2f}  回撤 {p46['mdd']:.2f}%")

# 最优变体分年度
print("\n" + "=" * 108)
print("择优变体的分年度（0 成本）")
print("=" * 108)
best = T.sort_values("夏普10bp", ascending=False).head(3)["改造"].tolist()
yd = {}
for k in best + ["满仓"]:
    if k == "满仓":
        pos = pd.Series(1.0, index=sub.index).shift(1)
    else:
        pos = variants[k]
    ok = pos.notna() & ret.notna()
    r = pos[ok] * ret[ok]
    yd[k] = (r.groupby(r.index.year).apply(lambda x: (1 + x).prod() - 1) * 100).round(2)
print(pd.DataFrame(yd).to_string(float_format=lambda x: f"{x:+.2f}"))
print("\n最优:", best)
