"""
NSI · 第 11 步：解决迟滞带粘性
痛点：2026-10-23 触发满仓后 NSI 已回落到 48.9，仓位仍锁在 100%，
     因为原设计要等跌破下阈值 34.4 才降仓。

两种修法：
  机制 A 不对称迟滞 —— 上阈值 80.4 触发，下阈值抬到 50/55/60/65/70（信号强度决定，不是时间）
  机制 B 时间衰减   —— 触发后 N 个交易日自动降回中仓，期间再触发则重置

判定：既要解决粘性（看 2026-10 这轮的反应速度），又不能把换手抬上去（成本会吃掉 alpha）。
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

sub = df.loc[df["NSI_cond"].first_valid_index():]
sig = sub["NSI_cond"]
ret = r_oo_full.reindex(sub.index)
TRAIN_END = pd.Timestamp("2023-12-31")
HI = sig[sig.index <= TRAIN_END].quantile(0.99)
LO = sig[sig.index <= TRAIN_END].quantile(0.50)
MID = 0.60


def asym(sig, hi, lo, m_hi=1.0, m_lo=0.25, m_mid=MID):
    """不对称迟滞：上穿 hi 满仓，下破 lo 才降；lo 由 param 给"""
    s = np.asarray(sig, dtype=float)
    m = np.full(len(s), np.nan)
    cur = m_mid
    for i in range(len(s)):
        if np.isnan(s[i]):
            m[i] = np.nan
        else:
            if s[i] >= hi: cur = m_hi
            elif s[i] <= lo: cur = m_lo if lo < LO * 1.5 else m_mid
            m[i] = cur
    return pd.Series(m, index=sig.index)


def decay(sig, hi, hold, m_hi=1.0, m_mid=MID, lo=LO, m_lo=0.25):
    """触发后 hold 个交易日内保持满仓，到期回中仓；跌破 lo 直接低仓"""
    s = np.asarray(sig, dtype=float)
    m = np.full(len(s), np.nan)
    last = -10**9
    low_state = False
    for i in range(len(s)):
        if np.isnan(s[i]):
            m[i] = np.nan
            continue
        if s[i] >= hi:
            last = i
            low_state = False
        if s[i] <= lo:
            low_state = True
        if low_state:
            m[i] = m_lo
        elif i - last <= hold:
            m[i] = m_hi
        else:
            m[i] = m_mid
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
                avgpos=avg * 100, turn=d.sum(), r=r)


print("=" * 112)
print(f"基准参数：上阈值 {HI:.1f}（训练期99分位）  中位数 {LO:.1f}  中仓 {MID}")
print("=" * 112)

print("\n机制 A：不对称迟滞（下阈值抬高）")
print(f"{'下阈值':<10}{'均仓':>7}{'年化':>8}{'夏普':>8}{'回撤':>9}{'换手':>7}{'当前乘数':>10}")
resA = {}
for lo in (LO, 45, 50, 55, 60, 65, 70):
    m = asym(sig, HI, lo)
    p = m.shift(2)
    r = perf(p, ret)
    cur = m.dropna().iloc[-1]
    resA[lo] = (r, cur, m)
    print(f"{lo:<10.1f}{r['avgpos']:>6.1f}%{r['ann']:>7.2f}%{r['sharpe']:>8.2f}"
          f"{r['mdd']:>8.2f}%{r['turn']:>7.0f}{cur:>10.2f}")

print("\n机制 B：时间衰减（触发后保持 N 日）")
print(f"{'保持日数':<10}{'均仓':>7}{'年化':>8}{'夏普':>8}{'回撤':>9}{'换手':>7}{'当前乘数':>10}")
resB = {}
for hold in (10, 20, 40, 60, 90, 250):
    m = decay(sig, HI, hold)
    p = m.shift(2)
    r = perf(p, ret)
    cur = m.dropna().iloc[-1]
    resB[hold] = (r, cur, m)
    print(f"{hold:<10}{r['avgpos']:>6.1f}%{r['ann']:>7.2f}%{r['sharpe']:>8.2f}"
          f"{r['mdd']:>8.2f}%{r['turn']:>7.0f}{cur:>10.2f}")

# ---------- 2026-10 这轮：粘性是否解决 ----------
print("\n" + "=" * 112)
print("2026-10 这轮的实际反应（触发日 10-23 NSI=83.9，之后回落到 48.9）")
print("=" * 112)
window = sig.loc["2026-10-20":]
print(f"{'方案':<18}{'降仓日':>14}{'降仓后乘数':>12}{'从触发到降仓(交易日)':>22}")
for name, m in [("原设计(下阈34.4)", resA[LO][2]),
                ("下阈55", resA[55][2]),
                ("下阈60", resA[60][2]),
                ("下阈65", resA[65][2]),
                ("时间衰减20日", resB[20][2]),
                ("时间衰减40日", resB[40][2])]:
    seg = m.loc["2026-10-23":].dropna()
    chg = seg.diff().ne(0)
    if chg.any():
        d = seg[chg].index[0]
        gap = list(sub.index).index(d) - list(sub.index).index(pd.Timestamp("2026-10-23"))
        print(f"{name:<18}{str(d.date()):>14}{seg.loc[d]:>12.2f}{gap:>22}")
    else:
        print(f"{name:<18}{'仍未降仓':>14}{seg.iloc[-1]:>12.2f}{'—':>22}")

# ---------- 选定方案：分年度 ----------
print("\n" + "=" * 112)
print("候选方案分年度对比（B 口径，10bp）")
print("=" * 112)
bench = pd.Series(1.0, index=sub.index).shift(2)
rb = perf(bench, ret, 0.0)
yb = (rb["r"].groupby(rb["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
cands = {"原设计(34.4)": resA[LO][2], "下阈55": resA[55][2], "下阈60": resA[60][2],
         "下阈65": resA[65][2], "衰减40日": resB[40][2]}
yd = {}
for k, m in cands.items():
    r = perf(m.shift(2), ret)
    yd[k] = (r["r"].groupby(r["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
yd["满仓"] = yb
Y = pd.DataFrame(yd)
print(Y.to_string(float_format=lambda x: f"{x:+.2f}"))
print("\n跑赢满仓年数：")
for k in cands:
    print(f"  {k:<14} {(Y[k] > Y['满仓']).sum()}/{len(Y)}")

print("\n" + "=" * 112)
print("结论判断")
print("=" * 112)
best_a = max([(v[0]['sharpe'], k) for k, v in resA.items() if k >= 50])
best_b = max([(v[0]['sharpe'], k) for k, v in resB.items()])
print(f"  机制 A 最优下阈值（>=50）: {best_a[1]:.0f}  夏普 {best_a[0]:.2f}")
print(f"  机制 B 最优保持日数: {best_b[1]}  夏普 {best_b[0]:.2f}")
print(f"  原设计（下阈 {LO:.1f}）夏普 {resA[LO][0]['sharpe']:.2f}")
