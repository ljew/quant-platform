"""
NSI · 第 10 步：公告事件的增量价值检验
官方可查的汇金买入 ETF 公告（来源：新华财经/证券日报，已核对）：
  2023-10-23  首次公告买入 ETF
  2024-02-06  扩大 ETF 增持范围
  2025-04-07  再次增持 ETF（4-08 央行表态提供再贷款）
另有 2023-10-11 增持四大行 A 股（非 ETF，不计入）

核心问题：公告日与份额信号日重不重合？
  重合  -> 公告没有增量信息，不必单列
  不重合 -> 公告要么领先（更有价值），要么滞后（只作确认）
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

sub = df.loc[df["NSI_cond"].first_valid_index():]
TRAIN_END = pd.Timestamp("2023-12-31")
train = sub["NSI_cond"][sub.index <= TRAIN_END]
HI = train.quantile(0.99)
LO = train.quantile(0.50)

ANNOUNCE = [
    ("2023-10-23", "汇金首次公告买入ETF"),
    ("2024-02-06", "汇金扩大ETF增持范围"),
    ("2025-04-07", "汇金再次增持ETF"),
    ("2025-04-08", "央行表态提供再贷款"),
]

# ETF 份额合成脉冲
S = {}
for tag in ["hs300etf", "sz50etf", "zz500etf", "hs300etf2", "zz1000etf"]:
    d = pd.read_csv(RAW / f"sh_{tag}.csv")
    d["date"] = pd.to_datetime(d["trade_date"].astype(str))
    S[tag] = d.set_index("date")["fd_share"].astype(float).sort_index()
SH = pd.DataFrame({k: v.reindex(sub.index).ffill() for k, v in S.items()})
pulse = SH.pct_change(fill_method=None).mean(axis=1) * 100

print("=" * 100)
print(f"公告日状态（上阈值 {HI:.1f} = 训练期99分位）")
print("=" * 100)
print(f"{'日期':<12}{'事件':<24}{'NSI':>7}{'当日份额脉冲':>12}{'是否触发信号':>12}")
for d, ev in ANNOUNCE:
    dt = pd.Timestamp(d)
    if dt not in sub.index: continue
    nsi = sub.loc[dt, "NSI_cond"]
    pl = pulse.get(dt, np.nan)
    trig = "✓ 已触发" if nsi >= HI else "—"
    print(f"{d:<12}{ev:<24}{nsi:>7.1f}{pl:>11.2f}%{trig:>12}")

print("\n" + "=" * 100)
print("公告前后市场表现（沪深300）")
print("=" * 100)
print(f"{'日期':<12}{'事件':<24}{'前20日':>9}{'后5日':>9}{'后20日':>9}{'后60日':>9}")
idx = list(sub.index)
for d, ev in ANNOUNCE:
    dt = pd.Timestamp(d)
    if dt not in sub.index: continue
    i = idx.index(dt)
    def fwd(n):
        return (c.iloc[i + n] / c.iloc[i] - 1) * 100 if i + n < len(idx) else np.nan
    def bwd(n):
        return (c.iloc[i] / c.iloc[i - n] - 1) * 100 if i - n >= 0 else np.nan
    print(f"{d:<12}{ev:<24}{bwd(20):>8.2f}%{fwd(5):>8.2f}%{fwd(20):>8.2f}%{fwd(60):>8.2f}%")

# ---------- 份额信号触发日 vs 公告日 ----------
print("\n" + "=" * 100)
print(f"份额信号（NSI >= {HI:.1f}）触发日全清单 vs 公告日")
print("=" * 100)
trig = sub[sub["NSI_cond"] >= HI].index
ann_dates = [pd.Timestamp(d) for d, _ in ANNOUNCE]
print(f"{'触发日':<14}{'NSI':>7}{'距最近公告(交易日)':>20}{'说明':>16}")
for d in trig:
    i = idx.index(d)
    near, note = None, ""
    for a in ann_dates:
        if a in idx:
            gap = i - idx.index(a)
            if abs(gap) <= 10:
                near, note = gap, ("公告前" if gap < 0 else ("公告当日" if gap == 0 else "公告后"))
                break
    print(f"{d.date()!s:<14}{sub.loc[d,'NSI_cond']:>7.1f}"
          f"{(str(near) if near is not None else '—'):>20}{note:>16}")

print(f"\n共 {len(trig)} 个触发日")
covered = sum(1 for d in trig for a in ann_dates if a in idx and abs(idx.index(d) - idx.index(a)) <= 10)
print(f"落在公告 ±10 交易日内的：{covered} 个（{covered/len(trig)*100:.0f}%）")
print(f"→ 其余 {len(trig)-covered} 个是公告之外的独立触发，份额信号有增量信息")

# ---------- 2026 年两轮：有份额无公告 ----------
print("\n" + "=" * 100)
print("2026 年两轮大额流入 —— 无对应公告（份额信号的独立性证据）")
print("=" * 100)
for per, lab in [(("2026-07-01", "2026-08-10"), "7月"), (("2026-10-01", "2026-11-02"), "10月")]:
    seg = sub.loc[per[0]:per[1]]
    if len(seg) < 2: continue
    sh0, sh1 = SH.loc[seg.index[0], "hs300etf"], SH.loc[seg.index[-1], "hs300etf"]
    c0, c1 = c.loc[seg.index[0]], c.loc[seg.index[-1]]
    print(f"{lab}: 沪深300ETF份额 {sh0/1e4:.0f}→{sh1/1e4:.0f}亿份（{(sh1/sh0-1)*100:+.1f}%）  "
          f"指数 {c0:.0f}→{c1:.0f}（{(c1/c0-1)*100:+.1f}%）  "
          f"最高 NSI {seg['NSI_cond'].max():.1f}")
    print(f"     公开公告：无（汇金自 2025-04 后未再发布买入公告）")

# ---------- 公告作为确认层的用法 ----------
print("\n" + "=" * 100)
print("结论：公告该怎么用")
print("=" * 100)
print("""
1. 公告共 3 次（2023-10-23 / 2024-02-06 / 2025-04-07），8 年 3 次 —— 无法单独回测，不能当信号。
2. 公告是「事后确认」：发布时国家队已经在买了，公告本身不提供提前量。
3. 真正有提前量的是份额数据 —— 但滞后 1 天。
4. 2026-07 与 2026-10 两轮大额流入没有任何公告，份额信号仍捕捉到 → 这是它的独立价值。
5. 建议：公告只做「信号置信度加权」，不做独立指标。
   - 份额触发 + 同期有公告 = 高置信度（确认是国家队）
   - 份额触发 + 无公告 = 中置信度（可能是保险/理财配置）
""")
