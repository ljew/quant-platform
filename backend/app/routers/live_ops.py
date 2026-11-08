"""实盘运营 API（L4-L7）。

与 `routers/live.py` 的区别：那个是早期的单笔下单 demo（硬编码 SimulatedBroker，
且 main.py 里一直注释未启用）。**本模块不提供任何下单能力**——合规上平台不得
生成交易指令，只提供：

  GET  /live/intents      当日订单意图（人看；或终端读取「目标股数」参考）
  GET  /live/candidates   候选池 + 参数（**数据**，供券商终端内策略拉取）
  GET  /live/positions    实际持仓（来自成交回报）
  GET  /live/equity       净值序列 + 影子对照
  GET  /live/recon        执行质量（滑点 / 成交率 / 未成交）
  POST /live/fills        录入成交（人工或对账单回传）
  POST /live/corporate-actions  录入送拆股与现金分红

合规边界（再强调一次，改代码前先读）：
  候选池里少一只股票 = 数据，允许；
  订单里少一笔 = 指令，禁止。
所以 /live/candidates 是唯一可以给终端消费的接口。
"""
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.live import STRATEGY_KEY
from app.live.recon import apply_corporate_action, apply_fill, reconcile
from app.models import (LiveDecisionSnapshot, LiveEquity, LiveOrderIntent,
                        LivePosition)

router = APIRouter(prefix="/live", tags=["live-ops"])


@router.get("/intents")
def intents(trade_date: str | None = Query(None, alias="date"),
            strategy: str = STRATEGY_KEY, db: Session = Depends(get_db)):
    """当日订单意图。按优先级排序（卖出在前）。"""
    q = select(LiveOrderIntent).where(LiveOrderIntent.strategy_key == strategy)
    if trade_date:
        q = q.where(LiveOrderIntent.trade_date == date.fromisoformat(trade_date))
    rows = db.scalars(q.order_by(LiveOrderIntent.trade_date.desc(),
                                 LiveOrderIntent.priority,
                                 LiveOrderIntent.symbol).limit(200)).all()
    return [{
        "id": r.id, "date": r.trade_date.isoformat(), "exec_date":
        r.exec_date.isoformat() if r.exec_date else None,
        "symbol": r.symbol, "name": r.name, "side": r.side, "qty": r.qty,
        "ref_price": r.ref_price, "limit_price": r.limit_price,
        "est_amount": r.est_amount, "reason": r.reason, "status": r.status,
        "reject_reason": r.reject_reason, "filled_qty": r.filled_qty,
    } for r in rows]


@router.get("/candidates")
def candidates(trade_date: str | None = Query(None, alias="date"),
               strategy: str = STRATEGY_KEY, db: Session = Depends(get_db)):
    """候选池（数据）。券商终端内的策略据此决定买什么、买多少。

    返回的是「候选 + 排序 + 参数」，不含股数——股数必须在终端里按实时资产算，
    这既是合规要求，也是唯一能处理「开盘价与收盘价不同」的方式。
    """
    q = select(LiveDecisionSnapshot).where(
        LiveDecisionSnapshot.strategy_key == strategy)
    if trade_date:
        q = q.where(LiveDecisionSnapshot.trade_date == date.fromisoformat(trade_date))
    snap = db.scalars(q.order_by(LiveDecisionSnapshot.trade_date.desc()).limit(1)).first()
    if snap is None:
        raise HTTPException(404, "尚无决策快照，请先跑 live_daily.py")
    cfg = json.loads(snap.config_json or "{}")
    return {
        "trade_date": snap.trade_date.isoformat(),
        "data_date": snap.data_date.isoformat() if snap.data_date else None,
        "data_ok": bool(snap.data_ok),
        "data_note": snap.data_note,
        "gate_open": bool(snap.gate_open),
        "gate_exc": snap.gate_exc,
        "gate_n": snap.gate_n,
        "enabled_strategies": json.loads(snap.enabled_json or "[]"),
        "candidates": json.loads(snap.candidates_json or "[]"),
        "params": {
            "max_positions": cfg.get("max_positions"),
            "single_pct": cfg.get("single_pct"),
            "stop_loss": cfg.get("stop_loss"),
            "trail_drawdown": cfg.get("trail_drawdown"),
            "max_hold_days": cfg.get("max_hold_days"),
        },
    }


@router.get("/positions")
def positions(strategy: str = STRATEGY_KEY, db: Session = Depends(get_db)):
    rows = db.scalars(select(LivePosition).where(
        LivePosition.strategy_key == strategy)).all()
    return [{
        "symbol": r.symbol, "name": r.name, "shares": r.shares,
        "avg_cost": r.avg_cost,
        "entry_date": r.entry_date.isoformat() if r.entry_date else None,
        "peak_close": r.peak_close, "last_mark": r.last_mark_date.isoformat()
        if r.last_mark_date else None,
    } for r in rows]


@router.get("/equity")
def equity(strategy: str = STRATEGY_KEY, limit: int = 250,
           db: Session = Depends(get_db)):
    rows = db.scalars(select(LiveEquity).where(
        LiveEquity.strategy_key == strategy)
        .order_by(LiveEquity.trade_date.desc()).limit(min(limit, 1000))).all()
    return [{
        "date": r.trade_date.isoformat(), "cash": r.cash,
        "market_value": r.market_value, "total": r.total,
        "n_positions": r.n_positions, "shadow_total": r.shadow_total,
        "divergence_pct": r.divergence_pct,
    } for r in reversed(rows)]


@router.get("/recon")
def recon(trade_date: str = Query(..., alias="date"),
          strategy: str = STRATEGY_KEY, db: Session = Depends(get_db)):
    return reconcile(db, date.fromisoformat(trade_date), strategy)


@router.get("/shadow")
def shadow(strategy: str = STRATEGY_KEY, db: Session = Depends(get_db)):
    """影子组合净值（全历史重演）。实盘未开始时，用它展示策略本身的表现。

    注意区分两件事：影子组合 = 「如果每笔都按开盘价成交」的理想结果；
    live_equity = 「真实成交」的结果。两者的差就是执行成本。
    """
    import json as _json

    from app.models import PortfolioExpState
    row = db.scalars(select(PortfolioExpState)
                     .order_by(PortfolioExpState.as_of_date.desc()).limit(1)).first()
    if row is None:
        raise HTTPException(404, "尚无影子组合数据，请先跑 live_daily.py 或组合重演")
    summary = _json.loads(row.summary_json or "{}")
    equity = _json.loads(row.equity_json or "[]")
    holdings = _json.loads(row.holdings_json or "[]")
    return {
        "as_of": row.as_of_date.isoformat(),
        "summary": summary,
        "equity": equity,
        "holdings": holdings,
        "n_points": len(equity),
    }


class FillPayload(BaseModel):
    symbol: str
    side: str = Field(..., pattern="^(BUY|SELL)$")
    qty: int = Field(..., gt=0)
    price: float = Field(..., gt=0)
    trade_date: str | None = None
    commission: float = Field(0.0, ge=0)
    stamp_tax: float = Field(0.0, ge=0)
    transfer_fee: float = Field(0.0, ge=0)
    intent_id: int | None = None
    external_id: str | None = None
    name: str = ""
    strategy: str = STRATEGY_KEY


@router.post("/fills")
def create_fill(payload: FillPayload, db: Session = Depends(get_db)):
    """录入一笔幂等成交事件，并由完整事件账本重建持仓。"""
    d = date.fromisoformat(payload.trade_date) if payload.trade_date else date.today()
    try:
        f = apply_fill(db, trade_date=d, symbol=payload.symbol, side=payload.side,
                       qty=payload.qty, price=payload.price,
                       commission=payload.commission, stamp_tax=payload.stamp_tax,
                       transfer_fee=payload.transfer_fee,
                       intent_id=payload.intent_id, source="manual",
                       external_id=payload.external_id,
                       name=payload.name, strategy_key=payload.strategy)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "fill_id": f.id, "trade_date": d.isoformat()}


class CorporateActionPayload(BaseModel):
    symbol: str
    trade_date: str
    share_ratio: float = Field(1.0, gt=0)
    cash_dividend_per_share: float = Field(0.0, ge=0)
    external_id: str | None = None
    note: str = ""
    strategy: str = STRATEGY_KEY


@router.post("/corporate-actions")
def create_corporate_action(payload: CorporateActionPayload,
                            db: Session = Depends(get_db)):
    """录入送拆股或现金分红；重复外部编号返回原事件。"""
    d = date.fromisoformat(payload.trade_date)
    try:
        action = apply_corporate_action(
            db, trade_date=d, symbol=payload.symbol,
            share_ratio=payload.share_ratio,
            cash_dividend_per_share=payload.cash_dividend_per_share,
            source="manual", external_id=payload.external_id,
            note=payload.note, strategy_key=payload.strategy)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "corporate_action_id": action.id, "trade_date": d.isoformat()}
