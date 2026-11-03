"""
NSI · 第 4 步：严格回测
要点：
  1. 统一区间（所有策略同起点，避免「起点差异」冒充 alpha）
  2. 分年度（协议硬要求）
  3. 滞后敏感性 shift(1) / shift(2) / shift(3) —— ETF 份额披露时点不确定，必须测
  4. 成本敏感性
  5. 同平均仓位的固定仓位对照（择时唯一有意义的对手）
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

CANDS = ["NSI_flow", "NSI_eq", "NSI_cond"]
LEVELS = [1.0, 0.8, 0.6, 0.4, 0.2]          # 分位 >=80/60/40/20/<20 对应仓位


def to_pos(sig, shift=1, levels=LEVELS):
    s = sig.reindex(df.index)
    pos = pd.Series(np.nan, index=df.index)
    pos[s >= 80] = levels[0]
    pos[(s >= 60) & (s < 80)] = levels[1]
    pos[(s >= 40) & (s < 60)] = levels[2]
    pos[(s >= 20) & (s < 40)] = levels[3]
    pos[s < 20] = levels[4]
    return pos.ffill().shift(shift)


def perf(pos, cost=0.0, tag=""):
    ok = pos.notna() & ret.notna()
    r = pos[ok] * ret[ok]
    dpos = pos[ok].diff().abs().fillna(0)
    r = r - dpos * cost
    eq = (1 + r).cumprod()
    n = len(r)
    ann = eq.iloc[-1] ** (250 / n) - 1
    vol = r.std() * np.sqrt(250)
    mdd = (eq / eq.cummax() - 1).min()
    return dict(tag=tag, ann=ann * 100, vol=vol * 100,
                sharpe=ann / vol if vol else np.nan, mdd=mdd * 100,
                calmar=(ann * 100) / abs(mdd * 100) if mdd else np.nan,
                avgpos=pos[ok].mean() * 100, nturn=dpos.sum(), win=(r > 0).mean() * 100, n=n)


# ---------- 统一区间 ----------
first = df[df["NSI_cond"].notna()].index[0]
print(f"统一回测区间：{first.date()} → {df.index[-1].date()}")
sub = df.loc[first:]
ret = ret.loc[first:]

print("\n" + "=" * 96)
print("主表：统一区间 · 无成本 · T+1 调仓")
print("=" * 96)
rows = []
for c in CANDS:
    rows.append(perf(to_pos(sub[c], 1), 0.0, c))
# 固定仓位对照：取与 NSI_cond 相同平均仓位的常数仓位
base_pos = to_pos(sub["NSI_cond"], 1)
avgp = base_pos.mean()
const = pd.Series(avgp, index=sub.index)
rows.append(perf(const, 0.0, f"固定仓位 {avgp*100:.0f}%"))
rows.append(perf(to_pos(pd.Series(100.0, index=sub.index), 1, [1, 1, 1, 1, 1]), 0.0, "满仓基准"))
rows.append(perf(to_pos(100 - sub["NSI_flow"], 1), 0.0, "NSI_flow 反向"))
rng = np.random.default_rng(7)
rand = pd.Series(rng.uniform(0, 100, len(sub)), index=sub.index).rolling(20, min_periods=1).mean()
rows.append(perf(to_pos(rand, 1), 0.0, "随机分位"))
print(pd.DataFrame(rows).to_string(index=False, float_format=lambda x: f"{x:.2f}"))

# ---------- 分年度 ----------
print("\n" + "=" * 96)
print("分年度收益（%）")
print("=" * 96)
yearly = {}
for c in CANDS + ["BENCH"]:
    if c == "BENCH":
        pos = pd.Series(1.0, index=sub.index).shift(1)
    else:
        pos = to_pos(sub[c], 1)
    ok = pos.notna() & ret.notna()
    r = pos[ok] * ret[ok]
    yearly[c] = (r.groupby(r.index.year).apply(lambda x: (1 + x).prod() - 1) * 100).round(2)
Y = pd.DataFrame(yearly)
print(Y.to_string(float_format=lambda x: f"{x:+.2f}"))
print("\n年度胜负（vs 满仓）:")
for c in CANDS:
    w = (Y[c] > Y["BENCH"]).sum()
    print(f"  {c}: {w}/{len(Y)} 年跑赢")

# ---------- 滞后敏感性 ----------
print("\n" + "=" * 96)
print("滞后敏感性：ETF 份额披露时点不确定时的稳健性")
print("=" * 96)
lag = []
for sh in (1, 2, 3, 5):
    for c in CANDS:
        p = perf(to_pos(sub[c], sh), 0.0, c)
        p["shift"] = sh
        lag.append(p)
L = pd.DataFrame(lag)[["shift", "tag", "ann", "sharpe", "mdd", "avgpos"]]
print(L.pivot_table(index="tag", columns="shift", values="sharpe").to_string(float_format=lambda x: f"{x:+.2f}"))
print("\n年化：")
print(L.pivot_table(index="tag", columns="shift", values="ann").to_string(float_format=lambda x: f"{x:+.2f}"))

# ---------- 成本敏感性 ----------
print("\n" + "=" * 96)
print("成本敏感性（单边）")
print("=" * 96)
cr = []
for cost in (0.0, 0.0005, 0.001, 0.002):
    for c in CANDS:
        p = perf(to_pos(sub[c], 1), cost, c)
        p["cost"] = cost * 10000
        cr.append(p)
C = pd.DataFrame(cr)[["cost", "tag", "ann", "sharpe"]]
print(C.pivot_table(index="tag", columns="cost", values="ann").to_string(float_format=lambda x: f"{x:+.2f}"))
