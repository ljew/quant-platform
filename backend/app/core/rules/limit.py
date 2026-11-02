"""A 股涨跌停幅度（2026 规则）—— 回测与实盘共用的唯一实现。

为什么放在 `core/rules` 而不是 `live/`：
    `core/engine`（回测）需要它，但 engine 不应反向依赖 live（实盘层）。
    `live/guard.py` 从这里 re-export，原有导入路径不变。

为什么必须共用：
    回测侧曾用固定 9.5% 一刀切，实盘侧用分板块规则，两侧口径不一致 →
    回测净值与实盘表现系统性背离（创业板在 +9.5% 被误判买不进，ST 在 +5%
    已封板却放行）。涨跌停幅度属于「市场规则」，不属于某一侧的实现细节。

依据（2026 规则）：主板 10%、ST/*ST 5%、科创板/创业板 20%、北交所 30%。
"""
from __future__ import annotations

#: 拦截缓冲（百分点）。封板价附近量能不足，且浮点比较需要容差，
#: 因此触发阈值取「法定幅度 − 0.5pp」。实盘 live/guard.py 用同一常量语义。
LIMIT_BUFFER = 0.005


def limit_pct(symbol: str, name: str = "") -> float:
    """返回该标的的涨跌停幅度（0.05 / 0.10 / 0.20 / 0.30）。

    Args:
        symbol: 带交易所前缀的代码（sh/sz/bj）。无前缀时按主板 10% 处理。
        name: 证券名称，仅用于识别 ST。回测侧若无法注入名称，ST 会退化为 10%
              —— 这是已知偏差，方向是「放行本应封板的票」（偏乐观），已在
              strategy-backtest-validation 的引擎卫生清单里记为待补项。
    """
    s = (symbol or "").strip().lower()
    if "ST" in (name or "").upper():
        return 0.05
    if s.startswith(("sh68", "sz30")):
        return 0.20
    if s.startswith("bj"):
        return 0.30
    return 0.10


def limit_trigger(symbol: str, name: str = "") -> float:
    """返回触发涨跌停拦截的日内涨跌幅阈值（已扣缓冲）。

    涨停判定 `chg >= limit_trigger(...)`、跌停判定 `chg <= -limit_trigger(...)`，
    与实盘 `live/guard.py` 的 `pc * (1 ± (limit_pct - 0.005))` 完全等价。
    """
    return limit_pct(symbol, name) - LIMIT_BUFFER
