"""L7 对账：成交回报 → 持仓 → 净值 → 执行质量。

实盘系统里唯一不可替代的一环。理由：
- 回测/影子组合的持仓是模型推出来的，可以重算；
- 实盘持仓只能由**成交回报**决定。混用会让「未成交」变成账本错乱。

所以本模块的铁律：`live_position` 只能由成交与公司行动事件重建，其他任何地方只读。

三个度量（判断系统健康，而不是判断赚没赚钱）：
  滑点     fill.price / intent.ref_price − 1     → 执行成本
  成交率   filled_qty / intent.qty               → 未成交比例（涨停/停牌/资金）
  漂移     live 累计收益 − shadow 累计收益        → 系统性问题

滑点稳定在成本量级 = 健康；成交率骤降 = 有系统性买不进；漂移扩大 = 数据或执行出错。
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live import STRATEGY_KEY
from app.models import (LiveCorporateAction, LiveEquity, LiveFill, LiveOrderIntent,
                        LivePosition, PortfolioExpState)


# ── 事件账本：成交 + 公司行动 → 持仓与现金 ──

def _replay_ledger(db: Session, strategy_key: str,
                   as_of: date | None = None) -> tuple[dict[str, dict], float]:
    """按事件顺序重建持仓，并返回相对初始本金的现金变化。

    同日公司行动先于成交处理；卖出校验使用总持仓扣除当日买入，落实 A 股 T+1。
    """
    fills = db.scalars(select(LiveFill).where(
        LiveFill.strategy_key == strategy_key,
        LiveFill.trade_date <= as_of if as_of else True,
    ).order_by(LiveFill.trade_date, LiveFill.created_at, LiveFill.id)).all()
    actions = db.scalars(select(LiveCorporateAction).where(
        LiveCorporateAction.strategy_key == strategy_key,
        LiveCorporateAction.trade_date <= as_of if as_of else True,
    ).order_by(LiveCorporateAction.trade_date, LiveCorporateAction.created_at,
               LiveCorporateAction.id)).all()
    events = ([(a.trade_date, 0, a.created_at, a.id, "action", a) for a in actions]
              + [(f.trade_date, 1, f.created_at, f.id, "fill", f) for f in fills])
    events.sort(key=lambda x: x[:4])

    states: dict[str, dict] = {}
    cash_delta = 0.0
    for _, _, _, _, kind, event in events:
        sym = event.symbol
        if kind == "action":
            st = states.get(sym)
            if st is None:
                continue
            cash_delta += st["shares"] * event.cash_dividend_per_share
            ratio = float(event.share_ratio)
            new_shares = st["shares"] * ratio
            rounded = round(new_shares)
            if abs(new_shares - rounded) > 1e-6:
                raise ValueError(f"{sym} 公司行动产生非整股持仓，请人工处理零股")
            st["shares"] = int(rounded)
            st["avg_cost"] /= ratio
            st["peak_close"] /= ratio
            if st["shares"] == 0:
                states.pop(sym, None)
            continue

        fee = (event.commission or 0) + (event.stamp_tax or 0) + (event.transfer_fee or 0)
        cash_delta += (-event.amount - fee) if event.side == "BUY" else (event.amount - fee)
        st = states.get(sym)
        if event.side == "BUY":
            if st is None:
                st = {"shares": 0, "avg_cost": 0.0, "entry_date": event.trade_date,
                      "peak_close": event.price, "name": "", "buy_by_date": {}}
                states[sym] = st
            total_cost = st["avg_cost"] * st["shares"] + event.price * event.qty
            st["shares"] += event.qty
            st["avg_cost"] = total_cost / st["shares"]
            st["peak_close"] = max(st["peak_close"], event.price)
            st["buy_by_date"][event.trade_date] = (
                st["buy_by_date"].get(event.trade_date, 0) + event.qty)
            continue

        if st is None or st["shares"] <= 0:
            raise ValueError(f"卖出 {sym} 但无持仓记录（对账异常，需人工核查）")
        sellable = st["shares"] - st["buy_by_date"].get(event.trade_date, 0)
        if event.qty > sellable:
            raise ValueError(f"{sym} 可卖数量不足：T+1 可卖 {sellable}，卖出 {event.qty}")
        st["shares"] -= event.qty
        if st["shares"] == 0:
            states.pop(sym, None)
    return states, cash_delta


def _sync_positions(db: Session, strategy_key: str) -> None:
    states, _ = _replay_ledger(db, strategy_key)
    existing = {p.symbol: p for p in db.scalars(select(LivePosition).where(
        LivePosition.strategy_key == strategy_key)).all()}
    for sym, pos in existing.items():
        if sym not in states:
            db.delete(pos)
    for sym, st in states.items():
        pos = existing.get(sym)
        if pos is None:
            pos = LivePosition(strategy_key=strategy_key, symbol=sym)
            db.add(pos)
        peak = st["peak_close"]
        if pos.entry_date == st["entry_date"] and pos.peak_close:
            peak = max(peak, pos.peak_close)
        pos.shares = st["shares"]
        pos.avg_cost = round(st["avg_cost"], 4)
        pos.entry_date = st["entry_date"]
        pos.peak_close = round(peak, 4)
        pos.name = pos.name or st["name"]
    db.flush()


def cash_from_fills(db: Session, *, capital: float,
                    strategy_key: str = STRATEGY_KEY,
                    as_of: date | None = None) -> float:
    """由完整事件账本推导现金；函数名保留以兼容既有调用。"""
    _, delta = _replay_ledger(db, strategy_key, as_of)
    return round(capital + delta, 2)


def _find_external_fill(db: Session, strategy_key: str, source: str,
                        external_id: str | None) -> LiveFill | None:
    if not external_id:
        return None
    return db.scalars(select(LiveFill).where(
        LiveFill.strategy_key == strategy_key,
        LiveFill.source == source,
        LiveFill.external_id == external_id,
    )).first()


def apply_fill(db: Session, *, trade_date: date, symbol: str, side: str,
               qty: int, price: float, commission: float = 0.0,
               stamp_tax: float = 0.0, transfer_fee: float = 0.0,
               intent_id: int | None = None, source: str = "manual",
               external_id: str | None = None,
               note: str = "", name: str = "",
               strategy_key: str = STRATEGY_KEY) -> LiveFill:
    """写入一笔成交事件并重建持仓；外部标识重复时直接返回原事件。"""
    if side not in ("BUY", "SELL"):
        raise ValueError(f"非法成交方向：{side}")
    if qty <= 0 or price <= 0:
        raise ValueError("成交数量和价格必须为正数")
    existing = _find_external_fill(db, strategy_key, source, external_id)
    if existing is not None:
        return existing

    fill = LiveFill(
        intent_id=intent_id, trade_date=trade_date, strategy_key=strategy_key,
        symbol=symbol, side=side, qty=qty, price=price,
        amount=round(qty * price, 2), commission=commission,
        stamp_tax=stamp_tax, transfer_fee=transfer_fee, source=source,
        external_id=external_id, note=note)
    db.add(fill)
    db.flush()

    if intent_id:
        it = db.get(LiveOrderIntent, intent_id)
        if it:
            if it.symbol != symbol or it.side != side:
                raise ValueError("成交与订单意图的股票或方向不一致")
            new_qty = it.filled_qty + qty
            if new_qty > it.qty:
                raise ValueError(f"累计成交 {new_qty} 超过意图数量 {it.qty}")
            it.filled_price = ((it.filled_price * it.filled_qty + price * qty) / new_qty)
            it.filled_qty = new_qty
            it.status = "FILLED" if new_qty == it.qty else "PARTIAL"

    _sync_positions(db, strategy_key)
    return fill


def apply_corporate_action(
    db: Session, *, trade_date: date, symbol: str, share_ratio: float = 1.0,
    cash_dividend_per_share: float = 0.0, source: str = "manual",
    external_id: str | None = None, note: str = "",
    strategy_key: str = STRATEGY_KEY,
) -> LiveCorporateAction:
    """录入公司行动；送拆股调整股数/成本/峰值，现金分红进入现金账本。"""
    if share_ratio <= 0 or cash_dividend_per_share < 0:
        raise ValueError("送拆股比例必须为正，现金分红不得为负")
    if external_id:
        existing = db.scalars(select(LiveCorporateAction).where(
            LiveCorporateAction.strategy_key == strategy_key,
            LiveCorporateAction.source == source,
            LiveCorporateAction.external_id == external_id,
        )).first()
        if existing is not None:
            return existing
    action = LiveCorporateAction(
        trade_date=trade_date, strategy_key=strategy_key, symbol=symbol,
        share_ratio=share_ratio, cash_dividend_per_share=cash_dividend_per_share,
        source=source, external_id=external_id, note=note)
    db.add(action)
    db.flush()
    # 保留逐日 mark 形成的历史峰值；先按同一比例折算，再由事件账本重建其余字段。
    pos = db.scalars(select(LivePosition).where(
        LivePosition.strategy_key == strategy_key,
        LivePosition.symbol == symbol,
    )).first()
    if pos is not None and share_ratio != 1.0:
        pos.peak_close = (pos.peak_close or pos.avg_cost) / share_ratio
    _sync_positions(db, strategy_key)
    return action


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
        for row_no, row in enumerate(csv.DictReader(f), start=2):
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

            # 优先使用券商成交编号。缺失时以“文件行号 + 完整行内容”生成稳定 ID：
            # 相同文件重导不会重复；两行完全相同的真实成交也不会被误删。
            external_id = pick("成交编号", "合同编号", "fill_id", "external_id")
            if not external_id:
                canonical = json.dumps(low, ensure_ascii=False, sort_keys=True)
                digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
                external_id = f"row-{row_no}-{digest}"
            if _find_external_fill(db, strategy_key, "csv", external_id):
                skip += 1
                continue

            apply_fill(db, trade_date=date.fromisoformat(d[:10]), symbol=sym,
                       side=side, qty=qty, price=price,
                       commission=float(pick("佣金", "commission", default="0") or 0),
                       stamp_tax=float(pick("印花税", "stamp_tax", default="0") or 0),
                       transfer_fee=float(pick("过户费", "transfer_fee", default="0") or 0),
                       source="csv", external_id=external_id, strategy_key=strategy_key)
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

    by_key: dict[tuple[str, str], list[LiveFill]] = {}
    for f in fills:
        by_key.setdefault((f.symbol, f.side), []).append(f)

    rows, slipped, missed = [], [], []
    for it in intents:
        if it.status == "REJECTED":
            continue
        # 同票同日可能同时存在买卖；只归集同方向且属于该意图（或尚未关联）的成交。
        fs = [f for f in by_key.get((it.symbol, it.side), [])
              if f.intent_id is None or f.intent_id == it.id]
        filled = sum(f.qty for f in fs)
        if it.ref_price:
            avg_px = (sum(f.qty * f.price for f in fs) / filled) if filled else 0.0
            # 统一为“正值=执行更差”：买入越高越差，卖出越低越差。
            slip = ((avg_px / it.ref_price - 1.0) * (1 if it.side == "BUY" else -1)
                    if avg_px else None)
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

    from statistics import median
    n = len(rows)
    return {
        "trade_date": trade_date.isoformat(),
        "n_intents": n,
        "n_fills": len(fills),
        "fill_rate": round(sum(r["fill_rate"] for r in rows) / n, 4) if n else None,
        "slippage_mean_pct": round(sum(slipped) / len(slipped) * 100, 3) if slipped else None,
        "slippage_median_pct": round(median(slipped) * 100, 3) if slipped else None,
        "slippage_worst_pct": round(max(slipped) * 100, 3) if slipped else None,
        "missed": missed,
        "rows": rows,
    }


# ── 净值 ──

def mark_equity(db: Session, trade_date: date, close_px: dict[str, float],
                *, capital: float, strategy_key: str = STRATEGY_KEY) -> LiveEquity:
    """按截至当日的完整事件账本重算净值；同日重复执行与历史补算均幂等。"""
    states, cash_delta = _replay_ledger(db, strategy_key, trade_date)
    cash = round(capital + cash_delta, 2)
    mv = 0.0
    for sym, st in states.items():
        px = close_px.get(sym) or st["avg_cost"]
        mv += px * st["shares"]
        pos = db.scalars(select(LivePosition).where(
            LivePosition.strategy_key == strategy_key,
            LivePosition.symbol == sym,
        )).first()
        if pos is not None and (pos.last_mark_date is None or pos.last_mark_date <= trade_date):
            pos.peak_close = max(pos.peak_close or 0.0, px)
            pos.last_mark_date = trade_date

    total = round(cash + mv, 2)
    shadow = db.scalars(select(PortfolioExpState).where(
        PortfolioExpState.as_of_date <= trade_date,
    ).order_by(PortfolioExpState.as_of_date.desc()).limit(1)).first()
    shadow_total, div = None, None
    if shadow:
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
    row.cash = cash
    row.market_value = round(mv, 2)
    row.total = total
    row.n_positions = len(states)
    row.shadow_total = shadow_total
    row.divergence_pct = div
    db.commit()
    return row
