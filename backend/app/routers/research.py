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


def _card_dict(r: ResearchCard) -> dict:
    return {
        "id": r.id, "card_type": r.card_type, "key": r.key, "name": r.name,
        "hypothesis": r.hypothesis, "data_spec": r.data_spec,
        "evidence": json.loads(r.evidence_json or "{}"),
        "cost_note": r.cost_note, "crowd_note": r.crowd_note,
        "fail_condition": r.fail_condition, "state": r.state,
        "history": json.loads(r.history_json or "[]"),
        "updated_at": r.updated_at.isoformat(timespec="seconds") if r.updated_at else None,
    }


@router.get("/cards")
def list_cards():
    db = SessionLocal()
    try:
        rows = db.execute(select(ResearchCard)
                          .order_by(ResearchCard.card_type, ResearchCard.id)).scalars().all()
        cards = [_card_dict(r) for r in rows]
        cards.sort(key=lambda c: (_STATE_ORDER.get(c["state"], 9), c["card_type"], c["id"]))
        state_count: dict[str, int] = {}
        for c in cards:
            state_count[c["state"]] = state_count.get(c["state"], 0) + 1
        return {"total": len(cards), "state_count": state_count, "cards": cards}
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
                "card": _card_dict(card)}
    finally:
        db.close()
