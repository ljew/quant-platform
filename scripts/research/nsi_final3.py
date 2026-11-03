"""
修正：位置惩罚必须作用在 NSI 原始刻度上，不能再用滚动分位（那是上一轮已证伪的做法）
两种实现：
  A 加分式：NSI_PEN = NSI + 位置惩罚 × 系数
  B 过滤式：触发 = NSI ≥ 阈值 且 p750 < 0.65（高位不放行）
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

pen = pd.Series(np.select([p750 >= 0.90, p750 >= 0.80, p750 >= 0.65, p750 <= 0.15],
                          [-3, -2, -1, 0.5], 0.0), index=IDX).fillna(0)


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


def bt(score, base=0.70, boost=1.0, hold=20, hi_q=0.97, gate=None, cap=None):
    tr = score[(score.index <= TRAIN_END)].dropna()
    thr = tr.quantile(hi_q)
    s = score.values
    p = np.full(len(s), np.nan)
    last = -10 ** 9
    for i in range(len(s)):
        if np.isnan(s[i]):
            p[i] = np.nan
            continue
        hit = s[i] >= thr
        if gate is not None and not np.isnan(gate[i]):
            hit = hit and (gate[i] is True or gate[i] == 1)
        if hit:
            last = i
        p[i] = boost if i - last <= hold else base
    p = pd.Series(p, index=score.index)
    if cap is not None:
        p = p.where(ann == 1, np.minimum(p, cap))
    return p, thr


print("=" * 102)
print("修正后的对照（A 加分式 vs B 过滤式）")
print("=" * 102)
rows = []
cands = [
    ("纯 NSI（无位置处理）", NSI, None, None),
    ("A1 NSI + pen×4", NSI + pen * 4, None, None),
    ("A2 NSI + pen×6", NSI + pen * 6, None, None),
    ("A3 NSI + pen×8", NSI + pen * 8, None, None),
    ("B1 过滤 p750<0.65", NSI, (p750 < 0.65).astype(float), None),
    ("B2 过滤 p750<0.80", NSI, (p750 < 0.80).astype(float), None),
]
for nm, sc, gate, cap in cands:
    p, thr = bt(sc, gate=gate, cap=cap)
    m = perf(p.shift(2))
    rows.append(dict(方案=nm, 阈值=round(thr, 1), 均仓=m["avgpos"], 年化=m["ann"],
                     夏普=m["sharpe"], 回撤=m["mdd"], 换手=m["turn"]))
print(pd.DataFrame(rows).to_string(index=False, float_format=lambda x: f"{x:.2f}"))

print("\n" + "=" * 102)
print("加公告封顶（在最优候选上）")
print("=" * 102)
for nm, sc, gate in [("A2 pen×6", NSI + pen * 6, None), ("B1 过滤0.65", NSI, (p750 < 0.65).astype(float))]:
    for cap in (None, 0.90):
        p, thr = bt(sc, gate=gate, cap=cap)
        m = perf(p.shift(2))
        lab = f"{nm} + 封顶{int(cap*100)}%" if cap else f"{nm}（无封顶）"
        print(f"{lab:<26}{m['avgpos']:>7.1f}%{m['ann']:>8.2f}%{m['sharpe']:>8.2f}"
              f"{m['mdd']:>8.2f}%{m['turn']:>7.1f}")

# 选定：B1 + 封顶
FINAL_GATE = (p750 < 0.65).astype(float)
FINAL, THR = bt(NSI, gate=FINAL_GATE, cap=0.90)
mf = perf(FINAL.shift(2))
mb = perf(pd.Series(0.7, index=IDX).shift(2), 0.0)
print("\n" + "=" * 102)
print(f"选定：NSI(份额) → 位置过滤 p750<0.65 → 二元加法(阈值{THR:.1f}) → 无公告封顶90%")
print("=" * 102)
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
diff = (y[c] - yb[c])
print(f"\n跑赢 {(y[c]>yb[c]).sum()}/{len(c)} 年  年均超额 {diff.mean():+.2f}pp")
print(f"去掉 2018（唯一异常年）后年均超额 {diff.drop(2018).mean():+.2f}pp")
print(f"熊市年 {diff[yb[c]<0].mean():+.2f}pp   牛市年 {diff[yb[c]>0].mean():+.2f}pp")

cur = FINAL.dropna().iloc[-1]
lc = FINAL.diff().ne(0).pipe(lambda s: s[s].index[-1])
print(f"\n当前 2026-11-02：NSI={NSI.dropna().iloc[-1]:.1f}  p750={p750.dropna().iloc[-1]:.3f}"
      f"（{'通过' if p750.dropna().iloc[-1]<0.65 else '被过滤'}）")
print(f"  阈值 {THR:.1f}  仓位 {cur*100:.0f}%  公告确认 {'有' if ann.iloc[-1] else '无'}")
print(f"  最后变动 {lc.date()}")
pd.DataFrame({"NSI": NSI, "pen": pen, "p750": p750, "position": FINAL, "ann": ann}
             ).to_csv("/data/research/nsi_final.csv")
print("\n-> /data/research/nsi_final.csv")
