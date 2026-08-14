"""策略与回测 API。

- GET  /strategy/strategies   列出可用策略模板（含参数 schema，供前端动态渲染）
- POST /strategy/backtest      运行一次回测（落库并返回结果）
- GET  /strategy/backtests     回测历史列表
- GET  /strategy/backtests/{id} 回测详情

支持两类策略：
1. 单标的策略（dual_ma / ma_cross / momentum）—— 走 BacktestEngine
2. 组合策略（csi800_enhanced 等 multi_asset）—— 走 PortfolioBacktestEngine
"""
from __future__ import annotations

import itertools
import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Backtest, Stock, KlineDaily, IndexKlineDaily, FundamentalsHistory
from app.schemas import (
    BacktestRequest,
    BacktestResult,
    BacktestSummary,
    EquityPointModel,
    OptimizeRequest,
    OptimizeTrial,
    StrategyInfo,
    TradePoint,
)
from app.services import data_source, duckdb_store, ingestion, membership_store
from app.core.engine.backtest_engine import BacktestEngine
from app.core.engine.portfolio_backtest import PortfolioBacktestEngine
from app.core.strategies.registry import get_strategy, list_strategies
from app.core import task_queue

router = APIRouter(prefix="/strategy", tags=["strategy"])


@router.get("/strategies", response_model=list[StrategyInfo])
def strategies():
    return list_strategies()


@router.post("/backtest", response_model=BacktestResult)
def run_backtest(req: BacktestRequest, db: Session = Depends(get_db)):
    """同步回测（原型/前端现有调用）。"""
    return _do_backtest(req, db)


def _do_backtest(req: BacktestRequest, db: Session, progress_cb=None) -> BacktestResult:
    """回测核心逻辑（同步/异步共用）。progress_cb(p, msg) 可选，用于上报进度。"""
    # 1) 校验策略
    try:
        meta = get_strategy(req.strategy)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"未知策略: {req.strategy}")

    # 2) 合并默认参数
    params = dict(meta["default_params"])
    params.update({k: v for k, v in req.params.items() if v is not None})

    multi = meta.get("multi_asset", False)

    if multi:
        result = _run_portfolio(db, req, meta, params, progress_cb=progress_cb)
    else:
        result = _run_single(db, req, meta, params)

    record = _persist(db, req, meta, params, result)
    return _to_result(record, meta["name"], multi)


# ——— 异步回测（任务队列，完整版架构：回测异步化 + 进度查询） ———
@router.post("/backtest/async")
def submit_backtest(req: BacktestRequest):
    """提交异步回测任务，立即返回 task_id；完成后 GET /backtest/tasks/{id} 取 result_id。"""
    tid = task_queue.submit(f"backtest:{req.strategy}", _execute_backtest_task, req.model_dump())
    return {"task_id": tid, "status": "running"}


@router.get("/backtest/tasks/{task_id}")
def backtest_task_status(task_id: str):
    """查询异步回测任务状态（running/done/error + 进度 + result_id）。"""
    t = task_queue.get(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return t


@router.get("/backtest/tasks")
def backtest_task_list():
    """最近任务列表（诊断用）。"""
    return task_queue.list_tasks(limit=20)


def _execute_backtest_task(payload: dict, _task_id: str = "") -> int:
    """后台线程执行体：独立 Session 跑回测，返回回测记录 id。"""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        req = BacktestRequest(**payload)

        def cb(p: float, msg: str) -> None:
            if _task_id:
                task_queue.update_progress(_task_id, p, msg)

        result = _do_backtest(req, db, progress_cb=cb)
        return result.id
    finally:
        db.close()


# ——— 单标的回测 ———
def _run_single(db, req, meta, params):
    bars = _load_bars(db, req.symbol, req.start, req.end, req.adj)
    if not bars:
        raise HTTPException(
            status_code=404,
            detail=f"未获取到 {req.symbol} 的行情数据（{req.start}~{req.end}）",
        )
    engine = BacktestEngine(
        bars, initial_cash=req.initial_cash,
        commission=req.commission, slippage=req.slippage,
    )
    try:
        return engine.run(meta["cls"], params)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"回测执行失败: {e}")


# ——— 组合（指数增强）回测 ———
def _run_portfolio(db, req, meta, params, progress_cb=None):
    index_code = meta.get("index_code", "000906")
    index_symbol = meta.get("index_symbol", "sh000906")
    rebalance_period = int(params.get("rebalance_period", 21))

    sd = date.fromisoformat(req.start)
    ed = date.fromisoformat(req.end)

    # 1) 时点(point-in-time)成分股成员资格：覆盖整个回测区间的月度快照
    #    消除『用当前成分股回测整段历史』带来的前视/幸存者偏差。
    #    优先读本地 index_membership 缓存（在线源波动不影响回测可复现性），
    #    缺失月份才在线拉取并回填。
    try:
        membership = membership_store.get_membership(db, index_code, sd, ed)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"获取指数 {index_code} 时点成分股失败：{e}。",
        )
    if not membership:
        raise HTTPException(status_code=404, detail="时点成分股快照为空")

    # 回测窗口内曾入选指数的全部标的并集（含已退出者），用于一次性拉取日K
    union_syms: list[str] = []
    seen: set = set()
    for _ds, sset in membership:
        for s in sset:
            if s not in seen:
                seen.add(s)
                union_syms.append(s)

    # 2) 加载各标的日K（DB 优先，缺失回源并落地）
    data: dict[str, list[dict]] = {}
    warmup_min = max(
        int(params.get("momentum_lookback", 120)),
        int(params.get("vol_lookback", 60)),
        int(params.get("beta_lookback", 120)),
        int(params.get("tail_lookback", 120)),
    ) + 5
    total_union = len(union_syms)
    for i, sym in enumerate(union_syms):
        bars = _load_bars(db, sym, req.start, req.end, req.adj)
        if len(bars) >= warmup_min:
            data[sym] = bars
        if progress_cb and (i % 50 == 0 or i == total_union - 1):
            progress_cb(i / max(total_union, 1), f"加载行情 {i}/{total_union}")

    if len(data) < 20:
        raise HTTPException(
            status_code=409,
            detail=(
                f"本地仅 {len(data)} 只成分股有可用日K，不足以回测。"
                "可运行 `python scripts/seed_index.py --index {index_code}` 预拉取成分股日K"
                "（不预拉取也会在回测时按需自动回源，但较慢）。"
            ).format(index_code=index_code),
        )

    # 3) 基准指数日K
    benchmark = _load_index_bars(db, index_symbol, sd, ed, req.adj)
    if not benchmark:
        raise HTTPException(
            status_code=404,
            detail=f"未获取到基准指数 {index_symbol} 的行情数据",
        )

    # 4) 标的截面属性（行业/市值/估值），供中性化与估值因子使用
    syms = list(data.keys())
    attrs_rows = db.execute(
        select(Stock.symbol, Stock.industry, Stock.market_cap, Stock.pe_ttm, Stock.pb,
               Stock.roe, Stock.revenue_yoy, Stock.profit_yoy)
        .where(Stock.symbol.in_(syms))
    ).all()
    attributes = {
        r.symbol: {
            "industry": r.industry,
            "market_cap": r.market_cap,
            "pe_ttm": r.pe_ttm,
            "pb": r.pb,
            "roe": r.roe,
            "revenue_yoy": r.revenue_yoy,
            "profit_yoy": r.profit_yoy,
        }
        for r in attrs_rows
    }

    # 5) 基本面历史快照（多报告期），供 point-in-time 时序因子（PEAD 盈余惊喜）使用
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
            "roe": r.roe,
            "revenue_yoy": r.revenue_yoy,
            "profit_yoy": r.profit_yoy,
        })

    # 6) 风险约束（借鉴 ai-hedge-fund risk/limits）：从参数取单只/总敞口上限，<=0 视为关闭
    risk_limits = {}
    mpp = float(params.get("max_position_pct", 0) or 0)
    mge = float(params.get("max_gross_exposure", 0) or 0)
    if mpp > 0:
        risk_limits["max_position_pct"] = mpp
    if mge > 0:
        risk_limits["max_gross_exposure"] = mge

    engine = PortfolioBacktestEngine(
        data, benchmark,
        initial_cash=req.initial_cash,
        commission=req.commission,
        slippage=req.slippage,
        rebalance_period=rebalance_period,
        warmup=warmup_min,
        attributes=attributes,
        membership=membership,
        risk_limits=risk_limits or None,
        fundamentals=fundamentals or None,
    )
    try:
        return engine.run(meta["cls"], params)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"组合回测执行失败: {e}")


# ——— 数据加载 ———
def _load_bars(db: Session, symbol: str, start: str, end: str, adj: str) -> list[dict]:
    sd = date.fromisoformat(start)
    ed = date.fromisoformat(end)
    # ① DuckDB 分析库（列式加速，完整版架构默认路径）
    cached = duckdb_store.get_stock_bars(symbol, adj, sd, ed)
    if cached:
        return cached
    # ② 回退 SQLite；③ 库无则在线拉取并写回
    stmt = (
        select(KlineDaily)
        .where(
            KlineDaily.symbol == symbol,
            KlineDaily.adj == adj,
            KlineDaily.trade_date >= sd,
            KlineDaily.trade_date <= ed,
        )
        .order_by(KlineDaily.trade_date)
    )
    rows = db.execute(stmt).scalars().all()
    if not rows:
        try:
            fetched = data_source.get_stock_daily_qfq(symbol, sd, ed)
            if fetched:
                ingestion.upsert_kline(db, fetched, symbol, adj)
                db.commit()
                rows = db.execute(stmt).scalars().all()
        except Exception:  # noqa: BLE001
            pass
    return [
        {
            "symbol": symbol,
            "date": r.trade_date.isoformat(),
            "open": r.open, "high": r.high, "low": r.low,
            "close": r.close, "volume": r.volume, "amount": r.amount,
        }
        for r in rows
    ]


def _load_index_bars(db: Session, symbol: str, sd: date, ed: date, adj: str) -> list[dict]:
    """加载基准指数日K。

    优先级：① DuckDB 分析库（首选，离线可用、列式加速）；② SQLite
    index_kline_daily；③ 库无则在线拉取并写回缓存，避免每次都依赖外部数据源。
    """
    # ① DuckDB 分析库
    cached = duckdb_store.get_index_bars(symbol, sd, ed)
    if cached:
        return cached
    # ② SQLite 库内已有该指数数据 → 直接读库
    cnt = db.execute(
        select(func.count()).select_from(IndexKlineDaily)
        .where(IndexKlineDaily.symbol == symbol)
    ).scalar() or 0
    if cnt:
        rows = db.execute(
            select(IndexKlineDaily).where(
                IndexKlineDaily.symbol == symbol,
                IndexKlineDaily.trade_date >= sd,
                IndexKlineDaily.trade_date <= ed,
            ).order_by(IndexKlineDaily.trade_date)
        ).scalars().all()
        return [{
            "symbol": r.symbol,
            "date": r.trade_date.isoformat(),
            "open": r.open, "high": r.high, "low": r.low,
            "close": r.close, "volume": r.volume, "amount": r.amount,
        } for r in rows]
    # ② 库内缺失 → 在线拉全量历史写库后返回
    from datetime import date as _date
    try:
        fetched = data_source.get_index_daily_kline(symbol, _date(1990, 1, 1), ed)
    except Exception:  # noqa: BLE001
        fetched = []
    if fetched:
        _cache_index_bars(db, symbol, fetched)
        return [{
            "symbol": symbol,
            "date": (r["trade_date"].isoformat() if hasattr(r["trade_date"], "isoformat") else str(r["trade_date"])),
            "open": r["open"], "high": r["high"], "low": r["low"],
            "close": r["close"], "volume": r["volume"], "amount": r["amount"],
        } for r in fetched if sd <= r["trade_date"] <= ed]
    return []


def _cache_index_bars(db: Session, symbol: str, fetched: list[dict]) -> None:
    """将在线拉取的指数行情 upsert 进 index_kline_daily（按 symbol+date 去重）。"""
    existing = {r[0] for r in db.execute(
        select(IndexKlineDaily.trade_date).where(IndexKlineDaily.symbol == symbol)
    ).all()}
    objs = []
    for r in fetched:
        d = r["trade_date"]
        if d in existing:
            continue
        objs.append(IndexKlineDaily(
            symbol=symbol, trade_date=d,
            open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
            close=float(r["close"]),
            volume=int(r.get("volume", 0) or 0), amount=float(r.get("amount", 0) or 0),
        ))
    if objs:
        db.bulk_save_objects(objs)
        db.commit()


# ——— 落库 ———
def _persist(db, req, meta, params, result) -> Backtest:
    multi = meta.get("multi_asset", False)
    record = Backtest(
        symbol=req.symbol,
        strategy_key=req.strategy,
        start_date=req.start,
        end_date=req.end,
        adj=req.adj,
        initial_cash=req.initial_cash,
        commission=req.commission,
        slippage=req.slippage,
        params_json=json.dumps(params, ensure_ascii=False),
        total_return=result.total_return,
        annual_return=result.annual_return,
        max_drawdown=result.max_drawdown,
        sharpe=result.sharpe,
        win_rate=getattr(result, "win_rate", 0.0),
        trade_count=result.trade_count,
        round_trips=getattr(result, "round_trips", 0),
        final_equity=result.final_equity,
        multi_asset=multi,
        universe_size=getattr(result, "universe_size", 0),
        symbols_used=getattr(result, "symbols_used", 0),
        benchmark_symbol=meta.get("index_symbol", "") if multi else "",
        benchmark_total_return=getattr(result, "benchmark_total_return", 0.0),
        excess_return=getattr(result, "excess_return", 0.0),
        info_ratio=getattr(result, "info_ratio", 0.0),
        equity_curve_json=json.dumps(
            [{"date": e.date, "equity": e.equity, "benchmark": e.benchmark,
              "hedged": getattr(e, "hedged", 0.0)} for e in result.equity_curve],
            ensure_ascii=False,
        ),
        trades_json=json.dumps(
            [
                {
                    "date": t.date, "symbol": getattr(t, "symbol", ""),
                    "side": t.side, "price": t.price, "shares": t.shares,
                    "cash_after": t.cash_after, "commission": t.commission,
                    "pnl": getattr(t, "pnl", 0.0),
                    "signal_type": getattr(t, "signal_type", ""),
                    "signal_reason": getattr(t, "signal_reason", ""),
                }
                for t in result.trades
            ],
            ensure_ascii=False,
        ),
        extra_json=json.dumps(
            {
                "holdings": getattr(result, "holdings", []),
                "industry_distribution": getattr(result, "industry_distribution", {}),
                "factor_analysis": getattr(result, "factor_analysis", None),
                "risk_limits": getattr(result, "risk_limits", None),
                "risk_clamps": getattr(result, "risk_clamps", []),
                "hedged": {
                    "beta": getattr(result, "hedged_beta", 0.0),
                    "total_return": getattr(result, "hedged_total_return", 0.0),
                    "annual_return": getattr(result, "hedged_annual_return", 0.0),
                    "sharpe": getattr(result, "hedged_sharpe", 0.0),
                    "max_drawdown": getattr(result, "hedged_max_drawdown", 0.0),
                },
            },
            ensure_ascii=False,
        ),
        status="done",
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("/backtests", response_model=list[BacktestSummary])
def list_backtests(limit: int = 50, db: Session = Depends(get_db)):
    stmt = select(Backtest).order_by(Backtest.created_at.desc()).limit(limit)
    rows = db.execute(stmt).scalars().all()
    out = []
    for r in rows:
        name = _strategy_name(r.strategy_key)
        out.append(
            BacktestSummary(
                id=r.id,
                symbol=r.symbol,
                strategy_key=r.strategy_key,
                strategy_name=name,
                multi_asset=r.multi_asset,
                start_date=r.start_date,
                end_date=r.end_date,
                total_return=r.total_return,
                annual_return=r.annual_return,
                max_drawdown=r.max_drawdown,
                sharpe=r.sharpe,
                win_rate=r.win_rate,
                trade_count=r.trade_count,
                benchmark_total_return=r.benchmark_total_return,
                excess_return=r.excess_return,
                info_ratio=r.info_ratio,
                created_at=r.created_at.isoformat() if r.created_at else None,
            )
        )
    return out


@router.get("/backtests/{bt_id}", response_model=BacktestResult)
def get_backtest(bt_id: int, db: Session = Depends(get_db)):
    r = db.get(Backtest, bt_id)
    if not r:
        raise HTTPException(status_code=404, detail="回测记录不存在")
    return _to_result(r, _strategy_name(r.strategy_key), r.multi_asset)


@router.post("/optimize", response_model=list[OptimizeTrial])
def optimize_strategy(req: OptimizeRequest, db: Session = Depends(get_db)):
    """网格搜索参数寻优：遍历 param_ranges 中所有参数组合，按 rank_by 排序返回。"""
    try:
        meta = get_strategy(req.strategy)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"未知策略: {req.strategy}")
    if meta.get("multi_asset"):
        raise HTTPException(status_code=400, detail="参数寻优暂不支持指数增强策略,请使用单标的策略")

    # 生成参数组合
    keys = list(req.param_ranges.keys())
    if not keys:
        raise HTTPException(status_code=400, detail="param_ranges 至少需要一个参数维度")
    combos = list(itertools.product(*req.param_ranges.values()))

    results = []
    for combo in combos:
        params = dict(zip(keys, combo))
        fixed = dict(meta["default_params"])
        fixed.update(params)
        try:
            result = _run_single(db, req, meta, fixed)
        except Exception:
            continue  # 某组参数可能无合格数据，跳过
        results.append(
            OptimizeTrial(
                params=params,
                total_return=result.total_return,
                annual_return=result.annual_return,
                max_drawdown=result.max_drawdown,
                sharpe=result.sharpe,
                win_rate=result.win_rate,
                trade_count=result.trade_count,
                final_equity=result.final_equity,
            )
        )

    # 排序
    rank_by = getattr(req, "rank_by", "sharpe") or "sharpe"
    if rank_by == "total_return":
        reverse = True
        results.sort(key=lambda x: x.total_return, reverse=True)
    elif rank_by == "max_drawdown":
        results.sort(key=lambda x: x.max_drawdown)  # 绝对值越小越好
    else:
        results.sort(key=lambda x: x.sharpe, reverse=True)

    return results


# ——— 内部工具 ———
def _strategy_name(key: str) -> str:
    try:
        return get_strategy(key)["name"]
    except Exception:
        return key


def _to_result(r: Backtest, name: str, multi: bool) -> BacktestResult:
    try:
        equity = json.loads(r.equity_curve_json)
    except Exception:
        equity = []
    try:
        trades = json.loads(r.trades_json)
    except Exception:
        trades = []
    try:
        extra = json.loads(r.extra_json)
    except Exception:
        extra = {}
    try:
        params = json.loads(r.params_json)
    except Exception:
        params = {}
    hedged = extra.get("hedged", {})
    return BacktestResult(
        id=r.id,
        symbol=r.symbol,
        start=r.start_date,
        end=r.end_date,
        strategy_key=r.strategy_key,
        strategy_name=name,
        multi_asset=r.multi_asset,
        universe_size=r.universe_size,
        symbols_used=r.symbols_used,
        params=params,
        initial_cash=r.initial_cash,
        final_equity=r.final_equity,
        total_return=r.total_return,
        annual_return=r.annual_return,
        max_drawdown=r.max_drawdown,
        sharpe=r.sharpe,
        win_rate=r.win_rate,
        trade_count=r.trade_count,
        round_trips=r.round_trips,
        benchmark_total_return=r.benchmark_total_return,
        excess_return=r.excess_return,
        info_ratio=r.info_ratio,
        equity_curve=[EquityPointModel(**e) for e in equity],
        trades=[TradePoint(**t) for t in trades],
        holdings=extra.get("holdings", []),
        industry_distribution=extra.get("industry_distribution", {}),
        factor_analysis=extra.get("factor_analysis", None),
        risk_limits=extra.get("risk_limits", None),
        risk_clamps=extra.get("risk_clamps", []),
        hedged_beta=hedged.get("beta", 0.0),
        hedged_total_return=hedged.get("total_return", 0.0),
        hedged_annual_return=hedged.get("annual_return", 0.0),
        hedged_sharpe=hedged.get("sharpe", 0.0),
        hedged_max_drawdown=hedged.get("max_drawdown", 0.0),
        created_at=r.created_at.isoformat() if r.created_at else None,
    )
