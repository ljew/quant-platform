"""每日选股/信号跑批入口：策略 → signal_daily → streak 聚合 → 判定 → signal_actions。

幂等：同一天重跑时先删该日的 signal_daily / signal_actions 再整批重写
（与 daily.sh「按数据日期幂等、支持重跑」的语义一致）。
每一步发 AgentEvent（P2 的驾驶舱/SSE 直接消费这些事件）。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.models import AgentEvent, SignalAction, SignalDaily
from app.services.xq import engine as xq_engine
from app.services.xq.data import Panel, load_panel
from app.services.xq.strategies import STRATEGIES, STRATEGY_NAME, STRATEGY_QUALITY


def emit(db: Session, run_id: int | None, agent: str, message: str,
         step: str = "", status: str | None = None, rows: int | None = None,
         level: str = "info", duration_ms: int | None = None) -> None:
    """发一条 Agent 事件（append-only）。"""
    db.add(AgentEvent(run_id=run_id, agent=agent, step=step, level=level,
                      status=status, message=message, rows=rows,
                      duration_ms=duration_ms))


def run_signal_scan(
    db: Session,
    duckdb_path: str,
    as_of: date | None = None,
    run_id: int | None = None,
    lookback: int = 260,
) -> dict:
    """跑一次完整选股 + 信号判定。返回摘要 dict（含各策略命中数与动作分布）。"""
    p: Panel = load_panel(duckdb_path, lookback=lookback, as_of=as_of)
    if p.df.empty:
        emit(db, run_id, "research", "面板为空：库内无行情数据", step="load_panel",
             status="fail", level="error")
        db.commit()
        return {"ok": False, "error": "行情面板为空"}
    return run_from_panel(db, p, run_id=run_id)


def panel_asof(p: Panel, d: date) -> Panel:
    """从已载入面板派生「截至 d 日」的视图（time_travel 重演用，免重复查库）。"""
    sub = p.df[p.df["date"] <= d]
    view = Panel(sub, p.names, as_of=d)
    view.latest = d
    return view


def run_from_panel(db: Session, p: Panel, run_id: int | None = None) -> dict:
    """在给定面板上跑完整流水线（scan 与 backfill 共用）。"""
    emit(db, run_id, "research", "载入全市场行情面板…", step="load_panel", status="running")
    db.commit()

    data_date: date = p.as_of or p.latest
    emit(db, run_id, "research", f"面板就绪：{p.df['symbol'].nunique()} 只 × {p.latest}",
         step="load_panel", status="ok", rows=int(p.df["symbol"].nunique()))

    # ST 黑名单（规则 blacklist_st）
    st = p.st_symbols() if xq_engine.DEFAULT_RULES["blacklist_st"] else set()

    # ── 1) 跑 8 策略 → signal_daily（先删后写，幂等）──
    db.query(SignalDaily).filter(SignalDaily.date == data_date).delete()
    picks_by_day: dict[str, list[str]] = {}
    for meta in STRATEGIES:
        key, name, fn = meta["key"], meta["name"], meta["fn"]
        st0 = _now_ms()
        try:
            symbols, _hits = fn(p)
        except Exception as exc:  # noqa: BLE001
            emit(db, run_id, "research", f"{name} 执行失败：{exc}", step=f"strategy:{key}",
                 status="fail", level="error")
            db.commit()
            continue
        symbols = [s for s in symbols if s not in st]
        picks_by_day[key] = symbols
        for sym in symbols:
            db.add(SignalDaily(date=data_date, strategy=key, symbol=sym,
                               name=p.name_of(sym) or None))
        emit(db, run_id, "research", f"{name}：命中 {len(symbols)} 只",
             step=f"strategy:{key}", status="ok", rows=len(symbols),
             duration_ms=_now_ms() - st0)
        db.commit()

    # ── 2) streak 聚合（含刚写入的今天）──
    st0 = _now_ms()
    history = xq_engine.load_history(db, end=data_date)
    history.setdefault(data_date, set())
    streaks = xq_engine.compute_streaks(history, data_date)
    emit(db, run_id, "signal", f"streak 聚合完成：{len(streaks)} 只在榜",
         step="streak", status="ok", rows=len(streaks), duration_ms=_now_ms() - st0)
    db.commit()

    # ── 3) 动作判定 → signal_actions（先删后写）──
    db.query(SignalAction).filter(SignalAction.date == data_date).delete()
    latest = p.df[p.df["date"] == p.latest].set_index("symbol")
    # 输出用均线（独立计算，避免依赖策略执行顺序留下的列）
    ma10 = p.df.groupby("symbol", sort=False)["c_hfq"].transform(
        lambda s: s.rolling(10).mean())
    ma20 = p.df.groupby("symbol", sort=False)["c_hfq"].transform(
        lambda s: s.rolling(20).mean())
    p.df["out_ma10"], p.df["out_ma20"] = ma10, ma20
    ma_map = p.df[p.df["date"] == p.latest].set_index("symbol")[["out_ma10", "out_ma20"]]

    rules = xq_engine.DEFAULT_RULES
    action_dist: dict[str, int] = {}
    for sym, info in streaks.items():
        today_strategies = sorted(
            k for k, syms in picks_by_day.items() if sym in syms
        )
        streak, reso = info["streak"], len(today_strategies)
        close = float(latest.loc[sym, "close"]) if sym in latest.index else None
        m10 = float(ma_map.loc[sym, "out_ma10"]) if sym in ma_map.index else None
        m20 = float(ma_map.loc[sym, "out_ma20"]) if sym in ma_map.index else None
        action, reason = xq_engine.judge(
            sym, streak, reso, today_strategies, close, m10, m20, rules,
        )
        db.add(SignalAction(
            date=data_date, symbol=sym, name=p.name_of(sym) or None,
            action=action, streak=streak, resonance=reso,
            strategies="、".join(STRATEGY_NAME.get(k, k) for k in today_strategies),
            quality_tier=xq_engine.best_tier(today_strategies),
            close=close, ma10=m10, ma20=m20, reason=reason,
        ))
        action_dist[action] = action_dist.get(action, 0) + 1
    emit(db, run_id, "signal", f"动作判定完成：{action_dist}", step="judge",
         status="ok", rows=sum(action_dist.values()))
    db.commit()

    return {
        "ok": True,
        "date": data_date.isoformat(),
        "picks": {k: len(v) for k, v in picks_by_day.items()},
        "actions": action_dist,
        "quality": STRATEGY_QUALITY,
        "on_radar": len(streaks),
    }


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
