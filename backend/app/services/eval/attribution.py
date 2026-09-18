"""逐笔交易归因：往返配对、盈亏结构、持仓周期、止损有效性、alpha 检验。

四条方法论
---------
1. **买卖点质量要看「相对基准的超额」，不能只看绝对涨跌**
   牛市里买入后上涨是常态，不代表买点好。绝对涨跌里混着 beta。

2. **止损有效性：比较「止损出局」与「继续持有」的后续走势**
   只统计止损后是否亏损是错的 —— 那必然成立（不然不会触发）。
   要看止损后标的的**后续收益**：继续跌 → 止损有效；反弹 → 止损是负贡献。

3. **alpha 必须做显著性检验**
   实测两版策略的超额收益 t 值分别为 0.73 / 0.58 —— **均不显著**，
   即「跑赢基准」在统计上无法确认。只报超额数值会误导。

4. **持仓周期要分桶看，不能只看均值**
   均值会被极端值带偏。核心用途是识别「参数切换点」：
   按持仓天数分桶（0-4/5-9/10-14/15-20/21-40/>40）看止损回撤中位的跳变位置，
   就是实际的持有期阈值 —— 曾因拿「以为的切点」切两层而误判阶梯未生效。
"""
from __future__ import annotations

from math import erfc, sqrt

import numpy as np
import pandas as pd

HOLD_BUCKETS = [(0, 4), (5, 9), (10, 14), (15, 20), (21, 40), (41, 10 ** 6)]
_STOP_WORDS = ("止损", "stop", "清仓", "风控")


def round_trips(trades: list[dict]) -> pd.DataFrame:
    """把 BUY/SELL 流水配对成完整往返交易。

    单标的回测的 trades 里 symbol 为空，统一归到 '_single'，不影响配对。
    """
    rows, open_pos = [], {}
    for t in trades:
        sym = str(t.get("symbol") or "_single")
        side = str(t.get("side") or "").upper()
        if side == "BUY":
            open_pos[sym] = t
        elif side in ("SELL", "CLOSE") and sym in open_pos:
            b = open_pos.pop(sym)
            try:
                bp, sp = float(b.get("price") or 0), float(t.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if bp <= 0:
                continue
            d0, d1 = str(b.get("date"))[:10], str(t.get("date"))[:10]
            try:
                hold = (pd.Timestamp(d1) - pd.Timestamp(d0)).days
            except (ValueError, TypeError):
                hold = np.nan
            rows.append(dict(
                symbol=sym, 买入日=d0, 卖出日=d1,
                买入价=bp, 卖出价=sp,
                股数=float(t.get("shares") or b.get("shares") or 0),
                持仓天数=hold,
                收益率=sp / bp - 1,
                盈亏=float(t.get("pnl") or 0),
                卖出信号=str(t.get("signal_type") or ""),
                是止损=any(w in str(t.get("signal_type") or "") for w in _STOP_WORDS),
            ))
    return pd.DataFrame(rows)


def summarize(rt: pd.DataFrame) -> dict:
    """往返交易的整体统计。"""
    if rt.empty:
        return dict(n=0)
    win = rt[rt.收益率 > 0]
    loss = rt[rt.收益率 <= 0]
    gross_win = float(win.收益率.sum())
    gross_loss = abs(float(loss.收益率.sum()))
    return dict(
        n=len(rt),
        胜率=float(len(win) / len(rt)),
        平均收益=float(rt.收益率.mean()),
        收益中位=float(rt.收益率.median()),
        盈亏比=float(gross_win / gross_loss) if gross_loss > 0 else np.nan,
        平均持仓天数=float(rt.持仓天数.mean()),
        持仓中位=float(rt.持仓天数.median()),
        最大单笔盈利=float(rt.收益率.max()),
        最大单笔亏损=float(rt.收益率.min()),
        总盈亏=float(rt.盈亏.sum()),
    )


def hold_buckets(rt: pd.DataFrame, extra_col: str = "收益率") -> pd.DataFrame:
    """按持仓天数分桶统计 —— 用于识别参数切换点。"""
    if rt.empty:
        return pd.DataFrame()
    rows = []
    for lo, hi in HOLD_BUCKETS:
        sub = rt[(rt.持仓天数 >= lo) & (rt.持仓天数 <= hi)]
        if sub.empty:
            continue
        rows.append(dict(
            桶=f"{lo}-{hi if hi < 10**5 else '+'}",
            笔数=len(sub),
            占比=len(sub) / len(rt),
            收益中位=float(sub[extra_col].median()),
            收益均值=float(sub[extra_col].mean()),
            胜率=float((sub[extra_col] > 0).mean()),
        ))
    return pd.DataFrame(rows)


def stop_loss_effect(rt: pd.DataFrame, forward_ret: dict[str, float] | None = None,
                     horizon_days: int = 20) -> dict:
    """止损有效性：止损出局后，标的在后续 horizon_days 的走势。

    forward_ret 需外部提供 {卖出日: 后续收益}；缺失时只报止损笔数占比。
    """
    if rt.empty or "是止损" not in rt.columns:
        return dict(n_stop=0)
    stop = rt[rt.是止损]
    out = dict(n_stop=len(stop), stop_ratio=float(len(stop) / len(rt)),
               stop_avg_ret=float(stop.收益率.mean()) if len(stop) else np.nan)
    if forward_ret and len(stop):
        after = [forward_ret.get(d) for d in stop.卖出日]
        after = [a for a in after if a is not None and a == a]
        if after:
            a = np.array(after, dtype=float)
            sd = a.std(ddof=1) if len(a) > 1 else 0.0
            out["after_ret_mean"] = float(a.mean())
            out["after_ret_t"] = float(a.mean() / sd * np.sqrt(len(a))) if sd > 0 else np.nan
            # 止损有效 = 止损后继续下跌（after_ret_mean < 0）
            out["verdict"] = ("止损有效（出局后继续下跌）" if a.mean() < 0
                              else "止损为负贡献（出局后反弹）")
    return out


def alpha_test(ret: pd.Series, bench_ret: pd.Series,
               ann: int = 252) -> dict:
    """超额收益显著性检验（日收益配对 t）。

    |t| < 2 只能写「无法确认超额收益」—— 只报数值会误导。
    """
    r = pd.Series(ret).dropna()
    b = pd.Series(bench_ret).dropna()
    idx = r.index.intersection(b.index)
    if len(idx) < 20:
        return dict(n=len(idx), t=np.nan, verdict="样本不足，无法判别")
    ex = r.reindex(idx) - b.reindex(idx)
    sd = ex.std(ddof=1)
    if sd == 0:
        return dict(n=len(idx), t=np.nan, verdict="方差为零，无法判别")
    t = float(ex.mean() / sd * np.sqrt(len(ex)))
    p = float(erfc(abs(t) / sqrt(2)))
    return dict(n=len(idx), 日均超额=float(ex.mean()), t=t, p=p,
                ir=float(ex.mean() / sd * np.sqrt(ann)),
                verdict=("超额显著为正" if t > 2 else
                         "超额显著为负" if t < -2 else "超额不显著（无法确认）"))


def turnover_ratio(trades: list[dict], avg_equity: float) -> dict:
    """换手率 = 总成交额 / 平均净值。年化需外部除以年数。"""
    if not trades or avg_equity <= 0:
        return dict(turnover=np.nan)
    amt = 0.0
    for t in trades:
        try:
            amt += abs(float(t.get("price") or 0) * float(t.get("shares") or 0))
        except (TypeError, ValueError):
            continue
    return dict(turnover=amt / avg_equity, n_trades=len(trades),
                avg_trade=amt / max(len(trades), 1))
