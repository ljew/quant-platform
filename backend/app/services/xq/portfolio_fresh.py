"""fresh_ma 实验组合引擎（2026-10-14 重新优化，paper sleeve，不接生产信号管道）。

规则来源（scripts/sim_fresh_signal.py 全历史重演 + 250 天信号统计）：
  2026-07 起 regime 切换——动量族（dual_param/rps_breakout/turtle_trade）T+10
  超额集体转负，卡片状态机正确降级；经典 BUY_STRONG 规则（streak>=9）近 90 天
  T+10 超额仅 +0.53%（日期级 t=-0.21），供给 250 天仅 238 个，规则已死。
  近 90 天 alpha 转移到：卡片 enabled 策略的**新鲜信号**（streak<=2）且价格处于
  **回调区**（close<ma20）——T+10 超额 +2.31%（日期级 t=+4.23，日胜率 73%），
  供给充足（日均候选 158 个）。

组合层验证（同窗口、含成本，scripts/sim_fresh_signal.py）：
  近90天：经典规则 +0.6%（超额+12.6pp，但 90% 时间空仓，是避 beta 不是 alpha）；
  本规则 top10 +10.7% / top20 +17.9%（种子敏感性 +7.5~+19.3% 全正）。
  全250天：本规则超额仅 +0.4pp、回撤 -29.7% —— **edge 是 regime 依赖的**。

因此本引擎带 regime 门控（核心设计，PIT）：
  门控 = 本规则自身候选池的「近 90 个信号交易日 T+10 超额」。严格 PIT：信号日 s
  计入门控的前提是其 T+10 出场日（s 后第 11 个交易日）已过（s+11td <= 当前日），
  绝不用未完成窗口的收益。超额 > 0 且样本 >= 20 才开新仓；门控关闭只挡新开仓，
  存量持仓按自身止损/移动止损/时间止损走完。
  按月验证：门控量 pre-July 十个月均值为负（-0.06/-0.46/-0.94），Jul-Sep 转正
  （+1.56）——恰好圈住 edge 存在的区间。

口径与经典镜像组合（portfolio.py）一致：T+1 开盘建仓、次日开盘出场、整手、
佣金万2.5(最低5元)双向 + 印花税万5(仅卖出) + 过户费万0.1双向 + 滑点万1（与主引擎对齐）。
差异：入场规则（新鲜信号×回调区）、regime 门控、风控参数取自模拟
（硬止损 -8% / 自峰值回撤 -8% / 持有 10 个交易日时间止损，无固定止盈——
模拟中 T+10 视口 alpha 见顶，固定止盈会切断右尾）。
同标的允许再入场（与模拟口径一致；经典引擎的 buy_dates 永久排除不适用本规则，
本规则持有期仅 10 个交易日，不存在经典规则的低频信号被反复追的问题）。
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.cost_defaults import COMMISSION, MIN_COMMISSION, SLIPPAGE, STAMP_TAX
from app.models import PortfolioExpState, SignalAction, SignalDaily
from app.services.research_card_svc import MIN_SAMPLES, resolve_strategy_quality
from app.services.xq.data import Panel, load_panel

DEFAULT_CONFIG: dict = {
    "capital": 2_000_000,
    "max_positions": 20,        # 模拟 top20 风险调整最优（V2：+17.9%/回撤-7.9%）
    "single_pct": 0.05,         # 单只 5% 初始资金
    "stop_loss": -0.08,         # 硬止损：自建仓浮亏 8%
    "trail_drawdown": -0.08,    # 移动止损：自期间峰值回撤 8%
    "max_hold_days": 10,        # 时间止损：持有 10 个交易日
    "gate_window_days": 90,     # regime 门控：近 90 个信号交易日
    "gate_min_samples": MIN_SAMPLES,
    "commission_rate": COMMISSION,
    "commission_min": MIN_COMMISSION,
    "stamp_tax": STAMP_TAX,
    "transfer_fee": 0.00001,    # 过户费万 0.1（双向；xq 组合轨特有）
    "slippage": SLIPPAGE,       # 与主引擎对齐（W45-xq / P1-2）
    "lot_size": 100,
}


def _buy_cost(price: float, shares: int, cfg: dict) -> float:
    gross = round(price * (1 + cfg["slippage"]) * shares, 2)
    commission = max(gross * cfg["commission_rate"], cfg["commission_min"])
    transfer = gross * cfg["transfer_fee"]
    return round(gross + commission + transfer, 2)


def _sell_proceeds(price: float, shares: int, cfg: dict) -> float:
    gross = round(price * (1 - cfg["slippage"]) * shares, 2)
    commission = max(gross * cfg["commission_rate"], cfg["commission_min"])
    stamp = gross * cfg["stamp_tax"]
    transfer = gross * cfg["transfer_fee"]
    return round(gross - commission - stamp - transfer, 2)


def _is_candidate(strats: list[str], act: dict, enabled: set[str]) -> bool:
    """fresh_ma 候选判定：enabled 策略的新鲜信号（streak<=2）且收盘处于回调区。"""
    if not any(s in enabled for s in strats):
        return False
    if act.get("streak", 0) > 2:
        return False
    close, ma20 = act.get("close"), act.get("ma20")
    return close is not None and ma20 is not None and close < ma20


def _daily_candidate_excess(
    sig_dates: list[date],
    act_map: dict[date, dict[str, dict]],
    sig_strats: dict[date, dict[str, list[str]]],
    enabled: set[str],
    px: pd.DataFrame,
) -> dict[date, float]:
    """每个信号日的候选池平均 T+10 超额（对全市场等权基准，同窗口同起止）。

    T+1 开盘建仓、T+1 后再持有 10 个交易日收盘出场；只有窗口完整的信号日
    才有值（其余 NaN 被丢弃）。门控逐日解锁时再按 PIT 取子集。
    """
    if px.empty:
        return {}
    open_p = px.pivot_table(index="date", columns="symbol", values="o_hfq")
    close_p = px.pivot_table(index="date", columns="symbol", values="c_hfq")
    fwd = (close_p.shift(-11) / open_p.shift(-1)) - 1.0
    bench = fwd.mean(axis=1)
    out: dict[date, float] = {}
    for d in sig_dates:
        vals = []
        for sym, act in act_map.get(d, {}).items():
            if not _is_candidate(sig_strats.get(d, {}).get(sym, []), act, enabled):
                continue
            if d not in fwd.index or sym not in fwd.columns:
                continue
            r, b = fwd.at[d, sym], bench.get(d)
            if pd.isna(r) or b is None or pd.isna(b):
                continue
            vals.append(float(r) - float(b))
        if vals:
            out[d] = sum(vals) / len(vals)
    return out


def run_portfolio_fresh(db: Session, duckdb_path: str, lookback: int = 260,
                        cfg: dict | None = None) -> dict:
    """fresh_ma 实验组合全历史重演（含 PIT regime 门控），落 portfolio_exp_state。"""
    cfg = dict(DEFAULT_CONFIG, **(cfg or {}))
    actions = db.scalars(select(SignalAction).order_by(SignalAction.date)).all()
    if not actions:
        return {"ok": False, "error": "暂无信号历史"}

    # 候选资格 = 卡片非 weak 档策略（与经典规则同一门禁口径）
    quality = resolve_strategy_quality(db)
    enabled = {k for k, v in quality.items() if v != "weak"}

    sig_rows = db.execute(
        select(SignalDaily.date, SignalDaily.symbol, SignalDaily.strategy)
    ).all()
    sig_strats: dict[date, dict[str, list[str]]] = {}
    for d, s, st in sig_rows:
        sig_strats.setdefault(d, {}).setdefault(s, []).append(st)

    act_map: dict[date, dict[str, dict]] = {}
    for a in actions:
        act_map.setdefault(a.date, {})[a.symbol] = {
            "action": a.action, "streak": a.streak or 0,
            "resonance": a.resonance or 0, "close": a.close, "ma20": a.ma20,
        }
    dates = sorted(act_map)

    panel: Panel = load_panel(duckdb_path, lookback=lookback)
    if panel.df.empty:
        return {"ok": False, "error": "行情面板为空"}
    px = panel.df.set_index(["date", "symbol"])[
        ["open", "close", "o_hfq", "c_hfq"]].sort_index()
    trade_dates = sorted({d for d, _ in px.index})
    tid = {d: i for i, d in enumerate(trade_dates)}

    daily_exc = _daily_candidate_excess(dates, act_map, sig_strats, enabled, px)

    cash = float(cfg["capital"])
    holdings: dict[str, dict] = {}
    pending_buys: list[str] = []
    pending_sells: list[str] = []
    equity_curve: list[dict] = []
    trades: list[dict] = []
    gate_log: list[dict] = []

    def _px(d: date, sym: str, col: str) -> float | None:
        try:
            v = px.loc[(d, sym), col]
            return float(v.iloc[0]) if isinstance(v, pd.Series) else float(v)
        except KeyError:
            return None

    def _gate_at(d: date) -> dict:
        """PIT 门控：只计入 T+10 窗口已成熟（s 后第 11 个交易日 <= d）的信号日。"""
        i = tid.get(d)
        if i is None:
            return {"open": False, "exc": None, "n": 0}
        mature = [s for s in dates
                  if s in daily_exc and s in tid and tid[s] + 11 <= i]
        recent = mature[-cfg["gate_window_days"]:]
        if len(recent) < cfg["gate_min_samples"]:
            return {"open": False, "exc": None, "n": len(recent)}
        m = sum(daily_exc[s] for s in recent) / len(recent)
        return {"open": m > 0, "exc": m, "n": len(recent)}

    for d in dates:
        gate = _gate_at(d)

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
                             "entry_date": d, "peak": price}
            trades.append({
                "date": d.isoformat(), "symbol": sym, "side": "BUY",
                "shares": shares, "price": round(price, 2), "cost": cost,
                "reason": "新鲜信号×回调区（regime 门控开启）",
            })
        pending_buys.clear()

        # ── 2) 收盘更新市值 + 风控决策（次日开盘执行）──
        market_value = 0.0
        for sym, h in list(holdings.items()):
            close = _px(d, sym, "close")
            if close:
                market_value += close * h["shares"]
                ret = close / h["entry_price"] - 1.0
                h["peak"] = max(h["peak"], close)
                dd = close / h["peak"] - 1.0
                held_td = tid[d] - tid[h["entry_date"]] if (d in tid and h["entry_date"] in tid) else 0
                if ret <= cfg["stop_loss"]:
                    h["exit_reason"] = f"止损：浮亏 {ret*100:.1f}%"
                    pending_sells.append(sym)
                elif dd <= cfg["trail_drawdown"]:
                    h["exit_reason"] = f"移动止损：自峰值回撤 {dd*100:.1f}%"
                    pending_sells.append(sym)
                elif held_td >= cfg["max_hold_days"]:
                    h["exit_reason"] = f"时间止损：持有满 {cfg['max_hold_days']} 个交易日"
                    pending_sells.append(sym)
            else:
                market_value += h["entry_price"] * h["shares"]  # 停牌按成本估值
        equity_curve.append({
            "date": d.isoformat(), "total": round(cash + market_value, 2),
            "cash": round(cash, 2), "market_value": round(market_value, 2),
            "n_positions": len(holdings), "gate_open": gate["open"],
        })

        # ── 3) 门控开启才生成新候选（确定性优先级：high_tight_flag 优先，
        #      同级按 symbol 字典序——保证重演结果与逐日实盘完全一致）──
        if gate["open"]:
            cands = []
            for sym, act in act_map.get(d, {}).items():
                if sym in holdings or sym in pending_buys:
                    continue
                if _is_candidate(sig_strats.get(d, {}).get(sym, []), act, enabled):
                    pri = 0 if "high_tight_flag" in sig_strats[d][sym] else 1
                    cands.append((pri, sym))
            cands.sort()
            for _, sym in cands:
                if len(holdings) + len(pending_buys) >= cfg["max_positions"]:
                    break
                pending_buys.append(sym)
        gate_log.append({"date": d.isoformat(), **gate})

    total_end = equity_curve[-1]["total"] if equity_curve else cfg["capital"]
    peak, mdd = 0.0, 0.0
    for e in equity_curve:
        peak = max(peak, e["total"])
        mdd = min(mdd, e["total"] / peak - 1.0) if peak else 0.0

    summary = {
        "capital": cfg["capital"],
        "total": round(total_end, 2),
        "return_pct": round((total_end / cfg["capital"] - 1) * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "n_trades": len(trades),
        "n_holdings": len(holdings),
        "days": len(equity_curve),
        "gate_open_days": sum(1 for g in gate_log if g["open"]),
        "gate_exc_now": gate["exc"],
        "rule": "fresh_ma",
        "enabled_strategies": sorted(enabled),
    }
    holdings_out = [
        {"symbol": s, "shares": h["shares"], "entry_price": h["entry_price"],
         "entry_date": h["entry_date"].isoformat()}
        for s, h in sorted(holdings.items())
    ]

    as_of = dates[-1] if dates else date.today()
    db.query(PortfolioExpState).filter(PortfolioExpState.as_of_date == as_of).delete()
    db.add(PortfolioExpState(
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
