"""
GJI 的 S 信号 vs NSI 加法模式 —— 同口径回测
上一版的错误：拿原始 GJI 值比 IC，丢掉了 S 的三层后处理（回撤加成/位置惩罚/公告封顶）。
GJI 文档的数据本身（2018Q4 = 44.2）与我的实现完全一致，说明实现无误，
差异只在后处理。本版按文档 §3 完整实现 S，再比。

同时检验：NSI + 位置惩罚 是否优于纯 NSI。
"""
import numpy as np
import pandas as pd
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

RAW = Path("/data/research/raw")
TRAIN_END = pd.Timestamp("2023-12-31")

d = pd.read_csv("/data/research/gji_vs_nsi.csv", index_col=0, parse_dates=True)
GJI, A, B, C = d["GJI"], d["A"], d["B"], d["C"]
NSI, FLOW, PRESS = d["NSI"], d["FLOW"], d["PRESS"]
dd250, p750 = d["dd250"], d["p750"]
IDX = d.index

hs = pd.read_csv(RAW / "idx_hs300.csv")
hs["date"] = pd.to_datetime(hs["trade_date"].astype(str))
hs = hs.sort_values("date").set_index("date").reindex(IDX)
o = hs["open"].astype(float)
r_oo = o.pct_change()

# 一级公告（汇金买入ETF，官方可查）
ANN = [pd.Timestamp(x) for x in ["2023-10-23", "2024-02-06", "2025-04-07"]]
ann_flag = pd.Series(0, index=IDX)
for a in ANN:
    if a in IDX:
        i = IDX.get_loc(a)
        ann_flag.iloc[i:i + 60] = 1        # 近 60 个交易日内有一级公告


def build_S(gji, dd, p750):
    """GJI 文档 §3 的 S 分（-5~+5）"""
    g5 = gji.rolling(5, min_periods=3).mean()
    # 承接分：30→0，每 +12 约 +1，顶 3；回撤>10% 且 GJI<15 则 -1.5
    s_carry = ((g5 - 30) / 12).clip(-1.5, 3.0)
    s_carry = s_carry.where(~((dd.abs() > 10) & (gji < 15)), -1.5)
    # 回撤加成
    ddabs = dd.abs()
    s_dd = np.select([ddabs >= 25, ddabs >= 15, ddabs >= 8], [1.5, 1.0, 0.5], 0.0)
    s_dd = pd.Series(s_dd, index=gji.index)
    # 位置惩罚
    s_pos = np.select([p750 >= 0.90, p750 >= 0.80, p750 >= 0.65, p750 <= 0.15],
                      [-3, -2, -1, 0.5], 0.0)
    s_pos = pd.Series(s_pos, index=gji.index).fillna(0)
    # 公告确认
    s_ann = ann_flag * 1.0
    S = s_carry + s_dd + s_pos + s_ann
    # 上限：有公告 +5，无公告封顶 +3
    cap = np.where(ann_flag == 1, 5.0, 3.0)
    return S.clip(-5, cap)


S_gji = build_S(GJI, dd250, p750)
print("=" * 100)
print("S 信号分布（GJI 文档 §3）")
print("=" * 100)
print(f"  均值 {S_gji.mean():+.2f}  标准差 {S_gji.std():.2f}  "
      f">=2.5 占比 {(S_gji >= 2.5).mean()*100:.1f}%  <=-2 占比 {(S_gji <= -2).mean()*100:.1f}%")
print(f"  最新 S = {S_gji.dropna().iloc[-1]:+.2f}")

# ---------- 关键时点核对（对照文档 §6 表）----------
print("\n关键时点读数核对（文档 §6 列出的转折点）")
print(f"{'时点':<16}{'日期':<12}{'GJI':>7}{'S(GJI)':>9}{'文档S':>8}{'NSI':>7}")
checks = [("2021-02顶", "2021-02-10", -3.0), ("2022-04底", "2022-04-26", 2.0),
          ("2022-10底", "2022-10-31", 2.4), ("2024-02底", "2024-02-05", 3.0),
          ("2024-09二底", "2024-09-13", 0.0), ("2025-04底", "2025-04-07", 1.5),
          ("2026-06顶", "2026-06-30", -3.0), ("2019-01底", "2019-01-04", 0.5)]
for nm, dt, doc in checks:
    t = pd.Timestamp(dt)
    if t not in IDX:
        t = IDX[IDX.get_indexer([t], method="nearest")[0]]
    print(f"{nm:<16}{str(t.date()):<12}{GJI.loc[t]:>7.1f}{S_gji.loc[t]:>+9.2f}{doc:>+8.1f}{NSI.loc[t]:>7.1f}")

# ---------- S 分桶有效性复核 ----------
print("\n" + "=" * 100)
print("S 分桶 vs 未来收益（复核文档 §3 表）")
print("=" * 100)
fwd60 = hs["close"].shift(-61) / hs["close"].shift(-1) - 1
fwd20 = hs["close"].shift(-21) / hs["close"].shift(-1) - 1
dd = pd.DataFrame({"S": S_gji, "f60": fwd60, "f20": fwd20}).dropna()
bins = [(-5, 0.01, "<=0"), (0.01, 2.0, "0.5~2"), (2.0, 3.4, "2.5~3"), (3.4, 5.1, ">=3.5")]
print(f"{'S桶':<10}{'n':>6}{'60日均':>9}{'60日胜率':>10}{'20日均':>9}")
for lo, hi, lab in bins:
    sub = dd[(dd["S"] >= lo) & (dd["S"] < hi)]
    if len(sub) < 5:
        print(f"{lab:<10}{len(sub):>6}   样本不足")
        continue
    print(f"{lab:<10}{len(sub):>6}{sub['f60'].mean()*100:>+8.2f}%"
          f"{(sub['f60']>0).mean()*100:>9.0f}%{sub['f20'].mean()*100:>+8.2f}%")

# ---------- 回测：三种方案 ----------
def pos_from_score(score, hi_frac=0.97, base=0.7, boost=1.0, hold=20):
    """把 0-100 分转成加法模式仓位；阈值由训练期分位定"""
    tr = score[score.index <= TRAIN_END].dropna()
    hi = tr.quantile(hi_frac)
    s = score.values
    p = np.full(len(s), np.nan)
    last = -10**9
    for i in range(len(s)):
        if np.isnan(s[i]):
            p[i] = np.nan
            continue
        if s[i] >= hi:
            last = i
        p[i] = boost if i - last <= hold else base
    return pd.Series(p, index=score.index), hi


def pos_from_S(S, s_thr=2.5, base=0.5, full=1.0):
    """GJI 的映射：50% + 10×S，按 3 日平滑"""
    s = S.rolling(3, min_periods=1).mean()
    return (0.5 + 0.1 * s).clip(0, 1)


def perf(p, cost=0.001, hold_cost=True):
    ok = p.notna() & r_oo.notna()
    r = p[ok] * r_oo[ok]
    dd_ = p[ok].diff().abs().fillna(0)
    if hold_cost:
        r = r - dd_[ok] * cost
    eq = (1 + r).cumprod()
    n = len(r)
    ann = eq.iloc[-1] ** (250 / n) - 1
    vol = r.std() * np.sqrt(250)
    mdd = (eq / eq.cummax() - 1).min()
    return dict(ann=ann * 100, sharpe=ann / vol if vol else np.nan, mdd=mdd * 100,
                avgpos=p[ok].mean() * 100, turn=dd_.sum(), r=r)


# NSI + 位置惩罚（惩罚折算到 0-100 刻度）
def n01(s, w=250):
    return s.rolling(w, min_periods=100).apply(lambda x: (x <= x[-1]).mean(), raw=True)


pos_pen = np.select([p750 >= 0.90, p750 >= 0.80, p750 >= 0.65, p750 <= 0.15],
                    [-3, -2, -1, 0.5], 0.0)
NSI_PEN = (n01(NSI) * 100 + pd.Series(pos_pen, index=IDX).fillna(0) * 8).clip(0, 100)

schemes = []
for nm, sc in [("NSI 纯份额", NSI), ("NSI+位置惩罚", NSI_PEN)]:
    p, hi = pos_from_score(sc, hi_frac=0.97)
    schemes.append((nm + f"(阈值{hi:.0f})", p.shift(2)))
p_gji = pos_from_S(S_gji).shift(2)
schemes.append(("GJI S 映射 50%+10×S", p_gji))

print("\n" + "=" * 100)
print("回测（B 口径 T+1 开盘，10bp 单边成本）")
print("=" * 100)
print(f"{'方案':<26}{'均仓':>8}{'年化':>9}{'夏普':>8}{'回撤':>9}{'换手':>7}")
for nm, p in schemes:
    m = perf(p)
    print(f"{nm:<26}{m['avgpos']:>7.1f}%{m['ann']:>8.2f}%{m['sharpe']:>8.2f}{m['mdd']:>8.2f}%{m['turn']:>7.0f}")

for nm, base_p in [("恒70%", 0.7), ("恒50%", 0.5), ("满仓", 1.0)]:
    m = perf(pd.Series(base_p, index=IDX).shift(2), 0.0)
    print(f"{nm:<26}{m['avgpos']:>7.1f}%{m['ann']:>8.2f}%{m['sharpe']:>8.2f}{m['mdd']:>8.2f}%{m['turn']:>7.0f}")

# 分年度
print("\n分年度（%）")
yd = {}
for nm, p in schemes:
    m = perf(p)
    yd[nm] = (m["r"].groupby(m["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
mb = perf(pd.Series(0.7, index=IDX).shift(2), 0.0)
yd["恒70%"] = (mb["r"].groupby(mb["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
print(pd.DataFrame(yd).to_string(float_format=lambda x: f"{x:+.2f}"))

pd.DataFrame({"NSI": NSI, "NSI_PEN": NSI_PEN, "S_gji": S_gji, "GJI": GJI}).to_csv("/data/research/gji_compare_v2.csv")
print("\n-> /data/research/gji_compare_v2.csv")
