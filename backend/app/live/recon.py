"""L7 对账：成交回报 → 持仓 → 净值 → 执行质量。

实盘系统里唯一不可替代的一环。理由：
- 回测/影子组合的持仓是模型推出来的，可以重算；
- 实盘持仓只能由**成交回报**决定。混用会让「未成交」变成账本错乱。

所以本模块的铁律：`live_position` 只允许由 `apply_fill` 写，其他任何地方只读。

三个度量（判断系统健康，而不是判断赚没赚钱）：
  滑点     fill.price / intent.ref_price − 1     → 执行成本
  成交率   filled_qty / intent.qty               → 未成交比例（涨停/停牌/资金）
  漂移     live 累计收益 − shadow 累计收益        → 系统性问题

滑点稳定在成本量级 = 健康；成交率骤降 = 有系统性买不进；漂移扩大 = 数据或执行出错。
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live import STRATEGY_KEY
from app.models import (LiveEquity, LiveFill, LiveOrderIntent, LivePosition,
                        PortfolioExpState)


# ── 成交 → 持仓 ──

def apply_fill(db: Session, *, trade_date: date, symbol: str, side: str,
               qty: int, price: float, commission: float = 0.0,
               stamp_tax: float = 0.0, transfer_fee: float = 0.0,
               intent_id: int | None = None, source: str = "manual",
               note: str = "", name: str = "",
               strategy_key: str = STRATEGY_KEY) -> LiveFill:
    """记一笔成交，并**同步**更新持仓。这是唯一允许改 live_position 的入口。

    同一 intent 重复录入会重复加仓——调用方需保证幂等（导入对账单前先按
    (trade_date,symbol,side,qty) 查重，见 import_fills_csv）。
    """
    fill = LiveFill(
        intent_id=intent_id, trade_date=trade_date, strategy_key=strategy_key,
        symbol=symbol, side=side, qty=qty, price=price,
        amount=round(qty * price, 2), commission=commission,
        stamp_tax=stamp_tax, transfer_fee=transfer_fee, source=source, note=note)
    db.add(fill)

    pos = db.scalars(select(LivePosition).where(
        LivePosition.strategy_key == strategy_key,
        LivePosition.symbol == symbol)).first()

    if side == "BUY":
        if pos is None:
            pos = LivePosition(strategy_key=strategy_key, symbol=symbol, name=name,
                               shares=qty, avg_cost=price, entry_date=trade_date,
                               peak_close=price)
            db.add(pos)
        else:
            total_cost = pos.avg_cost * pos.shares + price * qty
            pos.shares += qty
            pos.avg_cost = round(total_cost / pos.shares, 4) if pos.shares else 0.0
            if pos.entry_date is None:
                pos.entry_date = trade_date
            pos.peak_close = max(pos.peak_close or 0.0, price)
    else:
        if pos is None:
            raise ValueError(f"卖出 {symbol} 但无持仓记录（对账异常，需人工核查）")
        pos.shares -= qty
        if pos.shares <= 0:
            db.delete(pos)
        # 卖出不改 avg_cost / entry_date（剩余仓位成本不变）

    # intent 回填
    if intent_id:
        it = db.get(LiveOrderIntent, intent_id)
        if it:
            it.filled_qty += qty
            it.filled_price = price
            it.status = "FILLED" if it.filled_qty >= it.qty else "PARTIAL"

    db.flush()
    return fill


def import_fills_csv(db: Session, path: str | Path, *, trade_date: date | None = None,
                     strategy_key: str = STRATEGY_KEY) -> dict:
    """从券商对账单 CSV 导入成交。

    兼容列名（大小写不敏感，缺列按 0 处理）：
        日期/成交日期 date, 证券代码/代码 symbol, 买卖标志/操作 side,
        成交数量/数量 qty, 成交价格/价格 price, 佣金 commission,
        印花税 stamp_tax, 过户费 transfer_fee

    side 取值兼容：买/BUY/B/证券买入 → BUY；卖/SELL/S/证券卖出 → SELL
    """
    path = Path(path)
    ok = skip = 0
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            low = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}

            def pick(*keys, default=""):
                for k in keys:
                    if k in low and low[k] != "":
                        return low[k]
                return default

            raw_side = pick("买卖标志", "操作", "side", "方向").upper()
            side = "BUY" if raw_side[:1] in ("买", "B") or raw_side.startswith("证券买") else "SELL"
            sym = pick("证券代码", "代码", "symbol", "股票代码")
            if not sym:
                skip += 1
                continue
            sym = _normalize_symbol(sym)
            d = pick("日期", "成交日期", "date") or (trade_date.isoformat() if trade_date else "")
            qty = int(float(pick("成交数量", "数量", "qty", default="0") or 0))
            price = float(pick("成交价格", "价格", "price", default="0") or 0)
            if qty <= 0 or price <= 0 or not d:
                skip += 1
                continue

            # 幂等：同日同票同向同量同价视为已导入
            dup = db.scalars(select(LiveFill).where(
                LiveFill.strategy_key == strategy_key,
                LiveFill.trade_date == date.fromisoformat(d[:10]),
                LiveFill.symbol == sym, LiveFill.side == side,
                LiveFill.qty == qty, LiveFill.price == price)).first()
            if dup:
                skip += 1
                continue

            apply_fill(db, trade_date=date.fromisoformat(d[:10]), symbol=sym,
                       side=side, qty=qty, price=price,
                       commission=float(pick("佣金", "commission", default="0") or 0),
                       stamp_tax=float(pick("印花税", "stamp_tax", default="0") or 0),
                       transfer_fee=float(pick("过户费", "transfer_fee", default="0") or 0),
                       source="csv", strategy_key=strategy_key)
            ok += 1
    db.commit()
    return {"ok": ok, "skip": skip}


def _normalize_symbol(code: str) -> str:
    code = code.strip().lower()
    if code.startswith(("sh", "sz", "bj")):
        return code
    code = code.zfill(6)
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return code


# ── 执行质量 ──

def reconcile(db: Session, trade_date: date,
              strategy_key: str = STRATEGY_KEY) -> dict:
    """对比当日「意图 vs 成交」，产出滑点/成交率。"""
    intents = db.scalars(select(LiveOrderIntent).where(
        LiveOrderIntent.trade_date == trade_date,
        LiveOrderIntent.strategy_key == strategy_key)).all()
    fills = db.scalars(select(LiveFill).where(
        LiveFill.trade_date == trade_date,
        LiveFill.strategy_key == strategy_key)).all()

    by_sym: dict[str, list[LiveFill]] = {}
    for f in fills:
        by_sym.setdefault(f.symbol, []).append(f)

    rows, slipped, missed = [], [], []
    for it in intents:
        if it.status == "REJECTED":
            continue
        fs = by_sym.get(it.symbol, [])
        filled = sum(f.qty for f in fs)
        if it.ref_price:
            avg_px = (sum(f.qty * f.price for f in fs) / filled) if filled else 0.0
            slip = (avg_px / it.ref_price - 1.0) if avg_px else None
        else:
            avg_px, slip = 0.0, None
        rate = filled / it.qty if it.qty else 0.0
        rows.append({
            "symbol": it.symbol, "side": it.side, "intent_qty": it.qty,
            "filled_qty": filled, "fill_rate": round(rate, 4),
            "ref_price": it.ref_price, "avg_price": round(avg_px, 3),
            "slippage_pct": None if slip is None else round(slip * 100, 3),
            "status": it.status,
        })
        if slip is not None:
            slipped.append(slip)
        if rate < 0.999:
            missed.append({"symbol": it.symbol, "intent": it.qty, "filled": filled})

    n = len(rows)
    return {
        "trade_date": trade_date.isoformat(),
        "n_intents": n,
        "n_fills": len(fills),
        "fill_rate": round(sum(r["fill_rate"] for r in rows) / n, 4) if n else None,
        "slippage_mean_pct": round(sum(slipped) / len(slipped) * 100, 3) if slipped else None,
        "slippage_median_pct": (round(sorted(slipped)[len(slipped) // 2] * 100, 3)
                                if slipped else None),
        "slippage_worst_pct": round(min(slipped) * 100, 3) if slipped else None,
        "missed": missed,
        "rows": rows,
    }


# ── 净值 ──

def mark_equity(db: Session, trade_date: date, close_px: dict[str, float],
                *, capital: float, strategy_key: str = STRATEGY_KEY) -> LiveEquity:
    """记当日净值，并与影子组合（portfolio_exp_state）对比。"""
    positions = db.scalars(select(LivePosition).where(
        LivePosition.strategy_key == strategy_key)).all()

    mv = 0.0
    for p in positions:
        px = close_px.get(p.symbol) or p.avg_cost
        mv += px * p.shares
        p.peak_close = max(p.peak_close or 0.0, px)
        p.last_mark_date = trade_date

    last = db.scalars(select(LiveEquity).where(
        LiveEquity.strategy_key == strategy_key)
        .order_by(LiveEquity.trade_date.desc()).limit(1)).first()
    cash = last.cash if last else capital
    # 现金由成交现金流维护：这里用「上日现金 + 当日卖出回笼 − 当日买入支出」
    fills = db.scalars(select(LiveFill).where(
        LiveFill.trade_date == trade_date,
        LiveFill.strategy_key == strategy_key)).all()
    for f in fills:
        fee = (f.commission or 0) + (f.stamp_tax or 0) + (f.transfer_fee or 0)
        cash += (-f.amount - fee) if f.side == "BUY" else (f.amount - fee)

    total = round(cash + mv, 2)

    shadow = db.scalars(select(PortfolioExpState)
                        .order_by(PortfolioExpState.as_of_date.desc()).limit(1)).first()
    shadow_total, div = None, None
    if shadow:
        import json
        s = json.loads(shadow.summary_json or "{}")
        shadow_total = s.get("total")
        if shadow_total and s.get("capital"):
            div = round((total / capital - shadow_total / s["capital"]) * 100, 3)

    row = db.scalars(select(LiveEquity).where(
        LiveEquity.trade_date == trade_date,
        LiveEquity.strategy_key == strategy_key)).first()
    if row is None:
        row = LiveEquity(trade_date=trade_date, strategy_key=strategy_key)
        db.add(row)
    row.cash = round(cash, 2)
    row.market_value = round(mv, 2)
    row.total = total
    row.n_positions = len(positions)
    row.shadow_total = shadow_total
    row.divergence_pct = div
    db.commit()
    return row
