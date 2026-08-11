"""市场中性·可交易对冲分析 API。

输入一个「组合类型」模拟盘任务 id，读取其权益曲线与基准指数，
用股指期货连续合约构建可交易对冲，返回 多头/理论对冲/可交易对冲 三条曲线、
绩效指标与成本分解。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import PaperTask
from app.core.engine.hedge_engine import build_hedge_analysis

router = APIRouter(prefix="/hedge", tags=["hedge"])


class HedgeAnalyzeReq(BaseModel):
    task_id: int
    commission: float = 5.0
    margin_rate: float = 0.12


@router.post("/analyze")
def analyze(req: HedgeAnalyzeReq, db: Session = Depends(get_db)):
    task = db.get(PaperTask, req.task_id)
    if task is None:
        raise HTTPException(404, "模拟盘任务不存在")
    if task.kind != "portfolio":
        raise HTTPException(400, "可交易对冲分析仅支持组合类型任务")
    ec = json.loads(task.equity_curve_json or "[]")
    if not ec:
        raise HTTPException(400, "该任务尚无权益曲线，请先运行一次")
    index_code = task.index_code or "000906"
    try:
        res = build_hedge_analysis(
            ec, index_code,
            commission=req.commission, margin_rate=req.margin_rate,
        )
    except Exception as e:
        raise HTTPException(500, f"对冲分析失败: {e}")
    res["task_id"] = task.id
    res["task_name"] = task.name
    return res
