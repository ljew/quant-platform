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
