"""Agent 编排 API：启动/查询流水线、人工闸门、SSE 事件流。

- POST /agent/run                 启动研究流水线（后台线程）
- GET  /agent/state               编排器当前状态（驾驶舱轮询）
- GET  /agent/runs                运行历史
- POST /agent/runs/{rid}/gate     闸门处置：approve / reject
- GET  /agent/stream?run_id=      SSE 实时事件流（research_runs 终态时自动关闭）
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.models import AgentEvent, ResearchRun
from app.services import orchestrator

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/run")
def run(payload: dict | None = None, db: Session = Depends(get_db)):
    """启动一次研究流水线（trigger: manual / scheduler）。"""
    trigger = (payload or {}).get("trigger", "manual")
    try:
        run_id = orchestrator.start_run(db, trigger=trigger)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "run_id": run_id}


@router.get("/state")
def state():
    """编排器状态 + 最近一次 run 概要（驾驶舱 2s 轮询）。"""
    db = SessionLocal()
    try:
        last = db.scalars(
            select(ResearchRun).order_by(ResearchRun.id.desc()).limit(1)
        ).first()
        recent = db.scalars(
            select(ResearchRun).order_by(ResearchRun.id.desc()).limit(10)
        ).all()
    finally:
        db.close()
    return {
        "orchestrator": {k: v for k, v in orchestrator.STATE.items()},
        "scheduler": orchestrator.scheduler_state(),
        "gate_countdown_sec": orchestrator.GATE_COUNTDOWN_SEC,
        "latest_run": _run_dict(last) if last else None,
        "recent_runs": [_run_dict(r) for r in recent],
    }


def _run_dict(r: ResearchRun) -> dict:
    return {
        "id": r.id, "trigger": r.trigger, "status": r.status,
        "data_date": r.data_date.isoformat() if r.data_date else None,
        "gate_status": r.gate_status, "gate_note": r.gate_note,
        "gate_deadline": r.gate_deadline.isoformat() if r.gate_deadline else None,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "error": r.error,
    }


@router.get("/runs/{rid}")
def run_detail(rid: int, db: Session = Depends(get_db)):
    run = db.get(ResearchRun, rid)
    if not run:
        raise HTTPException(status_code=404, detail="run 不存在")
    events = db.scalars(
        select(AgentEvent).where(AgentEvent.run_id == rid).order_by(AgentEvent.id)
    ).all()
    return {"run": _run_dict(run),
            "events": [_event_dict(e) for e in events]}


class GatePayload(BaseModel):
    action: str = Field(..., description="approve / reject")
    note: str = ""


@router.post("/runs/{rid}/gate")
def gate(rid: int, payload: GatePayload, db: Session = Depends(get_db)):
    """人工闸门处置（倒计时内人工优先于自动放行）。"""
    if payload.action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action 须为 approve/reject")
    run = db.get(ResearchRun, rid)
    if not run:
        raise HTTPException(status_code=404, detail="run 不存在")
    if run.gate_status != "pending":
        raise HTTPException(status_code=409,
                            detail=f"闸门未挂起（当前 {run.gate_status}）")
    run.gate_status = "approved" if payload.action == "approve" else "rejected"
    run.gate_note = payload.note or ("人工放行" if payload.action == "approve" else "人工否决")
    db.commit()
    return {"ok": True, "gate_status": run.gate_status}


@router.get("/stream")
def stream(run_id: int = 0, db: Session = Depends(get_db)):
    """SSE 事件流：增量推 AgentEvent + run 状态，终态自动关闭。"""
    if not run_id:
        last = db.scalars(
            select(AgentEvent.run_id).order_by(AgentEvent.id.desc()).limit(1)
        ).first()
        run_id = last or 0

    def gen():
        last_id = 0
        idle = 0
        while True:
            db = SessionLocal()
            try:
                rows = db.scalars(
                    select(AgentEvent).where(
                        AgentEvent.run_id == run_id, AgentEvent.id > last_id
                    ).order_by(AgentEvent.id).limit(50)
                ).all()
                run = db.get(ResearchRun, run_id)
                for e in rows:
                    last_id = e.id
                    yield f"data: {json.dumps(_event_dict(e), ensure_ascii=False)}\n\n"
                run_over = run is not None and run.status != "RUNNING"
                snap = json.dumps({"run": _run_dict(run)} if run else {},
                                  ensure_ascii=False)
                yield (f"event: state\ndata: {snap}\n\n")
                if run_over:
                    yield "event: done\ndata: {}\n\n"
                    return
                idle = idle + 1 if not rows else 0
                if idle > 3600:  # 约 2 小时无活动自动断开
                    return
            finally:
                db.close()
            time.sleep(2)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _event_dict(e: AgentEvent) -> dict:
    return {
        "id": e.id, "run_id": e.run_id, "agent": e.agent, "step": e.step,
        "level": e.level, "status": e.status, "message": e.message,
        "rows": e.rows, "duration_ms": e.duration_ms,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


# ────────────────────────── 发布通道配置（SMTP 授权码轮换走这里）──────────────────────────

class NotifyConfigPayload(BaseModel):
    enabled: bool = True
    smtp_host: str = "smtp.163.com"
    smtp_port: int = 465
    user: str = ""
    password: str = ""   # 留空 = 不修改
    from_addr: str = ""
    to: list[str] = Field(default_factory=list)


@router.get("/notify-config")
def notify_config():
    """发布通道配置（password 脱敏）。"""
    from app.services import mailer

    return mailer.get_config_masked()


@router.put("/notify-config")
def update_notify_config(payload: NotifyConfigPayload):
    """更新发布通道配置（授权码留空 = 不修改）。"""
    from app.services import mailer

    return {"ok": True, "config": mailer.update_config(payload.model_dump())}


@router.post("/notify-test")
def notify_test(db: Session = Depends(get_db)):
    """发送测试邮件（验证授权码/通道连通）。"""
    from app.services import mailer

    return mailer.send_test(db)
