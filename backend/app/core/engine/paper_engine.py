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
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.core.engine.backtest_engine import BacktestEngine
from app.core.engine.portfolio_backtest import PortfolioBacktestEngine
from app.core.strategies.registry import get_strategy
from app.models import (Stock, PaperTask, PaperTrade, PaperSnapshot,
                        FundamentalsHistory, FinancialsRaw, SignalDaily)
from app.services.data_source import get_realtime_prices
from app.services.membership_store import get_membership as _get_membership
# 复用回测路由里的数据加载函数（DB 优先、缺失回源落地），避免重复实现
from app.routers.strategy import _load_bars, _load_index_bars
from app.core.eval_profile import build_cost_profile
from app.core.cost_defaults import COMMISSION as _COST_C, SLIPPAGE as _COST_S


def _merge_params(meta: dict, params_json: str) -> dict:
    p = dict(meta.get("default_params") or {})
    try:
        p.update(json.loads(params_json) or {})
    except Exception:
        pass
    return p


_UNSET = object()


def _load_single(db, task: PaperTask, since_date=_UNSET) -> dict:
    """单标的模拟盘：跑『到今天』的 BacktestEngine，最后一根 bar 用实时价覆盖。

    since_date：成交补录水位（paper_trades 里该任务已有的最新成交日）。
    显式传 None 表示「账本里一笔都没有」→ 从 task.start_date 起全量补；
    不传（_UNSET）时退回旧行为，用 task.last_bar_date。
    """
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

    # ⚠️ 盘中未收盘的 bar 不参与信号计算：调度器每 30s 跑一次，若用实时价/未收盘 bar 算信号，
    # 收盘后正式数据到位会导致信号漂移，同一笔仓位被重复买卖（2026-09-11 task#3 重复卖出事故）。
    # 做法：未收盘时把当日 bar 摘出来，只用它做估值；信号与成交一律基于「已收盘」的最后一根 bar。
    today_iso = end.isoformat()
    now = datetime.now()
    market_closed = (now.hour * 60 + now.minute) >= 15 * 60 + 5  # 15:05 后视为当日收盘
    pending_bar = None
    if bars and bars[-1]["date"] >= today_iso and not market_closed:
        pending_bar = bars.pop()

    engine = BacktestEngine(
        bars, initial_cash=task.initial_cash, commission=task.commission,
        slippage=task.slippage,
        symbol=sym,   # 按板块取涨跌停幅度，与实盘 guard 口径一致
    )
    engine.run(meta["cls"], params)

    last_date = bars[-1]["date"]  # 已收盘的最后一根 bar 日期（成交截止日）
    watermark = task.last_bar_date if since_date is _UNSET else since_date
    new_trades = _incremental_trades(engine.trades, watermark, last_date, task.start_date)
    positions = (
        {sym: {"shares": round(engine.shares, 2), "cost": round(engine.avg_cost, 4)}}
        if engine.shares > 0 else {}
    )
    # 实时价仅用于估值展示（不产生成交）：优先取实时报价，其次用未收盘 bar 的价
    live_price = 0.0
    q = get_realtime_prices([sym])[0]
    if q and q.get("price", 0) > 0:
        live_price = float(q["price"])
    elif pending_bar:
        live_price = float(pending_bar.get("close") or 0)
    equity = engine.equity_curve[-1].equity
    if live_price > 0 and engine.shares > 0:
        equity = round(engine.cash + engine.shares * live_price, 2)

    account_start = task.start_date or task.account_start or last_date
    curve = [{"date": e.date, "equity": e.equity}
             for e in engine.equity_curve if e.date >= account_start]
    # 盘中把「当前实时估值」作为曲线最后一点，前端能看到实时净值
    if pending_bar and live_price > 0:
        curve = curve + [{"date": pending_bar["date"], "equity": equity}]
    return {
        "symbol": sym, "last_bar_date": last_date, "account_start": account_start,
        "equity": equity, "cash": engine.cash,
        "positions": positions, "new_trades": new_trades, "curve": curve,
    }


def _load_portfolio(db, task: PaperTask, since_date=_UNSET) -> dict:
    """组合模拟盘：跑『到今天』的 PortfolioBacktestEngine（日频 rebalance）。

    since_date 语义同 _load_single：显式 None = 全量补录，_UNSET = 用 last_bar_date。
    """
    meta = get_strategy(task.strategy_key)
    index_code = task.index_code or meta.get("index_code") or "000906"
    # 399 开头是深证（国证A指 sz399317 是平台统一基准）。原来一刀切 "sh"+code，
    # 对 399317 会拼成 sh399317 → 基准取空 → 任务直接挂。与 routers/paper.py
    # 的 _index_symbol 保持同一口径。
    index_symbol = meta.get("index_symbol") or (
        index_code if index_code[:2] in ("sh", "sz", "bj")
        else ("sz" if str(index_code).startswith("399") else "sh") + str(index_code)
    )
    params = _merge_params(meta, task.params_json)
    end = date.today()
    # 回测起点 = 账户建账日，不是「5 年前」。
    # 原来固定 5 年前：chan_buy 的 signal_union 池有 4787 只（几乎全A），
    # 5 年 × 4787 只日线直接把 4.5G 地址空间打爆 → MemoryError，
    # 而 run_paper_task 的 except 吞掉它、error_msg 存成空串，
    # 任务状态看着「成功」但没净值（2026-11-11 实测两次才定位到）。
    # 账户从哪天开始，就只需要那天的数据 + 预热。
    acc = None
    for cand in (task.start_date, task.account_start):
        if cand:
            try:
                acc = date.fromisoformat(str(cand)[:10])
                break
            except ValueError:
                continue
    sd = acc or end
    # ⚠️ 必须往前多取预热数据：jk 这类策略要 260 根 bar 才出第一个信号。
    # 原来从 sd 当天开始取、warmup 只跳 125 根 → 策略看到的历史不足，
    # `_build_pool` 返回空、rebalance 静默 return，整段 5 年零交易
    # （2026-11-06 实测：模拟盘 0 笔 vs 同参数回测 3101 笔）。
    # 口径与 routers/strategy.py 的回测一致：warmup_days * 1.6 + 40 天。
    # 预热天数要按**实际需要的 bar 数**推，不能只看 warmup_days：
    # xq 系声明 warmup_days=20，但 warmup_min 要 125 根（动量/波动/尾部回看取 max），
    # 只推 72 天根本凑不满，_load_bars 会被 len(b) >= warmup_min 全部过滤掉。
    _wd = int(params.get("warmup_days", 0) or 0)
    _need = max(_wd, 125)
    load_start = sd - timedelta(days=int(_need * 1.6) + 40)

    # 股票池必须跟回测同口径 —— 这里原来无脑用指数成分并集，忽略了策略的
    # pool_mode。xq 系（signal_union）实测就是这个原因 OOM：
    # 全A池要加载几千只 × 5 年日线，RSS 到 2.4G 时地址空间先撞 4.5G 上限，
    # 抛 MemoryError，任务状态却记成「成功但没数据」（2026-11-11 实测）。
    # 回测侧 routers/strategy.py 早就按 signal_union 只取信号命中过的票，
    # 同参数下 200 秒能跑完；模拟盘照抄这套即可。
    pool_mode = str(meta.get("pool_mode") or params.get("pool_mode") or "")
    membership = None
    if pool_mode == "signal_union":
        # 池子已由信号名单限死，引擎不再需要 PIT 成分过滤（传 None 即跳过）
        sig_key = str(meta.get("signal_key") or params.get("signal_key") or "")
        union = [r[0] for r in db.execute(
            select(SignalDaily.symbol).distinct().where(
                SignalDaily.strategy == sig_key,
                SignalDaily.date <= end,
            )
        ).all()]
        if not union:
            raise ValueError(f"选股器 {sig_key} 没有任何历史信号，无法建立股票池")
    else:
        membership = _get_membership(db, index_code, sd, end)
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
        b = _load_bars(db, sym, load_start.isoformat(), end.isoformat(), task.adj)
        if len(b) >= warmup_min:
            data[sym] = b
    if len(data) < 20:
        raise ValueError(f"本地仅 {len(data)} 只成分股有可用日K，不足以模拟")

    benchmark = _load_index_bars(db, index_symbol, sd, end, task.adj)
    if not benchmark:
        raise ValueError(f"未获取到基准指数 {index_code} 的行情数据")

    syms = list(data.keys())
    attrs_rows = db.execute(
        select(Stock.symbol, Stock.industry, Stock.market_cap, Stock.pe_ttm, Stock.pb,
               Stock.roe, Stock.revenue_yoy, Stock.profit_yoy)
        .where(Stock.symbol.in_(syms))
    ).all()
    attributes = {
        r.symbol: {
            "industry": r.industry, "market_cap": r.market_cap,
            "pe_ttm": r.pe_ttm, "pb": r.pb,
            "roe": r.roe, "revenue_yoy": r.revenue_yoy, "profit_yoy": r.profit_yoy,
        }
        for r in attrs_rows
    }

    # PIT 财务明细（现金流 / 资本开支 / 总资产）：jk 这类 FCF 策略的
    # ROE / FCF-OE 筛选全靠它。2026-11-06 实测漏传 → ctx.financial() 全空
    # → _screen_fundamentals 返回 0 只 → rebalance 静默 return → 5 年零交易
    # （同参数回测 3101 笔，模拟盘 0 笔）。照抄 routers/strategy.py 的口径。
    fin_rows = db.execute(
        select(FinancialsRaw.symbol, FinancialsRaw.ann_date, FinancialsRaw.end_date,
               FinancialsRaw.roe, FinancialsRaw.n_cashflow_act, FinancialsRaw.capex,
               FinancialsRaw.total_assets)
        .where(FinancialsRaw.symbol.in_(syms))
    ).all()
    financials_raw: dict[str, list] = {}
    for r in fin_rows:
        financials_raw.setdefault(r.symbol, []).append({
            "ann_date": r.ann_date, "end_date": r.end_date, "roe": r.roe,
            "ocf": r.n_cashflow_act, "capex": r.capex,
            "total_assets": r.total_assets,
        })

    # 基本面历史快照（PEAD 盈余惊喜等时序因子用）
    fund_rows = db.execute(
        select(FundamentalsHistory.symbol, FundamentalsHistory.report_date,
               FundamentalsHistory.roe, FundamentalsHistory.revenue_yoy,
               FundamentalsHistory.profit_yoy)
        .where(FundamentalsHistory.symbol.in_(syms))
    ).all()
    fundamentals: dict[str, list] = {}
    for r in fund_rows:
        fundamentals.setdefault(r.symbol, []).append({
            "report_date": r.report_date,
            "roe": r.roe, "revenue_yoy": r.revenue_yoy, "profit_yoy": r.profit_yoy,
        })

    # 风险约束（可选，参数传入则启用）
    risk_limits = {}
    mpp = float(params.get("max_position_pct", 0) or 0)
    mge = float(params.get("max_gross_exposure", 0) or 0)
    if mpp > 0:
        risk_limits["max_position_pct"] = mpp
    if mge > 0:
        risk_limits["max_gross_exposure"] = mge

    # 与回测同口径：估值要按日期取，不能拿最新快照跑历史（见 valuation.py）
    from app.core.engine.valuation import load_valuation_series

    valuation = load_valuation_series(db, list(data.keys()), sd, end)

    engine = PortfolioBacktestEngine(
        data, benchmark,
        initial_cash=task.initial_cash, commission=task.commission, slippage=task.slippage,
        rebalance_period=int(params.get("rebalance_period", 21)),
        # warmup 必须取策略声明的 warmup_days（jk=270），否则引擎从第 125 根就开跑，
        # 策略的 260 日回看拿不满 → 选股全空。start_date 让引擎把「第 0 天」锚回 sd。
        warmup=max(warmup_min, int(params.get("warmup_days", 0) or 0)),
        attributes=attributes, valuation=valuation, membership=membership,
        risk_limits=risk_limits or None, fundamentals=fundamentals or None,
        financials_raw=financials_raw or None,
        start_date=sd,
    )
    result = engine.run(meta["cls"], params)

    last_date = engine.equity_curve[-1].date if engine.equity_curve else (engine.dates[-1] if engine.dates else end.isoformat())
    watermark = task.last_bar_date if since_date is _UNSET else since_date
    new_trades = _incremental_trades(engine.trades, watermark, last_date, task.start_date)
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
        "factor_analysis": result.factor_analysis,
        "risk_limits": result.risk_limits, "risk_clamps": result.risk_clamps,
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
        # 成交补录水位：以「账本里已有的最新成交日」为准，不是 task.last_bar_date。
        # last_bar_date 每跑一次就前进，只要有几天没跑（容器重启 / 数据库故障 /
        # 任务被停用），那几天的成交就永久漏掉。2026-11-11 发现 paper_trades
        # 一直是 0 行 —— 根因就是建任务时 last_bar_date 已是当天，
        # 起始那批成交从没被扫过，而后续每天又只扫当天。
        since = db.execute(
            select(func.max(PaperTrade.date)).where(PaperTrade.task_id == task.id)
        ).scalar()
        if task.kind == "portfolio":
            r = _load_portfolio(db, task, since_date=since)
        else:
            r = _load_single(db, task, since_date=since)

        # 1) 写入增量成交（幂等：同 任务/日期/标的/方向/数量 已存在则跳过，防重复成交）
        for t in r["new_trades"]:
            t_sym = getattr(t, "symbol", r.get("symbol", ""))
            dup = db.execute(
                select(PaperTrade.id).where(
                    PaperTrade.task_id == task.id,
                    PaperTrade.date == t.date,
                    PaperTrade.symbol == t_sym,
                    PaperTrade.side == t.side,
                    PaperTrade.shares == round(t.shares, 2),
                )
            ).first()
            if dup:
                continue
            db.add(PaperTrade(
                task_id=task.id, date=t.date,
                symbol=t_sym,
                side=t.side, price=round(t.price, 4), shares=round(t.shares, 2),
                cash_after=round(t.cash_after, 2), commission=round(t.commission, 2),
                pnl=round(getattr(t, "pnl", 0.0), 2),
                signal_type=getattr(t, "signal_type", ""),
                signal_reason=getattr(t, "signal_reason", ""),
            ))
        # 2) 写入净值快照（同一交易日 upsert：盘中每 30s 跑一次，不能每轮塞一条快照）
        equity = r["equity"]
        mkt_val = equity - r["cash"]
        pnl = equity - task.initial_cash
        pnl_pct = pnl / task.initial_cash if task.initial_cash else 0.0
        snap = db.execute(
            select(PaperSnapshot).where(
                PaperSnapshot.task_id == task.id,
                PaperSnapshot.date == r["last_bar_date"],
            )
        ).scalars().first()
        if snap:
            snap.equity = round(equity, 2)
            snap.cash = round(r["cash"], 2)
            snap.market_value = round(mkt_val, 2)
            snap.pnl = round(pnl, 2)
            snap.pnl_pct = round(pnl_pct, 6)
            snap.positions_json = json.dumps(r.get("positions", {}), ensure_ascii=False)
        else:
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
        # 存组合任务的因子研究（IC/IR/分层/PEAD 等）+ 风险硬上限审计，供模拟盘详情页展示
        task.factor_analysis_json = json.dumps({
            "factor_analysis": r.get("factor_analysis") or {},
            "risk_limits": r.get("risk_limits"),
            "risk_clamps": r.get("risk_clamps") or [],
        }, ensure_ascii=False)
        task.error_msg = None
        db.commit()
        return {
            "ok": True, "equity": equity, "pnl": pnl, "pnl_pct": pnl_pct,
            "new_trades": len(r["new_trades"]),
            "positions": r.get("positions", {}),
            "last_bar_date": r["last_bar_date"],
        }
    except MemoryError:
        # str(MemoryError()) == ""，被下面的 except Exception 接住就会写出
        # 一个空 error_msg —— 看着成功、实际啥也没算（踩过两次）。
        db.rollback()
        task.last_run_at = datetime.utcnow()
        task.error_msg = ("内存超限：策略股票池太大。缩短建账日到今天的区间，"
                          "或调高 PAPER_MEM_LIMIT_GB")
        db.commit()
        return {"ok": False, "error": task.error_msg}
    except Exception as e:  # noqa: BLE001
        db.rollback()
        task.last_run_at = datetime.utcnow()
        task.error_msg = f"{type(e).__name__}: {str(e)}"[:255]
        db.commit()
        return {"ok": False, "error": task.error_msg}


def _paper_fa_blob(task) -> dict:
    """兼容解析 factor_analysis_json：新格式为 {factor_analysis, risk_limits, risk_clamps}，
    旧格式直接存的是 factor_analysis 对象本身。统一返回三者。"""
    raw = getattr(task, "factor_analysis_json", None)
    try:
        blob = json.loads(raw) if raw else None
    except Exception:
        blob = None
    if isinstance(blob, dict) and "factor_analysis" in blob:
        return {
            "factor_analysis": blob.get("factor_analysis"),
            "risk_limits": blob.get("risk_limits"),
            "risk_clamps": blob.get("risk_clamps") or [],
        }
    # 旧格式：整个 dict 即 factor_analysis
    return {"factor_analysis": blob, "risk_limits": None, "risk_clamps": []}


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
            "trade_date": t.date.isoformat() if hasattr(t.date, "isoformat") else str(t.date),
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
    commission = float(getattr(task, "commission", 0.0) or 0.0)
    slippage = float(getattr(task, "slippage", 0.0) or 0.0)
    fees_paid_commission = float(sum(float(getattr(t, "commission", 0.0) or 0.0) for t in trades))
    # 数值等于 defaults → source=default；否则 user（详情无请求体，按落库费率推断）
    cost_src = "default" if (commission == _COST_C and slippage == _COST_S) else "user"
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
        "factor_analysis": _paper_fa_blob(task).get("factor_analysis"),
        "risk_limits": _paper_fa_blob(task).get("risk_limits"),
        "risk_clamps": _paper_fa_blob(task).get("risk_clamps") or [],
        # W47-1：详情回显与 create 同 shape 的成本字段（不 migration）
        "commission": commission,
        "slippage": slippage,
        "cost_profile": build_cost_profile(commission, slippage, source=cost_src),
        "fees_paid_commission": fees_paid_commission,
    }
