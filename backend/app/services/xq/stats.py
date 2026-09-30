"""信号有效性六维统计（Sequoia-X signal_stats.py 迁移，向量化重写）。

口径与原实现一致（防前视是底线）：
- 建仓价 = 信号日**次一交易日开盘价**（T+1 开盘，绝不用信号日收盘）
- 收益 = 持有 T+1/3/5/10/20 个交易日后的收盘价相对建仓价
- 超额 = 个股收益 − 同期**全市场等权**收益（同窗口、同起止日）
- 六维切分：按策略 / 连续天数(streak) / 共振数 / 质量分级 / 按月 / 综合

数据源：signal_daily（信号事实）+ signal_actions（streak/共振/分级）+ DuckDB 面板。
计算在内存向量化完成，约 7000 信号日 × 5 视口秒级出数；结果带 TTL 缓存。
"""

from __future__ import annotations

import time
from datetime import date

import pandas as pd

from app.services.xq.data import Panel, load_panel

HORIZONS = (1, 3, 5, 10, 20)
_CACHE: dict = {"ts": 0.0, "data": None}
_TTL = 600  # 10 分钟


def _load_signals(db, days: int = 90) -> pd.DataFrame:
    """signal_daily ⋈ signal_actions → 每条信号的策略/streak/共振/分级。"""
    from sqlalchemy import select

    from app.models import SignalAction, SignalDaily

    rows = db.execute(
        select(SignalDaily.date, SignalDaily.symbol, SignalDaily.strategy)
        .order_by(SignalDaily.date)
    ).all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["date", "symbol", "strategy"])
    uniq = sorted(df["date"].unique())
    cutoff = uniq[max(0, len(uniq) - days)] if days and uniq else None
    if cutoff:
        df = df[df["date"] >= cutoff]

    act_rows = db.scalars(select(SignalAction)).all()
    acts = pd.DataFrame(
        [(a.date, a.symbol, a.streak, a.resonance, a.quality_tier) for a in act_rows],
        columns=["date", "symbol", "streak", "resonance", "tier"],
    )
    if not acts.empty:
        df = df.merge(acts, on=["date", "symbol"], how="left")
    else:
        df["streak"] = 1
        df["resonance"] = 1
        df["tier"] = "neutral"
    df["streak"] = df["streak"].fillna(1).astype(int)
    df["resonance"] = df["resonance"].fillna(1).astype(int)
    df["tier"] = df["tier"].fillna("neutral")
    return df


def _forward_returns(panel: Panel) -> dict[int, pd.DataFrame]:
    """预计算每个视口的：个股 T+1开盘→T+N收盘 收益矩阵（date×symbol），
    以及全市场等权基准序列。"""
    df = panel.df
    # 建仓/出场必须同口径（都用 hfq）：混用未复权 open 与 hfq close 会差出
    # 上百倍复权因子（实测出现 +8154% 的荒谬收益）
    open_p = df.pivot_table(index="date", columns="symbol", values="o_hfq")
    close_p = df.pivot_table(index="date", columns="symbol", values="c_hfq")
    entry = open_p.shift(-1)  # T+1 开盘（信号日次一交易日）

    out: dict[int, pd.DataFrame] = {}
    for n in HORIZONS:
        exit_close = close_p.shift(-1 - n)  # T+1 后再持有 N 日的收盘
        out[n] = (exit_close / entry) - 1.0
    return out


def compute_stats(db, duckdb_path: str, days: int = 90, force: bool = False) -> dict:
    """六维有效性统计（带 10 分钟缓存；AttributionAgent 与归因页共用）。"""
    if not force and _CACHE["data"] and time.time() - _CACHE["ts"] < _TTL:
        return _CACHE["data"]

    signals = _load_signals(db, days=days)
    if signals.empty:
        return {"ok": False, "error": "暂无信号历史，先运行流水线/回填"}
    panel = load_panel(duckdb_path, lookback=260)
    if panel.df.empty:
        return {"ok": False, "error": "行情面板为空"}

    fwd = _forward_returns(panel)
    # 每 (信号日, 视口) 的全市场等权基准
    bench: dict[int, pd.Series] = {
        n: fwd[n].mean(axis=1) for n in HORIZONS
    }

    # —— 向量化：把信号表与各视口收益矩阵展平后 merge ——
    long_rets: list[pd.DataFrame] = []
    for n in HORIZONS:
        r = fwd[n].stack().rename("ret").reset_index()
        r.columns = ["date", "symbol", "ret"]
        b = bench[n].rename("bench").reset_index()
        b.columns = ["date", "bench"]
        m = r.merge(b, on="date")
        m["h"] = n
        long_rets.append(m)
    all_rets = pd.concat(long_rets)

    sig = signals.merge(all_rets, on=["date", "symbol"], how="left")
    sig = sig.dropna(subset=["ret"])
    sig["excess"] = sig["ret"] - sig["bench"]
    sig["win"] = sig["excess"] > 0
    sig["month"] = sig["date"].astype(str).str[:7]
    sig["streak_bucket"] = pd.cut(
        sig["streak"], [0, 2, 8, 15, 999],
        labels=["1-2天", "3-8天", "9-15天", "15天+"],
    ).astype(str)
    sig["reso_bucket"] = pd.cut(
        sig["resonance"], [0, 1, 2, 99], labels=["1策略", "2策略", "3+策略"]
    ).astype(str)

    def _agg(g: pd.DataFrame, keys: list[str]) -> list[dict]:
        out_rows = []
        for k, grp in g.groupby(keys, observed=True):
            kt = k if isinstance(k, tuple) else (k,)
            rec = {key: kt[i] for i, key in enumerate(keys)}
            for n in HORIZONS:
                sub = grp[grp["h"] == n]
                if len(sub) == 0:
                    continue
                rec[f"n_{n}"] = int(len(sub))
                rec[f"ret_{n}"] = round(float(sub["ret"].mean()) * 100, 2)
                rec[f"exc_{n}"] = round(float(sub["excess"].mean()) * 100, 2)
                rec[f"win_{n}"] = round(float(sub["win"].mean()) * 100, 1)
            out_rows.append(rec)
        return out_rows

    by_strategy = _agg(sig, ["strategy"])
    by_streak = _agg(sig, ["streak_bucket"])
    by_resonance = _agg(sig, ["reso_bucket"])
    by_tier = _agg(sig, ["tier"])
    by_month = _agg(sig, ["month"])

    result = {
        "ok": True,
        "window_days": days,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n_signals": int(len(sig.drop_duplicates(["date", "symbol"]))),
        "n_obs": int(len(sig)),
        "by_strategy": by_strategy,
        "by_streak": by_streak,
        "by_resonance": by_resonance,
        "by_tier": by_tier,
        "by_month": sorted(by_month, key=lambda r: r["month"]),
    }
    _CACHE.update(ts=time.time(), data=result)
    return result


def top_line(result: dict) -> str:
    """归因速览一句话（Orchestrator 事件用）。"""
    if not result.get("ok"):
        return result.get("error", "统计不可用")
    lines = []
    for r in result.get("by_strategy", []):
        if "exc_10" in r:
            lines.append(f"{r['strategy']} {r['exc_10']:+.2f}%")
    return "T+10超额 · " + " | ".join(lines[:8]) if lines else "样本不足"
