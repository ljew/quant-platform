"""
NSI · 第 6 步：诊断 + 映射标定 + 当前状态
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
ret = (hs["pct_chg"] / 100).reindex(df.index)

first = df[df["NSI_cond"].notna()].index[0]
sub = df.loc[first:]

print("=" * 90)
print("1. 指数分布（判断分位刻度是否偏保守）")
print("=" * 90)
for c in ["NSI_flow", "NSI_eq", "NSI_cond"]:
    s = sub[c].dropna()
    print(f"{c}: 均值{s.mean():5.1f} 中位{s.median():5.1f} "
          f"| >80占比 {(s>80).mean()*100:4.1f}%  <20占比 {(s<20).mean()*100:4.1f}%  "
          f"| 21-79占比 {((s>=20)&(s<=80)).mean()*100:4.1f}%")

print("\n" + "=" * 90)
print("2. 仓位映射标定：把平均仓位拉到目标值（只做线性平移，不改信号排序）")
print("=" * 90)


def hyst_centered(sig, hi_q=80, lo_q=20, p_hi=1.0, p_lo=0.3, p_mid=0.65):
    s = np.asarray(sig, dtype=float)
    pos = np.full(len(s), np.nan)
    cur = p_mid
    for i in range(len(s)):
        if np.isnan(s[i]):
            pos[i] = np.nan
        else:
            if s[i] >= hi_q: cur = p_hi
            elif s[i] <= lo_q: cur = p_lo
            pos[i] = cur
    return pd.Series(pos, index=sig.index).shift(1)


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
    return dict(ann=ann * 100, sharpe=ann / vol if vol else np.nan, mdd=mdd * 100,
                avgpos=pos[ok].mean() * 100, nturn=d.sum())


for c in ["NSI_flow", "NSI_cond"]:
    print(f"\n【{c}】不同中间仓位设定（迟滞带 80/20）")
    print(f"{'p_mid':>6}{'p_lo':>6}{'均仓':>7}{'年化0':>8}{'年化10bp':>9}{'夏普10bp':>9}{'回撤':>7}{'换手':>7}")
    for mid, lo in [(0.50, 0.20), (0.65, 0.25), (0.80, 0.30), (0.90, 0.40), (1.00, 0.50)]:
        p = hyst_centered(sub[c], 80, 20, 1.0, lo, mid)
        a, b = perf(p, 0.0), perf(p, 0.001)
        print(f"{mid:>6.2f}{lo:>6.2f}{a['avgpos']:>7.1f}{a['ann']:>8.2f}{b['ann']:>9.2f}"
              f"{b['sharpe']:>9.2f}{a['mdd']:>7.2f}{a['nturn']:>7.1f}")

print("\n" + "=" * 90)
print("3. 当前状态诊断（最近 15 个交易日）")
print("=" * 90)
cols = ["FLOW", "BEHAV", "PRESS", "C1_mom_20", "C4_vol_20", "A1_etf_pulse_1d",
        "A2_etf_mom_5d", "A3_etf_breadth", "A4_etf_netflow", "C3_etf_mom_20",
        "B1_close_pos", "B2_late_session", "B3_style", "B4_bank_excess"]
show = sub[cols + ["NSI_flow", "NSI_cond"]].tail(15).copy()
print(show.round(3).to_string())

print("\n" + "=" * 90)
print("4. 当前读数")
print("=" * 90)
last = sub.iloc[-1]
print(f"日期 {sub.index[-1].date()}")
print(f"  FLOW 分位 = {last['FLOW']:.3f}   （资金痕迹强度，5 项均值）")
print(f"  PRESS 分位 = {last['PRESS']:.3f}  （市场压力，跌得越多越大）")
print(f"  BEHAV 分位 = {last['BEHAV']:.3f} （价格行为痕迹，5 项均值）")
print(f"  NSI_flow = {last['NSI_flow']:.1f}   NSI_cond = {last['NSI_cond']:.1f}")
print(f"  建议仓位（迟滞带 80/20, mid=65%）= ", end="")
p = hyst_centered(sub["NSI_cond"], 80, 20, 1.0, 0.25, 0.65)
print(f"{p.dropna().iloc[-1]*100:.0f}%")

# 历史分位
print(f"\n  NSI_cond 在历史上处于 {sub['NSI_cond'].rank(pct=True).iloc[-1]*100:.1f}% 分位")
print(f"  NSI_flow 在历史上处于 {sub['NSI_flow'].rank(pct=True).iloc[-1]*100:.1f}% 分位")

# 近 60 日关键指标走向
print("\n最近 60 日：ETF 份额（亿份）与指数")
S = pd.read_csv(RAW / "sh_hs300etf.csv")
S["date"] = pd.to_datetime(S["trade_date"].astype(str))
S = S.sort_values("date").set_index("date")["fd_share"] / 1e4
tail = pd.DataFrame({"沪深300ETF份额亿份": S.reindex(sub.index).tail(60),
                     "沪深300收盘": hs["close"].reindex(sub.index).tail(60)})
print(tail.iloc[::10].round(1).to_string())
