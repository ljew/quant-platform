"""因子挖掘 API（自定义表达式 → 有效性检验报告）。

- GET  /factor/functions          表达式函数库与变量参考（前端提示）
- POST /factor/validate           校验表达式（受限命名空间试算）
- POST /factor/mine               因子挖掘（同步，返回检验报告；落库可查）
- GET  /factor/mine/results       历史挖掘结果列表
- GET  /factor/mine/results/{id}  挖掘结果详情
- POST /factor/mine/results/{id}/delete  删除
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FactorMineResult
from app.services.factor_mining import mine_factor, validate_expr

router = APIRouter(prefix="/factor", tags=["factor"])

# 表达式可用函数/变量参考（前端面板展示，与 factor_expr.FUNCS 对齐）
FUNCTION_REF = {
    "序列函数": {
        "returns(s)": "收益率序列", "roc(s, n)": "N期收益率", "std(s)": "标准差",
        "mean(s)": "均值", "sum(s)": "求和", "min(s)": "最小值", "max(s)": "最大值",
        "skew(s)": "偏度", "maxdd(s)": "最大回撤", "zscore(s)": "标准化",
        "rank(s)": "截面排序(0-1)", "winsor(s, p=0.05)": "缩尾",
    },
    "回归/其他": {
        "beta(stock, mkt)": "Beta", "idio_vol(stock, mkt)": "特异波动率",
        "safe_inv(x, lo, hi)": "安全倒数(限幅)", "ifnull(x, y)": "空值替换",
        "log/exp/sqrt/abs/pow/sign": "基础数学",
    },
    "可用变量": {
        "c_m/c_r/c_v/c_b/c_t": "收盘序列(动量/反转/波动/回归/尾部窗口)",
        "mkt_b": "基准(中证800)对齐序列", "pe_ttm": "市盈率", "pb": "市净率",
        "market_cap": "市值", "roe": "净资产收益率", "revenue_yoy": "营收增速",
        "profit_yoy": "利润增速", "earnings_surprise": "盈余惊喜(PEAD)",
        "industry": "行业",
    },
}


class MinePayload(BaseModel):
    expr: str = Field(..., min_length=1, description="因子表达式")
    name: str = "自定义因子"
    start: str = ""
    end: str = ""
    groups: int = Field(5, ge=2, le=10)
    forward: int = Field(20, ge=1, le=60)


@router.get("/functions")
def functions():
    return FUNCTION_REF


@router.post("/validate")
def validate(payload: MinePayload):
    ok, err, sample = validate_expr(payload.expr)
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True, "sample_value": round(sample, 6) if sample is not None else None}


@router.post("/mine")
def mine(payload: MinePayload, db: Session = Depends(get_db)):
    """同步因子挖掘（核心池截面检验）。返回报告并落库。"""
    result = mine_factor(
        db, payload.expr, name=payload.name,
        start=payload.start, end=payload.end,
        groups=payload.groups, forward=payload.forward,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "挖掘失败"))
    row = FactorMineResult(
        name=result["name"], expr=result["expr"], rating=result["rating"],
        ic_mean=result["ic_mean"], icir=result["icir"],
        result_json=json.dumps(result, ensure_ascii=False, default=str),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    result["id"] = row.id
    return result


@router.get("/mine/results")
def mine_results(db: Session = Depends(get_db), limit: int = 20):
    rows = db.execute(
        select(FactorMineResult).order_by(FactorMineResult.id.desc()).limit(min(limit, 100))
    ).scalars().all()
    return [
        {
            "id": r.id, "name": r.name, "expr": r.expr, "rating": r.rating,
            "ic_mean": r.ic_mean, "icir": r.icir,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.get("/mine/results/{rid}")
def mine_result_detail(rid: int, db: Session = Depends(get_db)):
    r = db.get(FactorMineResult, rid)
    if not r:
        raise HTTPException(status_code=404, detail="结果不存在")
    try:
        payload = json.loads(r.result_json)
    except Exception:  # noqa: BLE001
        payload = {}
    payload["id"] = r.id
    return payload


@router.post("/mine/results/{rid}/delete")
def mine_result_delete(rid: int, db: Session = Depends(get_db)):
    r = db.get(FactorMineResult, rid)
    if not r:
        raise HTTPException(status_code=404, detail="结果不存在")
    db.delete(r)
    db.commit()
    return {"ok": True}
