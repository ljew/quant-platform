"""因子表达式引擎：以『表达式』声明因子，引擎解释执行，新增因子仅需登记一条定义。

设计目标（呼应 Qlib 表达式范式 + alphalens 因子工厂）：
- 因子不再写死在 Python 里，而是存为表达式字符串（如 ``roe``、``std(returns(c_v))``、
  ``safe_inv(pe_ttm, 0, 1000)``），引擎在受限命名空间内求值；
- 量价与现金流类因子同样可表达：``corr(returns(c_v), returns(vol_v))``（量价相关）、
  ``mean(vol_r) / mean(vol_v)``（放量倍数）、``fcf_yield``（自由现金流收益率）；
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


def _corr(x: List[float], y: List[float]) -> float:
    """Pearson 相关系数（两序列按尾部对齐）。量价相关、背离类因子的核心算子。"""
    n = min(len(x or []), len(y or []))
    if n < 5:
        return 0.0
    xs, ys = x[-n:], y[-n:]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    vx = sum((a - mx) ** 2 for a in xs)
    vy = sum((b - my) ** 2 for b in ys)
    if vx <= 0 or vy <= 0:
        return 0.0
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    return cov / math.sqrt(vx * vy)


def _slope(s: List[float]) -> float:
    """相对趋势斜率：OLS(y~t) 斜率 ÷ mean(|y|)，无量纲，正=上行、负=下行。

    除以均值绝对值是为了抵消量纲（成交量、成交额、价格都能直接比大小）。
    """
    n = len(s or [])
    if n < 5:
        return 0.0
    mt = (n - 1) / 2.0
    my = statistics.fmean(s)
    denom = sum((i - mt) ** 2 for i in range(n))
    if denom <= 0:
        return 0.0
    num = sum((i - mt) * (s[i] - my) for i in range(n))
    scale = statistics.fmean([abs(v) for v in s]) or 1.0
    return num / denom / scale


def _diff(s: List[float]) -> List[float]:
    """一阶差分序列（长度 n-1）。"""
    return [s[i] - s[i - 1] for i in range(1, len(s))]


def _last(s: List[float]) -> float:
    return s[-1] if s else 0.0


def _median(s: List[float]) -> float:
    return statistics.median(s) if s else 0.0


def _count(s) -> float:
    return float(len(s))


def _div0(x: Optional[float], y: Optional[float]) -> float:
    """安全除法：分子或分母为空、分母为 0 时返回 0（不报错、不产生 inf）。"""
    if x is None or y is None or y == 0:
        return 0.0
    return x / y


FUNCS: Dict[str, Any] = {
    "returns": _returns, "std": _std, "mean": _mean, "sum": _sum,
    "min": _min, "max": _max, "roc": _roc, "skew": _skew, "maxdd": _maxdd,
    "beta": _beta, "idio_vol": _idio_vol, "corr": _corr, "slope": _slope,
    "zscore": _zscore, "rank": _rank, "winsor": _winsor, "diff": _diff,
    "last": _last, "median": _median, "count": _count,
    "safe_inv": _safe_inv, "div0": _div0, "ifnull": _ifnull,
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
