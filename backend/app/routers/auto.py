"""自动研究闭环 API（/auto/*）：状态 / 运行记录 / 手动触发 / 配置。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import auto_research

router = APIRouter(prefix="/auto", tags=["auto-research"])


@router.get("/status")
def status(db: Session = Depends(get_db)):
    """总览：配置 + 调度状态 + 最近一次挖矿/复盘摘要（今日观点页动态条也用它）。"""
    runs = auto_research.latest_runs(db, limit=50)
    last_mining = next((r for r in runs if r["job"] == "mining"), None)
    last_review = next((r for r in runs if r["job"] == "review"), None)
    st = auto_research.scheduler_state()
    return {
        **st,
        "last_mining": last_mining,
        "last_review": last_review,
        "total_runs": len(runs),
    }


@router.get("/runs")
def runs(db: Session = Depends(get_db), limit: int = 20):
    return auto_research.latest_runs(db, limit=min(limit, 100))


@router.get("/runs/{rid}")
def run_detail(rid: int, db: Session = Depends(get_db)):
    from app.models import AutoResearchRun

    r = db.get(AutoResearchRun, rid)
    if not r:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    report_html = None
    if r.report_path:
        from pathlib import Path
        p = Path(r.report_path)
        if p.exists():
            report_html = p.read_text(encoding="utf-8")
    import json
    return {
        "id": r.id, "job": r.job, "trigger": r.trigger, "status": r.status,
        "summary": json.loads(r.summary_json or "{}"), "error": r.error,
        "started_at": r.started_at.isoformat(timespec="seconds") if r.started_at else None,
        "finished_at": r.finished_at.isoformat(timespec="seconds") if r.finished_at else None,
        "report_html": report_html,
    }


@router.post("/run/{job}")
def run_job(job: str):
    """手动触发：job = mining / review（后台线程异步执行）。"""
    if job == "mining":
        rid = auto_research.run_mining(trigger="manual")
    elif job == "review":
        rid = auto_research.run_review(trigger="manual")
    else:
        raise HTTPException(status_code=400, detail="job 仅支持 mining / review")
    return {"ok": True, "run_id": rid}


class AutoCfgPatch(BaseModel):
    mining_enabled: bool | None = None
    review_enabled: bool | None = None
    auto_enable_excellent: bool | None = None
    email_review: bool | None = None
    mining_time: str | None = None
    review_weekday: int | None = None
    review_time: str | None = None
    mining_pop: int | None = None
    mining_gens: int | None = None
    mining_top_k: int | None = None


@router.put("/config")
def update_config(payload: AutoCfgPatch):
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "mining_time" in patch or "review_time" in patch:
        import re
        for k in ("mining_time", "review_time"):
            if k in patch and not re.fullmatch(r"\d{1,2}:\d{2}", patch[k]):
                raise HTTPException(status_code=400, detail=f"{k} 格式应为 HH:MM")
    return {"ok": True, "config": auto_research.save_config(patch)}
