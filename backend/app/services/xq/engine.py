"""信号引擎（Sequoia-X signal_engine.py 迁移）。

输入是 signal_daily 表的历史选股事实，输出每只股票的买卖动作判定。

规则参数 DEFAULT_RULES 为 Sequoia 2026-09-18 v2 重设计的**实测校准值**，
不要随手改 —— 每个数字背后都有实验（diag_redesign.py 55 组对照 + 组合回测）：
- streak_buy=9：连续 7~12 天构成超额平台（+18.6~+25.5pp），<7 天全负；
  「长确认」本质是内生择时器，趋势崩坏期长连续信号自动消失。
- 共振数不作硬门槛（组合层实测负贡献：−17.93% vs 去门槛 −5.60%）。
- ST/*ST 黑名单（5 笔实测全亏，均值 −7.15%）。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SignalDaily
from app.services.xq.strategies import STRATEGY_QUALITY, STRATEGY_NAME

# ── 规则参数（原样迁移，含义见模块 docstring 与行内注释）──
DEFAULT_RULES: dict = {
    "streak_buy": 9,
    "streak_watch": 3,
    "resonance_strong": 2,
    "blacklist_st": True,
    "max_hold_days": 15,
    "stop_loss": -0.08,
    "trail_drawdown": -0.08,
    "ma_break_soft": 10,
    "ma_break_hard": 20,
    "ma_check_min_days": 3,
    "recent_window": 20,
    "min_bars": 21,
}

_TIER_RANK = {"strong": 3, "good": 2, "neutral": 1, "weak": 0}
_TIER_LABEL = {"strong": "强", "good": "良", "neutral": "中", "weak": "弱"}


def best_tier(strategy_keys: list[str]) -> str:
    """当日命中策略中的最优质量档（决定能否建仓：weak 不建仓）。"""
    tiers = [STRATEGY_QUALITY.get(k, "neutral") for k in strategy_keys]
    return max(tiers, key=lambda t: _TIER_RANK.get(t, 1)) if tiers else "neutral"


def load_history(db: Session, end: date, window: int = 60) -> dict[date, set[str]]:
    """载入选股历史 → {日期: 出现过的 symbol 集合}（全策略合并）。"""
    rows = db.execute(
        select(SignalDaily.date, SignalDaily.symbol).where(SignalDaily.date <= end)
    ).all()
    # 只保留最近 window 个有信号的交易日（与原实现 timeline 等价，避免远古数据拖慢 streak）
    by_date: dict[date, set[str]] = {}
    for d, sym in rows:
        by_date.setdefault(d, set()).add(sym)
    dates = sorted(by_date)[-window:]
    return {d: by_date[d] for d in dates}


def compute_streaks(
    history: dict[date, set[str]], as_of: date
) -> dict[str, dict]:
    """对 as_of 当日出现的每只股票，计算 streak（连续出现天数）与首次日期。

    streak 语义与原实现一致：从 as_of 往回数，中间断一天即停。
    """
    timeline = sorted(history)
    res: dict[str, dict] = {}
    day_sets = [history[d] for d in timeline if d <= as_of]
    if not day_sets:
        return res
    today_set = day_sets[-1]
    for sym in today_set:
        streak = 0
        for day_set in reversed(day_sets):
            if sym in day_set:
                streak += 1
            else:
                break
        res[sym] = {"streak": streak}
    return res


def judge(
    symbol: str,
    streak: int,
    resonance: int,
    strategies: list[str],
    close: float | None,
    ma10: float | None,
    ma20: float | None,
    rules: dict,
    holding: dict | None = None,
) -> tuple[str, str]:
    """按规则判定动作，返回 (action, reason)。

    判定顺序（风控优先于信号强度，与原实现逐行对齐）：
      1. 硬止损 / 移动止盈 / 超期 —— 只要触发就无条件离场（仅对已建仓标的）
      2. 趋势破位 —— MA20 之下卖/减，MA10 之下减/观察（持有≥N 天才启用）
      3. 信号强度 —— 连续天数 × 策略质量（共振仅参考）
    holding 为镜像组合上下文（entry_price/hold_days/ret_pct/peak_ret_pct/
    dd_from_peak_pct），P1 阶段组合尚未迁移，传 None 即自动跳过风控段。
    """
    holding = holding or {}
    active = streak > 0

    # ── 1) 无条件风控（仅对已建仓标的生效）──
    entry_price = holding.get("entry_price")
    if entry_price:
        hold_days = holding.get("hold_days") or 0
        ret_pct = holding.get("ret_pct")
        peak_ret_pct = holding.get("peak_ret_pct")
        dd_from_peak_pct = holding.get("dd_from_peak_pct")
        same_day = hold_days == 0
        tail = "，当日买入（T+1），明日开盘执行" if same_day else ""
        if ret_pct is not None and ret_pct / 100.0 <= rules["stop_loss"]:
            return "SELL", f"自建仓浮亏 {ret_pct:.1f}%，触发止损线 {rules['stop_loss']*100:.0f}%{tail}"
        if (
            peak_ret_pct is not None and peak_ret_pct > 0
            and dd_from_peak_pct is not None
            and dd_from_peak_pct / 100.0 <= rules["trail_drawdown"]
        ):
            return "SELL", (
                f"自期间最高点回撤 {dd_from_peak_pct:.1f}%"
                f"（触发 {rules['trail_drawdown']*100:.0f}% 移动止盈）{tail}"
            )
        if hold_days >= rules["max_hold_days"] and not active:
            return "SELL", f"信号驱动持有已满 {hold_days} 个交易日，时间止盈"

    # ── 2) 趋势破位（持有不足 N 天不启用均线信号）──
    hold_days = holding.get("hold_days") or 0
    ma_ok = hold_days >= rules.get("ma_check_min_days", 3)
    if ma_ok and ma20 is not None and close is not None and close < ma20:
        if active:
            return "REDUCE", f"仍在推荐名单但已跌破 MA20（{close:.2f} < {ma20:.2f}），趋势转弱，建议减仓观察"
        return "SELL", f"跌破 MA20（{close:.2f} < {ma20:.2f}）中期趋势线，信号失效离场"
    if ma_ok and ma10 is not None and close is not None and close < ma10:
        if active:
            return "WATCH", f"信号仍在但跌破 MA10（{close:.2f} < {ma10:.2f}），短线走弱，仅观察不追"
        return "REDUCE", f"跌破 MA10（{close:.2f} < {ma10:.2f}）且已不在推荐名单，建议减仓"

    if not active:
        return "EXIT", "已连续未再被推荐，信号消失，保留观察"

    # ── 3) 信号强度（长确认 × 策略质量；共振只作参考）──
    tier = best_tier(strategies)
    names = "、".join(STRATEGY_NAME.get(s, s) for s in strategies)

    if streak >= rules["streak_buy"]:
        if tier != "weak":
            extra = f"，{resonance} 条策略共振" if resonance > 1 else ""
            return "BUY_STRONG", f"连续 {streak} 天出现（{names}）{extra}"
        return "WATCH", f"连续 {streak} 天但命中策略历史超额偏弱（{names}），不建仓"
    if streak >= rules["streak_watch"]:
        return "WATCH", (
            f"连续 {streak} 天出现（策略质量{_TIER_LABEL.get(tier, '中')}），"
            f"确认中——建仓需连续 ≥{rules['streak_buy']} 天"
        )
    if resonance >= rules["resonance_strong"]:
        return "WATCH", f"{resonance} 条策略同日共振（{names}），仅参考，不作建仓依据"
    return "NEW", f"今日首次出现（连续 {streak} 天），持续性待确认"
