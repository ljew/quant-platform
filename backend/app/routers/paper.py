"""模拟盘(Paper Trading) API：任务 CRUD、手动运行、详情、启用切换、重置。"""
from __future__ import annotations

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
    t = PaperTask(
        name=req.name, strategy_key=req.strategy_key, kind=kind,
        symbols=req.symbols, index_code=index_code, params_json=req.params_json,
        initial_cash=req.initial_cash, commission=req.commission, slippage=req.slippage,
        adj=req.adj, start_date=req.start_date, enabled=req.enabled,
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
