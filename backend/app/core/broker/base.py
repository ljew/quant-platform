"""券商适配层（设计 v1.0：统一 Order API + BrokerAdapter 适配器）。

策略/上层通过统一接口发单，BrokerAdapter 将标准订单翻译为各券商协议。
新增券商只需实现 BrokerAdapter 接口。

订单状态机：PENDING → SUBMITTED → PARTIAL_FILLED → FILLED / CANCELLED / REJECTED

实盘适配（CTP/XTP/QMT/IBKR）需对应券商凭证与网络环境，本层先提供接口定义
与 SimulatedBroker（模拟撮合），实盘适配按需实现。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"          # 已创建，待提交
    SUBMITTED = "SUBMITTED"      # 已提交券商
    PARTIAL_FILLED = "PARTIAL_FILLED"  # 部分成交
    FILLED = "FILLED"            # 全部成交
    CANCELLED = "CANCELLED"      # 已撤销
    REJECTED = "REJECTED"        # 被拒（风控/券商）

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


@dataclass
class OrderRequest:
    """标准订单（券商无关）。"""
    symbol: str                 # 归一化 symbol，如 sh600519
    side: OrderSide
    price: float = 0.0          # 0 = 市价
    quantity: int = 100
    order_id: str = ""
    strategy_key: str = "manual"
    tag: str = ""               # 备注/信号类型

    def __post_init__(self) -> None:
        if not self.order_id:
            import uuid
            self.order_id = uuid.uuid4().hex[:16]


@dataclass
class OrderResult:
    """券商返回结果。"""
    order_id: str
    status: OrderStatus
    filled_quantity: int = 0
    filled_price: float = 0.0
    message: str = ""
    extra: dict = field(default_factory=dict)


class BrokerAdapter:
    """券商适配器基类（实盘实现子类）。"""

    name = "base"

    def connect(self) -> None:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def submit_order(self, req: OrderRequest) -> OrderResult:
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> OrderResult:
        raise NotImplementedError

    def query_order(self, order_id: str) -> Optional[OrderResult]:
        raise NotImplementedError

    def query_positions(self) -> dict[str, float]:
        """返回 {symbol: 持仓数量}。"""
        raise NotImplementedError
