"""决策快照与订单意图的持久化约束。

快照一旦写入就不可覆盖。完全相同的重跑是 no-op；内容发生变化时明确拒绝，
避免历史决策被新数据、新参数或新代码静默改写。
"""
from __future__ import annotations

import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live.intent import IntentSpec
from app.models import LiveDecisionSnapshot, LiveOrderIntent


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _same_snapshot(row: LiveDecisionSnapshot, payload: dict) -> bool:
    return (
        row.data_date == payload["data_date"]
        and row.data_ok == payload["data_ok"]
        and row.data_note == payload["data_note"]
        and json.loads(row.enabled_json or "[]") == payload["enabled"]
        and json.loads(row.card_tiers_json or "{}") == payload["card_tiers"]
        and row.gate_open == payload["gate_open"]
        and row.gate_exc == payload["gate_exc"]
        and row.gate_n == payload["gate_n"]
        and json.loads(row.candidates_json or "[]") == payload["candidates"]
        and json.loads(row.config_json or "{}") == payload["config"]
    )


def persist_decision(
    db: Session,
    *,
    trade_date: date,
    strategy_key: str,
    data_date: date | None,
    data_ok: int,
    data_note: str,
    enabled: list[str],
    card_tiers: dict,
    gate: dict,
    candidates: list[dict],
    config: dict,
    items: list[IntentSpec],
) -> tuple[int, bool]:
    """返回 ``(snapshot_id, created)``；调用方负责提交事务。"""
    payload = {
        "data_date": data_date,
        "data_ok": int(bool(data_ok)),
        "data_note": data_note,
        "enabled": sorted(enabled),
        "card_tiers": card_tiers,
        "gate_open": int(bool(gate.get("open"))),
        "gate_exc": gate.get("exc"),
        "gate_n": gate.get("n", 0),
        "candidates": candidates,
        "config": config,
    }
    existing = db.scalars(select(LiveDecisionSnapshot).where(
        LiveDecisionSnapshot.trade_date == trade_date,
        LiveDecisionSnapshot.strategy_key == strategy_key,
    )).first()
    if existing is not None:
        if not _same_snapshot(existing, payload):
            raise ValueError(
                f"{trade_date} / {strategy_key} 的决策快照不可变；"
                "检测到内容变化，已拒绝覆盖"
            )
        return existing.id, False

    snap = LiveDecisionSnapshot(
        trade_date=trade_date,
        strategy_key=strategy_key,
        data_date=data_date,
        data_ok=payload["data_ok"],
        data_note=data_note,
        enabled_json=_json(payload["enabled"]),
        card_tiers_json=_json(card_tiers),
        gate_open=payload["gate_open"],
        gate_exc=payload["gate_exc"],
        gate_n=payload["gate_n"],
        candidates_json=_json(candidates),
        config_json=_json(config),
    )
    db.add(snap)
    db.flush()
    for item in items:
        db.add(LiveOrderIntent(
            trade_date=trade_date,
            strategy_key=strategy_key,
            symbol=item.symbol,
            name=item.name,
            side=item.side,
            target_shares=item.target_shares,
            actual_shares=item.actual_shares,
            qty=item.qty,
            ref_price=item.ref_price,
            limit_price=item.limit_price,
            est_amount=item.est_amount,
            reason=item.reason,
            priority=item.priority,
            status="DRAFT" if item.ok else "REJECTED",
            reject_reason=item.blocked,
            snapshot_id=snap.id,
        ))
    return snap.id, True
