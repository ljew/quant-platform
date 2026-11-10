"""研究卡 API：卡片查看 + 待确认建议 + 人工状态迁移。

设计（docs/research-card-v0.1.md）：
- 升级类迁移由每周复盘自动执行；本接口的写操作只服务于「人在页面上确认建议」，
  每次迁移必须带理由，全部进 history_json 留痕（actor=human）。
- 卡片状态是生产门禁唯一真相（research_card_svc），改状态下一次跑批即生效。
- 建议是「读时派生」的：不改库，直接根据卡片上的最新证据（每周复盘刷新）
  套用状态机条件算出来，前端横幅展示，确认后调 transition 执行。
"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.database import SessionLocal
from app.models import ResearchCard

router = APIRouter(prefix="/research", tags=["research"])

_STATE_ORDER = {"enabled": 0, "candidate": 1, "watch": 2, "downgraded": 3, "retired": 4}
_MIN_SAMPLES = 20
_RETIRE_NEG_WEEKS = 4


def match_strategy_key(card: ResearchCard) -> str:
    """研究卡 → registry 策略 key。找不到返回空串（= 不可回测）。

    卡片 key 和策略 key 不是一一对应的：卡片 `high_tight_flag` 对应策略
    `xq_high_tight_flag`，卡片 `turtle_trade` 对应 `turtle_classic`。
    所以按可信度依次试：signal_key → 同名 → 中文名。
    """
    from app.core.strategies.registry import STRATEGY_REGISTRY

    for k, meta in STRATEGY_REGISTRY.items():
        if meta.get("signal_key") == card.key:
            return k
    if card.key in STRATEGY_REGISTRY:
        return card.key
    for k, meta in STRATEGY_REGISTRY.items():
        if meta.get("name") == card.name:
            return k
    return ""


def _recent_backtest(db, strategy_key: str) -> dict | None:
    """该策略最近一次回测的关键指标（不含净值曲线，列表/详情都够用）。"""
    if not strategy_key:
        return None
    from app.models import Backtest

    r = db.execute(
        select(Backtest).where(Backtest.strategy_key == strategy_key)
        .order_by(Backtest.id.desc()).limit(1)
    ).scalar_one_or_none()
    if not r:
        return None
    return {
        "id": r.id, "start": r.start_date, "end": r.end_date,
        "total_return": r.total_return, "benchmark_total_return": r.benchmark_total_return,
        "excess_return": r.excess_return, "sharpe": r.sharpe,
        "max_drawdown": r.max_drawdown, "trade_count": r.trade_count,
        "win_rate": r.win_rate,
    }


def _card_dict(r: ResearchCard, db=None) -> dict:
    """卡片字典。传 db 时附带「对应策略 + 最近回测」，供详情页直接展示。"""
    d = {
        "id": r.id, "card_type": r.card_type, "key": r.key, "name": r.name,
        "hypothesis": r.hypothesis, "data_spec": r.data_spec,
        "evidence": json.loads(r.evidence_json or "{}"),
        "cost_note": r.cost_note, "crowd_note": r.crowd_note,
        "fail_condition": r.fail_condition, "state": r.state,
        "history": json.loads(r.history_json or "[]"),
        "updated_at": r.updated_at.isoformat(timespec="seconds") if r.updated_at else None,
    }
    if db is not None:
        skey = match_strategy_key(r)
        d["strategy_key"] = skey
        # strategy 卡：有回测就是有，没有就明确 None（前端据此说「还没跑过」）
        # factor 卡：本来就不是可回测的东西，恒 None，前端按 card_type 区分措辞
        d["backtest"] = _recent_backtest(db, skey) if r.card_type == "strategy" else None
    return d


@router.get("/cards")
def list_cards():
    db = SessionLocal()
    try:
        rows = db.execute(select(ResearchCard)
                          .order_by(ResearchCard.card_type, ResearchCard.id)).scalars().all()
        cards = [_card_dict(r, db) for r in rows]
        cards.sort(key=lambda c: (_STATE_ORDER.get(c["state"], 9), c["card_type"], c["id"]))
        state_count: dict[str, int] = {}
        for c in cards:
            state_count[c["state"]] = state_count.get(c["state"], 0) + 1
        return {"total": len(cards), "state_count": state_count, "cards": cards}
    finally:
        db.close()


class CardIn(BaseModel):
    """新建/更新研究卡的入参。"""

    card_type: str = Field(pattern="^(strategy|factor)$")
    key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    hypothesis: str = ""
    data_spec: str = ""
    evidence: dict = Field(default_factory=dict)
    cost_note: str = ""
    crowd_note: str = ""
    fail_condition: str = ""
    state: str = Field(default="watch", pattern="^(enabled|candidate|watch|downgraded|retired)$")
    history_note: str = ""          # 首条/本次流转理由
    actor: str = "human"            # human / agent


@router.post("/cards")
def upsert_card(body: CardIn):
    """新建或更新一张研究卡（按 card_type + key 幂等）。

    用途：把一次研究结论——**包括已证伪的假设**——落成卡片，让它成为可追踪的资产。
    失败结论同样有 value：它标记了一片已被排除的搜索空间。

    边界：这里只写 research_cards，不动 factor_registry / STRATEGY_QUALITY。
    卡片是否影响生产，由 research_card_svc 读取 state 决定（enabled 才算数）。
    """
    db = SessionLocal()
    try:
        card = db.execute(
            select(ResearchCard).where(
                ResearchCard.card_type == body.card_type,
                ResearchCard.key == body.key,
            )
        ).scalar_one_or_none()

        now = datetime.now()
        note = body.history_note or ("新建卡片" if card is None else "更新卡片")

        if card is None:
            card = ResearchCard(
                card_type=body.card_type,
                key=body.key,
                name=body.name,
                state=body.state,
                history_json=json.dumps(
                    [{"at": now.strftime("%Y-%m-%d %H:%M"), "from": None,
                      "to": body.state, "reason": note, "actor": body.actor}],
                    ensure_ascii=False,
                ),
            )
            db.add(card)
        else:
            history = json.loads(card.history_json or "[]")
            if card.state != body.state:
                history.append({
                    "at": now.strftime("%Y-%m-%d %H:%M"), "from": card.state,
                    "to": body.state, "reason": note, "actor": body.actor,
                })
                card.history_json = json.dumps(history, ensure_ascii=False)
            card.state = body.state

        card.name = body.name
        card.hypothesis = body.hypothesis
        card.data_spec = body.data_spec
        card.evidence_json = json.dumps(body.evidence, ensure_ascii=False)
        card.cost_note = body.cost_note
        card.crowd_note = body.crowd_note
        card.fail_condition = body.fail_condition
        card.updated_at = now

        db.commit()
        db.refresh(card)
        return _card_dict(card, db)
    finally:
        db.close()


def _suggest_for(card: ResearchCard, ev: dict) -> dict | None:
    """根据卡片最新证据（每周复盘刷新）派生待确认建议；无需重算归因。"""
    exc = ev.get("exc_10")
    n = ev.get("n_10") or 0
    enough = exc is not None and n >= _MIN_SAMPLES
    if card.card_type != "strategy":
        return None
    if card.state == "enabled" and enough and exc < 0:
        return {"kind": "downgrade", "to_state": "downgraded",
                "text": f"90 日 T+10 超额 {exc:+.2f}%（n={n}）触发失效条件，建议降级（停止建仓）"}
    if card.state == "downgraded":
        neg = int(ev.get("negative_weeks", 0))
        if neg >= _RETIRE_NEG_WEEKS:
            return {"kind": "retire", "to_state": "retired",
                    "text": f"降级后连续 {neg} 周负超额（本周 {exc:+.2f}%），达到退役条件"}
    if card.state == "candidate" and enough and exc > 0:
        return {"kind": "upgrade", "to_state": "enabled",
                "text": f"90 日超额 {exc:+.2f}%（n={n}，t={ev.get('t_10')}），可评估升启用"
                        f"（确认前请先看成本与相关性字段）"}
    if card.state == "candidate" and enough and exc < 0:
        return {"kind": "demote_watch", "to_state": "watch",
                "text": f"候选期 90 日超额 {exc:+.2f}%（n={n}）转负，建议降为观察"}
    return None


@router.get("/suggestions")
def pending_suggestions():
    """待人工确认的状态迁移建议（读时派生，与每周复盘的状态机同一套条件）。"""
    db = SessionLocal()
    try:
        rows = db.execute(select(ResearchCard)).scalars().all()
        items = []
        for r in rows:
            ev = json.loads(r.evidence_json or "{}")
            s = _suggest_for(r, ev)
            if s:
                items.append({
                    "card_id": r.id, "key": r.key, "name": r.name,
                    "state": r.state, "evidence_date": ev.get("evidence_updated_at"),
                    **s,
                })
        return {"total": len(items), "items": items}
    finally:
        db.close()


@router.get("/cards/{card_id}/signals")
def card_signals(card_id: int, date: str | None = None):
    """单策略的信号列表 + 全面分析（研究卡详情页「信号与分析」板块）。

    返回：90 日绩效行（含 t 值）、按月/连续天数/共振的细分、近 30 天信号量、
    指定日（默认最新）的信号标的与判定动作。因子卡无信号，返回提示。
    """
    from datetime import date as date_cls

    from sqlalchemy import func

    from app.config import DATA_DIR
    from app.models import SignalAction, SignalDaily
    from app.services.xq import stats as xq_stats

    db = SessionLocal()
    try:
        card = db.get(ResearchCard, card_id)
        if not card:
            raise HTTPException(404, "研究卡不存在")
        if card.card_type != "strategy":
            return {"ok": False, "error": "因子卡没有选股信号（因子分析请去「因子实验室」）"}

        stats = xq_stats.compute_stats(db, str(DATA_DIR / "quant.duckdb"),
                                       strategy=card.key)
        if not stats.get("ok"):
            return {"ok": False, "error": stats.get("error", "统计不可用")}
        perf = (stats.get("by_strategy") or [{}])[0]

        # 近 30 个有信号的交易日及命中数
        day_rows = db.execute(
            select(SignalDaily.date, func.count())
            .where(SignalDaily.strategy == card.key)
            .group_by(SignalDaily.date)
            .order_by(SignalDaily.date.desc()).limit(30)
        ).all()
        recent_days = [{"date": d.isoformat(), "count": c} for d, c in day_rows]

        # 指定日（默认最新）的信号标的 ⋈ 当日动作判定
        pick_date = None
        if date:
            try:
                pick_date = date_cls.fromisoformat(date)
            except ValueError:
                raise HTTPException(400, "date 格式应为 YYYY-MM-DD")
        elif day_rows:
            pick_date = day_rows[0][0]

        latest_actions: list[dict] = []
        total_latest = 0
        if pick_date:
            picks = db.execute(
                select(SignalDaily.symbol, SignalDaily.name)
                .where(SignalDaily.date == pick_date, SignalDaily.strategy == card.key)
            ).all()
            total_latest = len(picks)
            acts = {a.symbol: a for a in db.execute(
                select(SignalAction).where(SignalAction.date == pick_date)).scalars().all()}
            prio = {"BUY_STRONG": 0, "SELL": 1, "REDUCE": 2, "WATCH": 3}
            for sym, name in picks:
                a = acts.get(sym)
                latest_actions.append({
                    "symbol": sym, "name": name,
                    "action": a.action if a else None,
                    "streak": a.streak if a else None,
                    "resonance": a.resonance if a else None,
                    "close": a.close if a else None,
                    "reason": a.reason if a else None,
                })
            latest_actions.sort(key=lambda r: (prio.get(r["action"] or "", 9),
                                               -(r["streak"] or 0)))
            latest_actions = latest_actions[:100]  # 密集策略截断，总数单独返回

        return {
            "ok": True, "key": card.key, "name": card.name,
            "window_days": stats.get("window_days"),
            "perf": perf,
            "by_month": stats.get("by_month", []),
            "by_streak": stats.get("by_streak", []),
            "by_resonance": stats.get("by_resonance", []),
            "recent_days": recent_days,
            "latest_date": pick_date.isoformat() if pick_date else None,
            "latest_total": total_latest,
            "latest_actions": latest_actions,
        }
    finally:
        db.close()


_KIND_LABEL = {
    "created": "建卡", "state": "状态变更",
    "backtest": "回测", "optimize": "参数寻优", "paper": "模拟盘",
}


@router.get("/cards/{card_id}/timeline")
def card_timeline(card_id: int, limit: int = 80):
    """一张卡的决策时间线：从写下假设到上模拟盘，每一步是什么时候做的。

    设计文档 09 节标的缺口（「策略主页 · 缺决策时间线」）。原来页面上只有
    「当前是什么状态」，看不到「为什么变成这样、中间试过几轮」—— 复盘时
    只能翻聊天记录。

    三类事件按时间合并，但**不混为一谈**（kind 分开，前端按类上色）：
    · state / created：人的决策，带理由（history_json，actor=human 才是人改的）
    · backtest / optimize / paper：机器跑出来的客观事实，看得出来「试过几次」
    """
    from app.models import Backtest, OptimizeRun, PaperTask

    db = SessionLocal()
    try:
        card = db.get(ResearchCard, card_id)
        if not card:
            raise HTTPException(404, "研究卡不存在")
        skey = match_strategy_key(card)
        ev: list[dict] = []

        def add(at: str, kind: str, title: str, detail: str = "", metric=None):
            if not at:
                return
            ev.append({"at": at[:16], "kind": kind, "title": title,
                       "detail": detail, "metric": metric})

        # ① 建卡
        if card.created_at:
            add(card.created_at.strftime("%Y-%m-%d %H:%M"), "created",
                f"建卡 · {card.name}", card.hypothesis or "")

        # ② 人工/系统状态流转
        state_cn = {"candidate": "候选", "enabled": "启用", "watch": "观察",
                    "downgraded": "降级", "retired": "退役"}
        for h in json.loads(card.history_json or "[]"):
            at = h.get("at") or (h.get("date", "") + " 00:00")
            frm, to = h.get("from"), h.get("to")
            title = (f"{state_cn.get(frm, frm or '—')} → {state_cn.get(to, to)}")
            actor = "人工" if h.get("actor") == "human" else "系统"
            add(at, "state", f"{actor}改状态：{title}", h.get("reason", ""))

        if skey:
            # ③ 回测（最多 12 次，多了时间线会被淹掉）
            for b in db.execute(
                select(Backtest).where(Backtest.strategy_key == skey)
                .order_by(Backtest.id.desc()).limit(12)
            ).scalars().all():
                r = b.total_return or 0
                add(b.created_at.strftime("%Y-%m-%d %H:%M") if b.created_at else "",
                    "backtest",
                    f"回测 {b.start_date} ~ {b.end_date}",
                    f"最大回撤 {(b.max_drawdown or 0) * 100:.1f}% · 夏普 {b.sharpe or 0:.2f}"
                    f" · {b.trade_count or 0} 笔",
                    {"label": "累计收益", "value": f"{r * 100:+.2f}%", "good": r >= 0})

            # ④ 参数寻优
            for o in db.execute(
                select(OptimizeRun).where(OptimizeRun.strategy_key == skey)
                .order_by(OptimizeRun.id.desc()).limit(6)
            ).scalars().all():
                at = o.finished_at or o.started_at
                add(at.strftime("%Y-%m-%d %H:%M") if at else "", "optimize",
                    f"寻优 {o.total or 0} 组 · {o.status}",
                    f"最优夏普 {o.best_sharpe:.2f}" if o.best_sharpe is not None else "",
                    {"label": "最优夏普", "value": f"{o.best_sharpe:.2f}",
                     "good": (o.best_sharpe or 0) > 0} if o.best_sharpe is not None else None)

            # ⑤ 模拟盘
            for t in db.execute(
                select(PaperTask).where(PaperTask.strategy_key == skey)
                .order_by(PaperTask.id.desc()).limit(6)
            ).scalars().all():
                at = t.account_start or t.start_date
                add(f"{at} 00:00" if at else "", "paper",
                    f"建模拟盘 · {t.name}",
                    f"初始 {t.initial_cash / 1e4:.0f} 万 · 跑到 {t.last_bar_date or '—'}"
                    + ("" if t.enabled else " · 已停用"),
                    None)

        ev.sort(key=lambda x: x["at"], reverse=True)
        return {
            "card_id": card.id, "key": card.key, "name": card.name,
            "strategy_key": skey, "count": len(ev),
            "counts": {k: sum(1 for e in ev if e["kind"] == k) for k in _KIND_LABEL},
            "events": ev[:max(1, min(limit, 200))],
        }
    finally:
        db.close()


@router.get("/cards/{card_id}")
def get_card(card_id: int):
    db = SessionLocal()
    try:
        card = db.get(ResearchCard, card_id)
        if not card:
            raise HTTPException(404, "研究卡不存在")
        return _card_dict(card, db)
    finally:
        db.close()


class TransitionIn(BaseModel):
    to_state: str = Field(pattern="^(enabled|candidate|watch|downgraded|retired)$")
    reason: str = Field(min_length=2, max_length=500)


@router.post("/cards/{card_id}/transition")
def transition_card(card_id: int, body: TransitionIn):
    """人工确认的状态迁移：写状态 + 历史留痕。下一次跑批即生效（无需重启）。"""
    db = SessionLocal()
    try:
        card = db.get(ResearchCard, card_id)
        if not card:
            raise HTTPException(404, "研究卡不存在")
        if card.state == body.to_state:
            return {"ok": True, "changed": False, "state": card.state}
        # W45-lab：因子卡 → enabled 同注册表门禁（读 factor_registry / mine report）
        if body.to_state == "enabled" and card.card_type == "factor":
            from app.models import FactorRegistry
            from app.services.factor_lab_gate import assert_enable_allowed
            reg = db.execute(
                select(FactorRegistry).where(FactorRegistry.name == card.key)
            ).scalar_one_or_none()
            if reg is None:
                reg = db.execute(
                    select(FactorRegistry).where(FactorRegistry.name == card.name)
                ).scalar_one_or_none()
            try:
                if reg is not None:
                    assert_enable_allowed(db, expr=reg.expr, source_id=reg.source_id)
                else:
                    # 无注册表行时，尝试用 evidence.mine_id / expr
                    ev0 = json.loads(card.evidence_json or "{}")
                    expr = ev0.get("expr") or ""
                    mid = ev0.get("mine_id") or ev0.get("source_id")
                    if not expr and not mid:
                        raise ValueError("因子研究卡缺少关联挖掘报告（registry/evidence）")
                    assert_enable_allowed(
                        db, expr=expr or "",
                        source_id=int(mid) if mid is not None else None,
                    )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        hist = json.loads(card.history_json) if card.history_json else []
        hist.append({
            "at": f"{datetime.now():%Y-%m-%d %H:%M}",
            "from": card.state, "to": body.to_state,
            "reason": body.reason, "actor": "human",
        })
        card.history_json = json.dumps(hist, ensure_ascii=False)
        card.state = body.to_state
        card.updated_at = datetime.now()
        # 恢复正超时清零负周数计数
        ev = json.loads(card.evidence_json or "{}")
        if body.to_state in ("candidate", "enabled") and ev.get("negative_weeks"):
            ev["negative_weeks"] = 0
            card.evidence_json = json.dumps(ev, ensure_ascii=False)
        db.commit()
        return {"ok": True, "changed": True, "state": card.state,
                "card": _card_dict(card, db)}
    finally:
        db.close()
