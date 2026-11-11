"""模拟盘(Paper Trading) API：任务 CRUD、手动运行、详情、启用切换、重置。"""
from __future__ import annotations

import json
import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

import logging

logger = logging.getLogger("quant.paper")

from app.database import SessionLocal, get_db
from app.models import PaperTask, PaperTrade, PaperSnapshot
from app.core.strategies.registry import get_strategy, STRATEGY_REGISTRY
from app.core.engine.paper_engine import get_paper_task_detail
from app.core.cost_defaults import COMMISSION, SLIPPAGE
from app.core.eval_profile import build_eval_profile_for_paper

router = APIRouter(prefix="/paper", tags=["paper"])


class PaperTaskCreate(BaseModel):
    name: str
    strategy_key: str
    # 类型由策略 meta 决定（组合策略强制 portfolio），此处仅作前端初值
    kind: str = "single"
    symbols: str = ""
    index_code: str = ""
    params_json: str = "{}"
    initial_cash: float = 1_000_000.0
    commission: float = COMMISSION
    slippage: float = SLIPPAGE
    adj: str = "qfq"
    start_date: str | None = None
    enabled: bool = False


@router.get("/strategies")
def list_strategies():
    """可用策略清单（供前端下拉；自动按策略类型标记 single/portfolio）。"""
    out = []
    for key, meta in STRATEGY_REGISTRY.items():
        if meta.get("disabled"):
            continue
        out.append({
            "key": key,
            "name": meta["name"],
            "kind": "portfolio" if meta.get("multi_asset") else "single",
            "param_schema": meta.get("param_schema", []),
            "default_params": meta.get("default_params", {}),
            "index_symbol": meta.get("index_symbol", ""),
        })
    return out


def _index_symbol(code: str) -> str:
    """指数代码 → 行情表里的 symbol。

    399 开头是深证（sz399317 国证A指是平台统一基准），其余按上证。
    引擎里原来是 `"sh" + index_code` 一刀切 —— 对 399317 会拼成 sh399317，
    取不到任何行（2026-11-11 修）。两处必须同口径，改一处记得看另一处。
    """
    c = (code or "").strip().lower()
    if c.startswith(("sh", "sz", "bj")):
        return c
    return ("sz" if c.startswith("399") else "sh") + c


def _last_index_date(db, index_code: str):
    """该指数最近一个有行情的交易日（引擎要拿它算基准，空日会报错）。"""
    from sqlalchemy import func, select as _select

    from app.models import IndexKlineDaily

    return db.execute(
        _select(func.max(IndexKlineDaily.trade_date))
        .where(IndexKlineDaily.symbol == _index_symbol(index_code))
    ).scalar()


@router.post("/tasks")
def create_task(req: PaperTaskCreate, db=Depends(get_db)):
    try:
        meta = get_strategy(req.strategy_key)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"未知策略: {req.strategy_key}")
    # kind 由策略类型强制决定，避免误选
    kind = "portfolio" if meta.get("multi_asset") else "single"
    if kind == "single" and not req.symbols.strip():
        raise HTTPException(status_code=400, detail="单标的任务需要填写标的(symbols)")
    if kind == "portfolio" and not req.index_code.strip() and not meta.get("index_symbol"):
        raise HTTPException(status_code=400, detail="组合任务需要填写指数代码(index_code)")

    exists = db.execute(select(PaperTask).where(PaperTask.name == req.name)).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=400, detail="任务名称已存在")

    index_code = req.index_code or meta.get("index_code") or "000906"

    # 建仓日不能空着。空着时引擎会拿「今天」当起点，而今天（16:00 前）还没有
    # 日线 —— 基准取空 → 任务直接报「未获取到基准指数 399317 的行情数据」，
    # 老刘点「建盘」就是这个结果（2026-11-11 实测两个任务都死在这）。
    # 退到最近一个有指数行情的交易日，才是能算的起点。
    start_date = req.start_date or _last_index_date(db, index_code)
    if not start_date:
        raise HTTPException(status_code=400,
                            detail=f"{index_code} 还没有任何指数行情，先跑一次 ETL")

    t = PaperTask(
        name=req.name, strategy_key=req.strategy_key, kind=kind,
        symbols=req.symbols, index_code=index_code, params_json=req.params_json,
        initial_cash=req.initial_cash, commission=req.commission, slippage=req.slippage,
        adj=req.adj, start_date=start_date, enabled=req.enabled,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    prof = build_eval_profile_for_paper(req, multi_asset=(kind == "portfolio"))
    return {
        "ok": True,
        "id": t.id,
        "commission": t.commission,
        "slippage": t.slippage,
        **prof,
    }


@router.post("/auto-ensure")
def auto_ensure_paper(dry_run: bool = False, mode: str = "create",
                      db=Depends(get_db)):
    """为「启用且回测超额为正」的策略补建模拟盘任务。

    mode="create"（默认）：真的建盘 —— 只给页面上「确认建盘」这个动作。
    mode="notify"：只返回 proposals，不落库 —— watchdog 自动路径用这个。
    dry_run=true：预览，不落库（页面展示用）。
    幂等：已有任务的策略跳过，重复调用不会建重复任务。
    """
    from app.services.paper_auto import ensure_paper_for_enabled

    if mode not in ("create", "notify"):
        raise HTTPException(400, "mode 只能是 create 或 notify")
    return ensure_paper_for_enabled(db, dry_run=dry_run, mode=mode)


@router.get("/tasks")
def list_tasks(db=Depends(get_db)):
    rows = db.execute(select(PaperTask).order_by(PaperTask.created_at.desc())).scalars().all()
    out = []
    for t in rows:
        detail = get_paper_task_detail(db, t)
        out.append({
            "id": t.id, "name": t.name, "strategy_key": t.strategy_key,
            "kind": t.kind, "symbols": t.symbols, "index_code": t.index_code,
            "enabled": t.enabled, "initial_cash": t.initial_cash,
            "equity": detail["equity"], "pnl": detail["pnl"], "pnl_pct": detail["pnl_pct"],
            "positions_count": len(detail["positions"]),
            # ⚠️ 必须带 last_bar_date：智能体/前端靠它判断「净值跑到哪一天」。
            # 曾漏返回 → 外部读成 null，误判为「模拟盘从未跑过」（2026-11-10）。
            "last_bar_date": t.last_bar_date,
            "account_start": t.account_start, "start_date": t.start_date,
            "last_run_at": detail["last_run_at"], "error_msg": t.error_msg,
        })
    return out


@router.get("/tasks/{task_id}")
def task_detail(task_id: int, db=Depends(get_db)):
    t = db.get(PaperTask, task_id)
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    return get_paper_task_detail(db, t)


@router.get("/tasks/{task_id}/snapshots")
def task_snapshots(task_id: int, limit: int = 500):
    """净值快照序列（画曲线用）。按日期升序，方便直接喂图表。"""
    from app.models import PaperSnapshot

    db = SessionLocal()
    try:
        rows = (db.query(PaperSnapshot).filter(PaperSnapshot.task_id == task_id)
                .order_by(PaperSnapshot.date.desc())
                .limit(max(1, min(limit, 2000))).all())
        out = [{"date": str(r.date)[:10], "equity": r.equity, "pnl_pct": r.pnl_pct}
               for r in reversed(rows)]
        return {"task_id": task_id, "total": len(out), "points": out}
    finally:
        db.close()


@router.get("/tasks/{task_id}/deviation")
def task_deviation(task_id: int, db=Depends(get_db)):
    """跟踪偏离：实际净值 vs 理论净值。

    两条线从哪来，为什么必须分开：
    · 理论 = equity_curve_json —— 每次运行都用回测引擎从 start_date **全量重算**，
      相当于「如果完全按规则走，今天该值多少钱」。
    · 实际 = paper_snapshots —— 每天增量跑完**当场记下**的那一格，是不再重算的历史事实。

    两者本该重合。不重合的地方就是执行损耗：复权因子后补导致历史被重算、
    停牌/涨跌停买不进、整手取整的现金残留、资金精度累积。
    这个差值本身不说明策略好坏 —— 它是「模拟盘这面镜子平不平」的度量。

    ⚠️ 只对比**有快照的日子**。快照从建盘那天开始记，之前的历史只有理论值，
    拿没有实际值的日期去比会得到一条假的 0 偏离。
    """
    from app.models import PaperSnapshot

    t = db.get(PaperTask, task_id)
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")

    theory: dict[str, float] = {}
    try:
        for pt in json.loads(t.equity_curve_json or "[]"):
            d = str(pt.get("date", ""))[:10]
            if d and pt.get("equity") is not None:
                theory[d] = float(pt["equity"])
    except (ValueError, TypeError):
        theory = {}

    rows = (db.query(PaperSnapshot)
            .filter(PaperSnapshot.task_id == task_id)
            .order_by(PaperSnapshot.date).all())
    points, devs = [], []
    for r in rows:
        d = str(r.date)[:10]
        th = theory.get(d)
        act = float(r.equity) if r.equity is not None else None
        if act is None:
            continue
        dev = (act / th - 1.0) * 100 if th else None
        if dev is not None:
            devs.append(dev)
        points.append({"date": d, "theory": round(th, 2) if th else None,
                       "actual": round(act, 2), "dev_pct": round(dev, 3) if dev is not None else None})

    total_days = len(theory)
    return {
        "task_id": task_id, "name": t.name, "strategy_key": t.strategy_key,
        "account_start": t.account_start, "last_bar_date": t.last_bar_date,
        "theory_days": total_days, "actual_days": len(points),
        # 快照没覆盖全区间时，这组数字只代表有快照的那几天 —— 前端必须显示这句
        "partial": len(points) < total_days,
        "latest_dev_pct": round(devs[-1], 3) if devs else None,
        "max_dev_pct": round(max(devs), 3) if devs else None,
        "min_dev_pct": round(min(devs), 3) if devs else None,
        "points": points,
    }


@router.post("/tasks/{task_id}/run")
def run_task(task_id: int):
    """手动触发一次运行。

    组合走**子进程**（与调度器同一入口）：xq 系的池子要加载全市场面板，
    在主进程跑会 OOM 拖死整个容器 —— 手动点一下就把平台点挂，是最糟的体验。
    """
    db = SessionLocal()
    try:
        t = db.get(PaperTask, task_id)
        if not t:
            raise HTTPException(status_code=404, detail="任务不存在")
        kind = t.kind or "single"
    finally:
        db.close()

    def _bg():
        from app.services.paper_runner import run_in_subprocess
        ok, info = run_in_subprocess(task_id, kind)
        logger.info("手动运行模拟盘 #%s: ok=%s %s", task_id, ok,
                    info.get("error") or f"{info.get('secs')}s")

    threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "message": "已触发后台运行（子进程）", "kind": kind}


@router.post("/tasks/{task_id}/toggle")
def toggle_task(task_id: int, db=Depends(get_db)):
    t = db.get(PaperTask, task_id)
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    t.enabled = not t.enabled
    db.commit()
    return {"ok": True, "enabled": t.enabled}


@router.post("/tasks/{task_id}/reset")
def reset_task(task_id: int, db=Depends(get_db)):
    t = db.get(PaperTask, task_id)
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    db.execute(PaperTrade.__table__.delete().where(PaperTrade.task_id == task_id))
    db.execute(PaperSnapshot.__table__.delete().where(PaperSnapshot.task_id == task_id))
    t.last_bar_date = None
    t.account_start = None
    t.last_run_at = None
    t.state_json = "{}"
    t.error_msg = None
    db.commit()
    return {"ok": True}


@router.delete("/tasks/{task_id}")
def delete_task(task_id: int, db=Depends(get_db)):
    t = db.get(PaperTask, task_id)
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    db.execute(PaperTrade.__table__.delete().where(PaperTrade.task_id == task_id))
    db.execute(PaperSnapshot.__table__.delete().where(PaperSnapshot.task_id == task_id))
    db.delete(t)
    db.commit()
    return {"ok": True}
