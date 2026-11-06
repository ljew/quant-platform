"""镜像组合引擎（Sequoia-X watchlist.py 核心迁移，2026-09-30）。

按信号自动模拟的**真实成本**组合（非回测、非实盘）：
- 建仓：标的进入 BUY_STRONG 名单 → 次一交易日开盘买入（T+1，防前视）；
  等权单只 ≤5% 初始资金，每笔金额 = min(5%×初始资金, 可用现金÷剩余名额)，
  按 100 股整手取整
- 持有白名单 HOLD_ACTIONS = {BUY_STRONG, WATCH}：降级到 NEW/REDUCE/SELL/EXIT
  即出局（次日开盘卖出）
- 风控：收盘价自建仓浮亏 ≥10% 止损、浮盈 ≥30% 止盈（收盘触发，次日开盘执行）
- 成本（与原实现一致）：佣金万 2.5（最低 5 元，双向）+ 印花税万 5（仅卖出）
  + 过户费万 0.1（双向）+ 滑点 0.1%（买入向上取整、卖出向下取整，按分）

历史重演：以 signal_actions 全历史为驱动（time_travel 语义天然成立），
跑完输出净值曲线 / 持仓 / 成交流水，落 portfolio_state（同日覆盖）。
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PortfolioState, SignalAction
from app.services.xq.data import Panel, load_panel

DEFAULT_CONFIG: dict = {
    "capital": 2_000_000,        # 初始资金（与 Sequoia 一致 200 万）
    "take_profit": 0.30,         # 止盈：自建仓浮盈 ≥30%
    "stop_loss": -0.10,          # 止损：自建仓浮亏 ≥10%
    "max_positions": 30,         # 最大同时持仓
    "single_pct": 0.05,          # 单只 ≤5% 初始资金
    "commission_rate": 0.00025,  # 佣金万 2.5
    "commission_min": 5.0,       # 佣金最低 5 元
    "stamp_tax": 0.0005,         # 印花税万 5（仅卖出）
    "transfer_fee": 0.00001,     # 过户费万 0.1（双向）
    "slippage": 0.001,           # 滑点 0.1%
    "lot_size": 100,             # 整手
}
HOLD_ACTIONS = {"BUY_STRONG", "WATCH"}
EXIT_ACTIONS = {"NEW", "REDUCE", "SELL", "EXIT"}


def _buy_cost(price: float, shares: int, cfg: dict) -> float:
    """买入成交额 + 费用（滑点向上取整到分）。"""
    gross = round(price * (1 + cfg["slippage"]) * shares, 2)
    commission = max(gross * cfg["commission_rate"], cfg["commission_min"])
    transfer = gross * cfg["transfer_fee"]
    return round(gross + commission + transfer, 2)


def _sell_proceeds(price: float, shares: int, cfg: dict) -> float:
    """卖出净入（滑点向下取整到分）。"""
    gross = round(price * (1 - cfg["slippage"]) * shares, 2)
    commission = max(gross * cfg["commission_rate"], cfg["commission_min"])
    stamp = gross * cfg["stamp_tax"]
    transfer = gross * cfg["transfer_fee"]
    return round(gross - commission - stamp - transfer, 2)


def run_portfolio(db: Session, duckdb_path: str, lookback: int = 260) -> dict:
    """按 signal_actions 全历史重演镜像组合，落 portfolio_state。"""
    actions = db.scalars(
        select(SignalAction).order_by(SignalAction.date, SignalAction.action)
    ).all()
    if not actions:
        return {"ok": False, "error": "暂无信号历史"}

    cfg = dict(DEFAULT_CONFIG)
    panel: Panel = load_panel(duckdb_path, lookback=lookback)
    if panel.df.empty:
        return {"ok": False, "error": "行情面板为空"}
    px = panel.df.set_index(["date", "symbol"])[
        ["open", "close", "o_hfq", "c_hfq"]].sort_index()

    dates = sorted({(a.date if isinstance(a.date, date) else a.date) for a in actions})
    act_map: dict[date, dict[str, str]] = {}
    for a in actions:
        d = a.date if isinstance(a.date, date) else a.date
        act_map.setdefault(d, {})[a.symbol] = a.action

    cash = float(cfg["capital"])
    holdings: dict[str, dict] = {}   # symbol → {shares, entry_price, entry_date, cost}
    pending_buys: list[str] = []
    pending_sells: list[str] = []
    equity_curve: list[dict] = []
    trades: list[dict] = []
    buy_dates: dict[str, date] = {}  # 标的 → 触发买入的信号日（防重复建仓）

    def _px(d: date, sym: str, col: str) -> float | None:
        try:
            v = px.loc[(d, sym), col]
            return float(v.iloc[0]) if isinstance(v, pd.Series) else float(v)
        except KeyError:
            return None

    for d in dates:
        a_today = act_map.get(d, {})

        # ── 1) 开盘执行昨日挂单 ──
        for sym in list(pending_sells):
            if sym not in holdings:
                continue
            price = _px(d, sym, "open")
            h = holdings[sym]
            if price:
                proceeds = _sell_proceeds(price, h["shares"], cfg)
                cash += proceeds
                trades.append({
                    "date": d.isoformat(), "symbol": sym, "side": "SELL",
                    "shares": h["shares"], "price": round(price, 2),
                    "proceeds": proceeds,
                    "pnl_pct": round((price / h["entry_price"] - 1) * 100, 2),
                    "reason": h.get("exit_reason", "信号离场"),
                })
                del holdings[sym]
        pending_sells.clear()

        for sym in list(pending_buys):
            if sym in holdings or len(holdings) >= cfg["max_positions"]:
                continue
            price = _px(d, sym, "open")
            if not price:
                continue
            remaining = cfg["max_positions"] - len(holdings)
            target = min(cfg["capital"] * cfg["single_pct"],
                         cash / max(remaining, 1))
            shares = int(target / (price * (1 + cfg["slippage"]))
                         // cfg["lot_size"] * cfg["lot_size"])
            if shares <= 0:
                continue
            cost = _buy_cost(price, shares, cfg)
            if cost > cash:
                shares = int(cash / (price * (1 + cfg["slippage"]))
                             // cfg["lot_size"] * cfg["lot_size"])
                if shares <= 0:
                    continue
                cost = _buy_cost(price, shares, cfg)
            cash -= cost
            holdings[sym] = {"shares": shares, "entry_price": price,
                             "entry_date": d.isoformat(), "cost": cost}
            buy_dates[sym] = d
            trades.append({
                "date": d.isoformat(), "symbol": sym, "side": "BUY",
                "shares": shares, "price": round(price, 2), "cost": cost,
                "reason": "进入重点买入名单，次日开盘建仓",
            })
        pending_buys.clear()

        # ── 2) 收盘更新市值 + 生成风控/信号挂单 ──
        market_value = 0.0
        for sym, h in list(holdings.items()):
            close = _px(d, sym, "close")
            if close:
                market_value += close * h["shares"]
                ret = close / h["entry_price"] - 1.0
                if ret <= cfg["stop_loss"]:
                    h["exit_reason"] = f"止损：浮亏 {ret*100:.1f}%"
                    pending_sells.append(sym)
                elif ret >= cfg["take_profit"]:
                    h["exit_reason"] = f"止盈：浮盈 {ret*100:.1f}%"
                    pending_sells.append(sym)
            else:
                market_value += h["entry_price"] * h["shares"]  # 停牌按成本估值

        total = cash + market_value
        equity_curve.append({
            "date": d.isoformat(), "total": round(total, 2),
            "cash": round(cash, 2), "market_value": round(market_value, 2),
            "n_positions": len(holdings),
        })

        # ── 3) 当日动作 → 明日挂单 ──
        for sym, action in a_today.items():
            held = sym in holdings
            if held and action in EXIT_ACTIONS:
                h = holdings[sym]
                h["exit_reason"] = f"信号降级（{action}）"
                pending_sells.append(sym)
            elif (not held and action == "BUY_STRONG"
                  and sym not in pending_buys and sym not in buy_dates):
                pending_buys.append(sym)

    total_end = equity_curve[-1]["total"] if equity_curve else cfg["capital"]
    curve_vals = [e["total"] for e in equity_curve]
    peak, mdd = 0.0, 0.0
    for v in curve_vals:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0) if peak else 0.0

    holdings_out = [
        {"symbol": s, "shares": h["shares"], "entry_price": h["entry_price"],
         "entry_date": h["entry_date"]}
        for s, h in sorted(holdings.items())
    ]
    summary = {
        "capital": cfg["capital"],
        "total": round(total_end, 2),
        "return_pct": round((total_end / cfg["capital"] - 1) * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "n_trades": len(trades),
        "n_holdings": len(holdings),
        "days": len(equity_curve),
    }

    # ── 落库（按数据日 upsert）──
    as_of = dates[-1] if dates else date.today()
    db.query(PortfolioState).filter(PortfolioState.as_of_date == as_of).delete()
    db.add(PortfolioState(
        as_of_date=as_of,
        config_json=json.dumps(cfg, ensure_ascii=False),
        summary_json=json.dumps(summary, ensure_ascii=False),
        equity_json=json.dumps(equity_curve, ensure_ascii=False),
        holdings_json=json.dumps(holdings_out, ensure_ascii=False),
        trades_json=json.dumps(trades[-500:], ensure_ascii=False),
    ))
    db.commit()
    return {"ok": True, "as_of": as_of.isoformat(), "summary": summary,
            "equity": equity_curve, "holdings": holdings_out,
            "trades": trades[-50:]}
