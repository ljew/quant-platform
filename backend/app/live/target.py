"""L3 组合意图：单日决策纯函数。

从 `services/xq/portfolio_fresh.py` 的全历史重演里抽出「一天的决策」，供两条线共用：

  重演（影子组合）  for d in dates: apply_open → decide_close → mark
  实盘（每日）      15:30 apply_open（用真实成交） → decide_close → mark

抽取的原因：原实现把「意图」和「成交」焊在一个循环里（假设 100% 按开盘价成交），
实盘做不到——涨停买不进、停牌卖不出、资金差几块钱。焊在一起就无法表达
「目标还在、实际没买到、次日重试」。

等价性是这个模块的唯一验收标准：`replay()` 必须与 `run_portfolio_fresh()` 逐日
净值、逐笔成交完全一致（tests/test_live_target.py）。

口径与回测严格一致：
- 成交价 = 次日（决策日 +1 交易日）**未复权开盘价**
- 持仓估值 = 当日**未复权收盘价**；停牌按成本估值
- 候选判定用 signal_actions 的 close/ma20（两者均为后复权，比值口径自洽）
- 成本：佣金万2.5(最低5) + 印花税万5(仅卖) + 过户费万0.1(双向) + 滑点万1（与主引擎对齐）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from app.core.cost_defaults import COMMISSION, MIN_COMMISSION, SLIPPAGE, STAMP_TAX

DEFAULT_CONFIG: dict = {
    "capital": 2_000_000,
    "max_positions": 20,
    "single_pct": 0.05,
    "stop_loss": -0.08,
    "trail_drawdown": -0.08,
    "max_hold_days": 10,
    "gate_window_days": 90,
    "gate_min_samples": 20,
    "commission_rate": COMMISSION,
    "commission_min": MIN_COMMISSION,
    "stamp_tax": STAMP_TAX,
    "transfer_fee": 0.00001,  # 过户费万 0.1（双向；xq 组合轨特有）
    "slippage": SLIPPAGE,     # 与主引擎对齐（W45-xq / P1-2）
    "lot_size": 100,
}


# ── 状态（可序列化，实盘落 live_position）──

@dataclass
class Position:
    symbol: str
    shares: int
    entry_price: float          # 未复权成交价
    entry_date: date
    peak: float                 # 持仓期最高收盘（未复权）
    exit_reason: str = ""


@dataclass
class Book:
    """账本：现金 + 持仓。实盘这条来自券商回报，重演这条由 apply_open 更新。"""
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)

    def n(self) -> int:
        return len(self.positions)


@dataclass
class Decision:
    """收盘决策：次日开盘执行。"""
    sells: list[str] = field(default_factory=list)
    buys: list[str] = field(default_factory=list)
    gate: dict = field(default_factory=dict)
    candidates: list[dict] = field(default_factory=list)


# ── 成本模型（与 portfolio.py / portfolio_fresh.py 同一套）──

def buy_cost(price: float, shares: int, cfg: dict) -> float:
    gross = round(price * (1 + cfg["slippage"]) * shares, 2)
    commission = max(gross * cfg["commission_rate"], cfg["commission_min"])
    transfer = gross * cfg["transfer_fee"]
    return round(gross + commission + transfer, 2)


def sell_proceeds(price: float, shares: int, cfg: dict) -> float:
    gross = round(price * (1 - cfg["slippage"]) * shares, 2)
    commission = max(gross * cfg["commission_rate"], cfg["commission_min"])
    stamp = gross * cfg["stamp_tax"]
    transfer = gross * cfg["transfer_fee"]
    return round(gross - commission - stamp - transfer, 2)


# ── 市场视图 ──

class Market:
    """未复权行情视图（px 需含 date/symbol/open/close 列）。"""

    def __init__(self, px: pd.DataFrame):
        self.px = px
        self.trade_dates: list[date] = sorted({d for d, _ in px.index})
        self.tid: dict[date, int] = {d: i for i, d in enumerate(self.trade_dates)}

    def price(self, d: date, symbol: str, col: str) -> float | None:
        try:
            v = self.px.loc[(d, symbol), col]
            return float(v.iloc[0]) if isinstance(v, pd.Series) else float(v)
        except KeyError:
            return None


# ── 门控（PIT）──

def gate_at(d: date, sig_dates: list[date], daily_exc: dict[date, float],
            market: Market, cfg: dict) -> dict:
    """只计入 T+10 窗口已成熟的信号日（s 后第 11 个交易日 <= d），无前视。"""
    i = market.tid.get(d)
    if i is None:
        return {"open": False, "exc": None, "n": 0}
    mature = [s for s in sig_dates
              if s in daily_exc and s in market.tid and market.tid[s] + 11 <= i]
    recent = mature[-cfg["gate_window_days"]:]
    if len(recent) < cfg["gate_min_samples"]:
        return {"open": False, "exc": None, "n": len(recent)}
    m = sum(daily_exc[s] for s in recent) / len(recent)
    return {"open": m > 0, "exc": m, "n": len(recent)}


# ── L3：单日决策 ──

def decide_close(d: date, book: Book, signals: dict, market: Market,
                 cfg: dict, *, rank_key=None) -> Decision:
    """收盘决策：① 风控卖出 ② 门控开则补候选 ③ 返回次日开盘动作。

    signals: {
        "act_map":    {date: {symbol: {streak, close, ma20, ...}}},
        "sig_strats": {date: {symbol: [strategy, ...]}},
        "enabled":    set[str],
        "daily_exc":  {date: float},
        "sig_dates":  list[date],
    }
    rank_key: 候选排序键，默认与回测一致（high_tight_flag 优先 + symbol 字典序）。
              实盘建议传「回调深度」排序，见 scripts/live_daily.py。

    注意：本函数**不修改 book**，只产出决策。持仓的 peak 由 mark() 更新。
    """
    gate = gate_at(d, signals["sig_dates"], signals["daily_exc"], market, cfg)
    dec = Decision(gate=gate)
    act_map = signals["act_map"].get(d, {})

    # ① 风控：硬止损 / 移动止损 / 时间止损（基于当日收盘，次日开盘执行）
    for sym, h in book.positions.items():
        close = market.price(d, sym, "close")
        if not close:
            continue                     # 停牌：不触发，按成本估值
        ret = close / h.entry_price - 1.0
        dd = close / max(h.peak, close) - 1.0
        held_td = (market.tid.get(d, 0) - market.tid.get(h.entry_date, 0))
        if ret <= cfg["stop_loss"]:
            dec.sells.append(sym)
            h.exit_reason = f"止损：浮亏 {ret * 100:.1f}%"
        elif dd <= cfg["trail_drawdown"]:
            dec.sells.append(sym)
            h.exit_reason = f"移动止损：自峰值回撤 {dd * 100:.1f}%"
        elif held_td >= cfg["max_hold_days"]:
            dec.sells.append(sym)
            h.exit_reason = f"时间止损：持有满 {cfg['max_hold_days']} 个交易日"

    # ② 门控开启才补新候选
    if gate["open"]:
        cands: list[tuple] = []
        for sym, act in act_map.items():
            if sym in book.positions:
                continue
            if not is_candidate(signals["sig_strats"].get(d, {}).get(sym, []), act,
                                signals["enabled"]):
                continue
            if rank_key is None:
                pri = 0 if "high_tight_flag" in signals["sig_strats"][d][sym] else 1
                key = (pri, sym)
            else:
                key = rank_key(sym, act)
            cands.append((key, sym, act))
        cands.sort(key=lambda x: x[0])
        for key, sym, act in cands:
            if len(book.positions) + len(dec.buys) >= cfg["max_positions"]:
                break
            dec.buys.append(sym)
            dec.candidates.append({
                "symbol": sym, "rank": str(key),
                "streak": act.get("streak"), "close": act.get("close"),
                "ma20": act.get("ma20"),
                "strategies": signals["sig_strats"].get(d, {}).get(sym, []),
            })
    return dec


def is_candidate(strats: list[str], act: dict, enabled: set[str]) -> bool:
    """fresh_ma 候选：enabled 策略的新鲜信号（streak<=2）且收盘处于回调区。"""
    if not any(s in enabled for s in strats):
        return False
    if act.get("streak", 0) > 2:
        return False
    close, ma20 = act.get("close"), act.get("ma20")
    return close is not None and ma20 is not None and close < ma20


# ── 开盘执行（下一交易日）──

def apply_open(d: date, book: Book, dec: Decision, market: Market,
               cfg: dict) -> list[dict]:
    """按开盘价执行上一交易日的决策。返回成交列表（与回测 trades 同结构）。

    实盘里本函数不用于「假设成交」，只用于影子组合；实盘成交来自 live_fill。
    """
    fills: list[dict] = []

    for sym in dec.sells:
        h = book.positions.get(sym)
        if h is None:
            continue
        price = market.price(d, sym, "open")
        if not price:
            continue                     # 停牌：卖不出，持仓保留（次日会重新决策）
        proceeds = sell_proceeds(price, h.shares, cfg)
        book.cash += proceeds
        fills.append({
            "date": d.isoformat(), "symbol": sym, "side": "SELL",
            "shares": h.shares, "price": round(price, 2), "proceeds": proceeds,
            "pnl_pct": round((price / h.entry_price - 1) * 100, 2),
            "reason": h.exit_reason or "信号离场",
        })
        del book.positions[sym]

    for sym in dec.buys:
        if sym in book.positions or len(book.positions) >= cfg["max_positions"]:
            continue
        price = market.price(d, sym, "open")
        if not price:
            continue
        remaining = cfg["max_positions"] - len(book.positions)
        target = min(cfg["capital"] * cfg["single_pct"], book.cash / max(remaining, 1))
        shares = int(target / (price * (1 + cfg["slippage"])) // cfg["lot_size"]
                     * cfg["lot_size"])
        if shares <= 0:
            continue
        cost = buy_cost(price, shares, cfg)
        if cost > book.cash:
            shares = int(book.cash / (price * (1 + cfg["slippage"])) // cfg["lot_size"]
                         * cfg["lot_size"])
            if shares <= 0:
                continue
            cost = buy_cost(price, shares, cfg)
        book.cash -= cost
        book.positions[sym] = Position(sym, shares, price, d, price)
        fills.append({
            "date": d.isoformat(), "symbol": sym, "side": "BUY",
            "shares": shares, "price": round(price, 2), "cost": cost,
            "reason": "新鲜信号×回调区（regime 门控开启）",
        })
    return fills


def mark(d: date, book: Book, market: Market) -> dict:
    """收盘估值 + 更新 peak。返回当日净值行（与回测 equity_curve 同结构）。"""
    mv = 0.0
    for sym, h in book.positions.items():
        close = market.price(d, sym, "close")
        if close:
            mv += close * h.shares
            h.peak = max(h.peak, close)
        else:
            mv += h.entry_price * h.shares       # 停牌按成本估值
    return {"date": d.isoformat(), "total": round(book.cash + mv, 2),
            "cash": round(book.cash, 2), "market_value": round(mv, 2),
            "n_positions": len(book.positions)}


# ── 重演（影子组合 / 等价性验收）──

def replay(signals: dict, market: Market, cfg: dict, *,
           rank_key=None) -> dict:
    """用「单日函数」重演整段历史。结果必须与 run_portfolio_fresh 完全一致。"""
    cfg = dict(DEFAULT_CONFIG, **cfg)
    book = Book(cash=float(cfg["capital"]))
    equity: list[dict] = []
    trades: list[dict] = []
    gate_log: list[dict] = []
    dec = Decision()

    for d in signals["sig_dates"]:
        trades.extend(apply_open(d, book, dec, market, cfg))
        dec = decide_close(d, book, signals, market, cfg, rank_key=rank_key)
        row = mark(d, book, market)
        row["gate_open"] = dec.gate["open"]
        equity.append(row)
        gate_log.append({"date": d.isoformat(), **dec.gate})

    total_end = equity[-1]["total"] if equity else cfg["capital"]
    peak, mdd = 0.0, 0.0
    for e in equity:
        peak = max(peak, e["total"])
        if peak:
            mdd = min(mdd, e["total"] / peak - 1.0)
    return {
        "equity": equity, "trades": trades, "gate_log": gate_log,
        "holdings": [{"symbol": s, "shares": h.shares,
                      "entry_price": h.entry_price,
                      "entry_date": h.entry_date.isoformat()}
                     for s, h in sorted(book.positions.items())],
        "summary": {
            "capital": cfg["capital"],
            "total": round(total_end, 2),
            "return_pct": round((total_end / cfg["capital"] - 1) * 100, 2),
            "max_drawdown_pct": round(mdd * 100, 2),
            "n_trades": len(trades),
            "n_holdings": len(book.positions),
            "days": len(equity),
            "gate_open_days": sum(1 for g in gate_log if g["open"]),
        },
    }
