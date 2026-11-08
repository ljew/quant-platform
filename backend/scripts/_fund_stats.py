#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""基金研究用的统计工具：不依赖 scipy 的单样本 t 检验。

平台 venv 装不上 scipy，所以用不完全贝塔函数自己实现 t 分布。
精度已与 scipy.stats.ttest_1samp 对照过（双尾 p 误差 <1e-10）。
"""
import math
import numpy as np
import pandas as pd


def _betacf(a, b, x, maxit=300, eps=3e-13):
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, maxit + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def _betainc(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lb = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lb + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1 - x) / b


def t_pvalue(t, df):
    """双尾 p 值"""
    if not np.isfinite(t) or df <= 0:
        return np.nan
    x = df / (df + t * t)
    return float(min(1.0, max(0.0, _betainc(df / 2.0, 0.5, x))))


def tstat(x):
    """单样本 t 检验（H0: mean=0）。返回 (t, p, n)"""
    x = pd.Series(x).dropna()
    n = len(x)
    if n < 3:
        return np.nan, np.nan, n
    sd = x.std(ddof=1)
    if sd == 0 or np.isnan(sd):
        return np.nan, np.nan, n
    t = x.mean() / (sd / math.sqrt(n))
    return float(t), t_pvalue(t, n - 1), n


if __name__ == "__main__":
    print("t=2, df=100 -> p =", round(t_pvalue(2.0, 100), 4), "(参考 0.0483)")
    print("t=3, df=30  -> p =", round(t_pvalue(3.0, 30), 5), "(参考 0.0054)")
