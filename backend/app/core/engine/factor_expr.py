"""因子表达式引擎：以『表达式』声明因子，引擎解释执行，新增因子仅需登记一条定义。

设计目标（呼应 Qlib 表达式范式 + alphalens 因子工厂）：
- 因子不再写死在 Python 里，而是存为表达式字符串（如 ``roe``、``std(returns(c_v))``、
  ``safe_inv(pe_ttm, 0, 1000)``），引擎在受限命名空间内求值；
- 新增质量/成长/分析师预期因子 = 在 factor_library 登记一条定义 + 保证底层数据字段就绪，
  无需改动策略选股逻辑 —— 因子自动进入 IC 研究、分层分析、合成打分与前端展示。

安全性：表达式在 ``{"__builtins__": {}}`` 的受限全局下 eval，仅暴露白名单函数与调用方注入的
变量。因子定义由开发者维护（factor_library.py），非用户输入，故 eval 风险可控。
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Optional

# ============================ 表达式可用函数库 ============================
def _returns(s: List[float]) -> List[float]:
    """简单收益率序列 r_t = p_t/p_{t-1} - 1。"""
    return [s[i] / s[i - 1] - 1.0 for i in range(1, len(s))]


def _std(s: List[float]) -> float:
    return statistics.pstdev(s) if len(s) > 1 else 1e9


def _mean(s: List[float]) -> float:
    return statistics.fmean(s) if s else 0.0


def _sum(s: List[float]) -> float:
    return sum(s)


def _min(s: List[float]) -> float:
    return min(s) if s else 0.0


def _max(s: List[float]) -> float:
    return max(s) if s else 0.0


def _roc(s: List[float], n: int) -> float:
    """n 期收益率（价格比 - 1）。"""
    n = int(n)
    if len(s) <= n or n < 0 or s[-1 - n] == 0:
        return 0.0
    return s[-1] / s[-1 - n] - 1.0


def _skew(s: List[float]) -> float:
    """收益率偏度（总体定义，与旧 _skewness 一致）。"""
    n = len(s)
    if n < 3:
        return 0.0
    m = statistics.fmean(s)
    sd = statistics.pstdev(s)
    if sd <= 0:
        return 0.0
    return sum((x - m) ** 3 for x in s) / n / (sd ** 3)


def _maxdd(s: List[float]) -> float:
    """区间最大回撤（返回负数，越接近 0 越好，与旧 _max_drawdown 一致）。"""
    if len(s) < 2:
        return 0.0
    peak = s[0]
    mdd = 0.0
    for c in s:
        if c > peak:
            peak = c
        if peak > 0:
            dd = c / peak - 1.0
            if dd < mdd:
                mdd = dd
    return mdd


def _beta_idio(stock: List[float], mkt: List[float]):
    """对股票与基准收益率序列做 CAPM，返回 (beta, 残差波动率)。"""
    xs, ys = [], []
    n_min = min(len(stock), len(mkt))
    for i in range(1, n_min):
        sc0 = stock[i - 1]
        if i - 1 < len(mkt) and mkt[i - 1] and mkt[i] and mkt[i - 1] > 0 and sc0 > 0:
            xs.append(stock[i] / sc0 - 1.0)
            ys.append(mkt[i] / mkt[i - 1] - 1.0)
    n = len(xs)
    if n < 20:
        return 0.0, 0.0
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    cov = sum((xs[k] - mx) * (ys[k] - my) for k in range(n))
    beta = cov / vx if vx > 0 else 0.0
    resid = [xs[k] - beta * ys[k] for k in range(n)]
    idio = statistics.pstdev(resid)
    return beta, idio


def _beta(stock: List[float], mkt: List[float]) -> float:
    return _beta_idio(stock, mkt)[0]


def _idio_vol(stock: List[float], mkt: List[float]) -> float:
    return _beta_idio(stock, mkt)[1]


def _zscore(s: List[float]) -> List[float]:
    mm = statistics.fmean(s)
    sd = statistics.pstdev(s)
    return [(x - mm) / sd if sd > 0 else 0.0 for x in s]


def _rank(s: List[float]) -> List[float]:
    """分位排名 0..1。"""
    if not s:
        return s
    order = sorted(range(len(s)), key=lambda i: s[i])
    r = [0.0] * len(s)
    denom = len(s) - 1
    for pos, i in enumerate(order):
        r[i] = pos / denom if denom > 0 else 0.0
    return r


def _winsor(s: List[float], p: float = 0.05) -> List[float]:
    if not s:
        return s
    srt = sorted(s)
    lo = srt[max(0, int(p * len(s)))]
    hi = srt[min(len(s) - 1, int((1 - p) * len(s)))]
    return [min(max(x, lo), hi) for x in s]


def _safe_inv(x: Optional[float], lo: float, hi: float) -> Optional[float]:
    """有界倒数：x 在 (lo, hi) 内返回 1/x，否则 None（用于 EP/BP 估值因子，剔除无效 PE/PB）。"""
    if x is None:
        return None
    if x <= lo or x >= hi:
        return None
    return 1.0 / x


def _ifnull(x: Optional[float], y: float) -> float:
    return y if x is None else x


FUNCS: Dict[str, Any] = {
    "returns": _returns, "std": _std, "mean": _mean, "sum": _sum,
    "min": _min, "max": _max, "roc": _roc, "skew": _skew, "maxdd": _maxdd,
    "beta": _beta, "idio_vol": _idio_vol,
    "zscore": _zscore, "rank": _rank, "winsor": _winsor,
    "safe_inv": _safe_inv, "ifnull": _ifnull,
    "log": math.log, "abs": abs, "sqrt": math.sqrt,
    "sign": lambda x: (x > 0) - (x < 0), "exp": math.exp, "pow": pow,
}

_SAFE_GLOBALS: Dict[str, Any] = {"__builtins__": {}}


def eval_factor(expr: str, ns: Dict[str, Any]) -> Optional[float]:
    """在受限命名空间内求值因子表达式。

    返回 float 或 None：
    - 表达式求值异常（如除零、变量缺失、类型错误）一律返回 None，交由上层用截面中位数填充；
    - 结果非有限值（nan/inf）同样返回 None。
    """
    if not expr:
        return None
    try:
        val = eval(expr, _SAFE_GLOBALS, {**FUNCS, **ns})  # noqa: S307 受限命名空间，因子定义受控
    except Exception:
        return None
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f
