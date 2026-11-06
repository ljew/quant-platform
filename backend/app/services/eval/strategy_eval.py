"""策略评估：绩效指标、同区间 A/B、配对显著性检验、样本外分段。

四条方法论（均为踩坑后固化）
--------------------------
1. **配对显著性检验，不是比大小**
   两个版本在同一区间的收益差，必须对「日收益」做配对 t 检验。
   曾因只比总收益就下结论而翻车：15 日持有方案在 2025-2026 段略好，
   但配对 t = −0.81 → 属「无法判别」，不能写「15 日更优」。
   规矩：|t| < 2 一律写「无法判别」。

2. **同区间 A/B 必须先对齐共同交易日**
   区间不同则总收益差里混着市场环境差异，无法归因到策略改动。
   还需检查选股重合度：重合度 100% 时差异只来自仓位/时点，与选股无关。

3. **样本外分段用「每段选优」，不是「固定参数」**
   检验的是「按训练段最优参数去做验证段」这个**流程**。
   实测：56 组参数在验证段 56/56 全为正，但每段选优只有 6/9 为正 ——
   「参数稳定」远比「参数最优」重要。

4. **仓位指标要按实际持仓市值算，不是按信号数量**
   三档离散仓位在实测中退化成约 0.90 的固定值，说明分档没起作用。
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

TRADING_DAYS = 252


# ============================ 绩效指标 ============================
def perf_metrics(nav: pd.Series, bench: pd.Series | None = None,
                 rf: float = 0.0) -> dict:
    """由净值序列算绩效。nav 为按日期的净值（非收益率）。

    夏普用日收益年化；回撤用净值峰谷；仓位指标需外部另算（见 position_stats）。
    """
    nav = pd.Series(nav).dropna().sort_index()
    if len(nav) < 2:
        return dict(total=0.0, annual=0.0, sharpe=np.nan, mdd=0.0,
                    vol=np.nan, n=len(nav))
    r = nav.pct_change().dropna()
    years = len(r) / TRADING_DAYS
    total = float(nav.iloc[-1] / nav.iloc[0] - 1)
    annual = float((1 + total) ** (1 / years) - 1) if years > 0 else np.nan
    sd = r.std(ddof=1)
    sharpe = float((r.mean() - rf / TRADING_DAYS) / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else np.nan
    dd = nav / nav.cummax() - 1
    out = dict(total=total, annual=annual, sharpe=sharpe, mdd=float(dd.min()),
               vol=float(sd * np.sqrt(TRADING_DAYS)), n=len(nav))
    if bench is not None and len(bench) >= 2:
        b = pd.Series(bench).dropna().sort_index()
        bi = b.reindex(nav.index).ffill()
        out["bench_total"] = float(bi.iloc[-1] / bi.iloc[0] - 1)
        out["excess"] = out["total"] - out["bench_total"]
        br = bi.pct_change().dropna()
        common = r.index.intersection(br.index)
        if len(common) > 20:
            ex = r.reindex(common) - br.reindex(common)
            sd_ex = ex.std(ddof=1)
            out["alpha_t"] = float(ex.mean() / sd_ex * np.sqrt(len(ex))) if sd_ex > 0 else np.nan
            out["ir"] = float(ex.mean() / sd_ex * np.sqrt(TRADING_DAYS)) if sd_ex > 0 else np.nan
    return out


def position_stats(positions: pd.Series) -> dict:
    """仓位统计。positions 为 0~1 的日度仓位（持仓市值 / 总资产）。"""
    p = pd.Series(positions).dropna()
    if p.empty:
        return dict(avg=np.nan, max=np.nan, days=0)
    return dict(avg=float(p.mean()), max=float(p.max()), days=len(p),
                empty_ratio=float((p <= 1e-9).mean()))


# ============================ 配对显著性 ============================
def align_nav(nav_a: pd.Series, nav_b: pd.Series) -> tuple[pd.Series, pd.Series]:
    """对齐到共同交易日（交集），并各自归一化到 1.0 起。

    不先对齐就比总收益，等于把区间差异混入策略差异 —— 结论不可归因。
    """
    a = pd.Series(nav_a).dropna().sort_index()
    b = pd.Series(nav_b).dropna().sort_index()
    idx = a.index.intersection(b.index)
    a, b = a.reindex(idx), b.reindex(idx)
    if len(a) < 2:
        return a, b
    return a / a.iloc[0], b / b.iloc[0]


def paired_ttest(nav_a: pd.Series, nav_b: pd.Series) -> dict:
    """日收益配对 t 检验。t = 均值差 / 标准误。

    返回 dict(t, p_approx, diff_mean, n, verdict)。
    verdict 直接给出可用结论文案，避免误读 —— |t|<2 时不得声称优劣。
    """
    a, b = align_nav(nav_a, nav_b)
    if len(a) < 20:
        return dict(t=np.nan, p=np.nan, diff_mean=np.nan, n=len(a),
                    verdict="样本不足，无法判别")
    ra, rb = a.pct_change().dropna(), b.pct_change().dropna()
    idx = ra.index.intersection(rb.index)
    d = (ra.reindex(idx) - rb.reindex(idx)).dropna()
    if len(d) < 20 or d.std(ddof=1) == 0:
        return dict(t=np.nan, p=np.nan, diff_mean=np.nan, n=len(d),
                    verdict="样本不足或方差为零，无法判别")
    t = float(d.mean() / d.std(ddof=1) * np.sqrt(len(d)))
    # 正态近似双尾 p（n>20 时足够；不引 scipy 依赖）
    from math import erfc, sqrt
    p = float(erfc(abs(t) / sqrt(2)))
    if abs(t) < 2:
        verdict = f"无法判别（t={t:+.2f}，|t|<2）"
    elif t > 0:
        verdict = f"A 优于 B（t={t:+.2f}）"
    else:
        verdict = f"B 优于 A（t={t:+.2f}）"
    return dict(t=t, p=p, diff_mean=float(d.mean()), n=len(d), verdict=verdict)


# ============================ 样本外分段 ============================
def segment_metrics(nav: pd.Series, segments: dict[str, tuple[str, str]],
                    bench: pd.Series | None = None) -> pd.DataFrame:
    """按 {段名: (起, 止)} 切净值算指标。日期为 'YYYY-MM-DD' 字符串。"""
    nav = pd.Series(nav).dropna().sort_index()
    rows = []
    for name, (s, e) in segments.items():
        seg = nav[(nav.index >= s) & (nav.index <= e)]
        b = None
        if bench is not None:
            bb = pd.Series(bench).dropna().sort_index()
            b = bb[(bb.index >= s) & (bb.index <= e)]
        m = perf_metrics(seg, b)
        rows.append(dict(段=name, 起=s, 止=e, **m))
    return pd.DataFrame(rows)


def pick_best_per_segment(results: dict[str, dict[str, float]],
                          segments: list[str]) -> pd.DataFrame:
    """每段选优：对每个段，选出该段表现最好的参数，再看它在别的段如何。

    results: {参数名: {段名: 收益}}
    用来检验「参数稳定性」—— 若每段最优参数都不同，说明参数不可信。
    """
    rows = []
    for seg in segments:
        vals = {p: v.get(seg, np.nan) for p, v in results.items()}
        vals = {p: v for p, v in vals.items() if v == v}
        if not vals:
            continue
        best = max(vals, key=vals.get)
        row = dict(段=seg, 选优参数=best, 该段收益=vals[best])
        row.update({f"在{s}": results[best].get(s, np.nan) for s in segments})
        rows.append(row)
    return pd.DataFrame(rows)


# ============================ 与回测记录对接 ============================
def _series_from_curve(equity_curve_json: str, field: str = "equity") -> pd.Series:
    """把 backtests.equity_curve_json 转成 Series。

    实测格式（2026-09 校准）：
      [{"date": "2021-01-04", "equity": 1000000.0,
        "benchmark": 1000000.0, "hedged": 0.0}, ...]
    同时兼容 [[date, value], ...] 与 [{trade_date, nav}, ...] 等变体。
    """
    try:
        data = json.loads(equity_curve_json or "[]")
    except (ValueError, TypeError):
        return pd.Series(dtype=float)
    if not data:
        return pd.Series(dtype=float)
    alt = {"equity": ("equity", "nav", "value"),
           "benchmark": ("benchmark", "bench", "bench_equity")}[field]
    dates, vals = [], []
    for it in data:
        if isinstance(it, dict):
            d = it.get("date") or it.get("trade_date") or it.get("t")
            v = next((it[k] for k in alt if it.get(k) is not None), None)
        elif isinstance(it, (list, tuple)) and len(it) >= 2:
            d, v = it[0], it[1]
        else:
            continue
        if d is None or v is None:
            continue
        dates.append(str(d)[:10])
        vals.append(float(v))
    if not dates:
        return pd.Series(dtype=float)
    return pd.Series(vals, index=pd.Index(dates, name="trade_date")).sort_index()


def nav_from_equity_json(equity_curve_json: str) -> pd.Series:
    """策略净值序列。"""
    return _series_from_curve(equity_curve_json, "equity")


def bench_from_equity_json(equity_curve_json: str) -> pd.Series:
    """基准净值序列（净值曲线里自带，无需另取）。"""
    return _series_from_curve(equity_curve_json, "benchmark")
