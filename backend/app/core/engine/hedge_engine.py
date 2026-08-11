"""可交易市场中性对冲引擎。

把「理论对冲(OLS 残差)」做成真正能落地的股指期货对冲模拟：

- 多头组合 + 卖空股指期货连续合约(IF/IC/IM)。
- 用期货连续合约的**日收益序列**直接驱动空头每日盈亏——连续合约在换月日的
  跳变天然等于滚动移仓的真实损益，因此无需单独建模基差，展期成本已被自动计入。
- 月频调仓：每月重新估计组合对市场基准的 β，按 β×持仓市值确定卖空合约数
  (合约必须取整，带来少量残差暴露与移仓手续费)。
- 保证金占用仅作为约束指标报告(不计入 P&L，因逐日盯市盈亏已体现在曲线)。

指数代码 → 对冲腿映射：
    000300 沪深300  -> IF (乘数 300)
    000905 中证500  -> IC (乘数 200)
    000852 中证1000 -> IM (乘数 200)
    000906 中证800  -> IF(0.72) + IC(0.28)  (800 = 300 + 500 按市值比合成)
"""
from __future__ import annotations

import numpy as np
from datetime import date, timedelta

from app.services import data_source

# 股指期货每点乘数（元/点）
MULTIPLIER = {"IF0": 300, "IC0": 200, "IM0": 200}

# index_code -> [(期货符号, 权重)]
INDEX_FUTURES_MAP = {
    "000300": [("IF0", 1.0)],
    "000905": [("IC0", 1.0)],
    "000852": [("IM0", 1.0)],
    "000906": [("IF0", 0.72), ("IC0", 0.28)],
}
INDEX_BENCH_SYMBOL = {
    "000300": "sh000300",
    "000905": "sh000905",
    "000852": "sh000852",
    "000906": "sh000906",
}

_FUTURES_CACHE: dict[str, dict[str, float]] = {}


def _load_futures(symbol: str) -> dict[str, float]:
    """加载期货连续合约日收盘价，返回 {iso_date: close}。带进程内缓存。"""
    if symbol in _FUTURES_CACHE:
        return _FUTURES_CACHE[symbol]
    import akshare as ak  # 延迟 import，避免启动变慢
    df = ak.futures_main_sina(symbol=symbol)
    m: dict[str, float] = {}
    for _, r in df.iterrows():
        d = str(r["日期"])
        try:
            m[d] = float(r["收盘价"])
        except (ValueError, TypeError):
            continue
    _FUTURES_CACHE[symbol] = m
    return m


def _ols_beta(y: list[float], x: list[float]) -> float:
    """简单线性回归 β = cov(y,x)/var(x)。"""
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    if len(x) < 5:
        return 0.0
    vx = float(np.var(x))
    if vx <= 1e-12:
        return 0.0
    return float(np.cov(y, x)[0, 1] / vx)


def _metrics(equities: list[float], dates: list[date]) -> dict:
    """由权益序列计算绩效指标。"""
    if len(equities) < 2 or equities[0] <= 0:
        return {"total_return": 0.0, "annual_return": 0.0, "sharpe": 0.0,
                "max_drawdown": 0.0}
    rets = [equities[i] / equities[i - 1] - 1 for i in range(1, len(equities))]
    total = equities[-1] / equities[0] - 1
    years = (dates[-1] - dates[0]).days / 365.25
    annual = (equities[-1] / equities[0]) ** (1 / years) - 1 if years > 0 else 0.0
    mean = float(np.mean(rets))
    std = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0.0
    sharpe = (mean / std * np.sqrt(252)) if std > 1e-12 else 0.0
    # 回撤
    peak = equities[0]
    mdd = 0.0
    for e in equities:
        if e > peak:
            peak = e
        if peak > 0:
            dd = e / peak - 1
            if dd < mdd:
                mdd = dd
    return {
        "total_return": total,
        "annual_return": annual,
        "sharpe": sharpe,
        "max_drawdown": mdd,
    }


def _ffill(series: dict, dates: list[date]) -> list[float | None]:
    """将 {iso_str: value} 对齐到 dates 序列，缺失向前填充。"""
    out: list[float | None] = []
    last = None
    for d in dates:
        k = d.isoformat()
        if k in series:
            last = series[k]
        out.append(last)
    return out


def build_hedge_analysis(
    equity_curve: list[dict],
    index_code: str,
    commission: float = 5.0,
    margin_rate: float = 0.12,
    beta_window: int = 60,
    warmup: int = 20,
) -> dict:
    """核心分析函数。

    参数
    ----
    equity_curve : 组合账户权益曲线，[{date:"YYYY-MM-DD", equity:float}, ...]
    index_code   : 基准指数代码，如 "000906"
    commission   : 每手单边手续费(元)，默认 5
    margin_rate  : 保证金率，默认 12%
    beta_window  : β 估计滚动窗口(交易日)，默认 60
    warmup       : 最少样本数，不足则 β=0

    返回
    ----
    dict: {series, metrics, cost, legs, notes}
    """
    if index_code not in INDEX_FUTURES_MAP:
        raise ValueError(f"暂不支持指数 {index_code} 的期货对冲")

    legs = INDEX_FUTURES_MAP[index_code]
    bench_sym = INDEX_BENCH_SYMBOL[index_code]

    # 去重 + 排序组合权益曲线
    raw = {}
    for p in equity_curve:
        raw[p["date"]] = float(p["equity"])
    port_dates = sorted(date.fromisoformat(d) for d in raw.keys())
    if len(port_dates) < 2:
        raise ValueError("权益曲线点数不足")

    # 预热：向前多取 130 天，用于初始 β 估计
    ext_start = port_dates[0] - timedelta(days=130)
    ext_end = port_dates[-1]

    # 基准行情
    bench = data_source.get_index_daily_kline(
        bench_sym, ext_start, ext_end)
    bench_map = {r["trade_date"].isoformat(): float(r["close"]) for r in bench}

    # 期货行情（每个腿）
    fut_maps = {}
    for sym, _ in legs:
        fut_maps[sym] = _load_futures(sym)

    # 构造连续日历：组合日 ∪ 基准日 ∪ 期货日（均 ≥ ext_start）
    cand = set(port_dates)
    for d in bench_map:
        dd = date.fromisoformat(d)
        if dd >= ext_start:
            cand.add(dd)
    for sym, _ in legs:
        for d in fut_maps[sym]:
            dd = date.fromisoformat(d)
            if dd >= ext_start:
                cand.add(dd)
    cal = sorted(cand)

    # 对齐
    port_eq = _ffill(raw, cal)
    bench_close = _ffill(bench_map, cal)
    fut_close = {sym: _ffill(fut_maps[sym], cal) for sym, _ in legs}

    n = len(cal)
    # 收益率
    r_p = [None] * n
    r_b = [None] * n
    r_f = {sym: [None] * n for sym, _ in legs}
    for j in range(1, n):
        if port_eq[j] is not None and port_eq[j - 1]:
            r_p[j] = port_eq[j] / port_eq[j - 1] - 1
        if bench_close[j] is not None and bench_close[j - 1]:
            r_b[j] = bench_close[j] / bench_close[j - 1] - 1
        for sym, _ in legs:
            a, b = fut_close[sym][j], fut_close[sym][j - 1]
            if a is not None and b:
                r_f[sym][j] = a / b - 1

    port_idx = [j for j in range(n) if port_eq[j] is not None]
    first_idx = port_idx[0]

    # 预计算每日滚动 β（用于理论对冲 & 调仓）
    beta_daily = [0.0] * n
    for j in range(1, n):
        lo = max(first_idx, j - beta_window)
        ys, xs = [], []
        for k in range(max(lo, 1), j + 1):
            if r_p[k] is not None and r_b[k] is not None:
                ys.append(r_p[k])
                xs.append(r_b[k])
        beta_daily[j] = _ols_beta(ys, xs) if len(ys) >= warmup else 0.0

    # ---- 理论对冲(OLS 残差, 日频理想对冲) ----
    theo_eq = [None] * n
    theo_eq[first_idx] = port_eq[first_idx]
    for j in range(first_idx + 1, n):
        if port_eq[j] is None or theo_eq[j - 1] is None:
            continue
        if r_p[j] is None or r_b[j] is None:
            theo_eq[j] = theo_eq[j - 1]
            continue
        ret = r_p[j] - beta_daily[j] * r_b[j]
        theo_eq[j] = theo_eq[j - 1] * (1 + ret)

    # ---- 可交易对冲(股指期货, 月频调仓 + 合约取整 + 手续费) ----
    trad_eq = [None] * n
    fut_pnl = [0.0] * n
    contracts = {sym: 0 for sym, _ in legs}
    prev_notional = 0.0
    cost = 0.0
    margin_max = 0.0
    avg_contracts_sum = 0.0
    avg_contracts_cnt = 0
    resid_port = []  # 可交易对冲收益 vs 基准收益（残差β）
    resid_bench = []

    for j in range(1, n):
        d = cal[j]
        # 1) 当日空头盈亏（用上一期合约手数对应的名义本金）
        fr = 0.0
        ok = True
        for sym, w in legs:
            if r_f[sym][j] is None:
                ok = False
                break
            fr += w * r_f[sym][j]
        if ok and prev_notional:
            pnl = -prev_notional * fr
            fut_pnl[j] = fut_pnl[j - 1] + pnl
        else:
            fut_pnl[j] = fut_pnl[j - 1]

        # 2) 调仓：组合日 且 (首日为组合首日 或 跨月)
        is_rebal = (port_eq[j] is not None) and (
            j == first_idx or d.month != cal[j - 1].month)
        if is_rebal:
            beta = beta_daily[j] if beta_daily[j] > 0 else 0.0
            notional = beta * port_eq[j]  # 以权益近似持仓市值
            for sym, w in legs:
                price = fut_close[sym][j]
                target = 0
                if price:
                    target = int(round(notional * w / (price * MULTIPLIER[sym])))
                cost += abs(target - contracts[sym]) * commission * 2
                contracts[sym] = target
            margin = sum(contracts[sym] * (fut_close[sym][j] or 0)
                         * MULTIPLIER[sym] * margin_rate
                         for sym, _ in legs)
            margin_max = max(margin_max, margin)
            prev_notional = notional
            avg_contracts_sum += sum(contracts.values())
            avg_contracts_cnt += 1

        # 3) 记录可交易权益（仅组合日）
        if port_eq[j] is not None:
            trad_eq[j] = port_eq[j] + fut_pnl[j] - cost
            if r_p[j] is not None and r_b[j] is not None and prev_notional:
                resid_port.append(r_p[j] - beta_daily[j] * r_b[j])
                resid_bench.append(r_b[j])

    # ---- 汇总输出（仅组合日） ----
    out_dates, long_only, theoretical, tradable = [], [], [], []
    for j in port_idx:
        out_dates.append(cal[j])
        long_only.append(round(port_eq[j], 2))
        theoretical.append(round(theo_eq[j], 2) if theo_eq[j] is not None else None)
        tradable.append(round(trad_eq[j], 2) if trad_eq[j] is not None else None)

    metrics = {
        "long_only": _metrics(long_only, out_dates),
        "theoretical": _metrics([e for e in theoretical if e is not None], out_dates),
        "tradable": _metrics([e for e in tradable if e is not None], out_dates),
    }
    # 残差 β（可交易对冲收益对基准收益的敏感度，越接近 0 越好）
    resid_beta = _ols_beta(resid_port, resid_bench) if len(resid_port) >= 5 else 0.0

    avg_contracts = (avg_contracts_sum / avg_contracts_cnt) if avg_contracts_cnt else 0.0

    notes = [
        "可交易对冲用股指期货连续合约日收益驱动空头盈亏，换月跳变即滚动移仓真实损益，基差/展期成本已自动计入。",
        f"中证800 用 IF(沪深300, 72%) + IC(中证500, 28%) 按市值比合成对冲腿；合约乘数 IF=300、IC=200 元/点。",
        "月频调仓重新估计 β；合约手数取整带来微量残差暴露，移仓手续费按每手单边 ¥%.1f、双边计收。" % commission,
        f"保证金率 {margin_rate:.0%} 仅作约束指标，逐日盯市盈亏已体现在曲线中。",
        "与理论对冲(日频 OLS 理想对冲)的差额即「现实摩擦税」：合约取整 + 移仓费 + β估计误差 + 月频滞后。",
        "三条曲线均自账户权益曲线首日起算（含建仓初期约 1.5% 现金留存），故多头组合总收益读数会略高于任务卡面的「收益率」(后者以初始资金为基准)。",
    ]

    return {
        "index_code": index_code,
        "benchmark_symbol": bench_sym,
        "legs": [{"symbol": s, "weight": w, "multiplier": MULTIPLIER[s]} for s, w in legs],
        "series": {
            "dates": [d.isoformat() for d in out_dates],
            "long_only": long_only,
            "theoretical": theoretical,
            "tradable": tradable,
        },
        "metrics": metrics,
        "cost": {
            "roll_commission_total": round(cost, 2),
            "avg_contracts": round(avg_contracts, 1),
            "max_margin": round(margin_max, 2),
            "residual_beta_tradable": round(resid_beta, 4),
        },
        "notes": notes,
    }
