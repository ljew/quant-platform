"""
NSI · 第 13 步：干净的收益来源分解 + 最终方案锁定
修 bug：上一版「只有抄底端」在 hold=None 时退化为恒满仓，分解无效。
本版用纯 where 构造，两端独立。
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
MID = TRAIN.quantile(0.50)


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


print("=" * 100)
print(f"上阈值 {HI:.1f}（训练期99分位）   中位数 {MID:.1f}")
print("=" * 100)

# ---- 干净的两端分解（都用 where，不掺状态机）----
base_mid = 0.6
hi_only = pd.Series(np.where(sig >= HI, 1.0, base_mid), index=sub.index)      # 只抄底
lo_only = pd.Series(np.where(sig <= MID, 0.25, base_mid), index=sub.index)    # 只躲跌
both = pd.Series(np.where(sig >= HI, 1.0, np.where(sig <= MID, 0.25, base_mid)), index=sub.index)

rows = []
for nm, p, cost in [
    ("满仓沪深300", pd.Series(1.0, index=sub.index), 0.0),
    ("恒 60%（基准）", pd.Series(base_mid, index=sub.index), 0.0),
    ("只有抄底端 NSI>=%.0f→100%%" % HI, hi_only, 0.001),
    ("只有躲跌端 NSI<=%.0f→25%%" % MID, lo_only, 0.001),
    ("两端都用（即时版）", both, 0.001),
]:
    r = perf(p.shift(2), cost)
    rows.append(dict(方案=nm, 均仓=r["avgpos"], 年化=r["ann"], 夏普=r["sharpe"],
                     回撤=r["mdd"], 换手=r["turn"]))
print(pd.DataFrame(rows).to_string(index=False, float_format=lambda x: f"{x:.2f}"))

ins = {r["方案"]: r for r in rows}
m60 = ins["恒 60%（基准）"]["年化"]
print(f"\n增量分解（相对恒 60% 的 {m60:.2f}%）：")
print(f"  抄底端单独贡献 {ins['只有抄底端 NSI>=%.0f→100%%' % HI]['年化'] - m60:+.2f}pp")
print(f"  躲跌端单独贡献 {ins['只有躲跌端 NSI<=%.0f→25%%' % MID]['年化'] - m60:+.2f}pp")
print(f"  两端合用       {ins['两端都用（即时版）']['年化'] - m60:+.2f}pp")
print("\n注意：即时版（每天按当日 NSI 定仓）换手 = "
      f"{ins['两端都用（即时版）']['换手']:.0f}，成本吃掉很多，所以实盘要用迟滞带。")

# ---- 最终方案：迟滞带（上 99 分位 / 下中位数）----
def hyst(sig, hi, lo, m_hi=1.0, m_lo=0.25, m_mid=base_mid):
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


print("\n" + "=" * 100)
print("最终方案：迟滞带（上 99 分位 / 下中位数 / 中仓 60%）")
print("=" * 100)
final_m = hyst(sig, HI, MID)
rf = perf(final_m.shift(2))
print(f"均仓 {rf['avgpos']:.1f}%  年化 {rf['ann']:.2f}%  夏普 {rf['sharpe']:.2f}  "
      f"回撤 {rf['mdd']:.2f}%  换手 {rf['turn']:.0f}")

base = perf(pd.Series(1.0, index=sub.index).shift(2), 0.0)
yb = (base["r"].groupby(base["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
y = (rf["r"].groupby(rf["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
common = y.index.intersection(yb.index)
print("\n分年度：")
print(pd.DataFrame({"NSI": y[common], "满仓": yb[common], "差": (y[common]-yb[common]).round(2)})
      .to_string(float_format=lambda x: f"{x:+.2f}"))
bear = (y[common]-yb[common])[yb[common] < 0]
bull = (y[common]-yb[common])[yb[common] > 0]
print(f"\n熊市年({len(bear)})超额 {bear.mean():+.2f}pp   牛市年({len(bull)})超额 {bull.mean():+.2f}pp")
print(f"跑赢 {(y[common]>yb[common]).sum()}/{len(common)} 年")

mm = final_m.dropna()
print(f"\n仓位分布：满仓 {(mm>=1).mean()*100:.0f}%   中仓 {((mm>0.3)&(mm<1)).mean()*100:.0f}%   低仓 {(mm<=0.3).mean()*100:.0f}%")
last_change = final_m.diff().ne(0).pipe(lambda s: s[s].index[-1])
print(f"当前：NSI={sig.dropna().iloc[-1]:.1f}  乘数={mm.iloc[-1]:.2f}  "
      f"最后变动 {last_change.date()}")
print(f"若有公告确认（2023-10/2024-02/2025-04 级别）则可视为高置信度，否则中置信度")

pd.DataFrame({"NSI": sig, "multiplier": final_m,
              "FLOW": sub["FLOW"], "PRESS": sub["PRESS"]}).to_csv("/data/research/nsi_signal.csv")
print("\n信号表 -> /data/research/nsi_signal.csv")
