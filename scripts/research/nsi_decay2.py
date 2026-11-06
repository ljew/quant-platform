"""
NSI · 第 12 步（修订）：衰减机制 + 收益来源诊断
上一版的 bug：decay 里叠了 low_state 逻辑，触发后很快掉回低仓，hold 参数完全被掩盖。
本版拆开：low_floor 单独设（用 1% 分位，只在极端弱市降仓），hold 独立生效。

同时回答一个更根本的问题：
  均仓 29.5% 却有 2.78% 年化 —— 这钱是「NSI 高时抄底」赚的，
  还是「NSI 低时空仓躲跌」赚的？
  把两端拆开单独测，就知道该把研发力气放在哪一头。
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
TRAIN = sig[sig.index <= pd.Timestamp("2023-12-31")]
HI = TRAIN.quantile(0.99)
FLOOR = TRAIN.quantile(0.01)


def perf(pos, cost=0.001):
    ok = pos.notna() & ret.notna()
    r = pos[ok] * ret[ok]
    d = pos[ok].diff().abs().fillna(0)
    r = r - d[ok] * cost
    eq = (1 + r).cumprod()
    n = len(r)
    ann = eq.iloc[-1] ** (250 / n) - 1
    vol = r.std() * np.sqrt(250)
    mdd = (eq / eq.cummax() - 1).min()
    return dict(ann=ann * 100, sharpe=ann / vol if vol else np.nan, mdd=mdd * 100,
                avgpos=pos[ok].mean() * 100, turn=d.sum(), r=r)


def state_machine(sig, hi, floor, hold=None, m_hi=1.0, m_mid=0.6, m_lo=0.25):
    """hold=None -> 纯迟滞（跌破 floor 降低仓，否则触发后一直满仓）
       hold=N    -> 触发后 N 日回中仓"""
    s = np.asarray(sig, dtype=float)
    m = np.full(len(s), np.nan)
    last = -10**9
    lo_state = False
    for i in range(len(s)):
        if np.isnan(s[i]):
            m[i] = np.nan
            continue
        if s[i] >= hi:
            last = i
            lo_state = False
        if s[i] <= floor:
            lo_state = True
        if lo_state:
            m[i] = m_lo
        elif hold is None or i - last <= hold:
            m[i] = m_hi if i - last <= (hold if hold else 10**9) else m_mid
        else:
            m[i] = m_mid
    return pd.Series(m, index=sig.index)


print("=" * 106)
print(f"参数：上阈值 {HI:.1f}（99分位）  极端弱市阈值 {FLOOR:.1f}（1分位）  中仓 0.6")
print("=" * 106)

print("\n衰减机制：触发后保持 N 日回中仓")
print(f"{'hold':<10}{'均仓':>7}{'年化':>8}{'夏普':>8}{'回撤':>9}{'换手':>7}{'当前乘数':>10}")
res = {}
for hold in (10, 20, 40, 60, 120, None):
    m = state_machine(sig, HI, FLOOR, hold)
    r = perf(m.shift(2))
    res[hold] = (r, m)
    lbl = "纯迟滞" if hold is None else str(hold)
    print(f"{lbl:<10}{r['avgpos']:>6.1f}%{r['ann']:>7.2f}%{r['sharpe']:>8.2f}"
          f"{r['mdd']:>8.2f}%{r['turn']:>7.0f}{m.dropna().iloc[-1]:>10.2f}")

# ---------- 收益来源诊断 ----------
print("\n" + "=" * 106)
print("收益来源诊断：抄底端 vs 躲跌端，各自贡献多少")
print("=" * 106)
base = perf(pd.Series(1.0, index=sub.index).shift(2), 0.0)

# 只用高仓端：NSI >= hi 满仓，否则中仓 0.6
hi_only = state_machine(sig, HI, -999, None)
r_hi = perf(hi_only.shift(2))
# 只用低仓端：NSI <= floor 低仓，否则中仓 0.6
lo_only = pd.Series(np.where(sig <= FLOOR, 0.25, 0.6), index=sub.index)
r_lo = perf(lo_only.shift(2))
# 两者都不做：恒 0.6
r_mid = perf(pd.Series(0.6, index=sub.index).shift(2), 0.0)

print(f"{'方案':<26}{'均仓':>7}{'年化':>9}{'夏普':>8}{'回撤':>9}")
print(f"{'满仓沪深300':<26}{base['avgpos']:>6.1f}%{base['ann']:>8.2f}%{base['sharpe']:>8.2f}{base['mdd']:>8.2f}%")
print(f"{'恒 60% 仓位（基准）':<26}{r_mid['avgpos']:>6.1f}%{r_mid['ann']:>8.2f}%{r_mid['sharpe']:>8.2f}{r_mid['mdd']:>8.2f}%")
print(f"{'只有抄底端（NSI>=%.0f 满仓）'%HI:<26}{r_hi['avgpos']:>6.1f}%{r_hi['ann']:>8.2f}%{r_hi['sharpe']:>8.2f}{r_hi['mdd']:>8.2f}%")
print(f"{'只有躲跌端（NSI<=%.0f 空仓）'%FLOOR:<26}{r_lo['avgpos']:>6.1f}%{r_lo['ann']:>8.2f}%{r_lo['sharpe']:>8.2f}{r_lo['mdd']:>8.2f}%")
full = res[None][0]
print(f"{'两端都用（纯迟滞）':<26}{full['avgpos']:>6.1f}%{full['ann']:>8.2f}%{full['sharpe']:>8.2f}{full['mdd']:>8.2f}%")

print(f"\n增量分解（相对恒 60% 基准 {r_mid['ann']:.2f}%）：")
print(f"  抄底端贡献 {(r_hi['ann']-r_mid['ann']):+.2f}pp")
print(f"  躲跌端贡献 {(r_lo['ann']-r_mid['ann']):+.2f}pp")
print(f"  两端合计 {(full['ann']-r_mid['ann']):+.2f}pp（简单相加 {r_hi['ann']+r_lo['ann']-2*r_mid['ann']:+.2f}pp，差额=交互项）")

# ---------- 选定方案分年度 ----------
print("\n" + "=" * 106)
print("纯迟滞 分年度（B 口径，10bp）")
print("=" * 106)
m = res[None][1]
r = perf(m.shift(2))
yb = (base["r"].groupby(base["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
y = (r["r"].groupby(r["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
common = y.index.intersection(yb.index)
print(pd.DataFrame({"NSI": y[common], "满仓": yb[common], "差": (y[common]-yb[common]).round(2)})
      .to_string(float_format=lambda x: f"{x:+.2f}"))
bear = (y[common]-yb[common])[yb[common] < 0]
bull = (y[common]-yb[common])[yb[common] > 0]
print(f"\n熊市年({len(bear)})超额均值 {bear.mean():+.2f}pp   牛市年({len(bull)})超额均值 {bull.mean():+.2f}pp")

# 仓位时间分布
mm = m.dropna()
print(f"\n仓位时间分布：满仓 {(mm>=1).mean()*100:.0f}%   中仓 {((mm>0.5)&(mm<1)).mean()*100:.0f}%   低仓 {(mm<=0.3).mean()*100:.0f}%")
print(f"当前：NSI={sig.dropna().iloc[-1]:.1f}  乘数={mm.iloc[-1]:.2f}")

pd.DataFrame({"NSI": sig, "multiplier": m}).to_csv("/data/research/nsi_signal.csv")
print("\n信号表 -> /data/research/nsi_signal.csv")
