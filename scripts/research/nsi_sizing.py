"""
NSI · 第 8 步（修订）：仓位乘数方案 + 阈值稳健性
修正：改用滚动分位化后的指数 NSI_cond_r（原始值分布左偏，阈值失效）
执行口径：B（T+1 开盘建仓，open-to-open），10bp 单边成本
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

first = df[df["NSI_cond_r"].notna()].index[0]
sub = df.loc[first:]
SIG = "NSI_cond_r"
sig = sub[SIG]
ret = r_oo_full.reindex(sub.index)


def hyst(sig, hi=80, lo=20, m_hi=1.0, m_lo=0.4, m_mid=0.7):
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


base_m = hyst(sig)
print("=" * 106)
print(f"信号 = {SIG}（滚动250日分位，均匀 0-100）")
print("=" * 106)
print(f"乘数分布：满仓 {(base_m>=1).mean()*100:.0f}%  中间 {((base_m>0.4)&(base_m<1)).mean()*100:.0f}%  低仓 {(base_m<=0.4).mean()*100:.0f}%")

# ---------- 1. 仓位乘数 ----------
print("\n" + "=" * 106)
print("方案 A：仓位乘数 × 基础仓位（B 口径，10bp）")
print("=" * 106)
print(f"{'基础仓位':<10}{'平均仓位':>9}{'年化':>9}{'夏普':>8}{'回撤':>9}{'单位仓位年化':>13}")
res_a = {}
for base in (0.5, 0.7, 0.9, 1.0):
    p = (base * base_m).shift(2)
    r = perf(p, ret)
    res_a[base] = r
    print(f"{base*100:>6.0f}%{r['avgpos']:>10.1f}%{r['ann']:>8.2f}%{r['sharpe']:>8.2f}"
          f"{r['mdd']:>8.2f}%{r['eff']:>12.2f}%")

print("\n同平均仓位的固定仓位对照（无择时，0 成本）")
for base in (0.5, 0.7, 0.9):
    avg = (base * base_m).mean()
    p = pd.Series(avg, index=sub.index).shift(2)
    r = perf(p, ret, 0.0)
    print(f"  固定 {avg*100:4.0f}%   年化 {r['ann']:>6.2f}%  夏普 {r['sharpe']:.2f}  回撤 {r['mdd']:.2f}%")

# ---------- 2. 阈值稳健性 ----------
print("\n" + "=" * 106)
print("方案 B：阈值稳健性（满额映射，基础仓位 100%）")
print("=" * 106)
bench = pd.Series(1.0, index=sub.index).shift(2)
rb = perf(bench, ret, 0.0)
yb = (rb["r"].groupby(rb["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)

print(f"{'hi/lo':<10}{'均仓':>7}{'年化':>8}{'夏普':>8}{'回撤':>9}{'跑赢年数':>9}{'熊市超额':>10}{'牛市超额':>10}")
bt = {}
for hi, lo in [(90, 10), (85, 15), (80, 20), (75, 25), (70, 30), (65, 35)]:
    mm = hyst(sig, hi, lo)
    p = mm.shift(2)
    r = perf(p, ret)
    y = (r["r"].groupby(r["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
    common = y.index.intersection(yb.index)
    win = (y[common] > yb[common]).sum()
    bear = (y[common] - yb[common])[yb[common] < 0].mean()
    bull = (y[common] - yb[common])[yb[common] > 0].mean()
    bt[f"{hi}/{lo}"] = r["ann"]
    print(f"{hi}/{lo:<10}{r['avgpos']:>6.1f}%{r['ann']:>7.2f}%{r['sharpe']:>8.2f}"
          f"{r['mdd']:>8.2f}%{f'{win}/{len(common)}':>9}{bear:>+9.2f}pp{bull:>+9.2f}pp")
print(f"\n阈值敏感性：年化极差 {max(bt.values())-min(bt.values()):.2f}pp（<1pp = 不依赖特定阈值）")

# ---------- 3. 分年度（80/20） ----------
print("\n" + "=" * 106)
print("80/20 迟滞带 分年度（B 口径）")
print("=" * 106)
mm = hyst(sig, 80, 20)
p = mm.shift(2)
r = perf(p, ret)
y = (r["r"].groupby(r["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
common = y.index.intersection(yb.index)
print(pd.DataFrame({"NSI": y[common], "满仓": yb[common], "差": (y[common] - yb[common]).round(2)})
      .to_string(float_format=lambda x: f"{x:+.2f}"))

# ---------- 4. 当前落点 ----------
print("\n" + "=" * 106)
print("当前落点")
print("=" * 106)
cur = sig.dropna().iloc[-1]
cur_m = base_m.dropna().iloc[-1]
last_change = base_m.diff().ne(0).pipe(lambda s: s[s].index[-1])
print(f"日期 {sig.dropna().index[-1].date()}")
print(f"{SIG} = {cur:.1f}")
print(f"当前乘数 = {cur_m:.2f}   （最后一次变动 {last_change.date()}）")
print(f"→ 基础仓位 50% → 实际 {cur_m*0.5*100:.0f}%    70% → {cur_m*0.7*100:.0f}%    90% → {cur_m*0.9*100:.0f}%")

pd.DataFrame({"NSI_raw": sub["NSI_cond"], "NSI_rank": sig,
              "FLOW": sub["FLOW"], "PRESS": sub["PRESS"], "multiplier": base_m}
             ).to_csv("/data/research/nsi_signal.csv")
print("\n信号表 -> /data/research/nsi_signal.csv")
