"""缠论结构分解引擎（quantdesk js/chanlun.js 的 Python 移植，2026-09-30）。

处理流程与原实现一一对应：
  1. K线包含关系合并（merge_inclusions）
  2. 分型（顶/底，在合并后 K 线上识别）
  3. 笔（顶底分型交替连接，端点可被更极端分型延长，STROKE_GAP=4）
  4. 线段（特征序列法）
  5. 中枢（三个连续笔的重叠区间 ZG/ZD，可被后续笔扩展）
  6. 背驰（中枢背驰 + 笔级趋势背驰，MACD 面积对比）
  7. 买卖点（一/二/三买，一/二/三卖）+ 共振评分（0~100）

移植保真度：结构、阈值、判定顺序与 JS 版逐行对齐；数值差异只可能来自
浮点求和顺序（影响 MACD 面积的极端边界情况）。一致性由
scripts/verify_chanlun_port.js + tests 对照用例保障（见 verify_chanlun）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
    ref_index: int | None = None


@dataclass
class Macd:
    dif: list[float] = field(default_factory=list)
    dea: list[float] = field(default_factory=list)
    hist: list[float] = field(default_factory=list)


# ────────────────────────── 0. MACD（utils.js calcMACD 对应）──────────────────────────

def _ema(values: list[float], period: int) -> list[float]:
    k = 2.0 / (period + 1)
    out: list[float] = []
    prev = values[0]
    out.append(prev)
    for v in values[1:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def calc_macd(closes: list[float], fast: int = 12, slow: int = 26,
              signal: int = 9) -> Macd:
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = _ema(dif, signal)
    hist = [(d - e) * 2 for d, e in zip(dif, dea)]
    return Macd(dif=dif, dea=dea, hist=hist)


# ────────────────────────── 1. K线包含关系合并 ──────────────────────────

def merge_inclusions(candles: list[Candle]) -> list[Merged]:
    merged: list[Merged] = []
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

def find_fractals(merged: list[Merged], candles: list[Candle]) -> list[Fractal]:
    out: list[Fractal] = []
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

def find_strokes(fractals: list[Fractal]) -> list[Stroke]:
    strokes: list[Stroke] = []
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

def build_segments(strokes: list[Stroke]) -> list[Segment]:
    segments: list[Segment] = []
    if len(strokes) < 3:
        return segments
    s = 0
    while s + 2 < len(strokes):
        d = strokes[s].dir
        feats: list[tuple[int, float, float]] = []
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

def find_pivots(strokes: list[Stroke]) -> list[Pivot]:
    pivots: list[Pivot] = []
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

def _macd_area(hist: list[float], from_index: int, to_index: int) -> float:
    a, b = min(from_index, to_index), max(from_index, to_index)
    return sum(hist[i] for i in range(a, min(b + 1, len(hist))))


def find_signals(
    strokes: list[Stroke], pivots: list[Pivot], macd: Macd
) -> list[Signal]:
    signals: list[Signal] = []
    buy1: list[Signal] = []
    sell1: list[Signal] = []

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

    seen: set[str] = set()
    unique: list[Signal] = []
    for s in signals:
        key = f"{s.type}|{s.index}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(s)
    unique.sort(key=lambda s: s.index)
    return unique


# ────────────────────────── 状态汇总与评分 ──────────────────────────

def current_state(candles: list[Candle], segments: list[Segment],
                  strokes: list[Stroke], signals: list[Signal]) -> dict:
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


def score_signals(signals: list[Signal]) -> list[Signal]:
    """信号质量评分（0~100）：级别(中枢>笔级>盘整) + 转折点 + 同向共振。"""
    by_idx: dict[int, list[Signal]] = {}
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


def analyze_chanlun(candles: list[Candle], macd_fast: int = 12,
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


def verify_against_js(candles: list[Candle], js_result: dict, tol: float = 1e-6) -> dict:
    """与 JS 版输出对照（scripts/verify_chanlun_port.js 产出 js_result）。

    比对维度：分型数 / 笔数 / 线段数 / 中枢数 / 信号集合（type|index|price）。
    返回 {match: bool, diffs: [...]}，用于移植验收。
    """
    res = analyze_chanlun(candles)
    diffs: list[str] = []
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
