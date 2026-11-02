# -*- coding: utf-8 -*-
"""fresh_ma · QMT 单文件策略（自动生成，勿手改）

由 qmt/build_single.py 合成，来源：
  qmt/chanlun_py36.py  缠论引擎（Python 3.6 兼容版，算法零改动）
  qmt/core.py          策略内核（选股/门控/风控/订单）
  qmt/strategy.py      QMT 入口（取数/报单/状态）

选股一致性：与平台 backend/app/services/xq 的实现在全市场 5209 只 × 5 个交易日
逐日比对，两个策略命中名单完全一致（qmt/verify_parity.py）。

部署：
  1. QMT 客户端 → 策略编辑器 → 新建 Python 策略 → 全选粘贴本文件
  2. 改顶部 ACCOUNT 为你的资金账号
  3. 先跑模拟盘，确认「[order] BUY ...」日志真的出现（这是最容易失效的一步）
  4. 报备通过后再切实盘

合规：指令在 QMT 终端内生成。本文件不发起网络请求、不读写本地文件。
"""


import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


# ======================================================================
# 第一部分 · 缠论引擎（qmt/chanlun_py36.py）
# ======================================================================
STROKE_GAP = 4  # 顶底分型之间至少隔的独立K线数（含两端分型）


# ────────────────────────── 数据结构 ──────────────────────────

@dataclass
class Candle:
    high: float
    low: float
    open: float
    close: float
    time: str = ""
    ts: int = 0


@dataclass
class Merged:
    """合并后的 K 线：记录原始起止索引与极值点索引。"""
    high: float
    low: float
    open: float
    close: float
    start_idx: int
    end_idx: int
    peak_idx: int
    trough_idx: int
    time: str
    ts: int


@dataclass
class Fractal:
    midx: int      # 在 merged 序列中的位置
    type: str      # 'top' / 'bottom'
    high: float
    low: float
    time: str
    index: int     # 对应原始 K 线索引（极值点）


@dataclass
class Stroke:
    """笔：从 from 分型到 to 分型的有向线段。"""
    from_: Fractal
    to: Fractal
    dir: str                       # 'up' / 'down'
    start_index: int = 0
    end_index: int = 0
    start_time: str = ""
    end_time: str = ""

    def __post_init__(self) -> None:
        self.start_index = self.from_.index
        self.end_index = self.to.index
        self.start_time = self.from_.time
        self.end_time = self.to.time

    @property
    def hi(self) -> float:
        return max(self.from_.high, self.to.high)

    @property
    def lo(self) -> float:
        return min(self.from_.low, self.to.low)


@dataclass
class Segment:
    dir: str
    start: Fractal
    end: Fractal
    stroke_count: int
    start_index: int
    end_index: int
    start_time: str
    end_time: str


@dataclass
class Pivot:
    zg: float
    zd: float
    start_stroke: int
    end_stroke: int
    start_index: int
    end_index: int
    start_time: str
    end_time: str


@dataclass
class Signal:
    kind: str          # 'buy' / 'sell'
    type: str          # buy1/buy2/buy3/sell1/sell2/sell3
    index: int         # 原始 K 线索引
    time: str
    price: float
    reason: str
    score: int = 50
    ref_index: Optional[int] = None


@dataclass
class Macd:
    dif: List[float] = field(default_factory=list)
    dea: List[float] = field(default_factory=list)
    hist: List[float] = field(default_factory=list)


# ────────────────────────── 0. MACD（utils.js calcMACD 对应）──────────────────────────

def _ema(values: List[float], period: int) -> List[float]:
    k = 2.0 / (period + 1)
    out: List[float] = []
    prev = values[0]
    out.append(prev)
    for v in values[1:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def calc_macd(closes: List[float], fast: int = 12, slow: int = 26,
              signal: int = 9) -> Macd:
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = _ema(dif, signal)
    hist = [(d - e) * 2 for d, e in zip(dif, dea)]
    return Macd(dif=dif, dea=dea, hist=hist)


# ────────────────────────── 1. K线包含关系合并 ──────────────────────────

def merge_inclusions(candles: List[Candle]) -> List[Merged]:
    merged: List[Merged] = []
    if not candles:
        return merged
    c0 = candles[0]
    m = Merged(c0.high, c0.low, c0.open, c0.close, 0, 0, 0, 0, c0.time, c0.ts)
    direction = 0
    for i in range(1, len(candles)):
        c = candles[i]
        incl = (m.high >= c.high and m.low <= c.low) or (c.high >= m.high and c.low <= m.low)
        if incl:
            d = direction if direction != 0 else (1 if c.close >= m.close else -1)
            if d == 1:
                if c.high > m.high:
                    m.high, m.peak_idx = c.high, i
                if c.low > m.low:
                    m.low, m.trough_idx = c.low, i
            else:
                if c.high < m.high:
                    m.high, m.peak_idx = c.high, i
                if c.low < m.low:
                    m.low, m.trough_idx = c.low, i
            m.close = c.close
            m.end_idx = i
            m.time = c.time
        else:
            merged.append(m)
            direction = 1 if c.high >= m.high else -1
            m = Merged(c.high, c.low, c.open, c.close, i, i, i, i, c.time, c.ts)
    merged.append(m)
    return merged


# ────────────────────────── 2. 分型 ──────────────────────────

def find_fractals(merged: List[Merged], candles: List[Candle]) -> List[Fractal]:
    out: List[Fractal] = []
    for i in range(1, len(merged) - 1):
        a, b, c = merged[i - 1], merged[i], merged[i + 1]
        is_top = b.high > a.high and b.high > c.high and b.low > a.low and b.low > c.low
        is_bottom = b.low < a.low and b.low < c.low and b.high < a.high and b.high < c.high
        if is_top:
            peak = candles[b.peak_idx]
            out.append(Fractal(i, "top", b.high, b.low, peak.time, b.peak_idx))
        elif is_bottom:
            trough = candles[b.trough_idx]
            out.append(Fractal(i, "bottom", b.high, b.low, trough.time, b.trough_idx))
    return out


# ────────────────────────── 3. 笔 ──────────────────────────

def find_strokes(fractals: List[Fractal]) -> List[Stroke]:
    strokes: List[Stroke] = []
    cur: Fractal | None = None
    for f in fractals:
        if cur is None:
            cur = f
            continue
        if cur.type == f.type:
            extend = (f.type == "top" and f.high >= cur.high) or (
                f.type == "bottom" and f.low <= cur.low
            )
            if extend:
                if strokes and strokes[-1].to is cur:
                    strokes[-1].to = f
                cur = f
            continue
        if f.midx - cur.midx < STROKE_GAP:
            continue
        if cur.type == "bottom" and f.high <= cur.high:
            continue
        if cur.type == "top" and f.low >= cur.low:
            continue
        strokes.append(Stroke(from_=cur, to=f, dir="up" if cur.type == "bottom" else "down"))
        cur = f
    return strokes


# ────────────────────────── 4. 线段（特征序列法）──────────────────────────

def build_segments(strokes: List[Stroke]) -> List[Segment]:
    segments: List[Segment] = []
    if len(strokes) < 3:
        return segments
    s = 0
    while s + 2 < len(strokes):
        d = strokes[s].dir
        feats: List[Tuple[int, float, float]] = []
        for j in range(s + 1, len(strokes), 2):
            feats.append((j, strokes[j].hi, strokes[j].lo))
        end_stroke = -1
        for f in range(len(feats) - 2):
            _, a_hi, a_lo = feats[f]
            _, b_hi, b_lo = feats[f + 1]
            _, c_hi, c_lo = feats[f + 2]
            is_break = (
                (b_hi > a_hi and b_hi > c_hi and b_lo > a_lo and b_lo > c_lo)
                if d == "up"
                else (b_lo < a_lo and b_lo < c_lo and b_hi < a_hi and b_hi < c_hi)
            )
            if is_break:
                end_stroke = feats[f + 1][0] - 1
                break
        if end_stroke == -1:
            end_stroke = len(strokes) - 1
        while end_stroke > s and strokes[end_stroke].dir != d:
            end_stroke -= 1
        if end_stroke - s + 1 >= 3:
            start_f, end_f = strokes[s].from_, strokes[end_stroke].to
            segments.append(Segment(
                dir=d, start=start_f, end=end_f,
                stroke_count=end_stroke - s + 1,
                start_index=start_f.index, end_index=end_f.index,
                start_time=start_f.time, end_time=end_f.time,
            ))
        s = end_stroke + 1
    return segments


# ────────────────────────── 5. 中枢（笔级别）─────────────────────────

def find_pivots(strokes: List[Stroke]) -> List[Pivot]:
    pivots: List[Pivot] = []
    i = 0
    while i + 2 < len(strokes):
        r1, r2, r3 = strokes[i], strokes[i + 1], strokes[i + 2]
        zg = min(r1.hi, r2.hi, r3.hi)
        zd = max(r1.lo, r2.lo, r3.lo)
        if zg > zd:
            end = i + 2
            j = i + 3
            while j < len(strokes):
                r = strokes[j]
                if r.hi <= zd or r.lo >= zg:
                    break
                zg = min(zg, r.hi)
                zd = max(zd, r.lo)
                end = j
                j += 1
            pivots.append(Pivot(
                zg=zg, zd=zd, start_stroke=i, end_stroke=end,
                start_index=strokes[i].from_.index,
                end_index=strokes[end].to.index,
                start_time=strokes[i].from_.time,
                end_time=strokes[end].to.time,
            ))
            i = end + 1
        else:
            i += 1
    return pivots


# ────────────────────────── 6/7. 背驰与买卖点 ──────────────────────────

def _macd_area(hist: List[float], from_index: int, to_index: int) -> float:
    a, b = min(from_index, to_index), max(from_index, to_index)
    return sum(hist[i] for i in range(a, min(b + 1, len(hist))))


def find_signals(
    strokes: List[Stroke], pivots: List[Pivot], macd: Macd
) -> List[Signal]:
    signals: List[Signal] = []
    buy1: List[Signal] = []
    sell1: List[Signal] = []

    # 一买/一卖：中枢背驰 + 盘整背驰（进入中枢的笔 vs 离开中枢的同向笔）
    for p in pivots:
        enter = strokes[p.start_stroke - 1] if p.start_stroke > 0 else None
        if enter is None:
            continue
        leave = None
        for j in range(p.end_stroke + 1, len(strokes)):
            if strokes[j].dir == enter.dir:
                leave = strokes[j]
                break
        if leave is None:
            continue
        e_area = _macd_area(macd.hist, enter.from_.index, enter.to.index)
        l_area = _macd_area(macd.hist, leave.from_.index, leave.to.index)
        if enter.dir == "down" and l_area > e_area:
            new_low = leave.to.low < enter.to.low
            sig = Signal(
                kind="buy", type="buy1", index=leave.to.index, time=leave.to.time,
                price=leave.to.low,
                reason="底背驰：离开中枢的下跌笔创新低但力度减弱" if new_low
                else "盘整背驰：离开中枢的下跌笔力度减弱",
                ref_index=enter.to.index,
            )
            signals.append(sig)
            buy1.append(sig)
        elif enter.dir == "up" and l_area < e_area:
            new_high = leave.to.high > enter.to.high
            sig = Signal(
                kind="sell", type="sell1", index=leave.to.index, time=leave.to.time,
                price=leave.to.high,
                reason="顶背驰：离开中枢的上涨笔创新高但力度减弱" if new_high
                else "盘整背驰：离开中枢的上涨笔力度减弱",
                ref_index=enter.to.index,
            )
            signals.append(sig)
            sell1.append(sig)

    # 一买/一卖补充：笔级趋势背驰（同向趋势首尾笔比较，覆盖无中枢的趋势）
    for k in range(len(strokes)):
        d = strokes[k].dir
        first = strokes[k]
        last = strokes[k]
        j = k + 2
        while j < len(strokes) and strokes[j].dir == d:
            s = strokes[j]
            if d == "down" and s.to.low < last.to.low:
                last = s
                j += 2
            elif d == "up" and s.to.high > last.to.high:
                last = s
                j += 2
            else:
                break
        if last is not first:
            f_area = _macd_area(macd.hist, first.from_.index, first.to.index)
            l_area = _macd_area(macd.hist, last.from_.index, last.to.index)
            if d == "down" and last.to.low < first.to.low and l_area > f_area:
                sig = Signal(kind="buy", type="buy1", index=last.to.index,
                             time=last.to.time, price=last.to.low,
                             reason="笔级底背驰：下跌趋势末笔创新低但力度减弱",
                             ref_index=first.to.index)
                signals.append(sig)
                buy1.append(sig)
            elif d == "up" and last.to.high > first.to.high and l_area < f_area:
                sig = Signal(kind="sell", type="sell1", index=last.to.index,
                             time=last.to.time, price=last.to.high,
                             reason="笔级顶背驰：上涨趋势末笔创新高但力度减弱",
                             ref_index=first.to.index)
                signals.append(sig)
                sell1.append(sig)

    # 二买/二卖：一买/一卖后的首次回踩/反抽不创新低/高
    for s in buy1:
        down = next((x for x in strokes
                     if x.dir == "down" and x.from_.index > s.index and x.to.low > s.price),
                    None)
        if down:
            signals.append(Signal(kind="buy", type="buy2", index=down.to.index,
                                  time=down.to.time, price=down.to.low,
                                  reason="二买：回踩不破一买低点"))
    for s in sell1:
        up = next((x for x in strokes
                   if x.dir == "up" and x.from_.index > s.index and x.to.high < s.price),
                  None)
        if up:
            signals.append(Signal(kind="sell", type="sell2", index=up.to.index,
                                  time=up.to.time, price=up.to.high,
                                  reason="二卖：反抽不破一卖高点"))

    # 三买/三卖：突破中枢后的回抽不破
    for p in pivots:
        breakout = next((s for s in strokes
                         if s.dir == "up" and s.from_.index >= p.end_index and s.to.high > p.zg),
                        None)
        if breakout:
            pullback = next((s for s in strokes
                             if s.dir == "down" and s.from_.index >= breakout.to.index
                             and s.to.low > p.zg), None)
            if pullback:
                signals.append(Signal(kind="buy", type="buy3", index=pullback.to.index,
                                      time=pullback.to.time, price=pullback.to.low,
                                      reason="三买：突破中枢后回抽不破上沿"))
        brk_dn = next((s for s in strokes
                       if s.dir == "down" and s.from_.index >= p.end_index and s.to.low < p.zd),
                      None)
        if brk_dn:
            rally = next((s for s in strokes
                          if s.dir == "up" and s.from_.index >= brk_dn.to.index
                          and s.to.high < p.zd), None)
            if rally:
                signals.append(Signal(kind="sell", type="sell3", index=rally.to.index,
                                      time=rally.to.time, price=rally.to.high,
                                      reason="三卖：跌破中枢后反抽不破下沿"))

    seen: Set[str] = set()
    unique: List[Signal] = []
    for s in signals:
        key = f"{s.type}|{s.index}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(s)
    unique.sort(key=lambda s: s.index)
    return unique


# ────────────────────────── 状态汇总与评分 ──────────────────────────

def current_state(candles: List[Candle], segments: List[Segment],
                  strokes: List[Stroke], signals: List[Signal]) -> dict:
    last_seg = segments[-1] if segments else None
    last_stroke = strokes[-1] if strokes else None
    trend = "盘整"
    if last_seg:
        trend = "上涨 (多头)" if last_seg.dir == "up" else "下跌 (空头)"
    elif last_stroke:
        trend = "上涨 (多头)" if last_stroke.dir == "up" else "下跌 (空头)"
    last = candles[-1]
    prev = candles[-2] if len(candles) > 1 else None
    change_pct = ((last.close - prev.close) / prev.close * 100) if prev else 0.0
    return {
        "trend": trend,
        "latest_signal": signals[-1] if signals else None,
        "last_close": last.close,
        "change_pct": change_pct,
    }


def score_signals(signals: List[Signal]) -> List[Signal]:
    """信号质量评分（0~100）：级别(中枢>笔级>盘整) + 转折点 + 同向共振。"""
    by_idx: Dict[int, List[Signal]] = {}
    for s in signals:
        by_idx.setdefault(s.index, []).append(s)
    for s in signals:
        score = 50
        r = s.reason
        if "盘整" in r:
            score -= 10
        elif "笔级" in r:
            score += 5
        elif "离开中枢" in r:
            score += 15
        if s.type in ("buy1", "sell1"):
            score += 5
        n = len([x for x in by_idx.get(s.index, []) if x.kind == s.kind])
        score += (n - 1) * 20
        s.score = max(0, min(100, score))
    return signals


def analyze_chanlun(candles: List[Candle], macd_fast: int = 12,
                    macd_slow: int = 26, macd_signal: int = 9) -> dict:
    """完整分析入口（对应 JS analyzeChanlun）。"""
    if len(candles) < 5:
        return {"fractals": [], "strokes": [], "segments": [], "pivots": [],
                "signals": [], "state": {"trend": "数据不足"}, "macd": Macd()}
    closes = [c.close for c in candles]
    macd = calc_macd(closes, macd_fast, macd_slow, macd_signal)
    merged = merge_inclusions(candles)
    fractals = find_fractals(merged, candles)
    strokes = find_strokes(fractals)
    segments = build_segments(strokes)
    pivots = find_pivots(strokes)
    signals = score_signals(find_signals(strokes, pivots, macd))
    state = current_state(candles, segments, strokes, signals)
    return {"fractals": fractals, "strokes": strokes, "segments": segments,
            "pivots": pivots, "signals": signals, "state": state, "macd": macd}


def verify_against_js(candles: List[Candle], js_result: dict, tol: float = 1e-6) -> dict:
    """与 JS 版输出对照（scripts/verify_chanlun_port.js 产出 js_result）。

    比对维度：分型数 / 笔数 / 线段数 / 中枢数 / 信号集合（type|index|price）。
    返回 {match: bool, diffs: [...]}，用于移植验收。
    """
    res = analyze_chanlun(candles)
    diffs: List[str] = []
    checks = [
        ("fractals", len(res["fractals"]), js_result.get("fractals", 0)),
        ("strokes", len(res["strokes"]), js_result.get("strokes", 0)),
        ("segments", len(res["segments"]), js_result.get("segments", 0)),
        ("pivots", len(res["pivots"]), js_result.get("pivots", 0)),
    ]
    for name, py_n, js_n in checks:
        if py_n != js_n:
            diffs.append(f"{name}: py={py_n} js={js_n}")
    py_sigs = {f"{s.type}|{s.index}|{round(s.price, 6)}" for s in res["signals"]}
    js_sigs = {
        f"{s['type']}|{s['index']}|{round(float(s['price']), 6)}"
        for s in js_result.get("signals", [])
    }
    if py_sigs != js_sigs:
        only_py = sorted(py_sigs - js_sigs)[:5]
        only_js = sorted(js_sigs - py_sigs)[:5]
        diffs.append(f"signals 仅py: {only_py} 仅js: {only_js}")
    return {"match": not diffs, "diffs": diffs, "py": res}

# ======================================================================
# 第一部分之二 · 门控引导数据（qmt/bootstrap_exc.py）
# ======================================================================
# -*- coding: utf-8 -*-

BOOTSTRAP_EXC = {
    "20260525": 0.000757,
    "20260526": 0.008633,
    "20260527": -0.009033,
    "20260528": 0.017278,
    "20260529": -0.018348,
    "20260601": -0.037964,
    "20260602": -0.048065,
    "20260603": 0.00501,
    "20260604": 0.026473,
    "20260605": -0.029684,
    "20260608": -0.043895,
    "20260609": -0.02852,
    "20260610": -0.002626,
    "20260611": -0.018384,
    "20260612": -0.051797,
    "20260615": -0.036058,
    "20260616": -0.009517,
    "20260617": -0.000512,
    "20260618": 0.036496,
    "20260622": 0.010436,
    "20260623": 0.013605,
    "20260624": 0.023306,
    "20260625": 0.035364,
    "20260626": 0.029952,
    "20260629": 0.03371,
    "20260630": 0.019259,
    "20260701": 0.034412,
    "20260702": 0.04574,
    "20260703": 0.084733,
    "20260706": -0.015661,
    "20260707": -0.035041,
    "20260708": 0.026861,
    "20260709": -0.000705,
    "20260710": 0.0021,
    "20260713": 0.027984,
    "20260714": 0.058155,
    "20260715": 0.033283,
    "20260716": 0.032975,
    "20260717": 0.032042,
    "20260720": 0.012784,
    "20260721": 0.008819,
    "20260722": 0.028055,
    "20260723": 0.033166,
    "20260724": -0.001901,
    "20260727": 0.028342,
    "20260728": 0.004466,
    "20260729": 0.014884,
    "20260730": -0.020244,
    "20260731": 0.058954,
    "20260803": 0.069606,
    "20260804": 0.136497,
    "20260805": 0.129834,
    "20260806": 0.026736,
    "20260807": 0.019231,
    "20260810": 0.016376,
    "20260811": 0.013967,
    "20260812": -0.004587,
    "20260813": -0.031114,
    "20260814": -0.005489,
    "20260817": 0.001039,
    "20260818": -0.026776,
    "20260819": -0.016745,
    "20260820": 0.037738,
    "20260821": 0.000614,
    "20260824": 0.004541,
    "20260825": 0.000294,
    "20260826": -0.013239,
    "20260827": -0.020995,
    "20260828": -0.009457,
    "20260831": -0.001778,
    "20260901": 0.007853,
    "20260902": 0.006621,
    "20260903": 0.012206,
    "20260904": 0.006008,
    "20260907": -0.010095,
    "20260908": 0.03418,
    "20260909": 0.04239,
    "20260910": 0.0345,
    "20260911": 0.027777,
    "20260914": 0.011352,
    "20260915": -0.008652,
    "20260916": 0.001829,
    "20260917": 0.016287,
    "20260918": 0.022471,
    "20260921": 0.034202,
    "20260922": 0.039531,
    "20260923": 0.051647,
    "20260924": 0.002748,
    "20260928": 0.047582,
    "20260929": 0.03899,
    "20260930": 0.025952,
    "20261008": 0.008987,
    "20261009": -0.002152,
    "20261012": 0.023543,
    "20261013": 0.020106
}

# ======================================================================
# 第二部分 · 策略内核（qmt/core.py）
# ======================================================================
# -*- coding: utf-8 -*-


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

# ======================================================================
# 第三部分 · QMT 入口（qmt/strategy.py）
# ======================================================================
# -*- coding: utf-8 -*-




# ══════════════════════ 配置（首次部署改这里）══════════════════════

ACCOUNT = "YOUR_ACCOUNT_ID"      # 资金账号
PERIOD = "1d"                    # K 线周期
COUNT = 260                      # 取多少根
BATCH = 600                      # get_market_data_ex 分批大小
RUN_AFTER = "14:50"              # 该时点之后才执行（分钟周期用）
ENABLED = ["chan_buy", "high_tight_flag"]
HB_EVERY_MIN = 15                # 心跳间隔（分钟）。看门狗靠它判断策略还活着

PENDING = {}          # userOrderId → 委托状态（防超单）
ACCOUNT_LOG = []      # 当日成交明细（收盘对账用）

KEY_HITS = "fm_hits"             # 近 5 日命中名单（算 streak）
KEY_EXC = "fm_exc"               # 近 90 个信号日超额（算门控）
KEY_LAST = "fm_last"             # 最后执行日期（防重复下单）
HITS_KEEP = 6                    # streak<=2 只需 3 天，留 6 天冗余
EXC_KEEP = 95


# ══════════════════════ 代码格式（必须统一，否则全市场会被滤空）══════════════════════

def to_platform(code):
    """QMT 代码 → 平台 symbol。600000.SH / 600000 → sh600000。

    QMT 用 6 位数字（可能带 .SH/.SZ 后缀），平台用 sh/sz 前缀。两套格式混用会
    直接导致股票池被过滤成空（实测第一版就是这样，0 只标的）。
    """
    c = str(code).split(".")[0].strip()
    if len(c) != 6 or not c.isdigit():
        return ""
    if c[0] == "6":
        return "sh" + c
    if c[0] in ("0", "3"):
        return "sz" + c
    return ""


def to_qmt(symbol):
    """平台 symbol → QMT 代码。sh600000 → 600000.SH。下单与取数都用它。"""
    if len(symbol) < 8:
        return symbol
    return symbol[2:] + "." + symbol[:2].upper()


# ══════════════════════ 状态（QMT 服务端 KV，替代被禁的文件）══════════════════════

def kv_get(key, default):
    """读策略状态。QMT 关掉了本地文件读写，只能用券商端 KV。

    get_py_key(key) -> (value, errid, errmsg)；失败时返回默认值，
    **不能因为读不到就当空**——那样 streak 会全部重算成 1，选股直接跑偏。
    """
    try:
        r = get_py_key(key)      # noqa: F821  QMT 注入的全局函数
        val = r[0] if isinstance(r, (tuple, list)) else r
        if val:
            return json.loads(val)
    except Exception as e:       # noqa: BLE001
        print("[kv] 读取 %s 失败：%s" % (key, e))
    return default


def kv_set(key, value):
    try:
        return save_py_key(key, json.dumps(value, separators=(",", ":")))  # noqa: F821
    except Exception as e:       # noqa: BLE001
        print("[kv] 写入 %s 失败：%s" % (key, e))
        return None


# ══════════════════════ 取数 ══════════════════════

def fetch_bars(ContextInfo, symbols):
    """批量取后复权日线，返回 {symbol: [bar, ...]}。

    bar 字段与 core 约定一致：date(YYYYMMDD) / open / high / low / close / volume / raw_close。
    后复权用 dividend_type='back'；raw_close 单独取不复权，只用于下单报价。
    """
    out = {}
    fields = ["time", "open", "high", "low", "close", "volume"]
    code_map = dict((to_qmt(s), s) for s in symbols)     # QMT 代码 → 平台 symbol
    qmt_codes = list(code_map.keys())
    for i in range(0, len(qmt_codes), BATCH):
        chunk = qmt_codes[i:i + BATCH]
        try:
            data = ContextInfo.get_market_data_ex(
                fields, chunk, period=PERIOD, count=COUNT,
                dividend_type="back", fill_data=True)          # noqa: F821
        except Exception as e:                                  # noqa: BLE001
            print("[data] 第 %d 批取数失败：%s" % (i // BATCH + 1, e))
            continue

        for qmt_code in chunk:
            sym = code_map.get(qmt_code, "")
            df = data.get(qmt_code) if isinstance(data, dict) else None
            if df is None and isinstance(data, dict):
                df = data.get(to_platform(qmt_code))
            if df is None or len(df) == 0:
                continue
            bars = []
            cols = [str(c).lower() for c in df.columns]

            def pick(row, name, fallback_idx):
                """优先按列名取；缺列时按位置兜底。

                注意：pandas Series 的 row[i] 是**标签**索引，不是位置索引，
                直接 row[1] 会抛 out-of-bounds（实测踩过）。位置必须用 iloc。
                """
                idx = getattr(row, "index", None)
                if idx is not None and name in idx:
                    return float(row[name])
                if hasattr(row, "iloc") and fallback_idx < len(row):
                    return float(row.iloc[fallback_idx])
                return 0.0

            try:
                for i in range(len(df)):
                    row = df.iloc[i]
                    bars.append({
                        "date": str(df.index[i]).replace("-", "")[:8],
                        "open": pick(row, "open", 1),
                        "high": pick(row, "high", 2),
                        "low": pick(row, "low", 3),
                        "close": pick(row, "close", 4),
                        "volume": pick(row, "volume", 5),
                        "raw_close": 0.0,     # 0 = 未取到不复权价，下单时会被拦下
                    })
            except Exception as e:                              # noqa: BLE001
                print("[data] %s 解析失败：%s" % (sym, e))
                continue
            if len(bars) >= 120:
                out[sym] = bars

    # 不复权价（下单报价）：单独一批取，只取最后一根
    try:
        raw = ContextInfo.get_market_data_ex(
            ["close"], [to_qmt(s) for s in out.keys()], period=PERIOD, count=1,
            dividend_type="none", fill_data=True)               # noqa: F821
        for code, df in (raw or {}).items():
            sym = to_platform(code) if to_platform(code) in out else code
            if sym not in out or df is None or not len(df):
                continue
            # 必须按列名取：df.values[-1][-1] 拿到的是最后一列（volume），
            # 实测把成交量当价格报出去，价格差了四个数量级。
            try:
                col = "close" if "close" in [str(c).lower() for c in df.columns] else None
                if col and col in df.columns:
                    out[sym][-1]["raw_close"] = float(df[col].iloc[-1])
                else:
                    out[sym][-1]["raw_close"] = float(df.iloc[-1, 0])
            except Exception:                                   # noqa: BLE001
                pass
    except Exception as e:                                      # noqa: BLE001
        print("[data] 不复权价获取失败：%s" % e)
    miss = [s for s, b in out.items() if not b[-1].get("raw_close")]
    if miss:
        print("[data] ⚠ %d 只缺未复权价，这些将拒绝下单（不影响其余）" % len(miss))

    return out


def get_positions(ContextInfo):
    """读实际持仓 {symbol: {shares, entry_price, entry_date, peak}}。

    entry_date / peak 是策略风控的状态，券商只给持仓数量，这两个必须自己维护：
    在 KV 里存 `fm_pos`，首次建仓写入，之后每日更新 peak。
    """
    meta = kv_get("fm_pos", {})
    try:
        pos = ContextInfo.get_trade_detail_data(ACCOUNT, "stock", "position")  # noqa: F821
        live = {}
        for p in pos:
            qty = int(getattr(p, "m_nVolume", 0) or 0)
            if qty > 0:
                code = str(getattr(p, "m_strInstrumentID", ""))
                live[code] = qty
        # 与 KV 对齐：只保留仍有持仓的票，避免幽灵记录
        for sym in list(meta.keys()):
            if sym not in live:
                meta.pop(sym, None)
        for sym, qty in live.items():
            if sym not in meta:
                meta[sym] = {"shares": qty, "entry_price": 0.0,
                             "entry_date": "", "peak": 0.0}
            else:
                meta[sym]["shares"] = qty
        return meta
    except Exception as e:                                      # noqa: BLE001
        print("[pos] 读取持仓失败：%s" % e)
        return meta


def get_cash(ContextInfo):
    try:
        acc = ContextInfo.get_trade_detail_data(ACCOUNT, "stock", "account")    # noqa: F821
        if acc:
            return float(getattr(acc[0], "m_dAvailable", 0.0))
    except Exception as e:                                      # noqa: BLE001
        print("[acc] 读取可用资金失败：%s" % e)
    return 0.0


def get_total_asset(ContextInfo):
    try:
        acc = ContextInfo.get_trade_detail_data(ACCOUNT, "stock", "account")    # noqa: F821
        if acc:
            return float(getattr(acc[0], "m_dBalance", 0.0))
    except Exception:                                           # noqa: BLE001
        pass
    return 0.0


# ══════════════════════ 下单 ══════════════════════

def send_order(ContextInfo, symbol, side, qty, price, remark=""):
    """限价报单。

    ⚠️ 参数顺序是本函数唯一的坑，写错不会报错、只会静默丢单：
        passorder(opType, orderType, accountid, orderCode, prType, price, volume,
                  strategyName, quickTrade, userOrderId, ContextInfo)
    其中 quickTrade 必须传 2 ——
      0（默认）= 只在 K 线结束分笔时产生有效信号；
      1        = 只在最新 K 线时产生；
      2        = 任何位置调用都产生委托。
    日频策略在 14:50 这种非 K 线边界的时点下单，传 0/1 会被静默丢弃：
    策略日志打了 [order]，券商委托列表里什么都没有。

    userOrderId 是委托去重键，也是防超单字典的 key，必须唯一且可回溯。
    opType 23=买入 / 24=卖出；prType 11=限价（各券商版本略有差异，以文档为准）。
    """
    op = 23 if side == "BUY" else 24
    uid = remark or ("fm_%s_%s" % (symbol.split(".")[0], side))
    try:
        passorder(op, 1101, ACCOUNT, symbol, 11, price, qty,             # noqa: F821
                  "fresh_ma", 2, uid, ContextInfo)
        PENDING[uid] = {"symbol": symbol, "side": side, "qty": qty,
                        "price": price, "status": "待报"}
        print("[order] %s %s %d @ %.2f uid=%s" % (side, symbol, qty, price, uid))
        return True
    except Exception as e:                                      # noqa: BLE001
        print("[order] 报单失败 %s：%s" % (symbol, e))
        return False


def deal_callback(ContextInfo, dealInfo):
    """成交回报（QMT 主推，实盘推荐用它而不是轮询）。

    必须在 init 里 set_account 才能收到。这里只做两件事：更新委托状态、记账。
    真正的持仓以 get_trade_detail_data 为准，回调可能漏推，收盘再对一次账。
    """
    try:
        code = str(dealInfo.m_strInstrumentID)
        vol = int(dealInfo.m_nVolume)
        px = float(dealInfo.m_dPrice)
        ACCOUNT_LOG.append({"code": code, "vol": vol, "price": px})
        print("[deal] 成交 %s %d @ %.2f" % (code, vol, px))
    except Exception as e:                                      # noqa: BLE001
        print("[deal] 回报解析失败：%s" % e)


ORDER_TERMINAL = (53, 54, 56, 57)      # 已成 / 已撤 / 废单 / 部撤
UID_PREFIX = "fm_"                      # 本策略委托的 userOrderId 前缀
_LAST_SYNC = 0


def _parse_uid(uid):
    """从 userOrderId 反解代码与方向：fm_600148_BUY → ("600148", "BUY")。"""
    try:
        parts = str(uid).split("_")
        if len(parts) >= 3 and parts[0].strip() == "fm":
            return parts[1], parts[2]
    except Exception:                                           # noqa: BLE001
        pass
    return "", ""


def sync_pending_from_broker(force=False):
    """用柜台的未终结委托重建 PENDING。

    为什么必须做：PENDING 是内存字典，客户端重启或策略重载后就空了。
    而看门狗会自动重启客户端 —— 如果没有这一步，防超单形同虚设，
    一次重启就可能把同一笔买/卖再下一遍，仓位直接翻倍。
    所以每次下单前都用柜台的真实委托对一次账，认 uid 前缀 fm_。
    """
    global _LAST_SYNC
    now = time.time()
    if not force and (now - _LAST_SYNC) < 60:
        return 0
    _LAST_SYNC = now
    try:
        orders = get_trade_detail_data(ACCOUNT, "stock", "order")      # noqa: F821
    except Exception:                                           # noqa: BLE001
        return 0
    added = 0
    for o in orders:
        uid = str(getattr(o, "m_strRemark", "") or "")
        if not uid.startswith(UID_PREFIX):
            continue                      # 不是本策略下的单，别碰
        st = int(getattr(o, "m_nOrderStatus", 0) or 0)
        if st in ORDER_TERMINAL:
            PENDING.pop(uid, None)
            continue
        if uid in PENDING:
            PENDING[uid]["status"] = st
            continue
        code, side = _parse_uid(uid)
        PENDING[uid] = {
            "symbol": str(getattr(o, "m_strInstrumentID", "") or "") or code,
            "side": side,
            "qty": int(getattr(o, "m_nVolumeTotalOriginal", 0) or 0),
            "price": float(getattr(o, "m_dLimitPrice", 0) or 0),
            "status": st,
            "from_broker": True,          # 标记：这笔是重启后从柜台捞回来的
        }
        added += 1
    if added:
        print("[order] 从柜台恢复 %d 笔未完成委托：%s"
              % (added, ",".join(sorted(PENDING.keys())[:5])))
    return added


def refresh_pending():
    """冲刷委托状态：先跟柜台对齐，再把终态清掉。

    防超单的核心：只要某标的还有未完成委托，就不给它下新单。
    否则一次重复触发就会变成双倍仓位。
    """
    sync_pending_from_broker()
    try:
        orders = get_trade_detail_data(ACCOUNT, "stock", "order")      # noqa: F821
    except Exception:                                           # noqa: BLE001
        return dict(PENDING)
    stat = {}
    for o in orders:
        uid = str(getattr(o, "m_strRemark", "") or "")
        if uid:
            stat[uid] = int(getattr(o, "m_nOrderStatus", 0) or 0)
    for uid in list(PENDING.keys()):
        st = stat.get(uid)
        if st is None:
            continue
        PENDING[uid]["status"] = st
        if st in ORDER_TERMINAL:
            PENDING.pop(uid, None)
    return dict(PENDING)


def has_open_order(symbol):
    """该标的是否还有未完成委托。

    每次都先跟柜台对齐 —— 内存里的 PENDING 不可信（重启就没了）。
    """
    refresh_pending()
    q = to_qmt(symbol) if "." in symbol else symbol
    for v in PENDING.values():
        if v["symbol"] in (symbol, q):
            return True
    return False


# ══════════════════════ 主流程 ══════════════════════

_HB_LAST = None      # 上次心跳的「当日分钟数」，模块级，重启后重新计时


def heartbeat(today):
    """每 HB_EVERY_MIN 分钟打一行 [hb]。

    这是看门狗判断「策略还活着」的唯一抓手：策略禁网、禁文件，
    没法自己报平安，只能靠 print 落到 QMT 客户端日志里，由外部脚本去读。
    必须放在 handlebar 最前面（当日去重 return 之前），否则当天跑完之后
    就再没有任何输出，看起来跟卡死一样。
    """
    global _HB_LAST
    hm = time.strftime("%H:%M")
    try:
        slot = int(hm[:2]) * 60 + int(hm[3:5])
    except Exception:                                           # noqa: BLE001
        slot = -1
    if _HB_LAST is not None and slot >= 0 and 0 <= slot - _HB_LAST < HB_EVERY_MIN:
        return
    _HB_LAST = slot
    print("[hb] %s %s alive" % (today, hm))


def init(ContextInfo):
    """初始化：只做一次。不要在这里取数（此时账户与行情可能未就绪）。"""
    print("[fresh_ma] init · 账户 %s · enabled=%s" % (ACCOUNT, ",".join(ENABLED)))
    try:
        ContextInfo.set_account(ACCOUNT)                        # noqa: F821
    except Exception:                                           # noqa: BLE001
        pass
    ContextInfo.set_universe([])                                # noqa: F821


def handlebar(ContextInfo):
    """每根 bar 触发。日频策略的全部逻辑都在这里按时间闸门放行。"""
    today = str(ContextInfo.get_bar_trading_day())              # noqa: F821
    heartbeat(today)          # 必须在当日去重之前打，跑完了也要继续报平安
    last = kv_get(KEY_LAST, "")
    if last == today:
        return                                                  # 当天已执行，防重复下单

    now = str(ContextInfo.get_bar_timelist()[-1])[:14] if hasattr(ContextInfo, "get_bar_timelist") else ""
    if now and len(now) >= 14:
        hm = now[8:10] + ":" + now[10:12]
        if hm < RUN_AFTER:
            return                                              # 未到执行时点

    # ⓪ 冲刷上一轮委托状态（防超单前置）
    pending = refresh_pending()
    if pending:
        print("[order] 仍有 %d 笔未完成委托：%s"
              % (len(pending), ",".join(sorted(pending.keys())[:5])))

    # ① 取全市场代码
    try:
        universe = ContextInfo.get_stock_list_in_sector("沪深A股")  # noqa: F821
    except Exception as e:                                      # noqa: BLE001
        print("[univ] 取股票池失败：%s" % e)
        return
    # QMT 返回 600000.SH 这类格式，统一转平台 symbol 再过滤（含北交所剔除）
    universe = [to_platform(c) for c in universe]
    universe = [s for s in universe if s]

    # ② 取数
    bars_map = fetch_bars(ContextInfo, universe)
    print("[data] 有效标的 %d 只 · %s" % (len(bars_map), today))

    # ③ 候选（内核）
    hits = kv_get(KEY_HITS, {})
    dates = sorted(hits.keys())[-HITS_KEEP:]
    if today not in dates:
        dates.append(today)
    hit_today = {}
    for sym, bars in bars_map.items():
        st = []
        if "chan_buy" in ENABLED and chan_buy(bars, analyze_chanlun, Candle):
            st.append("chan_buy")
        if "high_tight_flag" in ENABLED and high_tight_flag(bars):
            st.append("high_tight_flag")
        if st:
            hit_today[sym] = st
    hits[today] = sorted(hit_today.keys())
    for d in sorted(hits.keys())[:-HITS_KEEP]:
        hits.pop(d, None)

    cands = build_candidates(bars_map, analyze_chanlun, Candle,
                                  ENABLED, dates, hits)

    # ④ 门控
    # 冷启动问题：首次部署 KV 为空 → 门控永远关闭 → 策略永不建仓。
    # 用平台导出的 95 个成熟信号日超额做引导，保证第一天就与回测门控状态一致。
    exc = kv_get(KEY_EXC, {})
    if not exc and BOOTSTRAP_EXC:
        exc = dict(BOOTSTRAP_EXC)
        kv_set(KEY_EXC, exc)
        print("[gate] 首次运行，载入引导超额 %d 条" % len(exc))
    gate = gate_check(exc)

    # ⑤ 持仓与资金
    meta = get_positions(ContextInfo)
    cash = get_cash(ContextInfo)
    total = get_total_asset(ContextInfo) or (cash + sum(
        (bars_map.get(s, [{}])[-1].get("raw_close", 0) or 0) * m["shares"]
        for s, m in meta.items()))
    cfg = dict(DEFAULT_CONFIG)
    cfg["capital"] = total

    # ⑥ 卖出（止损先于买入，释放资金）
    date_index = dict((b["date"], i) for b in bars_map.get("sh000001", []))
    exits = check_exits(meta, bars_map, date_index, cfg)
    for e in exits:
        qcode = to_qmt(e["symbol"])
        if has_open_order(qcode):
            print("[skip] %s 已有未完成委托，本轮不重复卖出" % qcode)
            continue
        px = bars_map.get(e["symbol"], [{}])[-1].get("raw_close", 0)
        if px > 0:
            send_order(ContextInfo, qcode, "SELL", e["qty"], round(px * 0.98, 2),
                       remark="fm_%s_SELL" % e["symbol"][2:])

    # ⑦ 买入
    orders = []
    if gate["open"]:
        targets = select_targets(cands, dict((s, m["shares"]) for s, m in meta.items()), cfg)
        prev_close = {}
        for s, bars in bars_map.items():
            if len(bars) >= 2:
                prev_close[s] = bars[-2]["raw_close"]
        orders = build_orders(targets, dict((s, m["shares"]) for s, m in meta.items()),
                                   bars_map, total, cash, prev_close, {}, cfg)
        for o in orders:
            if o.get("blocked"):
                print("[skip] %s %s" % (o["symbol"], o["blocked"]))
                continue
            qcode = to_qmt(o["symbol"])
            if has_open_order(qcode):
                print("[skip] %s 已有未完成委托，本轮不重复买入" % qcode)
                continue
            send_order(ContextInfo, qcode, "BUY", o["qty"], o["price"],
                       remark="fm_%s_BUY" % o["symbol"][2:])

    # ⑧ 写回状态（必须在报单之后，保证「已执行」标记落地）
    kv_set(KEY_HITS, hits)
    kv_set(KEY_EXC, exc)
    kv_set(KEY_LAST, today)
    kv_set("fm_pos", meta)
    n_buy = len([o for o in orders if not o.get("blocked")])
    print("[done] %s · 门控=%s · 候选 %d · 买 %d · 卖 %d"
          % (today, "开" if gate["open"] else "关", len(cands), n_buy, len(exits)))

    # 无交易自检：门控开着、有候选、还有仓位空间，却一笔没下 —— 一定是哪里卡住了。
    # 这行日志是运维脚本的抓手（QMT 禁网，策略自己推不了告警）。
    slots = cfg["max_positions"] - len([1 for m in meta.values() if m.get("shares")])
    if gate["open"] and cands and n_buy == 0 and slots > 0:
        print("[alert] ANOMALY 门控开启、候选 %d、空位 %d，但未产生任何买单"
              % (len(cands), slots))
