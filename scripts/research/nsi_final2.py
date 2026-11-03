"""
最终锁定：二元加法 + 位置惩罚 + 公告封顶
结论链：
  1. 信号源用份额（量能会带来方向不稳，IC 训练 -0.19/验证 +0.13）
  2. 位置惩罚来自 GJI，实测把年化从 1.88% 提到 3.79%
  3. 映射必须用二元+迟滞（连续映射在连续噪声信号上换手 116，年化仅 1.44%）
  4. 公告封顶来自 GJI，本轮验证有效
"""
import numpy as np
import pandas as pd
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

RAW = Path("/data/research/raw")
TRAIN_END = pd.Timestamp("2023-12-31")
d = pd.read_csv("/data/research/gji_vs_nsi.csv", index_col=0, parse_dates=True)
NSI, p750 = d["NSI"], d["p750"]
IDX = d.index

hs = pd.read_csv(RAW / "idx_hs300.csv")
hs["date"] = pd.to_datetime(hs["trade_date"].astype(str))
hs = hs.sort_values("date").set_index("date").reindex(IDX)
r_oo = hs["open"].astype(float).pct_change()

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
    a = eq.iloc[-1] ** (250 / n) - 1
    vol = r.std() * np.sqrt(250)
    mdd = (eq / eq.cummax() - 1).min()
    return dict(ann=a * 100, sharpe=a / vol if vol else np.nan, mdd=mdd * 100,
                avgpos=p[ok].mean() * 100, turn=dd_.sum(), r=r)


def add_map(score, base=0.70, boost=1.0, hold=20, hi_q=0.97, cap_nocap=None):
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
    p = pd.Series(p, index=score.index)
    if cap_nocap is not None:
        p = p.where(ann == 1, np.minimum(p, cap_nocap))
    return p, thr


print("=" * 100)
print("最终对照（B 口径 T+1 开盘，10bp 单边成本）")
print("=" * 100)
rows = []
for nm, sc, cap in [
    ("纯 NSI 二元加法", NSI, None),
    ("NSI+位置惩罚 二元加法", NSI_PEN, None),
    ("NSI+位置惩罚+无公告封顶90%", NSI_PEN, 0.90),
    ("NSI+位置惩罚+无公告封顶85%", NSI_PEN, 0.85),
]:
    p, thr = add_map(sc, cap_nocap=cap)
    m = perf(p.shift(2))
    rows.append(dict(方案=nm, 阈值=round(thr, 1), 均仓=m["avgpos"], 年化=m["ann"],
                     夏普=m["sharpe"], 回撤=m["mdd"], 换手=m["turn"]))
T = pd.DataFrame(rows)
print(T.to_string(index=False, float_format=lambda x: f"{x:.2f}"))

for nm, b in [("恒70%", 0.7), ("恒50%", 0.5)]:
    m = perf(pd.Series(b, index=IDX).shift(2), 0.0)
    print(f"{nm:<26}{m['avgpos']:>7.1f}%{m['ann']:>8.2f}%{m['sharpe']:>8.2f}{m['mdd']:>8.2f}%{m['turn']:>7.0f}")

# 选定
FINAL, THR = add_map(NSI_PEN, cap_nocap=0.90)
mf = perf(FINAL.shift(2))
mb = perf(pd.Series(0.7, index=IDX).shift(2), 0.0)
print("\n" + "=" * 100)
print(f"选定：NSI+位置惩罚 → 二元加法(阈值{THR:.0f}) → 无公告封顶90%")
print("=" * 100)
print(f"均仓 {mf['avgpos']:.1f}%  年化 {mf['ann']:.2f}%  夏普 {mf['sharpe']:.2f}  "
      f"回撤 {mf['mdd']:.2f}%  换手 {mf['turn']:.0f}")
print(f"对照恒70%：年化 {mb['ann']:.2f}%  夏普 {mb['sharpe']:.2f}  回撤 {mb['mdd']:.2f}%")
print(f"超额 {mf['ann']-mb['ann']:+.2f}pp")

y = (mf["r"].groupby(mf["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
yb = (mb["r"].groupby(mb["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
c = y.index.intersection(yb.index)
print("\n分年度 vs 恒70%：")
print(pd.DataFrame({"最终": y[c], "恒70%": yb[c], "差": (y[c] - yb[c]).round(2)})
      .to_string(float_format=lambda x: f"{x:+.2f}"))
print(f"\n跑赢 {(y[c]>yb[c]).sum()}/{len(c)} 年  年均超额 {(y[c]-yb[c]).mean():+.2f}pp  "
      f"熊市年 {(y[c]-yb[c])[yb[c]<0].mean():+.2f}pp  牛市年 {(y[c]-yb[c])[yb[c]>0].mean():+.2f}pp")

cur_pos = FINAL.dropna().iloc[-1]
print(f"\n当前 2026-11-02：NSI={NSI.dropna().iloc[-1]:.1f}  NSI_PEN={NSI_PEN.dropna().iloc[-1]:.1f}")
print(f"  仓位 {cur_pos*100:.0f}%   公告确认 {'有' if ann.iloc[-1] else '无'}")
lc = FINAL.diff().ne(0).pipe(lambda s: s[s].index[-1])
print(f"  最后变动 {lc.date()}")
pd.DataFrame({"NSI": NSI, "NSI_PEN": NSI_PEN, "position": FINAL, "ann": ann}
             ).to_csv("/data/research/nsi_final.csv")
print("\n-> /data/research/nsi_final.csv")
