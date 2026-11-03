"""
NSI · 第 3 步：合成 + 风险维度检验 + 条件化 + 仓位策略回测
关键转变：仓位控制关心的是「下行风险」，不是「收益预测」。
所以除 IC 外，必须看分组后的尾部损失 / 下行波动 / 最大回撤。
"""
import numpy as np
import pandas as pd
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

RAW = Path("/data/research/raw")
df = pd.read_csv("/data/research/nsi_features.csv", index_col=0, parse_dates=True)

def spear(a, b):
    a, b = pd.Series(a).astype(float), pd.Series(b).astype(float)
    m = a.notna() & b.notna()
    if m.sum() < 30: return np.nan
    ra, rb = a[m].rank(), b[m].rank()
    if ra.std() == 0 or rb.std() == 0: return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])

def roll_rank(s, w=250, minp=80):
    return s.rolling(w, min_periods=minp).apply(lambda x: (x <= x[-1]).mean(), raw=True)

TRAIN_END = pd.Timestamp("2023-12-31")

# ================= 1. 分组定义（全部按理论方向，不按 IC 调方向） =================
FLOW = ["A1_etf_pulse_1d", "A2_etf_mom_5d", "A3_etf_breadth", "A4_etf_netflow", "C3_etf_mom_20"]
BEHAV = ["B1_close_pos", "B2_late_session", "B3_style", "B4_bank_excess", "B5_upper_shadow"]
STATE = ["C1_mom_20", "C2_dd_250", "C4_vol_20"]

# 滚动分位（0-1），无未来信息
Z = pd.DataFrame({c: roll_rank(df[c]) for c in FLOW + BEHAV + STATE}, index=df.index)
df = df.join(Z, rsuffix="_p")

df["FLOW"] = df[[c + "_p" for c in FLOW]].mean(axis=1)
df["BEHAV"] = df[[c + "_p" for c in BEHAV]].mean(axis=1)

# 压力：跌得越多 + 波动越大 → 压力越大
df["PRESS"] = ((-df["C1_mom_20"]).pipe(roll_rank) + df["C4_vol_20"].pipe(roll_rank)) / 2

# 三个候选指数
df["NSI_flow"] = df["FLOW"] * 100
df["NSI_eq"] = (df["FLOW"] * 0.6 + df["BEHAV"] * 0.4) * 100
df["NSI_cond"] = df["FLOW"] * (0.4 + 0.6 * df["PRESS"]) * 100

# 关键修正：乘法结构使 NSI_cond 分布严重左偏（大量低值），80/20 阈值失去意义。
# 对最终指数再做一次滚动 250 日分位，使输出成为均匀分布的 0-100 刻度。
for c in ["NSI_flow", "NSI_eq", "NSI_cond"]:
    df[c + "_r"] = roll_rank(df[c]) * 100

CANDS = ["NSI_flow", "NSI_eq", "NSI_cond"]
CANDS_R = ["NSI_flow_r", "NSI_eq_r", "NSI_cond_r"]
print("\n分布检查（原始 vs 滚动分位后的 >80 / <20 占比）")
for c in CANDS + CANDS_R:
    s = df[c].dropna()
    print(f"  {c:<14} >80: {(s>80).mean()*100:5.1f}%   <20: {(s<20).mean()*100:5.1f}%   均值 {s.mean():5.1f}")

# ================= 2. 收益 + 风险双维度分组 =================
def report(col):
    d = df[[col, "fwd20"]].dropna()
    if len(d) < 300: return None
    q = pd.qcut(d[col], 5, labels=False, duplicates="drop")
    out = {}
    for seg, sub in [("训练", d[d.index <= TRAIN_END]), ("验证", d[d.index > TRAIN_END]), ("全段", d)]:
        qq = pd.qcut(sub[col], 5, labels=False, duplicates="drop")
        g = sub.groupby(qq)["fwd20"]
        mean = (g.mean() * 100).round(2)
        # 风险维度：最差 10% 分位、负收益均值、下行波动
        tail = (g.quantile(0.10) * 100).round(2)
        dnside = sub[sub["fwd20"] < 0].groupby(qq)["fwd20"].mean() * 100
        out[seg] = dict(mean=mean.tolist(), tail=tail.tolist(),
                        dns=dnside.round(2).tolist(), ic=spear(sub[col], sub["fwd20"]))
    return out

print("=" * 78)
print("候选指数 × 五分组：平均收益 / 最差10%分位（尾部风险）")
print("=" * 78)
for c in CANDS:
    r = report(c)
    if not r: continue
    print(f"\n【{c}】")
    for seg in ["训练", "验证", "全段"]:
        s = r[seg]
        print(f"  {seg} IC={s['ic']:+.4f}")
        print(f"    平均收益 Q1→Q5: {s['mean']}")
        print(f"    最差10%  Q1→Q5: {s['tail']}")
        print(f"    负收益均 Q1→Q5: {s['dns']}")
    # 关键：高分组尾部风险是否更小
    t0, t4 = r["全段"]["tail"][0], r["全段"]["tail"][-1]
    print(f"  >> 尾部改善 Q5−Q1 = {t4-t0:+.2f}pp（正=高分组跌得更少，是我们想要的）")

# ================= 3. 仓位策略回测 =================
print("\n" + "=" * 78)
print("仓位策略回测：按 NSI 分档调总仓位（T+1 收盘调仓，持有到下次信号）")
print("=" * 78)
# 日收益（沪深300），信号在 T 日收盘后，T+1 生效 → shift(1)
hs = pd.read_csv(RAW / "idx_hs300.csv")
hs["date"] = pd.to_datetime(hs["trade_date"].astype(str))
hs = hs.sort_values("date").set_index("date")
ret = hs["pct_chg"] / 100
ret = ret.reindex(df.index)

def backtest(sig, name, low=0.2, mid=0.6, high=1.0):
    """sig 为 0-100 分位刻度；T+1 收盘调仓"""
    s = sig.reindex(df.index)
    pos = pd.Series(np.nan, index=df.index)
    pos[s >= 80] = high
    pos[(s >= 60) & (s < 80)] = mid + 0.2
    pos[(s >= 40) & (s < 60)] = mid
    pos[(s >= 20) & (s < 40)] = mid - 0.2
    pos[s < 20] = low
    pos = pos.ffill().shift(1)          # T+1 生效，杜绝前视
    ok = pos.notna() & ret.notna()
    r = (pos[ok] * ret[ok])
    eq = (1 + r).cumprod()
    ann = eq.iloc[-1] ** (250 / len(r)) - 1
    vol = r.std() * np.sqrt(250)
    dd = (eq / eq.cummax() - 1).min()
    turn = pos[ok].diff().abs().sum()   # 累计换手
    return dict(name=name, ann=ann * 100, vol=vol * 100, sharpe=ann / vol if vol else np.nan,
                mdd=dd * 100, avgpos=pos[ok].mean() * 100, turn=turn, n=len(r),
                calmar=(ann * 100) / abs(dd * 100) if dd else np.nan,
                start=df.index[ok][0].date(), end=df.index[ok][-1].date())

rows = []
for c in CANDS:
    rows.append(backtest(df[c], c))
# 基准：满仓持有
bench = backtest(pd.Series(100.0, index=df.index), "满仓基准", 1.0, 1.0, 1.0)
rows.append(bench)
# 反向对照：NSI 越低仓位越高，若夏普反而更好，说明方向搞反了
rows.append(backtest(100 - df["NSI_flow"], "NSI_flow(反向对照)"))
# 随机对照：每日随机分位，检验是否只是「降仓位降波动」的机械效果
rng = np.random.default_rng(42)
rand = pd.Series(rng.uniform(0, 100, len(df)), index=df.index).rolling(20, min_periods=1).mean()
rows.append(backtest(rand, "随机分位对照"))
# 最关键对照：固定 60% 仓位（与 NSI 策略平均仓位相当）—— 择时必须打得过它
rows.append(backtest(pd.Series(60.0, index=df.index), "固定60%仓位"))

bt = pd.DataFrame(rows)
print(bt.to_string(index=False, float_format=lambda x: f"{x:.2f}"))

# ================= 4. 条件化事件：暴跌 × 流入 =================
print("\n" + "=" * 78)
print("条件化事件检验：当日跌幅与资金流同向（暴跌且流入）")
print("=" * 78)
df["ret_t"] = ret
df["crash"] = (df["ret_t"] < -0.01).astype(int)
for thr, lab in [(0.5, "资金分位>0.5"), (0.7, "资金分位>0.7")]:
    sel = df[(df["crash"] == 1) & (df["FLOW"] > thr)]
    base = df[df["crash"] == 1]
    if len(sel) > 20:
        print(f"{lab}: n={len(sel)}  后续20日均值 {sel['fwd20'].mean()*100:+.2f}%  "
              f"vs 纯暴跌 n={len(base)} {base['fwd20'].mean()*100:+.2f}%  "
              f"差 {(sel['fwd20'].mean()-base['fwd20'].mean())*100:+.2f}pp")

df.to_csv("/data/research/nsi_index.csv")
print("\n指数序列已存 /data/research/nsi_index.csv")
print(f"最新值: " + "  ".join(f"{c}={df[c].iloc[-1]:.1f}" for c in CANDS))
