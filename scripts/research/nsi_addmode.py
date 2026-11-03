"""
NSI · 第 14 步：加法模式（唯一正确的形态）
依据：分解显示抄底端 +0.49pp（换手 21），躲跌端 -2.42pp（换手 202）。
     所以只保留加法：触发时加仓，其余时间守住基础仓位，永不因 NSI 低而减仓。

检验三件事：
  1. 加法是否真能提升收益（相对同基础仓位的恒仓基准）
  2. 回撤是否失控
  3. 换手是否可接受
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


def add_mode(sig, hi, base, boost, hold=20):
    """触发后 hold 个交易日内仓位 = boost，其余 = base"""
    s = np.asarray(sig, dtype=float)
    p = np.full(len(s), np.nan)
    last = -10**9
    for i in range(len(s)):
        if np.isnan(s[i]):
            p[i] = np.nan
            continue
        if s[i] >= hi:
            last = i
        p[i] = boost if i - last <= hold else base
    return pd.Series(p, index=sig.index)


print("=" * 108)
print(f"上阈值 {HI:.1f}（训练期99分位）  加法模式：触发后加仓，其余守住基础仓位")
print("=" * 108)
print(f"{'基础仓位':<10}{'触发后仓位':>10}{'均仓':>8}{'年化':>8}{'夏普':>8}{'回撤':>9}{'换手':>7}{'vs恒仓':>9}")
rows = []
for base in (0.5, 0.7, 0.9):
    for boost in (0.9, 1.0):
        if boost <= base: continue
        p = add_mode(sig, HI, base, boost, hold=20)
        r = perf(p.shift(2))
        # 同基础仓位恒仓基准（0成本，公平对照）
        rb = perf(pd.Series(base, index=sub.index).shift(2), 0.0)
        rows.append(dict(base=base, boost=boost, avg=r["avgpos"], ann=r["ann"],
                         sharpe=r["sharpe"], mdd=r["mdd"], turn=r["turn"],
                         excess=r["ann"] - rb["ann"], base_ann=rb["ann"]))
        print(f"{base*100:>6.0f}%{boost*100:>11.0f}%{r['avgpos']:>7.1f}%{r['ann']:>7.2f}%"
              f"{r['sharpe']:>8.2f}{r['mdd']:>8.2f}%{r['turn']:>7.0f}{r['ann']-rb['ann']:>+8.2f}pp")

print("\n恒仓基准（0 成本）")
for base in (0.5, 0.7, 0.9):
    rb = perf(pd.Series(base, index=sub.index).shift(2), 0.0)
    print(f"  恒 {base*100:.0f}%   年化 {rb['ann']:>6.2f}%  夏普 {rb['sharpe']:.2f}  回撤 {rb['mdd']:.2f}%")

# 持仓天数敏感性（选 base=0.7, boost=1.0）
print("\n" + "=" * 108)
print("加仓持续天数敏感性（基础 70% → 触发后 100%）")
print("=" * 108)
print(f"{'hold':<8}{'均仓':>8}{'年化':>8}{'夏普':>8}{'回撤':>9}{'换手':>7}{'vs恒70%':>10}")
rb70 = perf(pd.Series(0.7, index=sub.index).shift(2), 0.0)
for hold in (5, 10, 20, 40, 60):
    p = add_mode(sig, HI, 0.7, 1.0, hold=hold)
    r = perf(p.shift(2))
    print(f"{hold:<8}{r['avgpos']:>7.1f}%{r['ann']:>7.2f}%{r['sharpe']:>8.2f}"
          f"{r['mdd']:>8.2f}%{r['turn']:>7.0f}{r['ann']-rb70['ann']:>+9.2f}pp")

# 选定：base 70, boost 100, hold 20
SEL = add_mode(sig, HI, 0.7, 1.0, hold=20)
rs = perf(SEL.shift(2))
print("\n" + "=" * 108)
print("选定方案：基础 70%，触发后 20 个交易日 100%")
print("=" * 108)
print(f"均仓 {rs['avgpos']:.1f}%  年化 {rs['ann']:.2f}%  夏普 {rs['sharpe']:.2f}  "
      f"回撤 {rs['mdd']:.2f}%  换手 {rs['turn']:.0f}")
print(f"对照：恒 70% 年化 {rb70['ann']:.2f}%  夏普 {rb70['sharpe']:.2f}  回撤 {rb70['mdd']:.2f}%")
print(f"    满仓    年化 {perf(pd.Series(1.0, index=sub.index).shift(2), 0).__getitem__('ann'):.2f}%")

yb = (rb70["r"].groupby(rb70["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
y = (rs["r"].groupby(rs["r"].index.year).apply(lambda x: (1 + x).prod() - 1) * 100)
common = y.index.intersection(yb.index)
print("\n分年度 vs 恒 70%：")
print(pd.DataFrame({"加法模式": y[common], "恒70%": yb[common], "差": (y[common]-yb[common]).round(2)})
      .to_string(float_format=lambda x: f"{x:+.2f}"))
print(f"\n跑赢恒仓 {(y[common]>yb[common]).sum()}/{len(common)} 年  "
      f"年均超额 {(y[common]-yb[common]).mean():+.2f}pp")

print("\n仓位分布：")
print(f"  100%（加仓中） {(SEL>=1).mean()*100:.1f}%   70%（基础） {(SEL<1).mean()*100:.1f}%")
lc = SEL.diff().ne(0).pipe(lambda s: s[s].index[-1])
print(f"当前：NSI={sig.dropna().iloc[-1]:.1f}  仓位={SEL.dropna().iloc[-1]*100:.0f}%  "
      f"最后变动 {lc.date()}")

pd.DataFrame({"NSI": sig, "position": SEL, "FLOW": sub["FLOW"], "PRESS": sub["PRESS"]}
             ).to_csv("/data/research/nsi_signal.csv")
print("\n信号表 -> /data/research/nsi_signal.csv")
