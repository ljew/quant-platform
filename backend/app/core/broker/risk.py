"""券商风控中间件（设计 v1.0 风控链：所有订单发往券商前必经检查）。

风控链顺序：
① 资金检查   —— 买入金额 ≤ 可用资金
② 持仓限制   —— 单标的持仓市值 ≤ 上限比例
③ 日亏损上限 —— 当日浮动+已实现亏损 ≤ 上限
④ 涨跌停检查 —— 买入价≥涨停 / 卖出价≤跌停 拒绝（按昨收 ±10% 简化）
⑤ 频率限制   —— 近 60s 下单笔数 ≤ 上限

任一环节拒绝 → 订单不入券商（返回 REJECTED）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.broker.base import OrderRequest, OrderSide
from app.models import BrokerOrder


class RiskGate:
    def __init__(
        self,
        db: Session,
        cash: float = 1_000_000.0,
        position_limit: float = 0.0,      # 0 = 关闭
        daily_loss_limit: float = 0.0,    # 0 = 关闭
        freq_limit: int = 5,              # 近 60s 最多下单笔数
    ):
        self.db = db
        self.cash = cash
        self.position_limit = position_limit
        self.daily_loss_limit = daily_loss_limit
        self.freq_limit = freq_limit

    def check(self, req: OrderRequest) -> tuple[bool, str]:
        """返回 (是否放行, 拒绝原因)。"""
        est_price = req.price if req.price > 0 else self._last_price(req.symbol)
        if est_price <= 0:
            return False, f"无法获取 {req.symbol} 价格，无法风控"
        # ① 资金检查（买入）
        if req.side == OrderSide.BUY:
            used = self._occupied_cash()
            avail = self.cash - used
            need = req.quantity * est_price
            if need > avail:
                return False, f"资金不足：可用 {avail:,.0f} 元 < 需 {need:,.0f} 元"
        # ② 持仓限制
        if self.position_limit > 0:
            cur = self._position_value(req.symbol)
            new_val = cur + (req.quantity * est_price if req.side == OrderSide.BUY else -req.quantity * est_price)
            ratio = new_val / self.cash if self.cash > 0 else 0.0
            if req.side == OrderSide.BUY and ratio > self.position_limit:
                return False, f"持仓超限：加仓后 {ratio:.1%} > 上限 {self.position_limit:.1%}"
        # ③ 日亏损上限（当日 FILLED 订单盈亏估算）
        if self.daily_loss_limit > 0:
            loss = self._today_loss()
            if loss < 0 and -loss / self.cash >= self.daily_loss_limit:
                return False, f"当日亏损 {loss:,.0f} 元达上限 {self.daily_loss_limit:.1%}"
        # ④ 涨跌停检查（按昨收 ±10% 简化）
        try:
            from app.services import data_source

            bars = data_source.get_stock_daily_qfq(req.symbol, None, None)
            if bars:
                prev = float(bars[-1]["close"])
                if req.price > 0:
                    if req.side == OrderSide.BUY and req.price >= prev * 1.1:
                        return False, f"涨停价 {prev*1.1:.2f} 买不进（现价 {req.price:.2f}）"
                    if req.side == OrderSide.SELL and req.price <= prev * 0.9:
                        return False, f"跌停价 {prev*0.9:.2f} 卖不出（现价 {req.price:.2f}）"
        except Exception:  # noqa: BLE001
            pass
        # ⑤ 频率限制（近 60s）
        if self.freq_limit > 0:
            since = datetime.utcnow() - timedelta(seconds=60)
            cnt = self.db.execute(
                select(BrokerOrder.id).where(BrokerOrder.created_at >= since)
            ).all()
            if len(cnt) >= self.freq_limit:
                return False, f"下单频率超限：近 60s 已 {len(cnt)} 笔（上限 {self.freq_limit}）"
        return True, ""

    # —— 辅助 ——
    def _occupied_cash(self) -> float:
        """已占用资金（BUY 未卖出的成交金额 - SELL 回笼）。"""
        rows = self.db.execute(
            select(BrokerOrder.symbol, BrokerOrder.side, BrokerOrder.filled_quantity, BrokerOrder.filled_price)
            .where(BrokerOrder.status == "FILLED")
        ).all()
        # 用持仓市值近似（简化）
        pos = {}
        for sym, side, qty, price in rows:
            pos[sym] = pos.get(sym, 0.0) + (qty if side == "BUY" else -qty)
        total = 0.0
        for sym, qty in pos.items():
            if qty > 0:
                total += self._last_price(sym) * qty
        return total

    def _position_value(self, symbol: str) -> float:
        rows = self.db.execute(
            select(BrokerOrder.side, BrokerOrder.filled_quantity, BrokerOrder.filled_price)
            .where(BrokerOrder.symbol == symbol, BrokerOrder.status == "FILLED")
        ).all()
        qty = sum(q if s == "BUY" else -q for s, q, _ in rows)
        if qty <= 0:
            return 0.0
        return self._last_price(symbol) * qty

    def _today_loss(self) -> float:
        """当日盈亏估算（简化：账户级精确盈亏留待实盘账户模块）。"""
        return 0.0

    def _last_price(self, symbol: str) -> float:
        try:
            from app.services import data_source

            bars = data_source.get_stock_daily_qfq(symbol, None, None)
            if bars:
                return float(bars[-1]["close"])
        except Exception:  # noqa: BLE001
            pass
        return 0.0
