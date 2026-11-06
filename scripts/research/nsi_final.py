"""
最终收敛：NSI(份额) + 位置惩罚 + 映射方式对比
本轮要回答两个被 GJI 推翻/修正的问题：
  Q1 减仓到底能不能做？——上一轮说「躲跌端 -2.42pp 有害」，但那版换手 202；
                          GJI 用连续映射+3日平滑换手仅 25，回撤做到 -18%。到底谁对？
  Q2 二元加法 vs 连续映射，哪个更好？
"""
import numpy as np
import pandas as pd
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

RAW = Path("/data/research/raw")
TRAIN_END = pd.Timestamp("2023-12-31")

d = pd.read_csv("/data/research/gji_vs_nsi.csv", index_col=0, parse_dates=True)
NSI, p750, dd250 = d["NSI"], d["p750"], d["dd250"]
IDX = d.index

hs = pd.read_csv(RAW / "idx_hs300.csv")
hs["date"] = pd.to_datetime(hs["trade_date"].astype(str))
hs = hs.sort_values("date").set_index("date").reindex(IDX)
o = hs["open"].astype(float)
r_oo = o.pct_change()

ANN = [pd.Timestamp(x) for x in ["2023-10-23", "2024-02-06", "2025-04-07"]]
ann = pd.Series(0, index=IDX)
for a in ANN:
    if a in IDX:
        i = IDX.get_loc(a)
        ann.iloc[i:i + 60] = 1


def n01(s, w=250):
    return s.rolling(w, min_periods=100).apply(lambda x: (x <= x[-1]).mean(), raw=True)


pos_pen = pd.Series(np.select([p750 >= 0.90, p750 >= 0.80, p750 >= 0.65, p750 <= 0.15],
                              [-3, -2, -1, 0.5], 0.0), index=IDX).fillna(0)
NSI_PEN = (n01(NSI) * 100 + pos_pen * 8).clip(0, 100)


def perf(p, cost=0.001):
    ok = p.notna() & r_oo.notna()
    r = p[ok] * r_oo[ok]
    dd_ = p[ok].diff().abs().fillna(0)
    r = r - dd_[ok] * cost
    eq = (1 + r).cumprod()
    n = len(r)
    annr = eq.iloc[-1] ** (250 / n) - 1
    vol = r.std() * np.sqrt(250)
    mdd = (eq / eq.cummax() - 1).min()
    return dict(ann=annr * 100, sharpe=annr / vol if vol else np.nan, mdd=mdd * 100,
                avgpos=p[ok].mean() * 100, turn=dd_.sum(), r=r)


def cont_map(score, lo=0.20, hi=1.00, smooth=3):
    """连续映射：score(0-100) 线性映射到 [lo,hi] 仓位，3日平滑"""
    s = score.rolling(smooth, min_periods=1).mean()
    return (lo + (hi - lo) * (s / 100)).clip(0, 1)


def add_map(score, base=0.70, boost=1.0, hold=20, hi_q=0.97):
    tr = score[score.index <= TRAIN_END].dropna()
    thr = tr.quantile(hi_q)
    s = score.values
    p = np.full(len(s), np.nan)
    last = -10 ** 9
    for i in range(len(s)):
        if np.isnan(s[i]):
            p[i] = np.nan
            continue
        if s[i] >= thr:
            last = i
        p[i] = boost if i - last <= hold else base
    return pd.Series(p, index=score.index), thr


print("=" * 104)
print("Q1+Q2 对照：同一信号(NSI+位置惩罚)，不同映射")
print("=" * 104)
rows = []
for nm, p in [
    ("连续映射 20%~100%（3日平滑）", cont_map(NSI_PEN, 0.20, 1.00, 3)),
    ("连续映射 40%~100%（3日平滑）", cont_map(NSI_PEN, 0.40, 1.00, 3)),
    ("连续映射 50%~100%（3日平滑）", cont_map(NSI_PEN, 0.50, 1.00, 3)),
    ("二元加法 70%→100%", add_map(NSI_PEN)[0]),
]:
    m = perf((p if isinstance(p, pd.Series) else p).shift(2))
    rows.append(dict(映射=nm, **{k: m[k] for k in ("avgpos", "ann", "sharpe", "mdd", "turn")}))
print(pd.DataFrame(rows).to_string(index=False, float_format=lambda x: f"{x:.2f}"))

# ---------- 净减仓能力诊断：把映射拆成「加仓端」和「减仓端」 ----------
print("\n" + "=" * 104)
print("Q1 深挖：连续映射下，减仓端是正贡献还是负贡献？")
print("=" * 104)
cont = cont_map(NSI_PEN, 0.20, 1.00, 3)
only_high = cont.where(cont >= 0.5, 0.5)      # 只保留高于 50% 的部分（不降仓）
only_low = cont.where(cont <= 0.5, 0.5)       # 只保留低于 50% 的部分（只降仓）
for nm, p in [("完整连续映射", cont), ("只保留加仓端(不低于50%)", only_high),
              ("只保留减仓端(不高于50%)", only_low),
              ("恒 50% 基准", pd.Series(0.5, index=IDX))]:
    m = perf(p.shift(2))
    print(f"{nm:<26}{m['avgpos']:>7.1f}%{m['ann']:>8.2f}%{m['sharpe']:>8.2f}{m['mdd']:>8.2f}%{m['turn']:>7.0f}")

# ---------- 公告封顶 ----------
print("\n" + "=" * 104)
print("公告封顶（GJI 的归因纪律）：无公告确认时压低仓位上限")
print("=" * 104)
for cap in (1.00, 0.90, 0.85, 0.80):
    p = cont.copy()
    p[ann == 0] = np.minimum(p[ann == 0], cap)
    m = perf(p.shift(2))
    print(f"无公告上限 {cap*100:.0f}%  均仓 {m['avgpos']:>5.1f}%  年化 {m['ann']:>6.2f}%  "
          f"夏普 {m['sharpe']:.2f}  回撤 {m['mdd']:>6.2f}%  换手 {m['turn']:.0f}")

# ---------- 选定方案分年度 ----------
FINAL = cont.copy()
FINAL[ann == 0] = np.minimum(FINAL[ann == 0], 0.90)
mf = perf(FINAL.shift(2))
print("\n" + "=" * 104)
print("选定方案：NSI+位置惩罚 → 连续映射 20~100%(3日平滑) → 无公告封顶 90%")
print("=" * 104)
print(f"均仓 {mf['avgpos']:.1f}%  年化 {mf['ann']:.2f}%  夏普 {mf['sharpe']:.2f}  "
      f"回撤 {mf['mdd']:.2f}%  换手 {mf['turn']:.0f}")

mb = perf(pd.Series(0.5, index=IDX).shift(2), 0.0)
y = (mf["r"].groupby(mf["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
yb = (mb["r"].groupby(mb["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
c = y.index.intersection(yb.index)
print("\n分年度 vs 恒 50%：")
print(pd.DataFrame({"最终方案": y[c], "恒50%": yb[c], "差": (y[c] - yb[c]).round(2)})
      .to_string(float_format=lambda x: f"{x:+.2f}"))
print(f"\n跑赢恒50% {(y[c]>yb[c]).sum()}/{len(c)} 年   年均超额 {(y[c]-yb[c]).mean():+.2f}pp")
print(f"熊市年({(yb[c]<0).sum()}) 超额 {(y[c]-yb[c])[yb[c]<0].mean():+.2f}pp   "
      f"牛市年({(yb[c]>0).sum()}) 超额 {(y[c]-yb[c])[yb[c]>0].mean():+.2f}pp")

cur = FINAL.dropna().iloc[-1]
print(f"\n当前：NSI={NSI.dropna().iloc[-1]:.1f}  NSI_PEN={NSI_PEN.dropna().iloc[-1]:.1f}  "
      f"仓位={cur*100:.0f}%  公告确认={'有' if ann.iloc[-1] else '无'}")
pd.DataFrame({"NSI": NSI, "NSI_PEN": NSI_PEN, "position": FINAL, "ann": ann}
             ).to_csv("/data/research/nsi_final.csv")
print("-> /data/research/nsi_final.csv")
