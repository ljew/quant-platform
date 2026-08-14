"""实盘/模拟交易 API（设计 v1.0 live 模块；当前接入模拟券商 SimulatedBroker）。

- GET  /live/brokers                可用券商适配器列表
- POST /live/order                  提交标准订单（模拟撮合）
- GET  /live/orders                 订单列表（最近 50 条）
- GET  /live/orders/{order_id}      订单状态
- POST /live/orders/{order_id}/cancel  撤销未成交订单
- GET  /live/positions              持仓汇总（按已成交订单净持仓）
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.broker import get_broker, list_brokers
from app.core.broker.base import OrderRequest, OrderSide
from app.database import get_db
from app.models import BrokerOrder

router = APIRouter(prefix="/live", tags=["live"])


class OrderPayload(BaseModel):
    symbol: str
    side: str = Field(..., pattern="^(BUY|SELL)$")
    price: float = 0.0
    quantity: int = Field(100, ge=1)
    strategy_key: str = "manual"
    tag: str = ""


@router.get("/brokers")
def brokers():
    return list_brokers()


@router.post("/order")
def submit_order(payload: OrderPayload, db: Session = Depends(get_db)):
    """提交标准订单（当前为模拟撮合，立即 FILLED）。"""
    broker = get_broker("simulated", db)
    req = OrderRequest(
        symbol=payload.symbol,
        side=OrderSide(payload.side),
        price=payload.price,
        quantity=payload.quantity,
        strategy_key=payload.strategy_key,
        tag=payload.tag,
    )
    result = broker.submit_order(req)
    if result.status.value in ("REJECTED",):
        raise HTTPException(status_code=400, detail=result.message)
    return {
        "order_id": result.order_id,
        "status": result.status.value,
        "symbol": payload.symbol,
        "side": payload.side,
        "filled_price": result.filled_price,
        "filled_quantity": result.filled_quantity,
        "message": result.message,
    }


@router.get("/orders")
def orders(db: Session = Depends(get_db), limit: int = 50):
    rows = db.execute(
        select(BrokerOrder).order_by(BrokerOrder.id.desc()).limit(min(limit, 200))
    ).scalars().all()
    return [
        {
            "order_id": r.order_id, "symbol": r.symbol, "side": r.side,
            "price": r.price, "quantity": r.quantity, "status": r.status,
            "filled_price": r.filled_price, "filled_quantity": r.filled_quantity,
            "strategy_key": r.strategy_key, "tag": r.tag, "message": r.message,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.get("/orders/{order_id}")
def order_status(order_id: str, db: Session = Depends(get_db)):
    broker = get_broker("simulated", db)
    r = broker.query_order(order_id)
    if r is None:
        raise HTTPException(status_code=404, detail=f"订单不存在: {order_id}")
    return {"order_id": r.order_id, "status": r.status.value,
            "filled_price": r.filled_price, "filled_quantity": r.filled_quantity}


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str, db: Session = Depends(get_db)):
    broker = get_broker("simulated", db)
    r = broker.cancel_order(order_id)
    if r.status.value == "REJECTED" and "不存在" in r.message:
        raise HTTPException(status_code=404, detail=r.message)
    return {"order_id": order_id, "status": r.status.value, "message": r.message}


@router.get("/positions")
def positions(db: Session = Depends(get_db)):
    broker = get_broker("simulated", db)
    return {"positions": broker.query_positions()}
