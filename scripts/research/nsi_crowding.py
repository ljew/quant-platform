"""NSI · 拥挤度指标：份额流入里有多少是「追涨的配置盘」

问题：份额跳升不一定是国家队，可能是保险/理财逆势或顺势配置。
可观测的区分点不需要新数据源，用现有份额 + 指数日线就能拆：

  国家队特征：暴跌日集中买（逆势、越跌越买、为托底）
  配置盘特征：上涨日平稳买（顺势、追业绩、怕踏空）

指标：
  crowding_t = 过去 N 日「上涨日的份额净增」/「全部份额净增」
               ∈ [0,1]，越接近 1 越像追涨配置盘（拥挤），越接近 0 越像托底

检验：
  1. crowding 的分布与时段特征
  2. crowding 是否调节 NSI 的预测力（高 crowding 时信号是否变差）
  3. 加拥挤度过滤后，策略是否改善
"""
import numpy as np
import pandas as pd
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

RAW = Path("/data/research/raw")
TRAIN_END = pd.Timestamp("2023-12-31")
CORE = ["hs300etf", "sz50etf", "zz500etf", "hs300etf2", "zz1000etf"]

hs = pd.read_csv(RAW / "idx_hs300.csv")
hs["date"] = pd.to_datetime(hs["trade_date"].astype(str))
hs = hs.sort_values("date").set_index("date")
BASE = hs[["open", "high", "low", "close", "pct_chg"]].dropna()

S = {}
for tag in CORE:
    d = pd.read_csv(RAW / f"sh_{tag}.csv")
    d["date"] = pd.to_datetime(d["trade_date"].astype(str))
    S[tag] = d.set_index("date")["fd_share"].astype(float).sort_index()
SD = pd.DataFrame({t: S[t].reindex(BASE.index).ffill() for t in CORE})

# 每日份额净增（份额单位：万份），全篮子加总
dS = SD.diff()
net = dS.sum(axis=1, min_count=3)
up = BASE["pct_chg"] > 0

# 滚动 20 日：上涨日净增 / 总净增（用绝对值做分母，避免正负抵消后分母趋零）
def rolling_crowding(n=20):
    up_in = net.where(up, 0.0).rolling(n, min_periods=10).sum()
    dn_in = net.where(~up, 0.0).rolling(n, min_periods=10).sum()
    total_abs = net.abs().rolling(n, min_periods=10).sum()
    # 分母用「净流入绝对值之和」会偏大；改用「正流入总和 + 负流入绝对值总和」的一半做归一
    denom = (up_in.clip(lower=0) + dn_in.abs()).replace(0, np.nan)
    return (up_in.clip(lower=0) / denom).clip(0, 1)

crowd = rolling_crowding(20)

nsi = pd.read_csv("/data/research/nsi_signal.csv", index_col=0, parse_dates=True)
NSI = nsi["NSI"].reindex(BASE.index)
SCORE = nsi["score"].reindex(BASE.index)

fwd20 = BASE["close"].shift(-21) / BASE["close"].shift(-1) - 1

d = pd.DataFrame({"crowd": crowd, "nsi": NSI, "score": SCORE, "fwd20": fwd20}).dropna()
print(f"可用样本 {len(d)} 行（{d.index[0].date()} ~ {d.index[-1].date()}）")
print(f"crowding 分布：均值 {d['crowd'].mean():.3f} 中位 {d['crowd'].median():.3f} "
      f"P10 {d['crowd'].quantile(.1):.3f} P90 {d['crowd'].quantile(.9):.3f}")

print("\n" + "=" * 94)
print("1. 分段特征：护盘期 vs 平静期的 crowding")
print("=" * 94)
segs = [("2018Q4维稳", "2018-10-01", "2018-12-31"),
        ("2024.1-2护盘", "2024-01-01", "2024-02-29"),
        ("2025.4关税", "2025-04-01", "2025-04-30"),
        ("2026.7流入", "2026-07-01", "2026-08-10"),
        ("2026.10流入", "2026-10-01", "2026-11-02"),
        ("2021牛市顶", "2021-01-01", "2021-06-30"),
        ("2023H1平静", "2023-01-01", "2023-06-30")]
print(f"{'区段':<16}{'crowding均值':>13}{'NSI均值':>10}{'判读':>16}")
for nm, s, e in segs:
    sl = d.loc[s:e]
    if len(sl) < 5:
        continue
    c = sl["crowd"].mean()
    tag = "偏托底(逆势)" if c < 0.35 else ("中性" if c < 0.55 else "偏追涨(顺势)")
    print(f"{nm:<16}{c:>13.3f}{sl['nsi'].mean():>10.1f}{tag:>16}")

print("\n" + "=" * 94)
print("2. crowding 是否调节 NSI 的预测力")
print("=" * 94)
print("做法：按 crowding 中位数分两组，各自算 NSI 对 fwd20 的 IC")


def spear(a, b):
    a, b = pd.Series(a).astype(float), pd.Series(b).astype(float)
    m = a.notna() & b.notna()
    if m.sum() < 50:
        return np.nan
    ra, rb = a[m].rank(), b[m].rank()
    if ra.std() == 0 or rb.std() == 0:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


med = d["crowd"].median()
for lab, sub in [("低 crowding（偏托底）", d[d["crowd"] <= med]),
                 ("高 crowding（偏追涨）", d[d["crowd"] > med])]:
    ic_nsi = spear(sub["nsi"], sub["fwd20"])
    ic_sc = spear(sub["score"], sub["fwd20"])
    print(f"{lab:<22} n={len(sub):>5}  IC(NSI)={ic_nsi:+.4f}  IC(score)={ic_sc:+.4f}")

print("\n分位细化（crowding 五分位）：")
q = pd.qcut(d["crowd"], 5, labels=False, duplicates="drop")
for i in range(5):
    sub = d[q == i]
    print(f"  Q{i+1} crowd∈[{sub['crowd'].min():.2f},{sub['crowd'].max():.2f}]  "
          f"n={len(sub):>4}  IC(NSI)={spear(sub['nsi'], sub['fwd20']):+.4f}  "
          f"fwd20均值={sub['fwd20'].mean()*100:+.2f}%")

print("\n" + "=" * 94)
print("3. 若加拥挤度过滤：只在「低 crowding」时允许加仓")
print("=" * 94)
# 简化检验：把 score 在 crowding 高时打折
for thr in (0.5, 0.6, 0.7):
    adj = SCORE.where(crowd <= thr, SCORE * 0.5)
    dd = pd.DataFrame({"s": adj, "r": fwd20}).dropna()
    print(f"  crowding>{thr} 时 score 打 5 折：IC={spear(dd['s'], dd['r']):+.4f} "
          f"（对照原 score IC={spear(d['score'], d['fwd20']):+.4f}）")

out = pd.DataFrame({"crowd": crowd, "NSI": NSI, "score": SCORE,
                    "pct_chg": BASE["pct_chg"]})
out.to_csv("/data/research/nsi_crowding.csv")
print("\n-> /data/research/nsi_crowding.csv")
print(f"\n当前 crowding = {crowd.dropna().iloc[-1]:.3f}")
