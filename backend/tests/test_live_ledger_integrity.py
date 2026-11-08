"""实盘账本不可变性与幂等回归测试。"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.live.intent import IntentSpec
from app.live.recon import (
    apply_corporate_action,
    apply_fill,
    cash_from_fills,
    mark_equity,
)
from app.models import (
    LiveDecisionSnapshot,
    LiveFill,
    LiveOrderIntent,
    LivePosition,
)
from scripts.live_daily import load_book
from app.live.persist import persist_decision

D0 = date(2026, 11, 5)
D1 = date(2026, 11, 6)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_mark_equity_is_idempotent(db):
    apply_fill(db, trade_date=D1, symbol="sh600000", side="BUY", qty=100, price=10.0)
    db.commit()

    first = mark_equity(db, D1, {"sh600000": 10.0}, capital=100_000.0)
    second = mark_equity(db, D1, {"sh600000": 10.0}, capital=100_000.0)

    assert second.cash == first.cash == 99_000.0
    assert second.total == first.total == 100_000.0


def test_load_book_preserves_cash_after_liquidation(db):
    apply_fill(db, trade_date=D0, symbol="sh600000", side="BUY", qty=100, price=10.0)
    apply_fill(db, trade_date=D1, symbol="sh600000", side="SELL", qty=100, price=12.0)
    db.commit()
    mark_equity(db, D1, {}, capital=100_000.0)

    book = load_book(db, 100_000.0)

    assert not book.positions
    assert book.cash == pytest.approx(100_200.0)


def test_external_fill_id_is_idempotent(db):
    first = apply_fill(
        db, trade_date=D0, symbol="sh600000", side="BUY", qty=100, price=10.0,
        external_id="broker-fill-1", source="broker",
    )
    second = apply_fill(
        db, trade_date=D0, symbol="sh600000", side="BUY", qty=100, price=10.0,
        external_id="broker-fill-1", source="broker",
    )
    db.commit()

    assert second.id == first.id
    assert len(db.scalars(select(LiveFill)).all()) == 1
    assert db.scalars(select(LivePosition)).one().shares == 100


def test_same_day_buy_cannot_be_sold_under_t_plus_one(db):
    apply_fill(db, trade_date=D0, symbol="sh600000", side="BUY", qty=100, price=10.0)

    with pytest.raises(ValueError, match="可卖数量不足"):
        apply_fill(db, trade_date=D0, symbol="sh600000", side="SELL", qty=100, price=11.0)


def test_corporate_action_adjusts_shares_cost_and_cash(db):
    apply_fill(db, trade_date=D0, symbol="sh600000", side="BUY", qty=100, price=10.0)
    apply_corporate_action(
        db, trade_date=D1, symbol="sh600000", share_ratio=2.0,
        cash_dividend_per_share=0.5, external_id="ca-1",
    )
    db.commit()

    pos = db.scalars(select(LivePosition)).one()
    assert pos.shares == 200
    assert pos.avg_cost == pytest.approx(5.0)
    assert pos.peak_close == pytest.approx(5.0)
    assert cash_from_fills(db, capital=100_000.0) == pytest.approx(99_050.0)

    equity = mark_equity(db, D1, {"sh600000": 5.0}, capital=100_000.0)
    assert equity.total == pytest.approx(100_050.0)


def _intent() -> IntentSpec:
    return IntentSpec(
        symbol="sh600000", side="BUY", target_shares=100, actual_shares=0,
        qty=100, ref_price=10.0, limit_price=10.2, est_amount=1000.0,
        reason="test", priority=100,
    )


def _snapshot_payload(config=None):
    return dict(
        trade_date=D1,
        strategy_key="fresh_ma",
        data_date=D1,
        data_ok=1,
        data_note="ok",
        enabled=["rps_breakout"],
        card_tiers={"rps_breakout": "strong"},
        gate={"open": True, "exc": 0.01, "n": 20},
        candidates=[{"symbol": "sh600000", "rank": "1"}],
        config=config or {"capital": 100_000.0},
        items=[_intent()],
    )


def test_snapshot_rerun_is_noop_and_change_is_rejected(db):
    snap_id, created = persist_decision(db, **_snapshot_payload())
    db.commit()
    again_id, created_again = persist_decision(db, **_snapshot_payload())

    assert created and not created_again
    assert again_id == snap_id
    assert len(db.scalars(select(LiveDecisionSnapshot)).all()) == 1
    assert len(db.scalars(select(LiveOrderIntent)).all()) == 1

    with pytest.raises(ValueError, match="不可变"):
        persist_decision(db, **_snapshot_payload(config={"capital": 200_000.0}))

    assert db.get(LiveDecisionSnapshot, snap_id).config_json == '{"capital": 100000.0}'
