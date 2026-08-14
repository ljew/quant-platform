"""券商适配层工厂。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.broker.base import BrokerAdapter
from app.core.broker.simulated import SimulatedBroker


def get_broker(name: str = "simulated", db: Session | None = None) -> BrokerAdapter:
    """按名称返回券商适配器（当前仅模拟券商；实盘适配按需扩展）。"""
    if name == "simulated":
        if db is None:
            from app.database import SessionLocal

            db = SessionLocal()
        return SimulatedBroker(db)
    raise ValueError(f"未知券商适配器: {name}（当前仅支持 simulated）")


def list_brokers() -> list[dict]:
    return [
        {
            "name": "simulated",
            "label": "模拟券商",
            "desc": "订单落库+状态机+实时价撮合，无需真实券商环境",
            "live": False,
        }
    ]
