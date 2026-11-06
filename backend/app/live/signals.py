"""信号装载：把决策所需的全部输入从库里取出，冻结成不可变快照。

为什么要单独一层：`portfolio_fresh` 每次重演都现读现算（`resolve_strategy_quality`
查卡片、`_daily_candidate_excess` 算 PIT 超额）。实盘不能这样——三个月后卡片档位
变了，同一历史日期重算出的候选池就不同，事故无法复盘。

所以实盘路径固定为：
    load_signals() → 落 live_decision_snapshot → decide_close() 只吃快照

`daily_exc` 与回测口径必须逐值一致（tests/test_live_target.py 用等价性验收兜底）。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SignalAction, SignalDaily
from app.services.research_card_svc import resolve_strategy_quality


def daily_candidate_excess(
    sig_dates: list[date],
    act_map: dict[date, dict[str, dict]],
    sig_strats: dict[date, dict[str, list[str]]],
    enabled: set[str],
    px: pd.DataFrame,
) -> dict[date, float]:
    """每个信号日候选池的平均 T+10 超额（对全市场等权基准，同窗口同起止）。

    与 portfolio_fresh._daily_candidate_excess 同口径：T+1 开盘进、持有 10 个
    交易日收盘出；只有窗口完整的信号日才有值。
    """
    if px.empty:
        return {}
    open_p = px.pivot_table(index="date", columns="symbol", values="o_hfq")
    close_p = px.pivot_table(index="date", columns="symbol", values="c_hfq")
    fwd = (close_p.shift(-11) / open_p.shift(-1)) - 1.0
    bench = fwd.mean(axis=1)
    out: dict[date, float] = {}
    for d in sig_dates:
        vals = []
        for sym, act in act_map.get(d, {}).items():
            strats = sig_strats.get(d, {}).get(sym, [])
            if not any(s in enabled for s in strats):
                continue
            if (act.get("streak") or 0) > 2:
                continue
            close, ma20 = act.get("close"), act.get("ma20")
            if close is None or ma20 is None or close >= ma20:
                continue
            if d not in fwd.index or sym not in fwd.columns:
                continue
            r, b = fwd.at[d, sym], bench.get(d)
            if pd.isna(r) or b is None or pd.isna(b):
                continue
            vals.append(float(r) - float(b))
        if vals:
            out[d] = sum(vals) / len(vals)
    return out


def load_signals(db: Session, px: pd.DataFrame, *, strategy_key: str = "fresh_ma",
                 enabled: set[str] | None = None) -> dict:
    """装载信号快照。

    Args:
        px: 行情面板（需含 date/symbol/o_hfq/c_hfq，用于算门控；不做交易价用）
        enabled: 显式指定候选策略集；None 则查研究卡（实盘必须落快照后再调）
    """
    actions = db.scalars(select(SignalAction).order_by(SignalAction.date)).all()
    if not actions:
        return {"act_map": {}, "sig_strats": {}, "enabled": set(),
                "daily_exc": {}, "sig_dates": [], "card_tiers": {}}

    sig_rows = db.execute(
        select(SignalDaily.date, SignalDaily.symbol, SignalDaily.strategy)
    ).all()
    sig_strats: dict[date, dict[str, list[str]]] = {}
    for d, s, st in sig_rows:
        sig_strats.setdefault(d, {}).setdefault(s, []).append(st)

    act_map: dict[date, dict[str, dict]] = {}
    for a in actions:
        act_map.setdefault(a.date, {})[a.symbol] = {
            "action": a.action, "streak": a.streak or 0,
            "resonance": a.resonance or 0, "close": a.close, "ma20": a.ma20,
        }

    tiers: dict[str, str] = {}
    if enabled is None:
        tiers = resolve_strategy_quality(db)
        enabled = {k for k, v in tiers.items() if v != "weak"}

    sig_dates = sorted(act_map)
    return {
        "act_map": act_map,
        "sig_strats": sig_strats,
        "enabled": enabled,
        "daily_exc": daily_candidate_excess(sig_dates, act_map, sig_strats,
                                            enabled, px),
        "sig_dates": sig_dates,
        "card_tiers": tiers,
    }
