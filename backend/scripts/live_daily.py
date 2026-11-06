#!/usr/bin/env python
"""实盘每日流水线（L1 校验 → L3 决策 → L4 订单意图 → 落库）。

用法（backend/ 下）：
    python scripts/live_daily.py --capital 400000              # 出今天的清单并落库
    python scripts/live_daily.py --capital 400000 --dry-run     # 不写库
    python scripts/live_daily.py --capital 400000 --rank pullback
    python scripts/live_daily.py --date 2026-10-27 --capital 400000   # 补算历史某天

设计约束：
1. **数据不合格就不出单**。宁可空跑，不可错跑——这是实盘第一条铁律。
   `trade_date == today` 不能当判据（本项目实测出现过 08:00 就写入当日 K 线），
   必须校验覆盖度/OHLC 合法性/成交额量级。
2. 幂等。同一天重跑只更新不新增（UNIQUE 键兜底）。
3. 决策快照先落库，再出订单。事后复盘只看快照，不看实时状态。

不做什么：
- 不下单。人工执行（合规：个人不得外接券商 API），或由 PTrade 内跑的策略自行下单。
- 不撮合。成交只能来自 live_fill（人工录入 / 对账单导入）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import delete, select                          # noqa: E402
from sqlalchemy.orm import Session                              # noqa: E402

from app.config import DATA_DIR                                 # noqa: E402
from app.database import SessionLocal, init_db                  # noqa: E402
from app.live import STRATEGY_KEY                              # noqa: E402
from app.live.intent import build_intents                       # noqa: E402
from app.live.signals import load_signals                       # noqa: E402
from app.live.target import (Book, DEFAULT_CONFIG, Market,      # noqa: E402
                             Position, decide_close)
from app.models import (LiveDecisionSnapshot, LiveOrderIntent,  # noqa: E402
                        LivePosition, Stock)
from app.services.xq.data import load_panel                     # noqa: E402

DUCKDB = str(DATA_DIR / "quant.duckdb")


# ── L1 数据完整性闸门 ──

def check_data(db: Session, target_date: date) -> dict:
    """出单前的硬校验。任一失败 → data_ok=0，不生成订单。"""
    from sqlalchemy import text
    conn = db.connection()
    rows = conn.execute(text(
        "select trade_date, count(*) n, sum(amount) amt, "
        "sum(case when high < max(open, close) or low > min(open, close) "
        "         or close <= 0 or volume <= 0 then 1 else 0 end) bad "
        "from kline_daily where trade_date <= :d "
        "group by trade_date order by trade_date desc limit 21"
    ), {"d": target_date.isoformat()}).fetchall()
    if not rows:
        return {"ok": False, "note": "无 K 线数据"}

    today = rows[0]
    if str(today[0]) != target_date.isoformat():
        return {"ok": False, "note": f"当日无 K 线（最新 {today[0]}）"}

    n, amt, bad = int(today[1]), float(today[2] or 0), int(today[3] or 0)
    prev_n = int(rows[1][1]) if len(rows) > 1 else n
    avg_amt = sum(float(r[2] or 0) for r in rows[1:21]) / max(len(rows[1:21]), 1)

    notes, ok = [], True
    cover = n / prev_n if prev_n else 1.0
    if cover < 0.98:
        ok = False
        notes.append(f"覆盖度 {cover:.1%}（{n}/{prev_n}）< 98%")
    if bad:
        ok = False
        notes.append(f"{bad} 只 OHLC 非法")
    if avg_amt and not (0.5 <= amt / avg_amt <= 2.0):
        ok = False
        notes.append(f"成交额 {amt/1e5:,.0f}亿 偏离 20 日均值 {avg_amt/1e5:,.0f}亿 超过 2 倍")
    if not notes:
        notes.append(f"覆盖 {n} 只 · 成交额 {amt/1e5:,.0f}亿（20日均 {avg_amt/1e5:,.0f}亿）· OHLC 合法")
    return {"ok": ok, "note": "；".join(notes), "n": n, "amount": amt}


def _prev_close(db: Session, target: date, symbols: list[str]) -> dict[str, float]:
    """target 之前最后一个交易日的收盘价（风控用昨收，不是当日价）。"""
    if not symbols:
        return {}
    from sqlalchemy import bindparam, text
    rows = db.execute(
        text("select symbol, close from kline_daily where symbol in :syms "
             "and trade_date = (select max(trade_date) from kline_daily "
             "                  where trade_date < :d)")
        .bindparams(bindparam("syms", expanding=True)),
        {"syms": symbols, "d": target.isoformat()}).fetchall()
    return {r[0]: float(r[1]) for r in rows}


def _industries(db: Session, symbols: list[str]) -> dict[str, str]:
    if not symbols:
        return {}
    from sqlalchemy import bindparam, text
    rows = db.execute(
        text("select symbol, industry from stocks where symbol in :syms")
        .bindparams(bindparam("syms", expanding=True)),
        {"syms": symbols}).fetchall()
    return {r[0]: (r[1] or "未知") for r in rows}


# ── 读实际持仓 ──

def load_book(db: Session, capital: float) -> Book:
    rows = db.scalars(select(LivePosition).where(
        LivePosition.strategy_key == STRATEGY_KEY)).all()
    book = Book(cash=capital)
    for r in rows:
        if r.shares > 0:
            book.positions[r.symbol] = Position(
                symbol=r.symbol, shares=r.shares, entry_price=r.avg_cost,
                entry_date=r.entry_date or date.today(), peak=r.peak_close or r.avg_cost)
    if rows:
        # 有持仓记录时，现金以最近净值为准（净值表由对账维护）
        from app.models import LiveEquity
        eq = db.scalars(select(LiveEquity).where(
            LiveEquity.strategy_key == STRATEGY_KEY)
            .order_by(LiveEquity.trade_date.desc()).limit(1)).first()
        if eq:
            book.cash = eq.cash
    return book


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=400_000)
    ap.add_argument("--date", type=str, default=None)
    ap.add_argument("--strategy", type=str, default=STRATEGY_KEY)
    ap.add_argument("--rank", choices=["code", "pullback"], default="pullback",
                    help="code=回测原口径(字典序) pullback=按回调深度")
    ap.add_argument("--max-positions", type=int, default=20)
    ap.add_argument("--single-pct", type=float, default=0.05)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        panel = load_panel(DUCKDB, lookback=260)
        if panel.df.empty:
            print("✗ 行情面板为空", file=sys.stderr)
            return 1
        as_of = panel.latest
        target = date.fromisoformat(args.date) if args.date else as_of

        # L1 闸门
        chk = check_data(db, target)
        print(f"【数据闸门】{target} → {'通过' if chk['ok'] else '不通过'}")
        print(f"  {chk['note']}")
        if not chk["ok"]:
            print("\n✗ 数据不合格，今日不出单（宁可空跑，不可错跑）", file=sys.stderr)
            return 2

        # L3 决策
        cfg = dict(DEFAULT_CONFIG, capital=args.capital,
                   max_positions=args.max_positions, single_pct=args.single_pct)
        px = panel.df.set_index(["date", "symbol"])[
            ["open", "close", "o_hfq", "c_hfq"]].sort_index()
        market = Market(px)
        sig = load_signals(db, panel.df, strategy_key=args.strategy)

        book = load_book(db, args.capital)
        rank_key = None
        if args.rank == "pullback":
            rank_key = lambda s, a: (a["close"] / a["ma20"], s)  # noqa: E731

        dec = decide_close(target, book, sig, market, cfg, rank_key=rank_key)

        exc_txt = "—" if dec.gate["exc"] is None else f"{dec.gate['exc'] * 100:+.2f}%"
        gate_txt = "开启" if dec.gate["open"] else "关闭"
        print(f"\n【决策】{target}  门控={gate_txt}"
              f"（近 {dec.gate['n']} 个信号日超额 {exc_txt}）")
        print(f"  候选 {len(dec.candidates)} 只 · 卖出 {len(dec.sells)} 只 · 买入 {len(dec.buys)} 只")
        print(f"  enabled 策略: {sorted(sig['enabled'])}")
        print(f"  期末持仓 {book.n()} 只 · 现金 {book.cash:,.0f}")

        # L4 订单意图
        close_px, names = {}, {}
        latest = panel.df[panel.df["date"] == target]
        for r in latest.itertuples():
            close_px[r.symbol] = float(r.close)
            names[r.symbol] = panel.names.get(r.symbol, "")
        ib = build_intents(target, dec, book, close_px, names, cfg)

        # L5 风控闸门（三层：单笔 / 组合 / 熔断）
        from app.live.guard import AccountState, GuardConfig, check as guard_check

        symbols = list({s for s, p in book.positions.items() if p.shares > 0}
                       | {i.symbol for i in ib.items if i.side == "BUY"})
        prev_close = _prev_close(db, target, symbols)
        industries = _industries(db, symbols)

        pos_value = {s: (close_px.get(s) or p.entry_price) * p.shares
                     for s, p in book.positions.items()}
        total_asset = book.cash + sum(pos_value.values())

        from app.models import LiveEquity
        hist = db.scalars(select(LiveEquity.total).where(
            LiveEquity.strategy_key == args.strategy)).all()
        peak_asset = max([args.capital] + [float(h) for h in hist if h])
        prev_eq = db.scalars(select(LiveEquity).where(
            LiveEquity.strategy_key == args.strategy,
            LiveEquity.trade_date < target)
            .order_by(LiveEquity.trade_date.desc()).limit(1)).first()

        acct = AccountState(
            total_asset=total_asset, cash=book.cash, peak_asset=peak_asset,
            prev_close_asset=prev_eq.total if prev_eq else None,
            positions={s: p.shares for s, p in book.positions.items()},
            position_value=pos_value, industries=industries)
        gres = guard_check(
            ib.items, acct, prev_close,
            GuardConfig(max_positions=args.max_positions,
                        max_single_pct=args.single_pct))

        if gres.halted:
            print(f"\n【风控】⛔ 熔断：{gres.halt_reason}")
        print(f"【风控】放行 {len(gres.allowed)} 笔 · 拦截 {len(gres.blocked)} 笔"
              f" · 调整 {len(gres.adjusted)} 笔"
              f"（峰值资产 {peak_asset:,.0f}，当前 {total_asset:,.0f}）")
        for it, why in gres.adjusted:
            print(f"    调整 {it.symbol[2:]}：{why}")
        for it, why in gres.blocked:
            print(f"    ⛔ {it.symbol[2:]} {it.name[:6]}：{why}")
        for w in gres.warnings:
            print(f"    ⚠ {w}")

        # 候选明细补「未复权原价」：signal_actions.close 是后复权价（close×adj_factor），
        # 差一个量级。终端若拿复权价算股数会买出天量，必须同时给出可交易价格。
        for c in dec.candidates:
            c["close_raw"] = close_px.get(c["symbol"])
            c["name"] = names.get(c["symbol"], "")

        block_map = {id(it): why for it, why in gres.blocked}
        adj_map = {id(it): why for it, why in gres.adjusted}
        # 统一回写到 spec，下游落库/打印只认 it.blocked
        for it in ib.items:
            if id(it) in block_map:
                it.blocked = block_map[id(it)]
                it.qty = 0

        # ── 落库（幂等）──
        snap_id = None
        if not args.dry_run:
            db.execute(delete(LiveDecisionSnapshot).where(
                LiveDecisionSnapshot.trade_date == target,
                LiveDecisionSnapshot.strategy_key == args.strategy))
            snap = LiveDecisionSnapshot(
                trade_date=target, strategy_key=args.strategy, data_date=as_of,
                data_ok=1 if chk["ok"] else 0, data_note=chk["note"],
                enabled_json=json.dumps(sorted(sig["enabled"]), ensure_ascii=False),
                card_tiers_json=json.dumps(sig.get("card_tiers", {}), ensure_ascii=False),
                gate_open=1 if dec.gate["open"] else 0,
                gate_exc=dec.gate["exc"], gate_n=dec.gate["n"],
                candidates_json=json.dumps(dec.candidates, ensure_ascii=False, default=str),
                config_json=json.dumps(cfg, ensure_ascii=False, default=str))
            db.add(snap)
            db.flush()
            snap_id = snap.id

            db.execute(delete(LiveOrderIntent).where(
                LiveOrderIntent.trade_date == target,
                LiveOrderIntent.strategy_key == args.strategy))
            for it in ib.items:
                db.add(LiveOrderIntent(
                    trade_date=target, strategy_key=args.strategy, symbol=it.symbol,
                    name=it.name, side=it.side,
                    target_shares=it.target_shares, actual_shares=it.actual_shares,
                    qty=it.qty, ref_price=it.ref_price, limit_price=it.limit_price,
                    est_amount=it.est_amount, reason=it.reason, priority=it.priority,
                    status="DRAFT" if it.ok else "REJECTED",
                    reject_reason=it.blocked, snapshot_id=snap_id))
            db.commit()

        # ── 打印清单 ──
        print("\n" + "=" * 86)
        print(f"次日开盘订单清单 · {target} · 本金 {args.capital:,.0f} · "
              f"排序={'回调深度' if args.rank == 'pullback' else '代码'}")
        print("=" * 86)
        hdr = f"{'代码':<9}{'名称':<11}{'方向':<6}{'股数':>7}{'参考价':>9}{'限价':>9}{'金额':>12}"
        print(hdr)
        print("-" * 86)
        for it in sorted(ib.items, key=lambda x: (x.priority, x.symbol)):
            if not it.ok:
                print(f"{it.symbol[2:]:<9}{it.name[:9]:<11}{it.side:<6}"
                      f"{'—':>7}{'—':>9}{'—':>9}{'—':>12}   ⛔ {it.blocked}")
                continue
            print(f"{it.symbol[2:]:<9}{it.name[:9]:<11}{it.side:<6}{it.qty:>7}"
                  f"{it.ref_price:>9.2f}{it.limit_price:>9.2f}{it.est_amount:>12,.0f}")
        print("-" * 86)
        buys = [i for i in ib.orders if i.side == "BUY"]
        sells = [i for i in ib.orders if i.side == "SELL"]
        print(f"卖 {len(sells)} 笔 {sum(i.est_amount for i in sells):,.0f} 元 · "
              f"买 {len(buys)} 笔 {sum(i.est_amount for i in buys):,.0f} 元 · "
              f"拦下 {len(ib.blocked)} 笔")
        ok_buy = sum(i.est_amount for i in ib.orders if i.side == "BUY")
        ok_sell = sum(i.est_amount for i in ib.orders if i.side == "SELL")
        print(f"预估现金（风控后）：{book.cash:,.0f} → "
              f"{book.cash - ok_buy + ok_sell:,.0f}")
        if not args.dry_run:
            print(f"\n已落库：snapshot#{snap_id} + {len(ib.items)} 条 order_intent")
        else:
            print("\n[dry-run] 未写库")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
