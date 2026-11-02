"""L4 订单意图：目标持仓 − 实际持仓。

与回测的差别（必须显式承认）：
  回测在次日开盘时**精确**知道开盘价，可以算出恰好花完预算的股数；
  实盘在 15:30 只能用当日收盘价**估算**，次日开盘价会偏，所以：
    - 预算留 buffer（默认 3%），避免开盘高开导致资金不足
    - 限价用 close × (1 + 溢价)，默认 2%，容忍开盘跳空但不追高
    - 实际成交股数由券商撮合决定，与 intent 的 qty 可能有偏差 → 记到 live_fill

这条差异不会消失，只能度量。`live_equity.divergence_pct`（实盘 vs 影子）就是度量。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.live.target import Book, Decision

# 板块最小交易单位（股）
LOT_MAIN = 100        # 沪深主板 / 创业板
LOT_STAR = 200        # 科创板（688）最小 200 股，之后 1 股递增
LOT_BJ = 100          # 北交所（平台默认剔除，保留以防）


def lot_size(symbol: str) -> int:
    code = symbol[2:] if len(symbol) > 2 else symbol
    if symbol.startswith("sh68"):
        return LOT_STAR
    return LOT_MAIN


def is_star(symbol: str) -> bool:
    return symbol.startswith("sh68")


@dataclass
class IntentSpec:
    symbol: str
    side: str                  # BUY / SELL
    target_shares: int
    actual_shares: int
    qty: int
    ref_price: float           # 未复权收盘价
    limit_price: float
    est_amount: float
    reason: str
    priority: int              # 越小越先
    name: str = ""
    blocked: str = ""          # 非空 = 未生成订单的原因（买不起/无价/超预算）

    @property
    def ok(self) -> bool:
        return self.qty > 0 and not self.blocked


@dataclass
class IntentBook:
    trade_date: date
    items: list[IntentSpec] = field(default_factory=list)
    cash_before: float = 0.0
    cash_after_est: float = 0.0

    @property
    def orders(self) -> list[IntentSpec]:
        return [i for i in self.items if i.ok]

    @property
    def blocked(self) -> list[IntentSpec]:
        return [i for i in self.items if i.blocked]


def build_intents(
    trade_date: date,
    dec: Decision,
    book: Book,
    close_px: dict[str, float],
    names: dict[str, str],
    cfg: dict,
    *,
    lot_resolver=lot_size,
) -> IntentBook:
    """把「次日开盘动作」翻译成可执行的订单清单。

    顺序：先卖后买（卖出回笼的现金参与买入预算）。这一点与回测一致——
    apply_open 也是先卖后买。
    """
    out = IntentBook(trade_date=trade_date, cash_before=book.cash)
    cash = book.cash
    buy_buffer = cfg.get("buy_buffer", 0.03)
    limit_premium = cfg.get("limit_premium", 0.02)

    # ── 卖出（清仓式，与回测 dec.sells 语义一致）──
    for sym in dec.sells:
        pos = book.positions.get(sym)
        if pos is None:
            continue
        px = close_px.get(sym)
        if not px:
            out.items.append(IntentSpec(
                symbol=sym, side="SELL", target_shares=0, actual_shares=pos.shares,
                qty=0, ref_price=0.0, limit_price=0.0, est_amount=0.0,
                reason=pos.exit_reason or "信号离场", priority=0,
                name=names.get(sym, ""), blocked="无当日收盘价（停牌？）"))
            continue
        out.items.append(IntentSpec(
            symbol=sym, side="SELL", target_shares=0, actual_shares=pos.shares,
            qty=pos.shares, ref_price=px, limit_price=round(px * (1 - limit_premium), 2),
            est_amount=round(px * pos.shares, 2),
            reason=pos.exit_reason or "信号离场", priority=0,
            name=names.get(sym, "")))
        cash += px * pos.shares * (1 - cfg["slippage"])

    # ── 买入（等权新建，预算 = min(单只上限, 剩余现金 / 剩余名额)）──
    single = cfg["capital"] * cfg["single_pct"]
    remaining_slots = max(cfg["max_positions"] - len(book.positions), 1)
    for i, sym in enumerate(dec.buys):
        if sym in book.positions:
            continue
        px = close_px.get(sym)
        if not px or px <= 0:
            out.items.append(IntentSpec(
                symbol=sym, side="BUY", target_shares=0, actual_shares=0, qty=0,
                ref_price=0.0, limit_price=0.0, est_amount=0.0,
                reason="新鲜信号×回调区", priority=100 + i,
                name=names.get(sym, ""), blocked="无当日收盘价"))
            continue
        budget = min(single, cash * (1 - buy_buffer) / remaining_slots)
        lot = lot_resolver(sym)
        shares = int(budget / (px * (1 + cfg["slippage"])) // lot * lot)
        est = round(shares * px, 2)
        if shares <= 0:
            out.items.append(IntentSpec(
                symbol=sym, side="BUY", target_shares=0, actual_shares=0, qty=0,
                ref_price=px, limit_price=round(px * (1 + limit_premium), 2),
                est_amount=0.0, reason="新鲜信号×回调区", priority=100 + i,
                name=names.get(sym, ""),
                blocked=f"预算 {budget:,.0f} 买不满 {lot} 股（股价 {px:.2f}）"))
            continue
        out.items.append(IntentSpec(
            symbol=sym, side="BUY", target_shares=shares, actual_shares=0, qty=shares,
            ref_price=px, limit_price=round(px * (1 + limit_premium), 2),
            est_amount=est, reason="新鲜信号×回调区", priority=100 + i,
            name=names.get(sym, "")))
        cash -= est
        remaining_slots = max(remaining_slots - 1, 1)

    out.cash_after_est = round(cash, 2)
    return out
