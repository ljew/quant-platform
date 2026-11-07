"""一档 / 二档分层，以及「成交额 → 申购」的判据。

## 来源

参照「国家队资金·ETF 抄底监控」的做法：对每只 ETF 做「成交额 → 净申购额」
的线性回归，得到弹性 b 与拟合度 R²，据此把标的分为两档——

  · **一档**（b 接近 1、R² 接近 1）：成交额基本是申购的镜像，
    盘中放量可以直接读成「国家队动手」
  · **二档**（b 低、R² 低）：成交额里混了大量二级换手，
    用成交额反推会严重失真，只能等收盘看份额

我们复现了它的 21 日窗口口径：一档 7 只合计 `净申购 = -115 + 0.876×成交额`，
R² = 0.908，与原文的 `-114.2 + 0.872×`、R² = 0.908 落在 ±0.01 内；
逐只 b/R² 也全部吻合。

## 三处与原文不同

**1. 分层用多窗口多数票，不用单一 21 日窗口。**
21 个点做回归，R² 容易虚高；而且窗口一旦跨进另一种资金状态，b 就会漂。
改为 21 / 60 / 120 三个窗口里至少两个满足 b≥0.80 且 R²≥0.50。

**2. 常态基准用 20 日中位数，不用 60 日。**
60 日中位数在成交额系统性抬升时会严重滞后——10 月到 11 月初，一档 5 只的
日均成交额从 100 亿抬到 500 亿，而 60 日中位数还停在 77 亿，倍数算出 7 倍，
等于「天天都在放量」，判据就废了。

**3. 输出的是「历史同档概率」，不是点估计金额。**
原文用 `净申购 = -114.2 + 0.872x` 反推出一个具体数字（如「上午净申购 55 亿」），
误差叠了两层（线性折算 + 弹性外推）。这里改成给分档区间与历史命中率，
让人知道「这个量级在历史上意味着什么」，而不是一个假装精确的点值。

## 必须提醒的一件事：弹性会符号翻转

同一口径按年算，一档 7 只的弹性 b 是：

    2023 +0.40 ｜ 2024 +0.40 ｜ 2025 +0.72 ｜ 2026 −0.64 ｜ 全样本 −0.27

因为 b ≈ 当年净流入 ÷ 当年成交额。净申购年 b 为正，净赎回年 b 为负。
2026 年是净赎回年（一档 7 只全年 −9,657 亿），所以放量意味着**赎回**放量。

原文写了「弹性在震荡市和单边市会漂移，建议每月重算」，但漂移的量级是
**符号翻转**，不是数值微调。把 2026-10~11 净申购窗口算出的 b=0.87 用到
2026-01，方向是反的。因此本模块额外输出 `sign_flag`，当前年份 b<0 时
在页面上明确警告「放量不等于买入」。
"""
from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import EtfDailyBar, EtfUniverse
from app.services import etf_flow

logger = logging.getLogger("etf_elasticity")

# ── 参数（改这里就够，页面上会跟着变）─────────────────────────────
TIER_WINDOWS = (21, 60, 120)   # 分层用的三个回归窗口
TIER_VOTES = 2                 # 三个窗口里至少几个达标才归一档
B_TIER = 0.80                  # 弹性门槛
R2_TIER = 0.50                 # 拟合度门槛
BASE_WIN = 20                  # 常态成交额的基准窗口（中位数）
BIG_NET = 100.0                # 「国家队级净申购」的门槛（亿元）
RECENT_DAYS = 20               # 前端展示的近期天数
BANDS = (
    (0.0, 0.8, "<0.8×", "未入场"),
    (0.8, 1.0, "0.8–1.0×", "未入场"),
    (1.0, 1.3, "1.0–1.3×", "无异动"),
    (1.3, 1.6, "1.3–1.6×", "有异动"),
    (1.6, 2.2, "1.6–2.2×", "疑似入场"),
    (2.2, 1e9, ">2.2×", "高度疑似"),
)


def _ols(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """一元 OLS，返回 (斜率 b, 截距 a, R²)。样本不足或 x 无波动时返回 NaN。"""
    ok = ~(np.isnan(x) | np.isnan(y))
    x, y = x[ok], y[ok]
    if len(x) < 8 or np.std(x) < 1e-9:
        return np.nan, np.nan, np.nan
    b, a = np.polyfit(x, y, 1)
    pred = a + b * x
    ss_res = float(((y - pred) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan
    return float(b), float(a), float(r2)


def _panel(db: Session) -> pd.DataFrame:
    """成交额 + 净申购的长表（sym / d / amt / net）。"""
    panel = etf_flow.flow_panel(db)
    bars = pd.DataFrame(db.execute(select(
        EtfDailyBar.symbol, EtfDailyBar.trade_date, EtfDailyBar.amount)).all(),
        columns=["symbol", "trade_date", "amt"])
    if not len(panel) or not len(bars):
        return pd.DataFrame(columns=["symbol", "d", "amt", "net"])
    # flow_panel 的 trade_date 已是 YYYYMMDD 字符串；EtfDailyBar 是 Date 类型，
    # 直接 astype(str) 会得到 "2026-11-06"，两边对不上、merge 出空表
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.strftime("%Y%m%d")
    panel["trade_date"] = panel["trade_date"].astype(str)
    m = bars.merge(panel[["trade_date", "symbol", "net"]],
                   on=["trade_date", "symbol"], how="inner")
    m = m.dropna(subset=["amt", "net"])
    m["d"] = pd.to_datetime(m["trade_date"], format="%Y%m%d")
    return m.sort_values(["symbol", "d"])


def _rolling_b(df: pd.DataFrame, win: int) -> pd.DataFrame:
    """逐只滚动窗口回归，取最新一期的 b / R²。"""
    out = []
    for sym, g in df.groupby("symbol"):
        x = g["amt"].to_numpy(float)[-win:]
        y = g["net"].to_numpy(float)[-win:]
        b, _a, r2 = _ols(x, y)
        out.append({"symbol": sym, "d": g["d"].iloc[-1],
                    f"b{win}": b, f"r2{win}": r2, f"n{win}": min(win, len(g))})
    return pd.DataFrame(out)


def _classify(df: pd.DataFrame) -> pd.DataFrame:
    """多窗口多数票定档。"""
    if not len(df):
        return pd.DataFrame()
    t = _rolling_b(df, TIER_WINDOWS[0])
    for w in TIER_WINDOWS[1:]:
        t = t.merge(_rolling_b(df, w).drop(columns=["d"]), on="symbol", how="outer")

    hits = np.zeros(len(t), dtype=int)
    # 每个窗口离「刚好达标」还有多远：取 b 与 R² 两个条件里更紧的那个。
    # 用它的绝对值判「边缘」——绝对值小说明阈值一抖就会翻档。
    gaps = []
    for w in TIER_WINDOWS:
        gap = np.minimum(t[f"b{w}"] - B_TIER, t[f"r2{w}"] - R2_TIER)
        gaps.append(gap.abs())
        ok = ((t[f"b{w}"] >= B_TIER) & (t[f"r2{w}"] >= R2_TIER)).fillna(False)
        hits += ok.astype(int).to_numpy()
    t["hits"] = hits
    t["margin"] = pd.concat(gaps, axis=1).min(axis=1)
    t["tier"] = np.where(hits >= TIER_VOTES, "一档", "二档")
    t["tier1"] = hits >= TIER_VOTES
    # 「边缘」= 多数票进了一档，但有窗口贴着阈值（±0.05）。
    # 510500 的 b60 在 0.79/0.81 之间随一天数据来回跳，就是典型；
    # 单独标出来，避免把阈值抖动当成信号变化。
    t["marginal"] = t["tier1"] & (t["margin"] < 0.05).fillna(False)
    return t.sort_values(["hits", f"b{TIER_WINDOWS[0]}"], ascending=False)


def _bands(day: pd.DataFrame) -> list[dict]:
    """按放量倍数分档，统计各档的净申购分布与「国家队级」概率。"""
    d = day.dropna(subset=["mult"])
    if not len(d):
        return []
    base_rate = float((d["net"] > BIG_NET).mean() * 100)
    out = []
    for lo, hi, label, verdict in BANDS:
        g = d[(d["mult"] >= lo) & (d["mult"] < hi)]
        if not len(g):
            out.append({"label": label, "verdict": verdict, "n": 0,
                        "median": None, "positive": None, "big": None,
                        "lift": None})
            continue
        prob = float((g["net"] > BIG_NET).mean() * 100)
        out.append({
            "label": label, "verdict": verdict, "n": int(len(g)),
            "median": round(float(g["net"].median()), 1),
            "positive": round(float((g["net"] > 0).mean() * 100), 0),
            "big": round(prob, 1),
            "lift": round(prob / base_rate, 1) if base_rate > 0 else None,
        })
    return out


def build_elasticity(db: Session) -> dict:
    """组装分层、当前判据、分档历史与符号检查。"""
    if db.scalar(select(func.count()).select_from(EtfDailyBar)) == 0:
        return {"available": False}

    df = _panel(db)
    if not len(df) or df["symbol"].nunique() < 3:
        return {"available": False}

    tiers = _classify(df)
    meta = pd.DataFrame(db.execute(select(
        EtfUniverse.symbol, EtfUniverse.name, EtfUniverse.category,
        EtfUniverse.scale)).all(), columns=["symbol", "name", "category", "scale"])
    tiers = tiers.merge(meta, on="symbol", how="left")

    members = [{
        "symbol": r.symbol,
        "name": r.name or "",
        "category": r.category or "",
        "scale": round(float(r.scale or 0), 0),
        "b21": None if pd.isna(r.b21) else round(float(r.b21), 2),
        "b60": None if pd.isna(r.b60) else round(float(r.b60), 2),
        "b120": None if pd.isna(r.b120) else round(float(r.b120), 2),
        "r2_60": None if pd.isna(r.r260) else round(float(r.r260), 2),
        "hits": int(r.hits),
        "tier": r.tier,
        "marginal": bool(r.marginal),
    } for r in tiers.itertuples()]

    t1 = tiers[tiers["tier1"]]["symbol"].tolist()
    if not t1:
        return {"available": False, "members": members,
                "reason": "当前没有标的满足一档条件"}

    # ── 一档合计的日度序列 ──
    sub = df[df["symbol"].isin(t1)]
    day = sub.groupby("d").agg(amt=("amt", "sum"), net=("net", "sum")).sort_index()
    day["base"] = day["amt"].rolling(BASE_WIN, min_periods=max(5, BASE_WIN // 2)).median()
    day["mult"] = day["amt"] / day["base"]
    day_ok = day.dropna(subset=["mult"])

    last = day.iloc[-1]
    mult = None if pd.isna(last["mult"]) else round(float(last["mult"]), 2)
    band = next((b for b in _bands(day) if mult is not None and (
        (b["label"] == "<0.8×" and mult < 0.8)
        or (b["label"] == "0.8–1.0×" and 0.8 <= mult < 1.0)
        or (b["label"] == "1.0–1.3×" and 1.0 <= mult < 1.3)
        or (b["label"] == "1.3–1.6×" and 1.3 <= mult < 1.6)
        or (b["label"] == "1.6–2.2×" and 1.6 <= mult < 2.2)
        or (b["label"] == ">2.2×" and mult >= 2.2))), None)

    base_rate = round(float((day_ok["net"] > BIG_NET).mean() * 100), 1) if len(day_ok) else None

    # ── 符号检查：年度弹性 vs 近 60 日弹性 ──
    # 一档 5 只在 2026 年同时存在两种状态：全年净赎回（b 为负）、近两月净申购（b≈1）。
    # 只看年度会误判成「放量等于卖出」，只看近 60 日又丢掉了「全年在撤退」的背景。
    # 所以两个都算，并判断当前处于哪一种 regime。
    cur_year = last.name.year
    ytd = day[day.index.year == cur_year]
    b_ytd, _a, r2_ytd = _ols(ytd["amt"].to_numpy(float), ytd["net"].to_numpy(float))
    b_now, _a2, r2_now = _ols(day["amt"].tail(60).to_numpy(float),
                              day["net"].tail(60).to_numpy(float))
    net_ytd = float(ytd["net"].sum())

    if pd.notna(b_now) and b_now >= 0.5 and pd.notna(b_ytd) and b_ytd < 0:
        regime, tone = "regime_shift", "warn"
        note = (f"{cur_year} 年至今一档合计净申购 {net_ytd:+.0f} 亿、年度弹性 b="
                f"{b_ytd:+.2f}（净赎回）；但近 60 日 b 已转为 {b_now:+.2f}，"
                "资金状态发生了切换 —— 当前判据有效，年度负号只是背景。")
    elif pd.notna(b_now) and b_now < -0.3:
        regime, tone = "aligned_sell", "down"
        note = (f"近 60 日弹性 b={b_now:+.2f} 为负，当前处于净赎回状态 —— "
                "放量意味着赎回放量，不能读成国家队买入，判据需反向使用。")
    elif pd.notna(b_now) and b_now >= 0.5:
        regime, tone = "aligned_buy", "up"
        note = (f"近 60 日弹性 b={b_now:+.2f}、年度 b={b_ytd:+.2f}，"
                "两个窗口同向 —— 成交额放大可读成资金进场，判据有效。")
    else:
        regime, tone = "unclear", "plain"
        note = (f"近 60 日弹性 b={b_now:+.2f}，处于阈值之间 —— "
                "成交额与申购的关系不稳定，当前判据仅供参考。")

    sign_flag = {
        "year": int(cur_year),
        "b_ytd": None if pd.isna(b_ytd) else round(float(b_ytd), 2),
        "r2_ytd": None if pd.isna(r2_ytd) else round(float(r2_ytd), 2),
        "b_now": None if pd.isna(b_now) else round(float(b_now), 2),
        "r2_now": None if pd.isna(r2_now) else round(float(r2_now), 2),
        "net_sum": round(net_ytd, 0),
        "regime": regime,
        "tone": tone,
        "note": note,
    }

    # ── 滚动 60 日弹性轨迹 ──
    # b 什么时候从正翻负、什么时候回到 0.8 以上，是判据「有效/失效」的唯一可视信号。
    # 只画一档合计的 b（逐只画会糊成一片）。
    trend = []
    arr_a = day["amt"].to_numpy(float)
    arr_n = day["net"].to_numpy(float)
    for i in range(59, len(day)):
        b_i, _ai, _ri = _ols(arr_a[i - 59:i + 1], arr_n[i - 59:i + 1])
        if pd.isna(b_i):
            continue
        trend.append({"date": day.index[i].strftime("%Y-%m-%d"),
                      "b": round(float(b_i), 2)})
    trend = trend[-250:]

    recent = [{
        "date": d.strftime("%Y-%m-%d"),
        "amount": round(float(r["amt"]), 1),
        "base": None if pd.isna(r["base"]) else round(float(r["base"]), 1),
        "mult": None if pd.isna(r["mult"]) else round(float(r["mult"]), 2),
        "net": None if pd.isna(r["net"]) else round(float(r["net"]), 1),
    } for d, r in day.tail(RECENT_DAYS).iterrows()]

    total_amt = float(day["amt"].sum())
    share_all = None
    all_amt = float(df.groupby("d")["amt"].sum().sum())
    if all_amt > 0:
        share_all = round(total_amt / all_amt * 100, 0)

    return {
        "available": True,
        "as_of": last.name.strftime("%Y-%m-%d"),
        "params": {"windows": list(TIER_WINDOWS), "votes": TIER_VOTES,
                   "b": B_TIER, "r2": R2_TIER, "base_win": BASE_WIN,
                   "big_net": BIG_NET},
        "members": members,
        "tier1": t1,
        "tier1_count": len(t1),
        "tier1_share": share_all,
        "judge": {
            "amount": round(float(last["amt"]), 1),
            "base": None if pd.isna(last["base"]) else round(float(last["base"]), 1),
            "mult": mult,
            "net": None if pd.isna(last["net"]) else round(float(last["net"]), 1),
            "band": band["label"] if band else None,
            "verdict": band["verdict"] if band else None,
            "prob": band["big"] if band else None,
            "lift": band["lift"] if band else None,
            "base_rate": base_rate,
        },
        "bands": _bands(day),
        "recent": recent,
        "trend": trend,
        "sign_flag": sign_flag,
    }
