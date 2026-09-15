"""策略与回测 API。

- GET  /strategy/strategies   列出可用策略模板（含参数 schema，供前端动态渲染）
- POST /strategy/backtest      运行一次回测（落库并返回结果）
- GET  /strategy/backtests     回测历史列表
- GET  /strategy/backtests/{id} 回测详情
- POST /strategy/optimize      网格寻优（同步，≤400 组）
- POST /strategy/optimize/async   大网格寻优（后台任务，≤5000 组，返回 job_id）
- GET  /strategy/optimize/async/{job_id}  查进度 / 取结果
- DELETE /strategy/optimize/async/{job_id} 取消任务

支持两类策略：
1. 单标的策略（dual_ma / ma_cross / momentum）—— 走 BacktestEngine
2. 组合策略（csi800_enhanced 等 multi_asset）—— 走 PortfolioBacktestEngine
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.config import DATA_DIR, settings
from app.database import get_db
from app.models import Backtest, Stock, KlineDaily, IndexKlineDaily, FundamentalsHistory, FinancialsRaw
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

# backend 目录（子进程脚本的工作目录与 PYTHONPATH 基准）
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


@router.websocket("/backtest/ws/{task_id}")
async def ws_backtest_task(websocket: WebSocket, task_id: str):
    """WebSocket 实时推送回测任务进度（设计 v1.0：/ws/backtest/{task_id}）。

    连接后立即推送当前状态，之后每次任务状态变更（进度/完成/失败）实时推送；
    done/error 后自动关闭。前端可据此替代轮询（保留轮询作兜底）。
    """
    await websocket.accept()
    t = task_queue.get(task_id)
    if t is None:
        await websocket.send_json({"type": "error", "message": f"任务不存在: {task_id}"})
        await websocket.close()
        return
    await websocket.send_json(t)
    if t["status"] in ("done", "error"):
        await websocket.close()
        return
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    task_queue.subscribe(task_id, loop, q)
    try:
        while True:
            data = await q.get()
            await websocket.send_json(data)
            if data.get("status") in ("done", "error"):
                break
    except WebSocketDisconnect:
        pass
    finally:
        task_queue.unsubscribe(task_id, q)


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
def _run_single(db, req, meta, params, start=None, end=None):
    """跑一次单标的回测。start/end 用于样本内/外分段（留空则用请求区间）。"""
    _s = start or req.start
    _e = end or req.end
    bars = _load_bars(db, req.symbol, _s, _e, req.adj)
    if not bars:
        raise HTTPException(
            status_code=404,
            detail=f"未获取到 {req.symbol} 的行情数据（{_s}~{_e}）",
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
_KLINE_MIN: dict = {}


def _kline_min_date(db) -> date | None:
    """本地个股日K的最早交易日（进程内缓存）。

    用于把回测的预热取数窗口夹在数据边界内 —— 否则 warmup 天数会被
    「只有指数有行情、个股还没有」的日期白白消耗掉，表现为回测开头长期空仓。
    """
    if "d" not in _KLINE_MIN:
        try:
            v = db.execute(
                select(KlineDaily.trade_date).order_by(KlineDaily.trade_date).limit(1)
            ).scalar()
        except Exception:  # noqa: BLE001
            v = None
        _KLINE_MIN["d"] = v
    return _KLINE_MIN["d"]


def _run_portfolio(db, req, meta, params, progress_cb=None):
    index_code = meta.get("index_code", "000906")
    index_symbol = meta.get("index_symbol", "sh000906")
    rebalance_period = int(params.get("rebalance_period", 21))

    sd = date.fromisoformat(req.start)
    ed = date.fromisoformat(req.end)
    # 预热期也要有行情：动量/波动等长回看因子需要 warmup_days 根 bar 才能出信号。
    # 不往前多取，等于回测区间的前 warmup_days 个交易日全部空仓（白丢一年收益）。
    warmup_days = int(params.get("warmup_days", 0) or 0)
    load_start = sd - timedelta(days=int(warmup_days * 1.6) + 40) if warmup_days > 0 else sd
    kmin = _kline_min_date(db)
    if kmin and load_start < kmin:
        load_start = kmin

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

    # 2) 加载各标的日K：DuckDB 批量一次取全部（列式加速），缺失走 _load_bars 兜底
    data: dict[str, list[dict]] = {}
    warmup_min = max(
        int(params.get("momentum_lookback", 120)),
        int(params.get("vol_lookback", 60)),
        int(params.get("beta_lookback", 120)),
        int(params.get("tail_lookback", 120)),
    ) + 5
    cached = duckdb_store.get_stock_bars_batch(union_syms, req.adj, load_start, ed)
    for sym, bars in cached.items():
        if len(bars) >= warmup_min:
            data[sym] = bars
    missing = [s for s in union_syms if s not in cached]
    total_missing = len(missing)
    for i, sym in enumerate(missing):
        bars = _load_bars(db, sym, load_start.isoformat(), req.end, req.adj)
        if len(bars) >= warmup_min:
            data[sym] = bars
        if progress_cb and (i % 50 == 0 or i == total_missing - 1):
            progress_cb(
                len(data) / max(len(union_syms), 1),
                f"补数据 {i}/{total_missing}（DuckDB 已取 {len(union_syms) - total_missing}）",
            )

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
    benchmark = _load_index_bars(db, index_symbol, load_start, ed, req.adj)
    if not benchmark:
        raise HTTPException(
            status_code=404,
            detail=f"未获取到基准指数 {index_symbol} 的行情数据",
        )

    # 4) 标的截面属性（行业/市值/估值），供中性化与估值因子使用
    syms = list(data.keys())
    attrs_rows = db.execute(
        select(Stock.symbol, Stock.name, Stock.industry, Stock.market_cap, Stock.pe_ttm, Stock.pb,
               Stock.roe, Stock.revenue_yoy, Stock.profit_yoy, Stock.list_date)
        .where(Stock.symbol.in_(syms))
    ).all()
    attributes = {
        r.symbol: {
            "name": r.name,
            "list_date": r.list_date,
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

    # 5b) PIT 财务明细（现金流 / 资本开支 / 总资产），供 FCF 类因子按 ann_date 点查。
    #     只在池内标的范围内加载，避免全市场拉大表拖慢回测。
    fin_rows = []
    if syms:
        fin_rows = db.execute(
            select(FinancialsRaw.symbol, FinancialsRaw.ann_date, FinancialsRaw.end_date,
                   FinancialsRaw.roe, FinancialsRaw.n_cashflow_act, FinancialsRaw.capex,
                   FinancialsRaw.total_assets)
            .where(FinancialsRaw.symbol.in_(syms))
        ).all()
    financials_raw: dict[str, list] = {}
    for r in fin_rows:
        financials_raw.setdefault(r.symbol, []).append({
            "ann_date": r.ann_date,
            "end_date": r.end_date,
            "roe": r.roe,
            "ocf": r.n_cashflow_act,
            "capex": r.capex,
            "total_assets": r.total_assets,
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
        warmup=max(warmup_min, int(params.get("warmup_days", 0) or 0)),
        attributes=attributes,
        membership=membership,
        risk_limits=risk_limits or None,
        fundamentals=fundamentals or None,
        financials_raw=financials_raw or None,
    )
    try:
        return engine.run(meta["cls"], params)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"组合回测执行失败: {e}")


# ——— 数据加载 ———
def _load_bars(db: Session, symbol: str, start: str, end: str, adj: str) -> list[dict]:
    """按复权口径加载个股日K。

    全A 建仓后 kline_daily 统一存**未复权原价**，复权在读取时用
    adj_factor_daily 折算 —— 三条路径都保持同一口径，避免回测取到未复权价。
    """
    sd = date.fromisoformat(start)
    ed = date.fromisoformat(end)
    # ① DuckDB 分析库（列式加速，完整版架构默认路径）
    cached = duckdb_store.get_stock_bars(symbol, adj, sd, ed)
    if cached:
        return cached

    # ② 回退 SQLite：读原价 + 因子，再按 adj 折算
    from app.models import AdjFactorDaily

    stmt = (
        select(KlineDaily)
        .where(
            KlineDaily.symbol == symbol,
            KlineDaily.adj == "none",
            KlineDaily.trade_date >= sd,
            KlineDaily.trade_date <= ed,
        )
        .order_by(KlineDaily.trade_date)
    )
    rows = db.execute(stmt).scalars().all()
    if rows:
        facs = {r.trade_date: r.adj_factor for r in db.execute(
            select(AdjFactorDaily).where(
                AdjFactorDaily.symbol == symbol,
                AdjFactorDaily.trade_date >= sd,
                AdjFactorDaily.trade_date <= ed,
            )).scalars().all()}
        bars = [
            {
                "symbol": symbol,
                "date": r.trade_date.isoformat(),
                "open": r.open, "high": r.high, "low": r.low,
                "close": r.close, "volume": r.volume, "amount": r.amount,
                "_f": facs.get(r.trade_date),
            }
            for r in rows
        ]
        return duckdb_store._adjust_bars(bars, adj)

    # ③ 在线兜底：东财/腾讯返回的是**前复权**，仅当 adj='qfq' 时口径一致。
    #    不写库 —— 库内要保持「只存未复权原价」这一单一事实源。
    try:
        return data_source.get_stock_daily_qfq(symbol, sd, ed) or []
    except Exception:  # noqa: BLE001
        return []


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


# 组合数上限。网格是笛卡尔积，维度一多就爆炸：
# 同步接口卡在请求线程里，跑久了会把整个后端堵住 → 上限收紧；
# 后台任务不占请求线程、可查进度可取消 → 放宽。
_MAX_OPTIMIZE_COMBOS = 400      # POST /optimize（同步）
_MAX_ASYNC_COMBOS = 5000        # POST /optimize/async（后台任务）


def _prepare_optimize(req: OptimizeRequest, max_combos: int):
    """校验寻优请求，返回 (meta, keys, axes, combos)。校验失败直接抛 HTTPException。"""
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
    unknown = [k for k in keys if k not in meta["default_params"]]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"未知参数 {unknown}；{req.strategy} 可用参数：{sorted(meta['default_params'])}",
        )
    axes = [list(req.param_ranges[k]) for k in keys]
    n_combo = 1
    for a in axes:
        n_combo *= len(a)
    if n_combo > max_combos:
        raise HTTPException(
            status_code=400,
            detail=f"参数组合数 {n_combo} 超过上限 {max_combos}，请缩小网格（减少维度或减少取值）",
        )
    return meta, keys, axes, list(itertools.product(*axes))


def _optimize_core(db, req: OptimizeRequest, max_combos: int,
                   progress=None, cancelled=None) -> list[OptimizeTrial]:
    """网格搜索的全部计算：样本内/外两段回测 + 邻域稳健性 + 排序。

    progress(done, total) 每跑完一组回调一次；cancelled() 为 True 时提前退出。
    同步接口与后台任务共用这一段，避免两套逻辑走偏。
    """
    meta, keys, axes, combos = _prepare_optimize(req, max_combos)

    # —— 样本内 / 样本外切分：按**交易日数量**从区间末尾切出验证段 ——
    oos_ratio = 0.3 if req.oos_ratio is None else float(req.oos_ratio)
    oos_ratio = min(max(oos_ratio, 0.0), 0.6)
    bars_all = _load_bars(db, req.symbol, req.start, req.end, req.adj)
    if not bars_all:
        raise HTTPException(
            status_code=404,
            detail=f"未获取到 {req.symbol} 的行情数据（{req.start}~{req.end}）",
        )
    split_date = None
    if oos_ratio > 0 and len(bars_all) >= 60:
        split_i = max(int(len(bars_all) * (1 - oos_ratio)), 30)  # 样本内至少留 30 根 bar
        if len(bars_all) - split_i >= 20:  # 验证段太短则不分
            split_date = bars_all[split_i]["date"]

    results: dict[tuple, OptimizeTrial] = {}
    total = len(combos)
    for i, combo in enumerate(combos):
        if cancelled is not None and cancelled():
            break  # 用户点了「取消」，剩下的不跑了
        params = dict(zip(keys, combo))
        fixed = dict(meta["default_params"])
        fixed.update(params)
        try:
            result = _run_single(
                db, req, meta, fixed,
                start=req.start, end=(split_date or req.end),
            )
        except Exception:  # noqa: BLE001
            if progress:
                progress(i + 1, total)
            continue  # 某组参数可能无合格数据，跳过

        trial = OptimizeTrial(
            params=params,
            total_return=result.total_return,
            annual_return=result.annual_return,
            max_drawdown=result.max_drawdown,
            sharpe=result.sharpe,
            win_rate=result.win_rate,
            trade_count=result.trade_count,
            final_equity=result.final_equity,
        )
        if split_date:
            trial.oos_start = split_date
            try:
                oos = _run_single(db, req, meta, fixed, start=split_date, end=req.end)
                trial.oos_total_return = oos.total_return
                trial.oos_annual_return = oos.annual_return
                trial.oos_max_drawdown = oos.max_drawdown
                trial.oos_sharpe = oos.sharpe
                trial.oos_win_rate = oos.win_rate
                trial.oos_trade_count = oos.trade_count
            except Exception:  # noqa: BLE001
                pass  # 验证段跑不通就留 None，前端显示 —
        results[combo] = trial
        if progress:
            progress(i + 1, total)

    if not results:
        raise HTTPException(status_code=400, detail="所有参数组合均未产生有效回测结果，请检查区间与数据来源")

    # —— 稳健性：与该组参数在网格中的「邻居」比较 ——
    # 最优参数若是一座孤峰（周围一圈都差），多半是运气而非有效；高原才敢上实盘。
    for combo, trial in results.items():
        vals = [results[n].sharpe for n in _grid_neighbors(combo, axes) if n in results]
        if not vals:
            continue
        trial.neighbor_count = len(vals)
        trial.neighbor_sharpe = sum(vals) / len(vals)
        # 归一化偏离度：自身夏普与邻域均值的差距，越小越好；0.5 为平滑常数
        trial.robustness = max(
            0.0, 1.0 - abs(trial.sharpe - trial.neighbor_sharpe) / (abs(trial.sharpe) + 0.5)
        )

    # 排序
    rank_by = getattr(req, "rank_by", "sharpe") or "sharpe"
    if rank_by == "total_return":
        out = sorted(results.values(), key=lambda x: x.total_return, reverse=True)
    elif rank_by == "max_drawdown":
        out = sorted(results.values(), key=lambda x: x.max_drawdown)  # 绝对值越小越好
    elif rank_by == "oos_sharpe":
        out = sorted(results.values(),  # 无 OOS 结果的排最后
                     key=lambda x: (x.oos_sharpe is not None, x.oos_sharpe or 0.0), reverse=True)
    elif rank_by == "robustness":
        out = sorted(results.values(),
                     key=lambda x: (x.robustness is not None, x.robustness or 0.0), reverse=True)
    else:
        out = sorted(results.values(), key=lambda x: x.sharpe, reverse=True)

    return out


@router.post("/optimize", response_model=list[OptimizeTrial])
def optimize_strategy(req: OptimizeRequest, db: Session = Depends(get_db)):
    """网格搜索参数寻优（同步）：遍历 param_ranges 全部组合，按 rank_by 排序返回。

    组合数上限 400；更大的网格请用 POST /optimize/async（后台任务 + 进度 + 可取消）。
    """
    return _optimize_core(db, req, _MAX_OPTIMIZE_COMBOS)


# ——— 大网格异步寻优 ———
# 关键：不放后台线程，走**子进程 + 硬超时**（与数据调度、一键修复同款）。
# 曾实测：同样逻辑在独立进程 0.4s 跑完，放进 uvicorn 进程的后台线程却卡死十几分钟，
# 且 cancel 标志只在每组开始时检查 —— 卡在第一组就永远退不出来，只能重启后端。
# 子进程可以被真正杀掉，也不会拖垮 API。
_OPT_JOB_ROOT = os.path.join(str(DATA_DIR), "opt_jobs")
_OPT_TIMEOUT = 3600  # 单任务硬超时（秒），超时直接 kill
_opt_jobs: dict[str, dict] = {}
_opt_lock = threading.Lock()
_OPT_JOBS_KEEP = 10  # 最多保留多少个任务目录，防止无限堆积


def _job_dir(job_id: str) -> str:
    return os.path.join(_OPT_JOB_ROOT, job_id)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _prune_jobs() -> None:
    """清理旧任务目录（只清已完成的），避免磁盘无限堆积。"""
    try:
        done = [jid for jid, j in _opt_jobs.items() if not j["running"]]
        for jid in done[:-_OPT_JOBS_KEEP]:
            _opt_jobs.pop(jid, None)
            shutil.rmtree(_job_dir(jid), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _watch_job(job_id: str, proc) -> None:
    """等子进程结束（带硬超时）；只负责收尾，不做任何计算。"""
    job = _opt_jobs.get(job_id)
    if not job:
        return
    try:
        proc.wait(timeout=_OPT_TIMEOUT)
    except subprocess.TimeoutExpired:  # noqa: F821
        proc.kill()
        job["error"] = f"超过 {_OPT_TIMEOUT}s 未完成，已强制终止"
    finally:
        job["running"] = False
        job["finished_at"] = time.time()
        res = _read_json(os.path.join(_job_dir(job_id), "result.json"))
        if res:
            job["ok"] = res.get("ok")
            job["error"] = res.get("error") or job["error"]
            job["results"] = res.get("results") or []
        else:
            # 连结果文件都没有 —— 多半是被取消或超时杀掉的
            job["ok"] = False
            job["error"] = job["error"] or ("任务已取消" if job["cancelled"] else "任务异常结束（无结果输出）")


@router.post("/optimize/async")
def optimize_async(req: OptimizeRequest):
    """提交一个大网格寻优任务（子进程执行），立即返回 job_id。

    用 GET /optimize/async/{job_id} 轮询进度，DELETE 可取消。
    同时只允许一个任务在跑（回测吃 CPU 和 DB 连接）。
    """
    _meta, _keys, _axes, combos = _prepare_optimize(req, _MAX_ASYNC_COMBOS)
    with _opt_lock:
        if any(j["running"] for j in _opt_jobs.values()):
            raise HTTPException(status_code=409, detail="已有寻优任务在运行，请等它跑完或先取消")
        _prune_jobs()
        job_id = uuid.uuid4().hex[:12]
        d = _job_dir(job_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "payload.json"), "w", encoding="utf-8") as f:
            json.dump(req.model_dump(), f, ensure_ascii=False)

        env = dict(os.environ)
        env["PYTHONPATH"] = _BACKEND_DIR
        proc = subprocess.Popen(
            [sys.executable, "scripts/optimize_job.py", d],
            env=env, cwd=_BACKEND_DIR,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        job = {
            "running": True, "cancelled": False, "ok": None, "error": None,
            "done": 0, "total": len(combos), "results": [],
            "started_at": time.time(), "finished_at": None, "pid": proc.pid,
        }
        _opt_jobs[job_id] = job
        threading.Thread(target=_watch_job, args=(job_id, proc), daemon=True).start()

    return {"job_id": job_id, "total": len(combos), "max_combos": _MAX_ASYNC_COMBOS, "pid": proc.pid}


@router.get("/optimize/async/{job_id}")
def optimize_async_status(job_id: str):
    """查询寻优任务进度。running=False 且 ok=True 时 results 为最终结果。"""
    job = _opt_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已被清理")
    if job["running"]:
        prog = _read_json(os.path.join(_job_dir(job_id), "progress.json")) or {}
        job["done"] = prog.get("done", job["done"])
        job["total"] = prog.get("total", job["total"])
    return {
        "job_id": job_id,
        "running": job["running"],
        "done": job["done"],
        "total": job["total"],
        "ok": job["ok"],
        "error": job["error"],
        "cancelled": job["cancelled"],
        "elapsed": round((job["finished_at"] or time.time()) - job["started_at"], 1),
        "results": [] if job["running"] else job["results"],
    }


@router.delete("/optimize/async/{job_id}")
def optimize_async_cancel(job_id: str):
    """取消正在跑的寻优任务（直接杀子进程，已算的部分不保留）。"""
    job = _opt_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已被清理")
    job["cancelled"] = True
    try:
        os.kill(job["pid"], signal.SIGTERM)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}


def _grid_neighbors(combo: tuple, axes: list[list]) -> list[tuple]:
    """网格中某组合的相邻点：每个参数维度上取相邻取值。

    网格是离散的，「相邻」即等同于把其中一个参数稍微改一档，
    用来判断最优点是孤峰还是高原。
    """
    out = []
    for d, v in enumerate(combo):
        try:
            i = list(axes[d]).index(v)
        except ValueError:
            continue
        for j in (i - 1, i + 1):
            if 0 <= j < len(axes[d]):
                cand = list(combo)
                cand[d] = axes[d][j]
                out.append(tuple(cand))
    return out


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
