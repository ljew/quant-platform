"""
NSI · 第 9 步：回到原始刻度 + 阈值由训练期分布决定（防拍数字）
教训记录：滚动分位化（NSI_cond_r）把信号毁了（夏普 0.38 -> 0.02）。
         原因：绝对流入强度本身就是信息，转成「相对过去 250 天」后，
         轻微流入在流出期也会被推成高分位 → 假信号。
结论：阈值必须作用在原始刻度上，且阈值数值要由「历史分位」反推，不能手拍。
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
hs = hs.sort_values("date").set_index("date").reindex(df.index)
o = hs["open"].astype(float)
r_oo_full = o.pct_change()

first = df["NSI_cond"].first_valid_index()
sub = df.loc[first:]
sig = sub["NSI_cond"]
ret = r_oo_full.reindex(sub.index)
TRAIN_END = pd.Timestamp("2023-12-31")
train = sig[sig.index <= TRAIN_END]
valid = sig[sig.index > TRAIN_END]

print("=" * 100)
print("NSI_cond 原始刻度分布")
print("=" * 100)
for q in [0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.97, 0.98, 0.99, 1.00]:
    print(f"  {int(q*100):>3}% 分位 = {train.quantile(q):6.1f}", end="")
    if q in (0.50, 0.95, 0.99): print()
print(f"\n  训练期(至{TRAIN_END.date()}) n={len(train)}   验证期 n={len(valid)}")
print(f"  当前值 {sig.dropna().iloc[-1]:.1f}")


def hyst(sig, hi, lo, m_hi=1.0, m_lo=0.25, m_mid=0.6):
    s = np.asarray(sig, dtype=float)
    m = np.full(len(s), np.nan)
    cur = m_mid
    for i in range(len(s)):
        if np.isnan(s[i]):
            m[i] = np.nan
        else:
            if s[i] >= hi: cur = m_hi
            elif s[i] <= lo: cur = m_lo
            m[i] = cur
    return pd.Series(m, index=sig.index)


def perf(pos, ret, cost=0.001):
    ok = pos.notna() & ret.notna()
    r = pos[ok] * ret[ok]
    d = pos[ok].diff().abs().fillna(0)
    r = r - d[ok] * cost
    eq = (1 + r).cumprod()
    n = len(r)
    ann = eq.iloc[-1] ** (250 / n) - 1
    vol = r.std() * np.sqrt(250)
    mdd = (eq / eq.cummax() - 1).min()
    avg = pos[ok].mean()
    return dict(ann=ann * 100, sharpe=ann / vol if vol else np.nan, mdd=mdd * 100,
                avgpos=avg * 100, eff=(ann * 100) / avg if avg else np.nan, r=r)


# ============ 阈值稳健性：阈值来自训练期分位 ============
print("\n" + "=" * 124)
print("阈值稳健性 —— 上阈值取训练期 90/95/97/98/99 分位，下阈值固定训练期中位数")
print("=" * 124)
lo_val = train.quantile(0.50)
print(f"下阈值 = 训练期中位数 {lo_val:.1f}\n")
print(f"{'上阈值分位':<12}{'阈值值':>8}{'均仓':>7}{'年化':>8}{'夏普':>8}{'回撤':>9}{'换手':>7}"
      f"{'训练年化':>10}{'验证年化':>10}")
rows = []
for qh in (0.90, 0.95, 0.97, 0.98, 0.99):
    hi_val = train.quantile(qh)
    mm = hyst(sig, hi_val, lo_val)
    p = mm.shift(2)
    r = perf(p, ret)
    # 训练/验证拆分
    ok = p.notna() & ret.notna()
    rr = (p * ret)[ok]
    tr_r = rr[rr.index <= TRAIN_END]
    va_r = rr[rr.index > TRAIN_END]
    a_tr = ((1 + tr_r).prod() ** (250 / len(tr_r)) - 1) * 100
    a_va = ((1 + va_r).prod() ** (250 / len(va_r)) - 1) * 100
    nt = p[ok].diff().abs().sum()
    print(f"{int(qh*100):<12}{hi_val:>8.1f}{r['avgpos']:>6.1f}%{r['ann']:>7.2f}%{r['sharpe']:>8.2f}"
          f"{r['mdd']:>8.2f}%{nt:>7.0f}{a_tr:>9.2f}%{a_va:>9.2f}%")
    rows.append(dict(q=qh, hi=hi_val, ann=r["ann"], sharpe=r["sharpe"],
                     avgpos=r["avgpos"], mdd=r["mdd"], tr=a_tr, va=a_va))

R = pd.DataFrame(rows)
print(f"\n年化极差 = {R['ann'].max()-R['ann'].min():.2f}pp   验证期年化极差 = {R['va'].max()-R['va'].min():.2f}pp")
print("（极差小 = 不依赖特定阈值；极差大 = 该阈值是拟合出来的）")

# ============ 参照 ============
print("\n" + "=" * 100)
print("参照（同区间，B 口径，10bp）")
print("=" * 100)
bench = pd.Series(1.0, index=sub.index).shift(2)
rb = perf(bench, ret, 0.0)
print(f"  满仓          年化 {rb['ann']:>6.2f}%  夏普 {rb['sharpe']:.2f}  回撤 {rb['mdd']:.2f}%")
for base in (0.25, 0.33, 0.45):
    p = pd.Series(base, index=sub.index).shift(2)
    r = perf(p, ret, 0.0)
    print(f"  固定 {base*100:>4.0f}%      年化 {r['ann']:>6.2f}%  夏普 {r['sharpe']:.2f}  回撤 {r['mdd']:.2f}%")

# ============ 选定方案：98 分位（历史前 2% 极端托底） ============
print("\n" + "=" * 100)
print("选定方案：上阈值 = 训练期 98 分位，下阈值 = 训练期中位数，迟滞带")
print("=" * 100)
hi_sel = train.quantile(0.98)
mm = hyst(sig, hi_sel, lo_val)
p = mm.shift(2)
r = perf(p, ret)
yb = (rb["r"].groupby(rb["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
y = (r["r"].groupby(r["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
common = y.index.intersection(yb.index)
print(f"上阈值 {hi_sel:.1f}  下阈值 {lo_val:.1f}")
print(f"年化 {r['ann']:.2f}%  夏普 {r['sharpe']:.2f}  回撤 {r['mdd']:.2f}%  平均仓位 {r['avgpos']:.1f}%")
print("\n分年度：")
print(pd.DataFrame({"NSI": y[common], "满仓": yb[common], "差": (y[common]-yb[common]).round(2)})
      .to_string(float_format=lambda x: f"{x:+.2f}"))
win = (y[common] > yb[common]).sum()
bear = (y[common]-yb[common])[yb[common] < 0]
bull = (y[common]-yb[common])[yb[common] > 0]
print(f"\n跑赢 {win}/{len(common)} 年  熊市年超额均值 {bear.mean():+.2f}pp  牛市年超额均值 {bull.mean():+.2f}pp")

# 当前
cur = sig.dropna().iloc[-1]
cur_m = mm.dropna().iloc[-1]
lc = mm.diff().ne(0).pipe(lambda s: s[s].index[-1])
print(f"\n当前：NSI={cur:.1f}（上阈值 {hi_sel:.1f}，下阈值 {lo_val:.1f}）→ 乘数 {cur_m:.2f}，"
      f"最后变动 {lc.date()}")
pd.DataFrame({"NSI": sig, "multiplier": mm, "FLOW": sub["FLOW"], "PRESS": sub["PRESS"]}
             ).to_csv("/data/research/nsi_signal.csv")
