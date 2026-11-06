"""
NSI · 第 7 步：执行假设敏感性（回答「披露时点」这个致命问题）
依据：上交所 PCF 规则 —— T 日 8:00 发布的清单含 T-1 日收盘后份额。
      推出：T 日份额最早 T+1 日盘前可见 → T+1 开盘可建仓。

三种执行口径：
  A close_T  : T 日收盘价建仓（隐含「收盘时就已知 T 日份额」→ 有前视，仅作上界）
  B open_T1  : T+1 开盘建仓（符合 PCF 规则，作为基准）
  C open_T2  : T+2 开盘建仓（再保守一天，作下界）

收益一律用 open-to-open，避免把隔夜跳空算成 alpha。
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

c = hs["close"].astype(float)
o = hs["open"].astype(float)
r_cc = c.pct_change()
r_oo = o.pct_change()

first = df[df["NSI_cond"].notna()].index[0]
sub = df.loc[first:]


def hyst(sig, hi=80, lo=20, p_hi=1.0, p_lo=0.25, p_mid=0.65):
    s = np.asarray(sig, dtype=float)
    pos = np.full(len(s), np.nan)
    cur = p_mid
    for i in range(len(s)):
        if np.isnan(s[i]):
            pos[i] = np.nan
        else:
            if s[i] >= hi: cur = p_hi
            elif s[i] <= lo: cur = p_lo
            pos[i] = cur
    return pd.Series(pos, index=sig.index)


def perf(pos, ret, cost=0.0):
    ok = pos.notna() & ret.notna()
    r = pos[ok] * ret[ok]
    d = pos[ok].diff().abs().fillna(0)
    r = r - d[ok] * cost
    eq = (1 + r).cumprod()
    n = len(r)
    ann = eq.iloc[-1] ** (250 / n) - 1
    vol = r.std() * np.sqrt(250)
    mdd = (eq / eq.cummax() - 1).min()
    return dict(ann=ann * 100, vol=vol * 100, sharpe=ann / vol if vol else np.nan,
                mdd=mdd * 100, avgpos=pos[ok].mean() * 100, n=n)


raw = hyst(sub["NSI_cond"])
bench = pd.Series(1.0, index=sub.index)

print("=" * 104)
print("执行假设敏感性（迟滞带策略，含 10bp 单边成本）")
print("=" * 104)
print(f"{'执行口径':<12}{'收益序列':<10}{'年化':>8}{'夏普':>8}{'回撤':>9}{'均仓':>7}")
out = {}
for name, pos_shift, ret, desc in [
    ("A close_T", 1, r_cc, "T日收盘建仓(有前视)"),
    ("B open_T1", 2, r_oo, "T+1开盘建仓(基准)"),
    ("C open_T2", 3, r_oo, "T+2开盘建仓(保守)"),
]:
    p = raw.shift(pos_shift)
    m = perf(p, ret, 0.001)
    out[name] = m
    print(f"{name:<12}{desc:<10}{m['ann']:>7.2f}%{m['sharpe']:>8.2f}{m['mdd']:>8.2f}%{m['avgpos']:>6.1f}%")

print("\n参照（同口径 B 下）")
for nm, pp in [("满仓", bench), ("固定46%", pd.Series(0.46, index=sub.index))]:
    m = perf(pp.shift(2), r_oo, 0.0)
    print(f"  {nm:<10} 年化 {m['ann']:>6.2f}%  夏普 {m['sharpe']:.2f}  回撤 {m['mdd']:.2f}%")

# 分年度（基准 B 口径）
print("\n" + "=" * 104)
print("B 口径分年度（%）")
print("=" * 104)
p = raw.shift(2)
ok = p.notna() & r_oo.notna()
r = p[ok] * r_oo[ok]
yr = (r.groupby(r.index.year).apply(lambda x: (1 + x).prod() - 1) * 100).round(2)
b = bench.shift(2)
okb = b.notna() & r_oo.notna()
rb = b[okb] * r_oo[okb]
yrb = (rb.groupby(rb.index.year).apply(lambda x: (1 + x).prod() - 1) * 100).round(2)
print(pd.DataFrame({"NSI_B口径": yr, "满仓": yrb, "差": (yr - yrb).round(2)}).to_string(float_format=lambda x: f"{x:+.2f}"))

# 关键事件：10-22 那轮在 B 口径下的实际表现
print("\n" + "=" * 104)
print("本轮（2026-10）事件还原 —— B 口径")
print("=" * 104)
ev = sub.loc["2026-10-15":]
hist = []
for d in ev.index:
    nsi = ev.loc[d, "NSI_cond"]
    if nsi >= 80: hist.append((d, nsi, "穿越80→满仓"))
    elif nsi <= 20: hist.append((d, nsi, "跌破20→低仓"))
for d, n, t in hist:
    print(f"  {d.date()}  NSI={n:.1f}  {t}")
print(f"\n沪深300：10-22 收 {c.loc['2026-10-22']:.1f} → 11-02 收 {c.loc['2026-11-02']:.1f}  "
      f"({(c.loc['2026-11-02']/c.loc['2026-10-22']-1)*100:+.2f}%)")
print(f"若 10-23 开盘建仓（B口径），至今收益：{(c.loc['2026-11-02']/o.loc['2026-10-23']-1)*100:+.2f}%")
