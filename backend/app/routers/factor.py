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
from app.services.factor_mining import mine_factor, validate_expr, compute_complexity, news_event_test
from app.services.factor_gp import gp_search, DIRECTION_TEMPLATES

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
        "news_senti": "个股新闻情绪(-1~1,近3日)", "industry": "行业",
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


@router.post("/mine/{rid}/save")
def mine_save(rid: int, db: Session = Depends(get_db)):
    """（占位保留）已自动落库。"""
    return {"ok": True}


class GpMinePayload(BaseModel):
    directions: list[str] = Field(default_factory=list)
    name_prefix: str = "GP"
    pop_size: int = Field(14, ge=6, le=40)
    generations: int = Field(6, ge=2, le=20)
    start: str = ""
    end: str = ""
    forward: int = Field(20, ge=1, le=60)
    step: int = Field(30, ge=10, le=60)
    pool_size: int | None = Field(220, description="快评抽样池；null=全核心池")
    top_k: int = Field(3, ge=1, le=5)
    orthogonal: bool = Field(False, description="正交增量模式：残差IC，专挖重复发现之外的alpha")
    crisis_only: bool = Field(False, description="危机Alpha：仅基准下跌窗口评IC")


class NewsTestPayload(BaseModel):
    extreme_pct: float = Field(0.10, ge=0.02, le=0.3)
    horizon: int = Field(5, ge=1, le=60)


@router.get("/news/daily")
def news_daily(db: Session = Depends(get_db), limit: int = 500):
    """市场新闻情绪时序（倒序）。"""
    from app.models import NewsMarketDaily

    rows = db.execute(
        select(NewsMarketDaily).order_by(NewsMarketDaily.date.desc()).limit(min(limit, 1500))
    ).scalars().all()
    return [
        {"date": r.date.isoformat(), "n_articles": r.n_articles, "n_finance": r.n_finance,
         "bull": r.bull_score, "bear": r.bear_score, "net_sentiment": r.net_sentiment}
        for r in rows
    ]


@router.post("/news/event-test")
def news_event(payload: NewsTestPayload, db: Session = Depends(get_db)):
    """极端新闻情绪日 → 未来 N 日指数收益检验（择时有效性）。"""
    return news_event_test(db, extreme_pct=payload.extreme_pct, horizon=payload.horizon)


@router.get("/gp/directions")
def gp_directions():
    return [
        {"key": k, "note": v.get("note", "")} for k, v in DIRECTION_TEMPLATES.items()
    ]


@router.post("/gp/mine")
def gp_mine(payload: GpMinePayload, db: Session = Depends(get_db)):
    """遗传规划批量挖掘：进化搜索 → 精英全池精评 → 落库。同步执行约 1~3 分钟。"""
    result = gp_search(
        db, directions=payload.directions or None,
        pop_size=payload.pop_size, generations=payload.generations,
        start=payload.start, end=payload.end, forward=payload.forward,
        step=payload.step, pool_size=payload.pool_size, top_k=payload.top_k,
        orthogonal=payload.orthogonal, crisis_only=payload.crisis_only,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "GP 挖掘失败"))
    saved = []
    for i, el in enumerate(result.get("elites", [])):
        row = FactorMineResult(
            name=f"{payload.name_prefix}-{i + 1}·{el.get('rating', '')}",
            expr=el["expr"], rating=el["rating"],
            ic_mean=el["ic_mean"], icir=el["icir"],
            result_json=json.dumps(el, ensure_ascii=False, default=str),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        el["id"] = row.id
        saved.append(row.id)
    result["saved_ids"] = saved
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


# ============ 因子注册表（生命周期管理） ============
class RegistryPayload(BaseModel):
    name: str = Field(..., min_length=1)
    expr: str
    direction: int = 1
    category: str = "mined"
    source_id: int | None = None
    ic_mean: float | None = None
    notes: str = ""


@router.get("/registry")
def registry_list(db: Session = Depends(get_db), status: str = ""):
    from app.models import FactorRegistry

    q = select(FactorRegistry).order_by(FactorRegistry.id.desc())
    if status:
        q = q.where(FactorRegistry.status == status)
    rows = db.execute(q).scalars().all()
    return [
        {"id": r.id, "name": r.name, "expr": r.expr, "direction": r.direction,
         "category": r.category, "status": r.status, "ic_mean": r.ic_mean,
         "source_id": r.source_id, "created_at": r.created_at.isoformat(timespec="seconds")
         if r.created_at else None}
        for r in rows
    ]


@router.post("/registry/register")
def registry_register(payload: RegistryPayload, db: Session = Depends(get_db)):
    """登记因子（candidate）；同名重复登记返回已有 id。"""
    from app.models import FactorRegistry

    exist = db.execute(select(FactorRegistry).where(
        FactorRegistry.name == payload.name)).scalar()
    if exist:
        return {"ok": True, "id": exist.id, "status": exist.status, "existed": True}
    row = FactorRegistry(name=payload.name, expr=payload.expr,
                         direction=payload.direction, category=payload.category,
                         source_id=payload.source_id, ic_mean=payload.ic_mean,
                         notes=payload.notes)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"ok": True, "id": row.id, "status": row.status}


@router.post("/registry/{rid}/toggle")
def registry_toggle(rid: int, db: Session = Depends(get_db)):
    """candidate<->enabled 切换；enabled 才进入每日计算。"""
    from app.models import FactorRegistry

    row = db.get(FactorRegistry, rid)
    if not row:
        raise HTTPException(status_code=404, detail="不存在")
    if row.status == "candidate":
        row.status = "enabled"
    elif row.status == "enabled":
        row.status = "disabled"
    else:
        row.status = "candidate"
    db.commit()
    return {"ok": True, "id": rid, "status": row.status}
