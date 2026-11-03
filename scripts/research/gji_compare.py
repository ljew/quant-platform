"""
GJI vs NSI 正面对比
=========================
两套口径：
  GJI_vol  : 7只宽基ETF「成交量」量比 —— GJI 原文口径，T日实时可得
  NSI_share: 5只宽基ETF「份额」变化   —— 本轮口径，T+1 可见

核心问题：有了份额数据，还需要量能吗？还是两者互补？
判定：对 fwd20 的 IC（训练/验证同向）、分组单调性、加法模式回测。
"""
import numpy as np
import pandas as pd
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

RAW = Path("/data/research/raw")
TRAIN_END = pd.Timestamp("2023-12-31")

# ---------- 基础 ----------
hs = pd.read_csv(RAW / "idx_hs300.csv")
hs["date"] = pd.to_datetime(hs["trade_date"].astype(str))
hs = hs.sort_values("date").set_index("date")
BASE = hs[["open", "high", "low", "close", "pct_chg"]].dropna()
IDX = list(BASE.index)

# ---------- 7 只 ETF 量能 ----------
VOL = {}
for tag in ["hs300a", "hs300b", "sz50", "zz500", "cyb", "zz1000", "kc50"]:
    d = pd.read_csv(RAW / f"etfvol_{tag}.csv")
    d["date"] = pd.to_datetime(d["trade_date"].astype(str))
    d = d.sort_values("date").set_index("date")
    d["vol"] = pd.to_numeric(d["vol"], errors="coerce")
    VOL[tag] = d["vol"]
V = pd.DataFrame({k: v.reindex(BASE.index) for k, v in VOL.items()})
VR = V / V.rolling(60, min_periods=20).median()          # 量比
EXC = (VR - 1).clip(lower=0)                              # 减1取正

print(f"量比分布：均值 {VR.stack().mean():.2f}  中位 {VR.stack().median():.2f} "
      f"P90 {VR.stack().quantile(.9):.2f}  有效只数(均值) {VR.notna().sum(axis=1).mean():.1f}")

# ---------- GJI 三分量 ----------
# A 量能强度 (0-40)：篮子平均异常量 → 10日均值 → 1.2倍饱和
A_raw = EXC.mean(axis=1)
A = ((A_raw.rolling(10, min_periods=5).mean()) / 1.2).clip(0, 1) * 40

# B 下跌承接 (0-40)：仅下跌日计，异常量×(0.5+收盘日内位置)，5日均值；近5日涨>3%打4折
rng = (BASE["high"] - BASE["low"]).replace(0, np.nan)
pos_in = ((BASE["close"] - BASE["low"]) / rng).clip(0, 1)
bid = EXC.mean(axis=1) * (0.5 + pos_in)
bid = bid.where(BASE["pct_chg"] < 0, 0.0)
B_ma = bid.rolling(5, min_periods=3).mean()
hot = BASE["close"].pct_change(5) > 0.03
B_ma = B_ma.where(~hot, B_ma * 0.4)
B = (B_ma / 1.2).clip(0, 1) * 40

# C 同步广度 (0-20)：量比>2 的 ETF 占比，3 日最大
breadth = (VR > 2).sum(axis=1) / VR.notna().sum(axis=1).replace(0, np.nan)
C = breadth.rolling(3, min_periods=1).max() * 20

# gate：250日回撤 <3% → 0.15，≥15% → 1
dd250 = (BASE["close"] / BASE["close"].rolling(250, min_periods=120).max() - 1) * 100
gate = 0.15 + 0.85 * (((-dd250) - 3) / 12).clip(0, 1)

GJI = (A + B + C) * gate
print(f"A(量能) 均值 {A.mean():.1f}  B(承接) 均值 {B.mean():.1f}  "
      f"C(广度) 均值 {C.mean():.1f}  gate 均值 {gate.mean():.2f}")

# ---------- NSI（份额口径，复用已算好的序列）----------
nsi_df = pd.read_csv("/data/research/nsi_index.csv", index_col=0, parse_dates=True)
NSI = nsi_df["NSI_cond"].reindex(BASE.index)
FLOW = nsi_df["FLOW"].reindex(BASE.index)
PRESS = nsi_df["PRESS"].reindex(BASE.index)

# ---------- 位置惩罚（GJI 的独创，我没有）----------
p750 = BASE["close"].rolling(750, min_periods=250).apply(lambda x: (x <= x[-1]).mean(), raw=True)

# ---------- 严格远期收益：T+1 收盘建仓 ----------
fwd20 = BASE["close"].shift(-21) / BASE["close"].shift(-1) - 1
fwd60 = BASE["close"].shift(-61) / BASE["close"].shift(-1) - 1


def spear(a, b):
    a, b = pd.Series(a).astype(float), pd.Series(b).astype(float)
    m = a.notna() & b.notna()
    if m.sum() < 30: return np.nan
    ra, rb = a[m].rank(), b[m].rank()
    if ra.std() == 0 or rb.std() == 0: return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def eval_sig(s, fwd, name):
    d = pd.concat([s.rename("s"), fwd.rename("r")], axis=1).dropna()
    out = {"name": name, "n": len(d)}
    for seg, sub in [("tr", d[d.index <= TRAIN_END]), ("va", d[d.index > TRAIN_END])]:
        out[f"ic_{seg}"] = spear(sub["s"], sub["r"])
    try:
        q = pd.qcut(d["s"], 5, labels=False, duplicates="drop")
        g = d.groupby(q)["r"].mean() * 100
        out["q"] = [round(x, 2) for x in g.tolist()]
        out["spread"] = round(g.iloc[-1] - g.iloc[0], 2)
    except Exception:
        out["q"], out["spread"] = None, np.nan
    return out


print("\n" + "=" * 100)
print("单信号对 fwd20 的预测力（训练 2018-2023 / 验证 2024-2026）")
print("=" * 100)
rows = [eval_sig(GJI, fwd20, "GJI(量能)"),
        eval_sig(NSI, fwd20, "NSI(份额)"),
        eval_sig(A, fwd20, "A 量能强度"),
        eval_sig(B, fwd20, "B 下跌承接"),
        eval_sig(C, fwd20, "C 同步广度"),
        eval_sig(FLOW, fwd20, "FLOW 份额痕迹")]
for r in rows:
    print(f"{r['name']:<14} IC训练 {r['ic_tr']:+.4f}  IC验证 {r['ic_va']:+.4f}  "
          f"五分组 {r['q']}  空多差 {r['spread']:+.2f}pp")

# ---------- 合并版 ----------
print("\n" + "=" * 100)
print("合并版尝试")
print("=" * 100)
# 各自转 0-1 后等权
def n01(s, w=250):
    return s.rolling(w, min_periods=100).apply(lambda x: (x <= x[-1]).mean(), raw=True)

COMBO1 = (n01(GJI) + n01(NSI)) / 2 * 100
COMBO2 = (n01(GJI) * 0.4 + n01(NSI) * 0.6) * 100
# GJI 的位置惩罚加成（用 750 日分位扣分）
POS_PEN = np.where(p750 >= 0.90, -3, np.where(p750 >= 0.80, -2,
                np.where(p750 >= 0.65, -1, np.where(p750 <= 0.15, 0.5, 0))))
NSI_POS = n01(NSI) * 100 + POS_PEN * 8     # 惩罚乘 8 折到 0-100 刻度

for nm, s in [("COMBO 等权", COMBO1), ("COMBO 4:6(量:份额)", COMBO2),
              ("NSI+位置惩罚", NSI_POS)]:
    r = eval_sig(s, fwd20, nm)
    print(f"{nm:<20} IC训练 {r['ic_tr']:+.4f}  IC验证 {r['ic_va']:+.4f}  "
          f"五分组 {r['q']}  空多差 {r['spread']:+.2f}pp")

# ---------- 关键区段对照 ----------
print("\n" + "=" * 100)
print("关键区段核对（GJI 文档列的护盘/对照期）")
print("=" * 100)
segments = [
    ("2018Q4维稳", "2018-10-01", "2018-12-31", "护盘(无公告)"),
    ("2024.1-2高峰", "2024-01-01", "2024-02-29", "护盘(2·6公告)"),
    ("2025.4关税", "2025-04-01", "2025-04-30", "护盘(4·7公告)"),
    ("2026.9-10回调", "2026-09-01", "2026-10-31", "待确认"),
    ("2023.10公告期", "2023-10-01", "2023-10-31", "公告但量小"),
    ("2024.5-9低吸", "2024-05-01", "2024-09-30", "GJI盲区"),
    ("2019Q1牛市", "2019-01-01", "2019-03-31", "对照"),
    ("2021核心资产牛", "2021-01-01", "2021-06-30", "对照"),
    ("2023H1平静", "2023-01-01", "2023-06-30", "对照"),
]
print(f"{'区段':<16}{'性质':<16}{'GJI均':>8}{'GJI>50占比':>11}{'NSI均':>8}{'NSI>82占比':>11}{'份额均':>8}")
for nm, s, e, kind in segments:
    sl = slice(pd.Timestamp(s), pd.Timestamp(e))
    g, n, f = GJI.loc[sl], NSI.loc[sl], FLOW.loc[sl]
    print(f"{nm:<16}{kind:<16}{g.mean():>8.1f}{(g > 50).mean()*100:>10.0f}%"
          f"{n.mean():>8.1f}{(n > 82).mean()*100:>10.0f}%{f.mean():>8.2f}")

# 保存供回测使用
pd.DataFrame({"GJI": GJI, "A": A, "B": B, "C": C, "gate": gate,
              "NSI": NSI, "FLOW": FLOW, "PRESS": PRESS,
              "dd250": dd250, "p750": p750,
              "combo_combo2": COMBO2, "nsi_pos": NSI_POS}).to_csv("/data/research/gji_vs_nsi.csv")
print("\n序列 -> /data/research/gji_vs_nsi.csv")
