# -*- coding: utf-8 -*-
"""fresh_ma 策略内核（QMT / PTrade 兼容 · Python 3.6+ · 只用标准库）。

设计约束（决定了它能搬进券商终端）：
  1. 零第三方依赖。不用 pandas / numpy —— PTrade 白名单不确定，QMT 内置 3.6.8
     装不上新库。所有指标用原生 list 实现。
  2. 纯函数。输入行情与状态，输出目标持仓与订单，不碰文件、不碰网络、不碰数据库。
     这样既能被 QMT 调用，也能被本地 mock 逐日重放做一致性验证。
  3. 与平台口径逐项对齐。任何一处不一致都会让实盘跑的不是回测的策略，
     所以对齐点全部注释标出（对应 backend/app/services/xq/strategies.py）。

移植来源：
  chan_buy        → backend/app/services/xq/strategies.py::chan_buy
  high_tight_flag → backend/app/services/xq/strategies.py::high_tight_flag
  候选判定/门控   → backend/app/services/xq/portfolio_fresh.py
  成本/风控       → backend/app/live/target.py / guard.py
"""
from typing import Dict, List, Optional


# ══════════════════════ 参数（与平台 DEFAULT_CONFIG 一致）══════════════════════

DEFAULT_CONFIG = {
    "capital": 400000,
    "max_positions": 20,
    "single_pct": 0.05,
    "stop_loss": -0.08,          # 硬止损：自建仓浮亏
    "trail_drawdown": -0.08,     # 移动止损：自峰值回撤
    "max_hold_days": 10,         # 时间止损
    "gate_window_days": 90,      # 门控窗口：近 90 个信号日
    "gate_min_samples": 20,      # 门控最少样本
    "limit_premium": 0.02,       # 买入限价 = 参考价 × (1+2%)
    "buy_buffer": 0.03,          # 买入预算留 3% 余量
    "lot_main": 100,
    "lot_star": 200,
    "enabled_strategies": ["chan_buy", "high_tight_flag"],  # 冻结的 enabled 集合
}

# 与平台 STRATEGY_QUALITY 的 default_tier 对齐；实盘固定用 enabled 两个策略。
CHAN_MIN_SCORE = 55
CHAN_RECENT_BARS = 3
STRONG_STREAK_MAX = 2           # streak <= 2 = 新鲜信号


# ══════════════════════ 基础指标（原生实现）══════════════════════

def sma(values, window):
    # type: (List[float], int) -> List[float]
    """简单移动平均。前 window-1 个位置为 None（与 pandas rolling 一致）。"""
    out = [None] * len(values)
    if window <= 0:
        return out
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= window:
            s -= values[i - window]
        if i >= window - 1:
            out[i] = s / window
    return out


def rolling_max(values, window):
    # type: (List[float], int) -> List[float]
    out = [None] * len(values)
    for i in range(len(values)):
        if i >= window - 1:
            out[i] = max(values[i - window + 1:i + 1])
    return out


def rolling_min(values, window):
    # type: (List[float], int) -> List[float]
    out = [None] * len(values)
    for i in range(len(values)):
        if i >= window - 1:
            out[i] = min(values[i - window + 1:i + 1])
    return out


# ══════════════════════ 策略 1：high_tight_flag ══════════════════════

def high_tight_flag(bars):
    # type: (List[dict]) -> bool
    """40 日涨幅>60% + 近 10 日振幅<15% + 高位抗跌 + 缩量 0.6 倍。

    对齐平台：用后复权 high/low（h_hfq/l_hfq）与未复权 volume。
    平台版是截面向量化，这里改成单只判定，条件逐项照抄。
    """
    n = len(bars)
    if n < 41:
        return False
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b["volume"] for b in bars]

    h40 = max(highs[-40:])
    l40 = min(lows[-40:])
    h10 = max(highs[-10:])
    l10 = min(lows[-10:])

    if l40 <= 0 or l10 <= 0:
        return False
    if h40 / l40 <= 1.6:                 # 40 日涨幅不足 60%
        return False
    if h10 / l10 >= 1.15:                # 近 10 日振幅 >= 15%
        return False
    if l10 < h40 * 0.8:                  # 未保持高位
        return False

    # 平台：volume < shift(vol_ma20, 1) * 0.6，即「今日量 相对 昨日为止的 20 日均量」
    if n < 22:
        return False
    vol_ma20_prev = sum(vols[-21:-1]) / 20.0
    return vols[-1] < vol_ma20_prev * 0.6


# ══════════════════════ 策略 2：chan_buy ══════════════════════

def chan_buy(bars, analyze_chanlun, Candle, min_score=CHAN_MIN_SCORE,
             recent_bars=CHAN_RECENT_BARS):
    # type: (List[dict], object, object, int, int) -> bool
    """缠论买点：最近 N 根内出现买点且评分 >= 55。

    对齐平台：用后复权 OHLC 构造 Candle（平台 o_hfq/h_hfq/l_hfq/c_hfq），
    ts 用序号（引擎内部只做相对定位）。
    analyze_chanlun / Candle 由调用方注入，便于单文件打包与本地测试。
    """
    n = len(bars)
    if n < 120:                          # 平台：len(g) < 120 跳过
        return False
    candles = []
    for i, b in enumerate(bars):
        candles.append(Candle(high=b["high"], low=b["low"], open=b["open"],
                              close=b["close"], time=b["date"], ts=i))
    try:
        res = analyze_chanlun(candles)
    except Exception:
        return False
    for s in res.get("signals", []):
        if s.kind != "buy" or s.score < min_score:
            continue
        if s.index >= n - recent_bars:   # 买点落在最近 N 根内
            return True
    return False


# ══════════════════════ 候选判定 ══════════════════════

def compute_streak(sym, dates, hits_by_date):
    # type: (str, List[str], Dict[str, List[str]]) -> int
    """从最新日期往回数连续命中天数，断一天即停（与平台 xq/engine.py 一致）。

    dates 必须按升序传入；hits_by_date 的键是 YYYYMMDD 或 YYYY-MM-DD，统一用前者。
    """
    streak = 0
    for d in reversed(dates):
        if sym in hits_by_date.get(d, ()):
            streak += 1
        else:
            break
    return streak


def build_candidates(bars_map, analyze_chanlun, Candle, enabled, dates,
                     hits_by_date, cfg=DEFAULT_CONFIG):
    # type: (Dict[str, List[dict]], object, object, List[str], List[str], Dict[str, List[str]], dict) -> List[dict]
    """当日候选池：enabled 策略命中 + streak<=2 + close<ma20。返回带排序键的列表。

    排序用「回调深度」close/ma20 升序（越小回调越深），比原字典序有经济含义，
    且完全确定（同分按代码），可复现。平台侧用 --rank pullback 得到同一结果。
    """
    out = []
    for sym, bars in bars_map.items():
        if not bars:
            continue
        hit_strats = []
        if "chan_buy" in enabled and chan_buy(bars, analyze_chanlun, Candle):
            hit_strats.append("chan_buy")
        if "high_tight_flag" in enabled and high_tight_flag(bars):
            hit_strats.append("high_tight_flag")
        if not hit_strats:
            continue

        streak = compute_streak(sym, dates, hits_by_date)
        if streak > STRONG_STREAK_MAX:
            continue

        closes = [b["close"] for b in bars]
        ma20 = sum(closes[-20:]) / 20.0 if len(closes) >= 20 else None
        if ma20 is None or ma20 <= 0:
            continue
        close = closes[-1]
        if close >= ma20:                # 必须在回调区
            continue

        out.append({
            "symbol": sym,
            "close": close,
            "ma20": ma20,
            "pullback": close / ma20,
            "streak": streak,
            "strategies": hit_strats,
            "raw_close": bars[-1].get("raw_close", close),
        })
    out.sort(key=lambda c: (c["pullback"], c["symbol"]))
    return out


# ══════════════════════ 门控（PIT）══════════════════════

def gate_check(daily_exc, cfg=DEFAULT_CONFIG):
    # type: (Dict[str, float], dict) -> dict
    """近 N 个信号日的候选池平均 T+10 超额 > 0 才开仓。

    PIT 保证由调用方负责：写入 daily_exc 的前提是该信号日的 T+10 窗口已经走完
    （平台 portfolio_fresh._gate_at 的 tid[s]+11 <= tid[d]）。
    """
    window = cfg["gate_window_days"]
    years = sorted(daily_exc.keys())[-window:]
    if len(years) < cfg["gate_min_samples"]:
        return {"open": False, "exc": None, "n": len(years)}
    exc = sum(daily_exc[d] for d in years) / len(years)
    return {"open": exc > 0, "exc": exc, "n": len(years)}


# ══════════════════════ 目标持仓 ══════════════════════

def select_targets(candidates, positions, cfg=DEFAULT_CONFIG):
    # type: (List[dict], Dict[str, int], dict) -> List[str]
    """从候选池补足到 max_positions。已在持仓中的票不重复买入（策略是等权新建）。"""
    held = set(s for s, q in positions.items() if q > 0)
    slots = cfg["max_positions"] - len(held)
    if slots <= 0:
        return []
    out = []
    for c in candidates:
        if c["symbol"] in held or c["symbol"] in out:
            continue
        out.append(c["symbol"])
        if len(out) >= slots:
            break
    return out


# ══════════════════════ 风控（组合层 + 单笔）══════════════════════

def limit_pct(symbol, name=""):
    # type: (str, str) -> float
    if "ST" in (name or "").upper():
        return 0.05
    if symbol.startswith("sh68") or symbol.startswith("sz30"):
        return 0.20
    if symbol.startswith("bj"):
        return 0.30
    return 0.10


def lot_size(symbol, cfg=DEFAULT_CONFIG):
    # type: (str, dict) -> int
    return cfg["lot_star"] if symbol.startswith("sh68") else cfg["lot_main"]


def build_orders(targets, positions, bars_map, total_asset, cash,
                 prev_close_map=None, names=None, cfg=DEFAULT_CONFIG):
    # type: (List[str], Dict[str, int], Dict[str, List[dict]], float, float, Dict[str, float], Dict[str, str], dict) -> List[dict]
    """目标 → 可执行订单。返回 [{symbol, side, qty, price, reason, blocked}]。

    规则与平台 live/intent.py 一致：
      - 单只预算 = min(总资产×5%, 可用现金×(1−buffer)/剩余名额)
      - 按整手取整（科创板 200）
      - 涨停买不进、无价跳过、预算买不满最小手则拦下（记录原因而不是静默丢弃）
    """
    prev_close_map = prev_close_map or {}
    names = names or {}
    orders = []
    single = total_asset * cfg["single_pct"]
    remaining = max(cfg["max_positions"] - len([s for s, q in positions.items() if q > 0]), 1)

    for sym in targets:
        bars = bars_map.get(sym) or []
        if not bars:
            orders.append({"symbol": sym, "side": "BUY", "qty": 0, "price": 0.0,
                           "reason": "无行情", "blocked": "无当日行情"})
            continue
        # 只用未复权价下单。绝不能回退到复权价——复权价可能是原价的十几倍，
        # 按它报单会挂出永远不可能成交的天价，而且日志看起来一切正常。
        px = bars[-1].get("raw_close") or 0
        if px <= 0:
            orders.append({"symbol": sym, "side": "BUY", "qty": 0, "price": 0.0,
                           "reason": "无未复权价",
                           "blocked": "无未复权价（拒绝用复权价下单）"})
            continue

        pc = prev_close_map.get(sym, 0.0)
        if pc > 0:
            if px >= pc * (1 + limit_pct(sym, names.get(sym, "")) - 0.005):
                orders.append({"symbol": sym, "side": "BUY", "qty": 0, "price": px,
                               "reason": "涨停", "blocked": "涨停买不进"})
                continue

        budget = min(single, cash * (1 - cfg["buy_buffer"]) / remaining)
        lot = lot_size(sym, cfg)
        qty = int(budget / px // lot) * lot
        if qty <= 0:
            orders.append({"symbol": sym, "side": "BUY", "qty": 0, "price": px,
                           "reason": "预算不足",
                           "blocked": "预算 %.0f 买不满 %d 股（股价 %.2f）" % (budget, lot, px)})
            continue

        orders.append({
            "symbol": sym, "side": "BUY", "qty": qty,
            "price": round(px * (1 + cfg["limit_premium"]), 2),
            "ref_price": px, "amount": round(qty * px, 2),
            "reason": "新鲜信号×回调区", "blocked": "",
        })
        cash -= qty * px
        remaining = max(remaining - 1, 1)
    return orders


def check_exits(meta, bars_map, date_index, cfg=DEFAULT_CONFIG):
    # type: (Dict[str, dict], Dict[str, List[dict]], Dict[str, int], dict) -> List[dict]
    """止损判定（基于当日收盘，次日开盘执行），对齐 target.py::decide_close。

    meta: {symbol: {shares, entry_price, entry_date, peak}}
    date_index: {YYYYMMDD: 序号} 用于算持有交易日数

    三条依次判定，命中即出（顺序不可换，平台是 if/elif）：
      ① 硬止损   收盘/建仓价-1 <= -8%
      ② 移动止损 收盘/持仓期峰值-1 <= -8%
      ③ 时间止损 持有交易日数 >= 10
    """
    out = []
    for sym, h in meta.items():
        bars = bars_map.get(sym) or []
        if not bars:
            continue                      # 停牌：不触发
        close = bars[-1]["close"]
        entry = h.get("entry_price") or 0
        if entry <= 0:
            continue
        ret = close / entry - 1.0
        peak = max(h.get("peak") or close, close)
        dd = close / peak - 1.0
        held = date_index.get(bars[-1]["date"], 0) - date_index.get(h.get("entry_date", ""), 0)

        reason = ""
        if ret <= cfg["stop_loss"]:
            reason = "止损：浮亏 %.1f%%" % (ret * 100)
        elif dd <= cfg["trail_drawdown"]:
            reason = "移动止损：自峰值回撤 %.1f%%" % (dd * 100)
        elif held >= cfg["max_hold_days"]:
            reason = "时间止损：持有满 %d 个交易日" % cfg["max_hold_days"]
        if reason:
            out.append({"symbol": sym, "side": "SELL", "qty": h.get("shares", 0),
                        "reason": reason, "blocked": ""})
    return out
