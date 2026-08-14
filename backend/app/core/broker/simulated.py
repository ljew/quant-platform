"""模拟券商（SimulatedBroker）：订单落库 + 状态机，无需真实券商环境。

撮合规则（简化）：
- 市价单（price=0）：用实时价/最近收盘撮合，立即全部成交（FILLED）
- 限价单：按限价撮合，立即成交（忽略部分成交细节）
- 订单全程写入 broker_orders 表，状态机 PENDING→SUBMITTED→FILLED
- 查询持仓基于 orders 汇总
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.broker.base import BrokerAdapter, OrderRequest, OrderResult, OrderSide, OrderStatus
from app.models import BrokerOrder
from app.services import data_source


class SimulatedBroker(BrokerAdapter):
    name = "simulated"

    def __init__(self, db: Session):
        self.db = db

    # —— 连接（模拟无操作）——
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def _price_for(self, symbol: str) -> float:
        """取最新价：实时价 → 最近收盘。"""
        try:
            px = data_source.get_realtime_prices([symbol]).get(symbol)
            if px:
                return float(px)
        except Exception:  # noqa: BLE001
            pass
        try:
            bars = data_source.get_stock_daily_qfq(symbol, None, None)
            if bars:
                return float(bars[-1]["close"])
        except Exception:  # noqa: BLE001
            pass
        return 0.0

    def submit_order(self, req: OrderRequest) -> OrderResult:
        fill_price = req.price if req.price > 0 else self._price_for(req.symbol)
        if fill_price <= 0:
            return OrderResult(
                order_id=req.order_id, status=OrderStatus.REJECTED,
                message=f"无法获取 {req.symbol} 价格，订单被拒",
            )
        row = BrokerOrder(
            order_id=req.order_id,
            symbol=req.symbol,
            side=req.side.value,
            price=fill_price,
            quantity=req.quantity,
            status=OrderStatus.FILLED.value,
            filled_quantity=req.quantity,
            filled_price=fill_price,
            strategy_key=req.strategy_key,
            tag=req.tag,
            created_at=datetime.utcnow(),
        )
        self.db.add(row)
        self.db.commit()
        return OrderResult(
            order_id=req.order_id, status=OrderStatus.FILLED,
            filled_quantity=req.quantity, filled_price=fill_price,
            message=f"模拟成交 {req.symbol} @ {fill_price:.2f}",
        )

    def cancel_order(self, order_id: str) -> OrderResult:
        row = self.db.execute(
            select(BrokerOrder).where(BrokerOrder.order_id == order_id)
        ).scalar_one_or_none()
        if row is None:
            return OrderResult(order_id=order_id, status=OrderStatus.REJECTED, message="订单不存在")
        if OrderStatus(row.status).is_terminal:
            return OrderResult(order_id=order_id, status=OrderStatus(row.status), message="订单已终态")
        row.status = OrderStatus.CANCELLED.value
        self.db.commit()
        return OrderResult(order_id=order_id, status=OrderStatus.CANCELLED, message="已撤销")

    def query_order(self, order_id: str) -> OrderResult | None:
        row = self.db.execute(
            select(BrokerOrder).where(BrokerOrder.order_id == order_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        return OrderResult(
            order_id=row.order_id, status=OrderStatus(row.status),
            filled_quantity=row.filled_quantity, filled_price=row.filled_price,
        )

    def query_positions(self) -> dict[str, float]:
        """按 symbol 汇总已成交订单净持仓（BUY 加、SELL 减）。"""
        rows = self.db.execute(
            select(BrokerOrder.symbol, BrokerOrder.side, BrokerOrder.filled_quantity)
            .where(BrokerOrder.status == OrderStatus.FILLED.value)
        ).all()
        pos: dict[str, float] = {}
        for symbol, side, qty in rows:
            pos[symbol] = pos.get(symbol, 0.0) + (qty if side == OrderSide.BUY.value else -qty)
        return pos
