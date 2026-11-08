"""因子挖掘 API（自定义表达式 → 有效性检验报告）。

- GET  /factor/functions          表达式函数库与变量参考（前端提示）
- POST /factor/validate           校验表达式（受限命名空间试算）
- POST /factor/mine               因子挖掘（同步，返回检验报告；落库可查）
- POST /factor/mine/horizons      多 horizon IC 热力（复用 mine_factor）
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
    snapshot_dates, news_event_test,
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


class HorizonsPayload(BaseModel):
    expr: str = Field(..., min_length=1, description="因子表达式")
    name: str = "自定义因子"
    start: str = ""
    end: str = ""
    groups: int = Field(5, ge=2, le=10)
    forwards: list[int] = Field(default_factory=lambda: [5, 10, 20, 40],
                                description="未来收益窗口列表")
    force: bool = False


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


@router.post("/mine/horizons")
def mine_horizons(payload: HorizonsPayload, db: Session = Depends(get_db)):
    """多 horizon IC 热力：对每个 forward 复用 mine_factor，只汇总关键指标。

    不落库、不复活 GP。默认 forwards=[5,10,20,40]。
    """
    expr = payload.expr.strip()
    forwards = sorted({int(f) for f in (payload.forwards or [5, 10, 20, 40]) if int(f) >= 1})
    if len(forwards) < 1:
        raise HTTPException(status_code=400, detail="forwards 不能为空")
    if len(forwards) > 8:
        raise HTTPException(status_code=400, detail="forwards 最多 8 个窗口")

    rows: list[dict] = []
    for fwd in forwards:
        result = mine_factor(
            db, expr, name=payload.name,
            start=payload.start, end=payload.end,
            groups=payload.groups, forward=fwd,
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=400,
                detail=f"horizon={fwd} 失败：{result.get('error', '挖掘失败')}",
            )
        oos = result.get("oos") or {}
        rows.append({
            "horizon": fwd,
            "ic_mean": result.get("ic_mean"),
            "icir": result.get("icir"),
            "t_stat": result.get("t_stat"),
            "rating": result.get("rating"),
            "oos": {
                "verdict": oos.get("verdict") if isinstance(oos, dict) else None,
                "enabled": oos.get("enabled") if isinstance(oos, dict) else None,
            },
            "n_periods": result.get("n_periods"),
            "pead_pit": result.get("pead_pit"),
            "valuation_pit": result.get("valuation_pit"),
            "cost_profile": result.get("cost_profile"),
        })
    return {"ok": True, "expr": expr, "name": payload.name, "horizons": rows}


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
    """登记因子（candidate）；同名重复登记返回已有 id。

    W45-lab 门禁：最近一次 mine report 须 OOS 通过且评级优秀/可用；
    依赖 earnings_surprise 时 pead_pit 不可为 legacy_static。
    """
    from app.models import FactorRegistry
    from app.services.factor_lab_gate import assert_enable_allowed

    exist = db.execute(select(FactorRegistry).where(
        FactorRegistry.name == payload.name)).scalar()
    if exist:
        return {"ok": True, "id": exist.id, "status": exist.status, "existed": True}
    try:
        assert_enable_allowed(db, expr=payload.expr, source_id=payload.source_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
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
    """candidate<->enabled 切换；enabled 才进入每日计算。

    W45-lab：切入 enabled 时走与 register 相同的挖掘报告门禁；切出不校验。
    """
    from app.models import FactorRegistry
    from app.services.factor_lab_gate import assert_enable_allowed

    row = db.get(FactorRegistry, rid)
    if not row:
        raise HTTPException(status_code=404, detail="不存在")
    if row.status == "candidate":
        try:
            assert_enable_allowed(db, expr=row.expr, source_id=row.source_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        row.status = "enabled"
    elif row.status == "enabled":
        row.status = "disabled"
    else:
        # disabled → candidate（待评估），不启用，无需门禁
        row.status = "candidate"
    db.commit()
    return {"ok": True, "id": rid, "status": row.status}

# ───────── 非结构化（新闻情绪）─────────
# 2026-09-18 随链路下线删除，2026-11-07 恢复市场级部分（数据已够）。
class NewsTestPayload(BaseModel):
    extreme_pct: float = Field(0.10, ge=0.02, le=0.3)
    horizon: int = Field(5, ge=1, le=60)
    # news=东财新闻/公告、mp=公众号语料、both=合并（默认：用户 2026-11-07 要求把公众号纳入）
    source: str = Field("both", description="news / mp / both")


@router.get("/news/daily")
def news_daily(db: Session = Depends(get_db), limit: int = 500, source: str = "news"):
    """情绪时序（倒序）。source=news(东财) / mp(公众号)。"""
    from app.models import MpSentimentDaily, NewsMarketDaily

    model = MpSentimentDaily if source == "mp" else NewsMarketDaily
    rows = db.execute(
        select(model).order_by(model.date.desc()).limit(min(limit, 1500))
    ).scalars().all()
    return [
        {"date": r.date.isoformat(), "n_articles": r.n_articles, "n_finance": r.n_finance,
         "bull": r.bull_score, "bear": r.bear_score, "net_sentiment": r.net_sentiment}
        for r in rows
    ]


@router.post("/news/event-test")
def news_event(payload: NewsTestPayload, db: Session = Depends(get_db)):
    """极端情绪日 → 未来 N 日指数收益检验（择时有效性）。"""
    return news_event_test(db, extreme_pct=payload.extreme_pct, horizon=payload.horizon,
                           source=payload.source)

@router.get("/social/heat")
def social_heat(db: Session = Depends(get_db), days: int = 30):
    """社交关注度（雪球 A 股热股 / 微博热搜）近 N 天。

    只有热度没有正文，故不做多空打分；`n_finance` 是命中财经关键词的条数。
    """
    from app.models import SocialHeatDaily

    rows = db.execute(
        select(SocialHeatDaily).order_by(SocialHeatDaily.date.desc())
        .limit(min(max(days, 1) * 3, 300))
    ).scalars().all()
    return [
        {"date": r.date.isoformat(), "source": r.source, "n_items": r.n_items,
         "n_finance": r.n_finance, "heat_total": r.heat_total, "heat_top10": r.heat_top10}
        for r in rows
    ]

@router.get("/sentiment/strategy")
def sentiment_strategy(top_industry: int = 8, top_stock: int = 15,
                       db: Session = Depends(get_db)):
    """舆情策略：情绪择时 + 重点行业 + 重点个股（三层合一）。

    与 /social/heat（纯热度计数）、/news/daily（纯情绪时序）的区别：
    这里给的是**可执行结论**，不是指标。
    """
    from app.services.sentiment_strategy import snapshot

    return snapshot(db, top_industry=top_industry, top_stock=top_stock)


# ───────── 热点简报（按交易时段；非结构化资讯 → LLM 解读）─────────
@router.get("/hot-brief/latest")
def hot_brief_latest(db: Session = Depends(get_db)):
    """最新一份简报（只返回有内容的）。"""
    from app.services.hot_brief import latest_brief

    return latest_brief(db) or {}


@router.get("/hot-brief/today")
def hot_brief_today(db: Session = Depends(get_db)):
    """今日累计：各轮条目合并去重 + 最新综述（首页用这个，而不是只看最新一轮）。"""
    from app.services.hot_brief import today_brief

    return today_brief(db) or {}


@router.get("/hot-brief/list")
def hot_brief_list(limit: int = 24, db: Session = Depends(get_db)):
    from app.services.hot_brief import list_briefs

    return list_briefs(db, limit)


@router.get("/hot-brief/input")
def hot_brief_input(ref: str = "", run_id: int | None = None, full: bool = False,
                    db: Session = Depends(get_db)):
    """单条输入的原文内容。

    默认返回生成简报时存下的正文快照（快讯=财联社电报正文，
    搜索=DeepSeek 返回的 600~800 字摘要）。
    `full=true` 才实时抓整页正文，按次计费，只在用户点「抓全文」时用。
    """
    from app.services.hot_brief import input_detail

    return input_detail(db, ref, run_id=run_id, full=full)


@router.get("/brief-digest/latest")
def brief_digest_latest(before: str | None = None, db: Session = Depends(get_db)):
    """最近一份当日全景综述。

    `before=YYYY-MM-DD` 时取该日及之前最近的一份 —— 早上 7 点看首页时今天还没
    收盘、没有今日综述，应该展示昨天收盘后那份，而不是空白。
    """
    from app.services.brief_digest import latest

    return latest(db, before=before)


@router.get("/brief-digest/list")
def brief_digest_list(limit: int = 30, db: Session = Depends(get_db)):
    """历史综述列表（不含正文，只给日期与主线）。"""
    from app.services.brief_digest import list_digests

    return list_digests(db, limit=limit)


@router.post("/brief-digest/run")
def brief_digest_run(day: str | None = None, db: Session = Depends(get_db)):
    """手动生成某日综述（默认今天）。耗时约 100 秒 —— v4-pro 是推理模型。

    调度器每天 17:45 自动跑一次；这个接口用于补跑或重跑。
    """
    from app.services.brief_digest import run as run_digest

    return run_digest(db, day=day)


@router.post("/hot-brief/run")
def hot_brief_run(minutes: int = 60, db: Session = Depends(get_db)):
    """手动触发一次（调试用；调度器每小时 :02 自动跑）。"""
    from app.services.hot_brief import run_brief

    return run_brief(db, minutes=minutes)
