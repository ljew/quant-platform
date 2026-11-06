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
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.datahub.ns_vars import VAR_DOC
from app.database import get_db
from app.models import FactorMineResult
from app.services.factor_mining import (
    mine_factor, validate_expr, compute_complexity, resolve_range,
    snapshot_dates,
)
from app.services.factor_gp import gp_search, DIRECTION_TEMPLATES

router = APIRouter(prefix="/factor", tags=["factor"])

# 表达式可用函数/变量参考（前端面板展示）
# 函数说明为人工中文分组；变量表由 ns_vars.var_names() 动态生成，
# 并自动收纳尚未登记说明的函数 —— 新增能力后参考表不会悄悄过期。
_FUNC_GROUPS = {
    "序列函数": {
        "returns(s)": "收益率序列", "roc(s, n)": "N期收益率", "std(s)": "标准差",
        "mean(s)": "均值", "sum(s)": "求和", "min(s)": "最小值", "max(s)": "最大值",
        "median(s)": "中位数", "skew(s)": "偏度", "maxdd(s)": "最大回撤",
        "zscore(s)": "标准化", "rank(s)": "截面排序(0-1)", "winsor(s, p=0.05)": "缩尾",
        "diff(s)": "一阶差分序列", "last(s)": "最后一个值", "count(s)": "序列长度",
        "slope(s)": "相对趋势斜率(已按均值归一化)",
    },
    "量价 / 回归": {
        "corr(x, y)": "两序列 Pearson 相关(量价相关、量价背离)",
        "beta(stock, mkt)": "Beta", "idio_vol(stock, mkt)": "特异波动率",
    },
    "标量工具": {
        "safe_inv(x, lo, hi)": "安全倒数(限幅)", "div0(x, y)": "安全除法(分母为0返回0)",
        "ifnull(x, y)": "空值替换",
        "log/exp/sqrt/abs/pow/sign": "基础数学",
    },
}

def _build_function_ref() -> dict:
    import re

    from app.core.engine.factor_expr import FUNCS
    from app.datahub.ns_vars import var_names

    ref = {k: dict(v) for k, v in _FUNC_GROUPS.items()}
    documented: set[str] = set()
    for grp in ref.values():
        for key in grp:
            for part in key.split("/"):
                m = re.match(r"[a-z_0-9]+", part.strip())
                if m:
                    documented.add(m.group(0))
    extra = {fn: "（未登记说明）" for fn in sorted(FUNCS) if fn not in documented}
    if extra:
        ref["其他"] = extra
    ref["可用变量"] = {v: VAR_DOC.get(v, "") for v in sorted(var_names())}
    return ref


FUNCTION_REF = _build_function_ref()


class MinePayload(BaseModel):
    expr: str = Field(..., min_length=1, description="因子表达式")
    name: str = "自定义因子"
    start: str = ""
    end: str = ""
    groups: int = Field(5, ge=2, le=10)
    forward: int = Field(20, ge=1, le=60)
    force: bool = Field(False, description="忽略重复表达式提示，强制重算并另存一条记录")


@router.get("/functions")
def functions():
    return FUNCTION_REF


@router.post("/validate")
def validate(payload: MinePayload):
    ok, err, sample = validate_expr(payload.expr)
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True, "sample_value": round(sample, 6) if sample is not None else None}


def _find_existing(db: Session, expr: str, payload: MinePayload, sig: str):
    """查找同「表达式 + 区间 + groups + forward」的历史记录。

    返回 (记录, 已解析报告)；记录存在但报告不可解析时报告为 None。

    两级匹配 ——
    ① 精确：表达式 + 参数签名完全一致；
    ② 兼容：params_json 是后加的列，老记录为空。但那些记录的 ic_series 日期
       序列由「区间 + forward + step」唯一决定，日期序列一致即参数等价；
       命中后顺手把签名回填，下次走①的快路径。
    """

    def _load(row) -> dict | None:
        try:
            cached = json.loads(row.result_json)
        except (TypeError, ValueError):
            return None
        return cached if isinstance(cached, dict) and cached.get("ok") else None

    row = db.execute(
        select(FactorMineResult)
        .where(FactorMineResult.expr == expr, FactorMineResult.params_json == sig)
        .order_by(FactorMineResult.id.desc()).limit(1)
    ).scalar_one_or_none()
    if row is not None:
        return row, _load(row)

    legacy = db.execute(
        select(FactorMineResult)
        .where(FactorMineResult.expr == expr, FactorMineResult.params_json == "")
        .order_by(FactorMineResult.id.desc()).limit(5)
    ).scalars().all()
    snaps: list[str] | None = None
    for cand in legacy:
        c = _load(cand)
        if not c:
            continue
        if c.get("groups") != payload.groups or c.get("forward_days") != payload.forward:
            continue
        if snaps is None:
            snaps = snapshot_dates(db, payload.start, payload.end, payload.forward)
        if [x.get("date") for x in c.get("ic_series", [])] == snaps:
            cand.params_json = sig
            db.commit()
            return cand, c
    return None, None


@router.post("/mine")
def mine(payload: MinePayload, db: Session = Depends(get_db)):
    """同步因子挖掘（核心池截面检验）。返回报告并落库。

    防重复：同「表达式 + 区间 + groups + forward」若已挖过，直接回放上次报告
    并打上 duplicate_of 标记，既不重算也不落库 —— Spearman IC 是确定性计算，
    同参数重复跑只会得到同一个数字，却会在历史列表里堆出一串看似「挖了新因子」
    的重复记录。底层数据已更新、确实需要重算时传 force=true（此时**就地更新**
    原记录而非再插一条，历史列表不会膨胀）。
    """
    expr = payload.expr.strip()
    sd, ed = resolve_range(payload.start, payload.end)
    sig = f"{sd.isoformat()}|{ed.isoformat()}|{payload.groups}|{payload.forward}"

    row, cached = _find_existing(db, expr, payload, sig)
    if not payload.force and cached is not None:
        cached["duplicate_of"] = row.id
        cached["duplicate_created_at"] = row.created_at.isoformat() if row.created_at else None
        return cached

    result = mine_factor(
        db, expr, name=payload.name,
        start=payload.start, end=payload.end,
        groups=payload.groups, forward=payload.forward,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "挖掘失败"))
    # 同参数已有记录 → 原地更新（顺带补齐 factor_stats 等新增字段），否则新增
    if row is None:
        row = FactorMineResult(expr=expr, params_json=sig)
        db.add(row)
    row.name = result["name"]
    row.rating = result["rating"]
    row.ic_mean = result["ic_mean"]
    row.icir = result["icir"]
    row.result_json = json.dumps(result, ensure_ascii=False, default=str)
    row.created_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    result["id"] = row.id
    return result


@router.post("/mine/{rid}/save")
def mine_save(rid: int, db: Session = Depends(get_db)):
    """（占位保留）已自动落库。"""
    return {"ok": True}


# ============ AI：自然语言 ↔ 表达式 ============
class AiGeneratePayload(BaseModel):
    text: str = Field(..., min_length=2, max_length=500, description="自然语言因子想法")
    max_retry: int = Field(3, ge=0, le=5, description="校验失败后的自动重修轮数")


class AiExplainPayload(BaseModel):
    expr: str = Field(..., min_length=1, description="待解读的因子表达式")


@router.get("/ai/status")
def ai_status():
    """AI 模式可用性（前端据此决定是否展示自然语言输入）。"""
    from app.services.factor_llm import llm_info

    return llm_info()


@router.post("/ai/generate")
def ai_generate(payload: AiGeneratePayload):
    """自然语言 → 因子表达式。

    只做「生成 + 校验」，**不自动挖掘、不自动落库** —— 交给用户过目确认后再走
    /factor/mine，避免 AI 生成的因子未经审视就进入研究记录。
    """
    from app.services.factor_llm import LLMUnavailable, nl_to_expr

    try:
        return nl_to_expr(payload.text, max_retry=payload.max_retry)
    except LLMUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.post("/ai/explain")
def ai_explain(payload: AiExplainPayload):
    """因子表达式 → 中文逻辑说明（读懂 GP 挖出的表达式）。"""
    from app.services.factor_llm import LLMUnavailable, explain_expr

    try:
        return explain_expr(payload.expr)
    except LLMUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


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
