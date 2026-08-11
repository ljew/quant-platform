"""模拟盘(Paper Trading)核心引擎。

设计要点：
- 复用现有回测引擎（BacktestEngine / PortfolioBacktestEngine）的 run() 与撮合逻辑，
  仅把「最后一根 bar 用实时价覆盖」，跑一段「到今天」的回测，等价于「截至此刻的模拟账户」；
  这样模拟盘与回测共用一套成交/风控/成本模型，不会分叉。
- 每次 run 是全量重算（单标的几百根 bar 很快；组合日频、已 seed 的日K从 sqlite 取）。
- 账户「当前状态」= 本次回测的最终状态（cash / 持仓 / 权益）；
  成交记录增量提取：仅把 date > 上次处理日期（首次仅取最后一根 bar 当日）的成交写入 paper_trades，
  避免把整段历史当成模拟成交。
- 净值曲线 = 回测 equity_curve 中 date >= 账户建账日(account_start) 的区段。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import select

from app.core.engine.backtest_engine import BacktestEngine
from app.core.engine.portfolio_backtest import PortfolioBacktestEngine
from app.core.strategies.registry import get_strategy
from app.models import Stock, PaperTask, PaperTrade, PaperSnapshot
from app.services.data_source import get_realtime_prices, get_index_membership
# 复用回测路由里的数据加载函数（DB 优先、缺失回源落地），避免重复实现
from app.routers.strategy import _load_bars, _load_index_bars


def _merge_params(meta: dict, params_json: str) -> dict:
    p = dict(meta.get("default_params") or {})
    try:
        p.update(json.loads(params_json) or {})
    except Exception:
        pass
    return p


def _load_single(db, task: PaperTask) -> dict:
    """单标的模拟盘：跑『到今天』的 BacktestEngine，最后一根 bar 用实时价覆盖。"""
    symbols = [s.strip() for s in task.symbols.split(",") if s.strip()]
    if not symbols:
        raise ValueError("单标的任务需要 symbols")
    sym = symbols[0]  # MVP：单标的任务只跑列表中的第一个标的
    meta = get_strategy(task.strategy_key)
    params = _merge_params(meta, task.params_json)
    end = date.today()
    bars = _load_bars(db, sym, "2015-01-01", end.isoformat(), task.adj)
    if len(bars) < 30:
        raise ValueError(f"{sym} 历史日K不足({len(bars)}根)，无法预热指标")

    # 用实时价覆盖最后一根 bar（非交易时段实时价可能取不到，则保留最近收盘）
    q = get_realtime_prices([sym])[0]
    if q and q.get("price", 0) > 0:
        bars[-1] = {**bars[-1], "open": q["price"], "high": q["price"],
                    "low": q["price"], "close": q["price"]}

    engine = BacktestEngine(
        bars, initial_cash=task.initial_cash, commission=task.commission,
        slippage=task.slippage,
    )
    engine.run(meta["cls"], params)

    last_date = bars[-1]["date"]
    new_trades = _incremental_trades(engine.trades, task.last_bar_date, last_date, task.start_date)
    positions = (
        {sym: {"shares": round(engine.shares, 2), "cost": round(engine.avg_cost, 4)}}
        if engine.shares > 0 else {}
    )
    account_start = task.start_date or task.account_start or last_date
    curve = [{"date": e.date, "equity": e.equity}
             for e in engine.equity_curve if e.date >= account_start]
    return {
        "symbol": sym, "last_bar_date": last_date, "account_start": account_start,
        "equity": engine.equity_curve[-1].equity, "cash": engine.cash,
        "positions": positions, "new_trades": new_trades, "curve": curve,
    }


def _load_portfolio(db, task: PaperTask) -> dict:
    """组合模拟盘：跑『到今天』的 PortfolioBacktestEngine（日频 rebalance）。"""
    meta = get_strategy(task.strategy_key)
    index_code = task.index_code or meta.get("index_code") or "000906"
    index_symbol = meta.get("index_symbol") or ("sh" + index_code)
    params = _merge_params(meta, task.params_json)
    end = date.today()
    sd = date(end.year - 5, 1, 1)
    start = sd.isoformat()

    membership = get_index_membership(index_code, sd, end)
    if not membership:
        raise ValueError(f"未获取到指数 {index_code} 的时点成分股")
    union: list[str] = []
    seen: set = set()
    for _ds, sset in membership:
        for s in sset:
            if s not in seen:
                seen.add(s)
                union.append(s)

    warmup_min = (
        max(
            int(params.get("momentum_lookback", 120)),
            int(params.get("vol_lookback", 60)),
            int(params.get("beta_lookback", 120)),
            int(params.get("tail_lookback", 120)),
        )
        + 5
    )
    data: dict[str, list[dict]] = {}
    for sym in union:
        b = _load_bars(db, sym, start, end.isoformat(), task.adj)
        if len(b) >= warmup_min:
            data[sym] = b
    if len(data) < 20:
        raise ValueError(f"本地仅 {len(data)} 只成分股有可用日K，不足以模拟")

    benchmark = _load_index_bars(db, index_symbol, sd, end, task.adj)
    if not benchmark:
        raise ValueError(f"未获取到基准指数 {index_code} 的行情数据")

    syms = list(data.keys())
    attrs_rows = db.execute(
        select(Stock.symbol, Stock.industry, Stock.market_cap, Stock.pe_ttm, Stock.pb)
        .where(Stock.symbol.in_(syms))
    ).all()
    attributes = {
        r.symbol: {
            "industry": r.industry, "market_cap": r.market_cap,
            "pe_ttm": r.pe_ttm, "pb": r.pb,
        }
        for r in attrs_rows
    }

    engine = PortfolioBacktestEngine(
        data, benchmark,
        initial_cash=task.initial_cash, commission=task.commission, slippage=task.slippage,
        rebalance_period=int(params.get("rebalance_period", 21)),
        warmup=warmup_min, attributes=attributes, membership=membership,
    )
    engine.run(meta["cls"], params)

    last_date = engine.equity_curve[-1].date if engine.equity_curve else (engine.dates[-1] if engine.dates else end.isoformat())
    new_trades = _incremental_trades(engine.trades, task.last_bar_date, last_date, task.start_date)
    positions = {
        s: {"shares": round(sh, 2), "cost": round(engine.avg_cost.get(s, 0.0), 4)}
        for s, sh in engine.positions.items() if sh > 0
    }
    account_start = task.start_date or task.account_start or last_date
    curve = [{"date": e.date, "equity": e.equity}
             for e in engine.equity_curve if e.date >= account_start]
    return {
        "symbol": "", "last_bar_date": last_date, "account_start": account_start,
        "equity": engine.equity_curve[-1].equity if engine.equity_curve else task.initial_cash,
        "cash": engine.cash, "positions": positions,
        "new_trades": new_trades, "curve": curve,
    }


def _incremental_trades(trades, last_bar_date, last_date, start_date):
    """增量提取新增成交：首次若指定起始日则取该日之后全部，否则仅取最后一根 bar 当日；之后取 date > 上次处理日期。"""
    if last_bar_date is None:
        if start_date:
            return [t for t in trades if t.date >= start_date]
        return [t for t in trades if t.date == last_date]
    return [t for t in trades if t.date > last_bar_date]


def run_paper_task(db, task: PaperTask) -> dict:
    """执行一次模拟盘任务，写入成交/快照并更新任务状态。返回摘要dict。"""
    try:
        meta = get_strategy(task.strategy_key)
        if task.kind == "portfolio":
            r = _load_portfolio(db, task)
        else:
            r = _load_single(db, task)

        # 1) 写入增量成交
        for t in r["new_trades"]:
            db.add(PaperTrade(
                task_id=task.id, date=t.date,
                symbol=getattr(t, "symbol", r.get("symbol", "")),
                side=t.side, price=round(t.price, 4), shares=round(t.shares, 2),
                cash_after=round(t.cash_after, 2), commission=round(t.commission, 2),
                pnl=round(getattr(t, "pnl", 0.0), 2),
                signal_type=getattr(t, "signal_type", ""),
                signal_reason=getattr(t, "signal_reason", ""),
            ))
        # 2) 写入净值快照
        equity = r["equity"]
        mkt_val = equity - r["cash"]
        pnl = equity - task.initial_cash
        pnl_pct = pnl / task.initial_cash if task.initial_cash else 0.0
        db.add(PaperSnapshot(
            task_id=task.id, date=r["last_bar_date"], equity=round(equity, 2),
            cash=round(r["cash"], 2), market_value=round(mkt_val, 2),
            pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 6),
            positions_json=json.dumps(r.get("positions", {}), ensure_ascii=False),
        ))
        # 3) 更新任务状态
        task.last_bar_date = r["last_bar_date"]
        task.last_run_at = datetime.utcnow()
        if not task.account_start:
            task.account_start = r["account_start"]
        task.state_json = json.dumps(
            {"cash": r["cash"], "equity": equity, "positions": r.get("positions", {})},
            ensure_ascii=False,
        )
        # 存最近一次运行的完整每日净值曲线（date>=建仓日），供前端绘制
        task.equity_curve_json = json.dumps(r.get("curve", []), ensure_ascii=False)
        task.error_msg = None
        db.commit()
        return {
            "ok": True, "equity": equity, "pnl": pnl, "pnl_pct": pnl_pct,
            "new_trades": len(r["new_trades"]),
            "positions": r.get("positions", {}),
            "last_bar_date": r["last_bar_date"],
        }
    except Exception as e:  # noqa: BLE001
        db.rollback()
        task.last_run_at = datetime.utcnow()
        task.error_msg = str(e)[:255]
        db.commit()
        return {"ok": False, "error": str(e)}


def get_paper_task_detail(db, task: PaperTask) -> dict:
    """聚合任务详情：账户摘要 + 净值曲线 + 持仓 + 成交列表。"""
    snaps = (
        db.execute(
            select(PaperSnapshot)
            .where(PaperSnapshot.task_id == task.id)
            .order_by(PaperSnapshot.date)
        ).scalars().all()
    )
    trades = (
        db.execute(
            select(PaperTrade)
            .where(PaperTrade.task_id == task.id)
            .order_by(PaperTrade.date)
        ).scalars().all()
    )
    # 优先用最近一次运行的完整每日曲线；否则回退到快照序列
    try:
        full_curve = json.loads(task.equity_curve_json) if getattr(task, "equity_curve_json", None) else []
    except Exception:
        full_curve = []
    curve = full_curve if full_curve else [
        {"date": s.date, "equity": s.equity} for s in snaps
    ]
    trade_list = [
        {
            "date": t.date, "symbol": t.symbol, "side": t.side, "price": t.price,
            "shares": t.shares, "cash_after": t.cash_after, "commission": t.commission,
            "pnl": t.pnl, "signal_type": t.signal_type, "signal_reason": t.signal_reason,
        }
        for t in trades
    ]
    try:
        state = json.loads(task.state_json) if task.state_json else {}
    except Exception:
        state = {}
    latest = snaps[-1] if snaps else None
    return {
        "id": task.id, "name": task.name, "strategy_key": task.strategy_key,
        "kind": task.kind, "symbols": task.symbols, "index_code": task.index_code,
        "enabled": task.enabled, "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
        "account_start": task.account_start, "last_bar_date": task.last_bar_date,
        "error_msg": task.error_msg,
        "initial_cash": task.initial_cash,
        "equity": latest.equity if latest else task.initial_cash,
        "cash": latest.cash if latest else task.initial_cash,
        "market_value": latest.market_value if latest else 0.0,
        "pnl": latest.pnl if latest else 0.0,
        "pnl_pct": latest.pnl_pct if latest else 0.0,
        "positions": state.get("positions", {}),
        "curve": curve, "trades": trade_list,
    }
