"""研究卡只读 API（v0.1：档案查看；状态迁移走复盘建议，未开放写）。"""
from __future__ import annotations

import json

from fastapi import APIRouter
from sqlalchemy import select

from app.database import SessionLocal
from app.models import ResearchCard

router = APIRouter(prefix="/research", tags=["research"])


@router.get("/cards")
def list_cards():
    db = SessionLocal()
    try:
        rows = db.execute(select(ResearchCard)
                          .order_by(ResearchCard.card_type, ResearchCard.id)).scalars().all()
        order = {"enabled": 0, "candidate": 1, "watch": 2, "downgraded": 3, "retired": 4}
        cards = [{
            "id": r.id, "card_type": r.card_type, "key": r.key, "name": r.name,
            "hypothesis": r.hypothesis, "data_spec": r.data_spec,
            "evidence": json.loads(r.evidence_json or "{}"),
            "cost_note": r.cost_note, "crowd_note": r.crowd_note,
            "fail_condition": r.fail_condition, "state": r.state,
            "history": json.loads(r.history_json or "[]"),
            "updated_at": r.updated_at.isoformat(timespec="seconds") if r.updated_at else None,
        } for r in rows]
        cards.sort(key=lambda c: (order.get(c["state"], 9), c["card_type"], c["id"]))
        state_count: dict[str, int] = {}
        for c in cards:
            state_count[c["state"]] = state_count.get(c["state"], 0) + 1
        return {"total": len(cards), "state_count": state_count, "cards": cards}
    finally:
        db.close()
