"""技术指标库（纯函数，输入/输出均为 list[float]，warmup 区间返回 None）。

设计原则：
- 无第三方依赖，便于回测引擎与实盘复用；
- 输出长度与输入一致，前 (period-1) 个位置为 None（数据不足）；
- 回测时每根 K 线只取 .pop() 的当前值即可。
"""
from __future__ import annotations

from typing import List, Optional


def sma(values: List[float], period: int) -> List[Optional[float]]:
    """简单移动平均。"""
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def ema(values: List[float], period: int) -> List[Optional[float]]:
    """指数移动平均。前 period-1 个位置标记为 None（视为未稳定）。"""
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    k = 2.0 / (period + 1)
    prev = values[0]
    out[0] = values[0]
    for i in range(1, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    for i in range(period - 1):
        out[i] = None
    return out


def roc(values: List[float], period: int) -> List[Optional[float]]:
    """变动率（Rate of Change）= 当前价 / N 日前价格 - 1。"""
    n = len(values)
    out: List[Optional[float]] = [None] * n
    for i in range(period, n):
        base = values[i - period]
        if base != 0:
            out[i] = values[i] / base - 1.0
    return out


def stddev(values: List[float], period: int) -> List[Optional[float]]:
    """滚动标准差（总体方差）。"""
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        out[i] = var ** 0.5
    return out


def rsi(values: List[float], period: int = 14) -> List[Optional[float]]:
    """相对强弱指数（Wilder 平滑）。"""
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, n):
        diff = values[i] - values[i - 1]
        gain = diff if diff >= 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


# ——— 完整版 SDK 扩展指标（设计 v1.0 ctx.indicator：MACD/BOLL/ATR/KDJ/OBV 等） ———

def macd(values: List[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD：返回 (dif, dea, hist) 三个与输入等长的序列（前段为 None）。"""
    n = len(values)
    dif: List[Optional[float]] = [None] * n
    ema_f = ema(values, fast)
    ema_s = ema(values, slow)
    for i in range(n):
        if ema_f[i] is not None and ema_s[i] is not None:
            dif[i] = ema_f[i] - ema_s[i]  # type: ignore[operator]
    # DEA = DIF 的 EMA(signal)
    valid = [(i, v) for i, v in enumerate(dif) if v is not None]
    dea: List[Optional[float]] = [None] * n
    if valid:
        base = [v for _, v in valid]
        ema_dea = ema(base, signal)
        for (i, _), v in zip(valid, ema_dea):
            dea[i] = v
    hist: List[Optional[float]] = [None] * n
    for i in range(n):
        if dif[i] is not None and dea[i] is not None:
            hist[i] = (dif[i] - dea[i]) * 2.0  # type: ignore[operator]
    return dif, dea, hist


def boll(values: List[float], period: int = 20, k: float = 2.0):
    """布林带：返回 (mid, upper, lower) 三个等长序列。"""
    n = len(values)
    mid: List[Optional[float]] = [None] * n
    upper: List[Optional[float]] = [None] * n
    lower: List[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return mid, upper, lower
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        m = sum(window) / period
        var = sum((x - m) ** 2 for x in window) / period
        sd = var ** 0.5
        mid[i] = m
        upper[i] = m + k * sd
        lower[i] = m - k * sd
    return mid, upper, lower


def atr(high: List[float], low: List[float], close: List[float], period: int = 14) -> List[Optional[float]]:
    """平均真实波幅（Wilder 平滑）。需要 high/low/close 三个序列。"""
    n = len(high)
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    trs: List[float] = []
    for i in range(1, n):
        tr = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        trs.append(tr)
    first = sum(trs[:period]) / period
    out[period] = first
    prev = first
    for i in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i + 1] = prev
    return out


def kdj(high: List[float], low: List[float], close: List[float], n: int = 9, m1: int = 3, m2: int = 3):
    """随机指标 KDJ：返回 (k, d, j) 三个等长序列。"""
    size = len(close)
    k: List[Optional[float]] = [None] * size
    d: List[Optional[float]] = [None] * size
    j: List[Optional[float]] = [None] * size
    if size < n:
        return k, d, j
    k_prev, d_prev = 50.0, 50.0
    for i in range(n - 1, size):
        hh = max(high[i - n + 1 : i + 1])
        ll = min(low[i - n + 1 : i + 1])
        rsv = 0.0 if hh == ll else (close[i] - ll) / (hh - ll) * 100.0
        k_cur = (m1 - 1) / m1 * k_prev + 1.0 / m1 * rsv
        d_cur = (m2 - 1) / m2 * d_prev + 1.0 / m2 * k_cur
        k[i], d[i], j[i] = k_cur, d_cur, 3.0 * k_cur - 2.0 * d_cur
        k_prev, d_prev = k_cur, d_cur
    return k, d, j


def obv(close: List[float], volume: List[float]) -> List[Optional[float]]:
    """能量潮 OBV：收盘涨加量、跌减量。"""
    n = len(close)
    out: List[Optional[float]] = [None] * n
    if n == 0:
        return out
    acc = 0.0
    out[0] = 0.0
    for i in range(1, n):
        if close[i] > close[i - 1]:
            acc += volume[i]
        elif close[i] < close[i - 1]:
            acc -= volume[i]
        out[i] = acc
    return out


def mom(values: List[float], period: int = 10) -> List[Optional[float]]:
    """动量 = 当前价 - N 日前价格。"""
    n = len(values)
    out: List[Optional[float]] = [None] * n
    for i in range(period, n):
        out[i] = values[i] - values[i - period]
    return out


def bias(values: List[float], period: int = 6) -> List[Optional[float]]:
    """乖离率 = (收盘价 - N日均线) / N日均线。"""
    n = len(values)
    out: List[Optional[float]] = [None] * n
    ma = sma(values, period)
    for i in range(period - 1, n):
        if ma[i]:
            out[i] = (values[i] - ma[i]) / ma[i]  # type: ignore[operator]
    return out


def willr(high: List[float], low: List[float], close: List[float], period: int = 14) -> List[Optional[float]]:
    """威廉指标 W%R = -100 * (HH - close) / (HH - LL)。"""
    n = len(close)
    out: List[Optional[float]] = [None] * n
    if n < period:
        return out
    for i in range(period - 1, n):
        hh = max(high[i - period + 1 : i + 1])
        ll = min(low[i - period + 1 : i + 1])
        out[i] = -100.0 * (hh - close[i]) / (hh - ll) if hh != ll else 0.0
    return out
