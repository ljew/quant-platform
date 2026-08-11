"""组合回测引擎（多标的、横截面因子、定期调仓）。

与单标的 BacktestEngine 并存：本引擎面向指数增强、多因子选股等组合策略，
输入为『股票池各标的日K + 基准指数日K』，按交易日驱动，在每个调仓日把
当期横截面行情注入策略，由策略完成选股与调仓。

设计要点：
- 多头组合、按目标仓位（百分比）调仓；
- 成交价采用当日收盘价（收盘调仓模型），计入佣金与滑点；
- 停牌/未上市标的采用前向填充（ffill）定价，但因子历史只取真实交易日；
- 基准为传入的指数日K，输出策略净值、基准净值、超额收益与信息比率(IR)。
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, List, Optional

from app.core.engine.base_strategy import Mode, PortfolioStrategy

logger = logging.getLogger("quant.portfolio")


@dataclass
class PTrade:
    date: str
    symbol: str
    side: str          # BUY / SELL
    price: float
    shares: float
    cash_after: float
    commission: float
    pnl: float = 0.0   # SELL 的已实现盈亏
    signal_type: str = ""     # 触发该笔成交的信号类型（如"选股买入"/"调出清仓"）
    signal_reason: str = ""   # 触发该笔成交的数据支撑说明（为何买/卖）


@dataclass
class PEquityPoint:
    date: str
    equity: float
    benchmark: float
    hedged: float = 0.0   # 市场中性（对冲 beta 后）净值


@dataclass
class PortfolioResult:
    universe_size: int
    symbols_used: int
    start: str
    end: str
    strategy_key: str
    params: dict
    initial_cash: float
    final_equity: float
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float
    benchmark_total_return: float
    excess_return: float
    info_ratio: float
    trade_count: int
    win_rate: float = 0.0   # 调仓区间超额收益为正的占比（组合胜率）
    equity_curve: List[PEquityPoint] = field(default_factory=list)
    trades: List[PTrade] = field(default_factory=list)
    holdings: List[dict] = field(default_factory=list)          # 每次调仓后的持仓权重快照
    industry_distribution: dict = field(default_factory=dict)    # 期末行业分布 {行业: 权重}
    factor_analysis: Optional[dict] = None                        # 因子 IC/IR 研究（None=未计算/单标的）
    # 市场中性（对冲 beta）视角下的净值与绩效
    hedged_beta: float = 0.0
    hedged_total_return: float = 0.0
    hedged_annual_return: float = 0.0
    hedged_sharpe: float = 0.0
    hedged_max_drawdown: float = 0.0


class PortfolioContext:
    """注入给组合策略的运行时上下文（回测版）。"""

    def __init__(self, engine: "PortfolioBacktestEngine", params: dict):
        self.engine = engine
        self.params = params
        self.mode = Mode.BACKTEST
        self.date: Optional[str] = None

    # —— 行情查询 ——
    def universe(self) -> List[str]:
        """当前调仓日的合法股票池。

        若引擎注入了时点成分股成员资格(membership)，则只返回『截至今日的
        最新成分股快照』内的标的，从而消除前视/幸存者偏差；否则返回全部已载入标的。
        """
        if self.date is None or not self.engine.membership:
            return self.engine.universe
        mem = self.engine._members_on(self.date)
        if not mem:
            return self.engine.universe
        return [s for s in self.engine.universe if s in mem]

    def price(self, symbol: str) -> Optional[float]:
        return self.engine._price_today(symbol)

    def history(self, symbol: str, n: int) -> Optional[List[float]]:
        """截至今日的真实交易日收盘价序列（最近 n 个）。不足返回 None。"""
        h = self.engine.hist[symbol]
        if len(h) < n:
            return None
        return h[-n:]

    def history_dates(self, symbol: str, n: int) -> Optional[List[str]]:
        """与 history() 对齐的交易日序列（最近 n 个）。"""
        h = self.engine.hist_dates.get(symbol, [])
        if len(h) < n:
            return None
        return h[-n:]

    def benchmark_aligned(self, symbol: str) -> List[Optional[float]]:
        """返回与『该标的自身交易日』对齐的基准收盘价序列（无基准日为 None）。

        用于计算 BETA / 特异度(残差波动) 等需要市场收益的因子：直接按标的
        自身的 hist_dates 取基准收盘价，完美规避个股停牌/数据缺口导致的错位。
        """
        dates = self.engine.hist_dates.get(symbol, [])
        return [self.engine.bench_map.get(d) for d in dates]

    def position(self, symbol: str) -> float:
        return self.engine.positions.get(symbol, 0.0)

    def positions(self) -> dict:
        return {k: v for k, v in self.engine.positions.items() if v > 0}

    def attribute(self, symbol: str, key: str):
        """查询标的截面属性（industry / market_cap / pe_ttm / pb 等）。"""
        return (self.engine.attributes.get(symbol, {}) or {}).get(key)

    def report_factor(self, symbol: str, name: str, value: float) -> None:
        """策略在 rebalance 中上报各因子暴露，供引擎做 IC/IR 因子研究。"""
        self.engine._pending_factors.append(
            {"symbol": symbol, "name": name, "value": float(value)}
        )

    def attributes_snapshot(self) -> dict:
        """当前持仓权重快照 {symbol: weight}。"""
        eq = self.engine._equity_today()
        snap: dict = {}
        if eq <= 0:
            return snap
        for sym, sh in self.engine.positions.items():
            if sh > 0:
                p = self.engine._price_today(sym)
                if p:
                    snap[sym] = round(sh * p / eq, 4)
        return snap

    # —— 下单 ——
    def order_target(self, symbol: str, shares: float, signal_type: str = "", signal_reason: str = "") -> None:
        self.engine._order_target(self, symbol, shares, signal_type, signal_reason)

    def order_target_percent(self, symbol: str, pct: float, signal_type: str = "", signal_reason: str = "") -> None:
        pct = max(0.0, min(1.0, float(pct)))
        price = self.engine._price_today(symbol)
        if price is None or price <= 0:
            return
        equity = self.engine._equity_today()
        # 保留现金缓冲，避免极端情况下满仓导致无法支付佣金/滑点
        avail = equity * (1.0 - self.engine.cash_buffer)
        target_shares = (avail * pct) / price
        self.engine._order_target(self, symbol, target_shares, signal_type, signal_reason)


class PortfolioBacktestEngine:
    def __init__(
        self,
        data: dict[str, List[dict]],
        benchmark: List[dict],
        initial_cash: float = 1_000_000.0,
        commission: float = 0.0003,
        slippage: float = 0.0,
        rebalance_period: int = 21,
        warmup: int = 60,
        min_commission: float = 5.0,
        lot_size: int = 100,
        limit_enabled: bool = True,
        cash_buffer: float = 0.005,
        attributes: dict | None = None,
        membership: list | None = None,
    ):
        self.data = data
        self.benchmark = benchmark
        self.initial_cash = float(initial_cash)
        self.commission = float(commission)
        self.slippage = float(slippage)
        self.rebalance_period = int(rebalance_period)
        self.warmup = int(warmup)
        # 实盘化参数
        self.min_commission = float(min_commission)
        self.lot_size = int(lot_size)
        self.limit_enabled = bool(limit_enabled)
        self.cash_buffer = float(cash_buffer)
        # 标的截面属性（行业/市值/估值），由调用方注入
        self.attributes: dict = attributes or {}
        # 时点(point-in-time)成分股成员资格：[(trade_date_str, {symbol}), ...] 升序。
        # 为空表示关闭 PIT 过滤（沿用全部已载入标的，兼容旧行为）。
        self.membership: list = list(membership) if membership else []
        self.holdings_snapshots: list[dict] = []
        # 因子研究（IC/IR）：上期因子记录 / 每期 IC 序列 / 当期待收集因子
        self.factor_records: list[dict] = []
        self.factor_ic_series: list[dict] = []
        self._pending_factors: list[dict] = []
        self._factor_history: list[dict] = []   # {date, records} 每调仓日的因子记录快照（用于IC衰减）

        # 交易日历：所有股票 + 基准的交易日并集，升序
        dset = set()
        for sym, bars in data.items():
            for b in bars:
                dset.add(b["date"])
        for b in benchmark:
            dset.add(b["date"])
        self.dates = sorted(dset)
        self.universe = list(data.keys())

        # 每只股票：date->close 映射 & 真实交易日收盘价历史（ffill 定价用 last_close）
        self.close_map = {
            sym: {b["date"]: float(b["close"]) for b in bars}
            for sym, bars in data.items()
        }
        self.hist = {sym: [] for sym in data}
        self.hist_dates = {sym: [] for sym in data}  # 与 hist 对齐的交易日
        self.positions: dict[str, float] = {sym: 0.0 for sym in data}
        self.avg_cost: dict[str, float] = {sym: 0.0 for sym in data}
        self.cash = self.initial_cash

        self.trades: List[PTrade] = []
        self.equity_curve: List[PEquityPoint] = []
        self._last_close = {sym: None for sym in data}

        # 基准对齐
        self.bench_map = {b["date"]: float(b["close"]) for b in benchmark}
        self.bench_first = benchmark[0]["close"] if benchmark else None
        self.bench_hist: list[float] = []  # 与 dates 对齐的基准净值（每日更新）

    # ——— 行情访问 ———
    def _price_today(self, symbol: str):
        return self._last_close.get(symbol)

    def _members_on(self, date_str: str) -> set:
        """返回截至 date_str 最新的成分股快照集合（时点成员资格）。"""
        if not self.membership:
            return set()
        res: set = set()
        for ds, s in self.membership:
            if ds <= date_str:
                res = s
            else:
                break
        return res

    def _equity_today(self) -> float:
        eq = self.cash
        for sym, sh in self.positions.items():
            p = self._last_close.get(sym)
            if p and sh > 0:
                eq += sh * p
        return eq

    # ——— 撮合 ———
    def _order_target(self, ctx: PortfolioContext, symbol: str, target_shares: float,
                      signal_type: str = "", signal_reason: str = "") -> None:
        price = self._last_close.get(symbol)
        if price is None or price <= 0:
            return
        cur = self.positions.get(symbol, 0.0)
        delta = target_shares - cur
        if abs(delta) < 1e-6:
            return
        is_buy = delta > 0
        # 涨跌停限制：涨停买不进、跌停卖不出（A股 ±10%，用 9.5% 阈值防浮点误差）
        if self.limit_enabled:
            h = self.hist.get(symbol, [])
            prev = h[-2] if len(h) >= 2 else None
            if prev and prev > 0:
                chg = price / prev - 1.0
                if is_buy and chg >= 0.095:
                    return
                if (not is_buy) and chg <= -0.095:
                    return
        # 最小交易单位（手）：向下取整到 lot_size 的整数倍
        if self.lot_size and self.lot_size > 1:
            signed = 1 if is_buy else -1
            delta = math.floor(abs(delta) / self.lot_size) * self.lot_size * signed
            if abs(delta) < 1e-6:
                return
        exec_price = price * (1 + self.slippage * (1 if is_buy else -1))
        notional = abs(delta) * exec_price
        commission = max(notional * self.commission, self.min_commission)
        pnl_val = 0.0
        if is_buy:
            new_shares = cur + delta
            if cur > 0:
                self.avg_cost[symbol] = (
                    self.avg_cost[symbol] * cur + exec_price * delta + commission
                ) / new_shares
            else:
                self.avg_cost[symbol] = exec_price + (commission / delta if delta else 0.0)
            self.positions[symbol] = new_shares
            self.cash -= notional + commission
            side = "BUY"
        else:
            pnl_val = (exec_price - self.avg_cost.get(symbol, exec_price)) * (-delta) - commission
            self.cash += notional - commission
            self.positions[symbol] = cur - (-delta)
            if self.positions[symbol] <= 1e-9:
                self.positions[symbol] = 0.0
                self.avg_cost[symbol] = 0.0
            side = "SELL"
        self.trades.append(
            PTrade(
                date=ctx.date or "",
                symbol=symbol,
                side=side,
                price=round(exec_price, 4),
                shares=round(abs(delta), 2),
                cash_after=round(self.cash, 2),
                commission=round(commission, 2),
                pnl=round(pnl_val, 2),
                signal_type=signal_type,
                signal_reason=signal_reason,
            )
        )

    # ——— 主循环 ———
    def run(self, strategy_class: type, params: dict) -> PortfolioResult:
        if not self.dates:
            raise ValueError("回测数据为空")
        strat: PortfolioStrategy = strategy_class()
        ctx = PortfolioContext(self, params)
        strat.init(ctx)

        n = len(self.dates)
        bench_last = self.bench_first

        for i, d in enumerate(self.dates):
            for sym in self.universe:
                c = self.close_map.get(sym, {}).get(d)
                if c is not None:
                    self._last_close[sym] = c
                    self.hist[sym].append(c)
                    self.hist_dates[sym].append(d)

            if d in self.bench_map:
                bench_last = self.bench_map[d]
            self.bench_hist.append(bench_last)
            bench_equity = (
                self.initial_cash * (bench_last / self.bench_first)
                if self.bench_first else self.initial_cash
            )

            if i >= self.warmup and (i - self.warmup) % self.rebalance_period == 0:
                ctx.date = d
                # 1) 用本期价格对上期因子记录算区间收益，得到本期截面 IC
                ic = self._compute_period_ic(d)
                if ic is not None:
                    self.factor_ic_series.append(ic)
                # 2) 执行策略调仓，期间策略通过 ctx.report_factor 填充 _pending_factors
                self._pending_factors = []
                try:
                    strat.rebalance(ctx, d)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"rebalance {d} failed: {e}")
                # 3) 保存本期因子记录（含本期价格），供下期计算 IC
                self.factor_records = self._collect_factor_records()
                # 3a) 存档本期的因子记录 + 全量价格快照，用于事后IC衰减/Brinson
                price_snap = {s: self._price_today(s) for s in self.universe}
                self._factor_history.append({
                    "date": d, "records": self.factor_records, "price_snap": price_snap,
                })
                # 4) 记录调仓后的持仓权重快照
                self.holdings_snapshots.append({"date": d, "positions": ctx.attributes_snapshot()})

            eq = self._equity_today()
            self.equity_curve.append(
                PEquityPoint(date=d, equity=round(eq, 2), benchmark=round(bench_equity, 2))
            )

        return self._build_result(strategy_class, params)

    # ——— 因子研究（IC/IR）———
    @staticmethod
    def _rank(vals):
        """平均秩（处理并列）。"""
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    @staticmethod
    def _spearman(xs, ys):
        n = len(xs)
        if n < 3:
            return None
        rx = PortfolioBacktestEngine._rank(xs)
        ry = PortfolioBacktestEngine._rank(ys)
        mx = sum(rx) / n
        my = sum(ry) / n
        vx = sum((x - mx) ** 2 for x in rx)
        vy = sum((y - my) ** 2 for y in ry)
        if vx <= 0 or vy <= 0:
            return 0.0
        cov = sum((rx[k] - mx) * (ry[k] - my) for k in range(n))
        return cov / math.sqrt(vx * vy)

    def _collect_factor_records(self) -> list[dict]:
        """把策略本次调仓上报的因子暴露按股票聚合，并记录本期价格。"""
        grouped: dict[str, dict] = {}
        for rec in self._pending_factors:
            grouped.setdefault(rec["symbol"], {})[rec["name"]] = rec["value"]
        return [
            {"sym": sym, "factors": facs, "price": self._price_today(sym)}
            for sym, facs in grouped.items()
        ]

    def _compute_period_ic(self, date: str):
        """用本期价格对上期因子记录算区间收益，计算本期各因子截面 Spearman IC。"""
        if not self.factor_records:
            return None
        rows = []
        for rec in self.factor_records:
            p = self._price_today(rec["sym"])
            if p and rec["price"]:
                rows.append((rec["factors"], p / rec["price"] - 1.0))
        if len(rows) < 10:
            return None
        names = list(rows[0][0].keys())
        ic = {"date": date}
        ok = False
        for nm in names:
            xs = [r[0][nm] for r in rows]
            ys = [r[1] for r in rows]
            s = self._spearman(xs, ys)
            if s is not None:
                ic[nm] = round(s, 4)
                ok = True
        return ic if ok else None

    def _summarize_factor_analysis(self):
        """汇总因子研究全貌：IC统计 / 相关性矩阵 / IC衰减 / 因子分布 / Brinson归因。"""
        series = self.factor_ic_series
        if not series:
            return None
        names = sorted(set().union(*(set(s.keys()) - {"date"} for s in series)))
        out = {
            "periods": len(series), "ic_series": series, "factors": {},
            "universe_industry": {}, "correlation_matrix": {}, "ic_decay": [],
            "factor_distributions": {}, "brinson": {},
        }

        # ——— 基准行业分布 ———
        uni_ind: dict[str, float] = {}
        for sym in self.universe:
            ind = (self.attributes.get(sym, {}) or {}).get("industry") or "未知"
            uni_ind[ind] = uni_ind.get(ind, 0.0) + 1.0
        total_u = sum(uni_ind.values()) or 1.0
        out["universe_industry"] = {k: round(v / total_u, 4) for k, v in sorted(uni_ind.items(), key=lambda x: -x[1])}

        # ——— IC 统计 ———
        for nm in names:
            ics = [s[nm] for s in series if nm in s]
            if len(ics) < 2:
                out["factors"][nm] = {"ic_mean": round(sum(ics)/len(ics),4) if ics else 0,"ic_std":0,"ir":0,"positive_ratio":0}
                continue
            m = sum(ics) / len(ics)
            sd = statistics.pstdev(ics)
            ir = m / sd if sd > 0 else 0.0
            pos = sum(1 for x in ics if x > 0) / len(ics)
            out["factors"][nm] = {"ic_mean": round(m,4),"ic_std": round(sd,4),"ir": round(ir,4),"positive_ratio": round(pos,4)}

        # ——— 相关性矩阵（IC 序列的 Pearson 相关）———
        if len(names) >= 2:
            mat = {}
            all_ics = {nm: [s.get(nm, 0) for s in series] for nm in names}
            for a in names:
                mat[a] = {}
                for b in names:
                    xs, ys = all_ics[a], all_ics[b]
                    n_ = len(xs)
                    if n_ < 3 or a == b:
                        mat[a][b] = 1.0 if a == b else 0.0
                        continue
                    mx, my = sum(xs)/n_, sum(ys)/n_
                    vx = sum((x-mx)**2 for x in xs)
                    vy = sum((y-my)**2 for y in ys)
                    cov = sum((xs[i]-mx)*(ys[i]-my) for i in range(n_))
                    den = math.sqrt(vx * vy)
                    mat[a][b] = round(cov / den, 4) if den > 0 else 0.0
            out["correlation_matrix"] = mat

        # ——— IC 衰减（多期间前向收益的截面 IC）———
        history = self._factor_history
        horizons = [1, 2, 3, 5, 10]
        if len(history) >= 3:
            decay_series = []
            for h in horizons:
                if len(history) <= h:
                    break
                ic_row = {"horizon": h, "label": f"T→T+{h}"}
                ok = False
                for nm in names[:1]:  # 只用第一个因子名找实际因子列表
                    pass
                for nm in names:
                    xs, ys = [], []
                    for t in range(len(history) - h):
                        rec_t = history[t]["records"]
                        rec_f = history[t + h]
                        price_t = history[t].get("price_snap", {})
                        price_f = rec_f.get("price_snap", {})
                        for r_t in rec_t:
                            sym = r_t["sym"]
                            p_t = price_t.get(sym)
                            p_f = price_f.get(sym)
                            fv = r_t.get("factors", {}).get(nm)
                            if p_t and p_f and p_t > 0 and fv is not None:
                                xs.append(fv)
                                ys.append(p_f / p_t - 1.0)
                    if len(xs) >= 10:
                        ic_val = self._spearman(xs, ys)
                        if ic_val is not None:
                            ic_row[nm] = round(ic_val, 4)
                            ok = True
                if ok:
                    decay_series.append(ic_row)
            out["ic_decay"] = decay_series

        # ——— 最后一期的因子值分布 ———
        if history:
            last = history[-1]["records"]
            dist = {}
            for nm in names:
                vals = [r["factors"].get(nm) for r in last if nm in r.get("factors", {})]
                if vals:
                    dist[nm] = vals
            out["factor_distributions"] = dist

        # ——— 简化版 Brinson 归因 ———
        # 行业映射
        ind_map: dict[str, str] = {}
        for sym in self.universe:
            ind_map[sym] = (self.attributes.get(sym, {}) or {}).get("industry") or "未知"
        all_inds = sorted(set(ind_map.values()))

        # 汇总每期持仓的行业权重、组合行业收益、基准行业收益
        alloc_eff, sel_eff, icon_eff = 0.0, 0.0, 0.0
        total_excess = self.equity_curve[-1].equity / self.initial_cash - 1.0 - (
            self.bench_map.get(self.dates[-1], self.bench_first) / self.bench_first - 1.0
        )
        period_count = 0

        for h_idx in range(len(self.holdings_snapshots) - 1):
            h = self.holdings_snapshots[h_idx]
            h_next = self.holdings_snapshots[h_idx + 1]
            date_t = h["date"]
            date_f = h_next["date"]
            pos = h["positions"]
            pos_next = h_next["positions"]

            # 每只标的的期间收益
            ret_map: dict[str, float] = {}
            for sym in self.universe:
                p_t = self.close_map.get(sym, {}).get(date_t)
                p_f = self.close_map.get(sym, {}).get(date_f)
                if p_t and p_f and p_t > 0:
                    ret_map[sym] = p_f / p_t - 1.0
                else:
                    ret_map[sym] = 0.0

            # 行业级组合权重和收益
            port_w: dict[str, float] = {}
            port_r: dict[str, float] = {}
            bench_w: dict[str, float] = {}
            bench_r: dict[str, float] = {}
            for ind in all_inds:
                syms_in = [s for s in self.universe if ind_map.get(s) == ind]
                total_weight = sum(pos.get(s, 0) for s in syms_in)
                if total_weight > 0:
                    port_w[ind] = total_weight
                    port_r[ind] = sum(pos.get(s, 0) * ret_map.get(s, 0) for s in syms_in) / total_weight
                else:
                    port_w[ind] = 0.0
                    port_r[ind] = 0.0
                if syms_in:
                    bench_w[ind] = len(syms_in) / len(self.universe)
                    bench_r[ind] = sum(ret_map.get(s, 0) for s in syms_in) / len(syms_in) if syms_in else 0.0
                else:
                    bench_w[ind] = 0.0
                    bench_r[ind] = 0.0

            # 组合和基准的总期间收益
            r_p = sum(port_w.get(i, 0) * port_r[i] for i in all_inds)
            r_b = sum(bench_w.get(i, 0) * bench_r[i] for i in all_inds)
            excess_p = r_p - r_b

            alloc_e = sum((port_w.get(i, 0) - bench_w.get(i, 0)) * (bench_r[i] - r_b) for i in all_inds)
            sel_e = sum(port_w.get(i, 0) * (port_r[i] - bench_r[i]) for i in all_inds)
            inter_e = excess_p - alloc_e - sel_e

            alloc_eff += alloc_e
            sel_eff += sel_e
            icon_eff += inter_e
            period_count += 1

        out["brinson"] = {
            "allocation_effect": round(alloc_eff, 6),
            "selection_effect": round(sel_eff, 6),
            "interaction_effect": round(icon_eff, 6),
            "total_excess": round(total_excess, 6),
            "periods": period_count,
        }

        # —— alphalens 式增强：IC 月度热力图 / 分层组合 / 因子自相关（均复用 _factor_history，无需新数据）——
        out["ic_monthly"] = self._monthly_ic()
        out["quantile"] = self._quantile_analysis()
        out["factor_autocorr"] = self._factor_autocorr()

        return out

    # —— alphalens 式因子分析增强 ——
    def _monthly_ic(self) -> dict:
        """IC 月度热力图数据：{factor: {yyyymm: avg_ic}}。"""
        series = self.factor_ic_series
        if not series:
            return {}
        buckets: dict = {}
        for s in series:
            ym = s.get("date", "")[:7]
            if not ym:
                continue
            for nm, v in s.items():
                if nm == "date":
                    continue
                buckets.setdefault(nm, {}).setdefault(ym, []).append(v)
        return {
            nm: {ym: round(sum(v) / len(v), 4) for ym, v in d.items()}
            for nm, d in buckets.items()
        }

    def _factor_autocorr(self) -> dict:
        """因子秩自相关（稳定性）：相邻调仓期因子排序的 Spearman 相关均值，越接近 1 越稳定。"""
        hist = self._factor_history
        if len(hist) < 2:
            return {}
        per = [{r["sym"]: r["factors"] for r in h["records"]} for h in hist]
        names = set()
        for h in hist:
            for r in h["records"]:
                names.update(r["factors"].keys())
        out = {}
        for nm in names:
            cors = []
            for t in range(len(per) - 1):
                vt = {s: f[nm] for s, f in per[t].items() if nm in f and f[nm] is not None}
                vn = {s: f[nm] for s, f in per[t + 1].items() if nm in f and f[nm] is not None}
                common = list(set(vt) & set(vn))
                if len(common) >= 10:
                    c = self._spearman([vt[s] for s in common], [vn[s] for s in common])
                    if c is not None:
                        cors.append(c)
            if cors:
                out[nm] = round(sum(cors) / len(cors), 4)
        return out

    def _quantile_analysis(self, quantiles: int = 5) -> dict:
        """alphalens 式分层组合：每期按因子值分位，计算各组下期平均收益并累乘成净值。

        因子值已按「越大越优」取向（report_factor 约定），故第 5 组=最高因子值=最优选股。
        采用与 IC 一致的口径：下期收益 = 下期调仓日价格 / 本期调仓日价格 - 1。
        """
        hist = self._factor_history
        if len(hist) < 2:
            return {}
        names = sorted(
            set().union(*(set(r["factors"].keys()) for h in hist for r in h["records"]))
        )
        per = [
            (h["date"], {r["sym"]: (r["factors"], r["price"]) for r in h["records"]}, h.get("price_snap", {}))
            for h in hist
        ]
        ppy = 252.0 / max(1, self.rebalance_period)  # 每年调仓次数

        def cumulate(rets):
            c = 1.0
            return [round((c := c * (1 + r)) - 1, 4) for r in rets]

        def ann(pct_rets):
            if not pct_rets:
                return 0.0
            m = sum(pct_rets) / len(pct_rets)
            return round(((1 + m) ** ppy - 1) * 100, 2)

        result = {}
        for nm in names:
            group_rets = {q: [] for q in range(1, quantiles + 1)}
            spread_rets = []
            valid = 0
            for t in range(len(per) - 1):
                price_t = per[t][2]
                snap_n = per[t + 1][2]
                vals = []
                for sym, (fac, p_t) in per[t][1].items():
                    if nm not in fac:
                        continue
                    p_n = snap_n.get(sym)
                    if not p_t or not p_n or p_t <= 0:
                        continue
                    vals.append((fac[nm], p_n / p_t - 1.0))
                if len(vals) < quantiles * 3:
                    continue
                svals = sorted(vals, key=lambda x: x[0])
                n = len(svals)
                gmean = {}
                for q in range(1, quantiles + 1):
                    lo, hi = (q - 1) * n // quantiles, q * n // quantiles
                    seg = svals[lo:hi]
                    if seg:
                        gmean[q] = sum(x[1] for x in seg) / len(seg)
                if len(gmean) == quantiles:
                    valid += 1
                    for q, m in gmean.items():
                        group_rets[q].append(m)
                    spread_rets.append(gmean[quantiles] - gmean[1])
            if valid == 0:
                continue
            result[nm] = {
                "quantiles": quantiles,
                "valid_dates": valid,
                "cum_returns": {str(q): cumulate(group_rets[q]) for q in group_rets},
                "spread_cum": cumulate(spread_rets),
                "group_annual_pct": {str(q): ann(group_rets[q]) for q in group_rets},
                "spread_annual_pct": ann(spread_rets),
            }
        return result

    def _build_result(self, strategy_class, params: dict) -> PortfolioResult:
        eq = [e.equity for e in self.equity_curve]
        n = len(eq)
        start = self.dates[0]
        end = self.dates[-1]
        if n == 0:
            return PortfolioResult(
                universe_size=len(self.universe), symbols_used=0, start=start, end=end,
                strategy_key=strategy_class.__name__, params=params,
                initial_cash=self.initial_cash, final_equity=self.initial_cash,
                total_return=0.0, annual_return=0.0, max_drawdown=0.0, sharpe=0.0,
                benchmark_total_return=0.0, excess_return=0.0, info_ratio=0.0,
                trade_count=0,
            )

        total_return = eq[-1] / eq[0] - 1
        annual = ((eq[-1] / eq[0]) ** (252.0 / n) - 1) if n > 1 else 0.0

        rets = [eq[i] / eq[i - 1] - 1 for i in range(1, n)]
        if rets:
            m = sum(rets) / len(rets)
            var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
            std = math.sqrt(var)
            sharpe = (m / std) * math.sqrt(252.0) if std > 0 else 0.0
        else:
            sharpe = 0.0

        peak = eq[0]
        mdd = 0.0
        for e in eq:
            if e > peak:
                peak = e
            dd = (e - peak) / peak if peak > 0 else 0.0
            if dd < mdd:
                mdd = dd

        # —— 组合胜率：调仓区间组合超额收益为正的占比 ——
        win_rate = 0.0
        if self.holdings_snapshots:
            eq_map = {e.date: e.equity for e in self.equity_curve}
            bk_map = {e.date: e.benchmark for e in self.equity_curve}
            hd = [h["date"] for h in self.holdings_snapshots
                  if h["date"] in eq_map and h["date"] in bk_map]
            pos = 0
            total = 0
            for a, b in zip(hd, hd[1:]):
                rp = eq_map[b] / eq_map[a] - 1.0 if eq_map[a] > 0 else 0.0
                rb = bk_map[b] / bk_map[a] - 1.0 if bk_map[a] > 0 else 0.0
                total += 1
                if rp - rb > 0:
                    pos += 1
            win_rate = pos / total if total > 0 else 0.0

        bench = [e.benchmark for e in self.equity_curve]
        bench_ret = (bench[-1] / bench[0] - 1) if bench and bench[0] > 0 else 0.0
        bench_rets = [bench[i] / bench[i - 1] - 1 for i in range(1, n)] if len(bench) > 1 else []
        ex = [rets[i] - bench_rets[i] for i in range(min(len(rets), len(bench_rets)))]
        if ex:
            exm = sum(ex) / len(ex)
            exv = sum((x - exm) ** 2 for x in ex) / (len(ex) - 1) if len(ex) > 1 else 0.0
            exstd = math.sqrt(exv)
            ir = (exm / exstd) * math.sqrt(252.0) if exstd > 0 else 0.0
            excess = eq[-1] / eq[0] - bench[-1] / bench[0]
        else:
            ir = 0.0
            excess = 0.0

        # 期末行业分布（按当前持仓权重聚合）
        industry_dist: dict = {}
        last_eq = eq[-1] if eq else self.initial_cash
        for sym, sh in self.positions.items():
            if sh > 0:
                p = self._last_close.get(sym)
                if p and last_eq > 0:
                    w = sh * p / last_eq
                    ind = (self.attributes.get(sym, {}) or {}).get("industry") or "未知"
                    industry_dist[ind] = industry_dist.get(ind, 0.0) + w
        industry_dist = {k: round(v, 4) for k, v in sorted(industry_dist.items(), key=lambda x: -x[1])}

        # —— 市场中性（对冲 beta）净值与指标 ——
        # 组合日收益对基准日收益 OLS 估计 beta，再扣除 beta*基准 得纯 alpha 净值。
        # 说明：这是「理论对冲」视图（用基准反推），未计入股指期货基差/展期/保证金成本，
        # 真实可交易对冲需用对应股指期货合约建模，收益会略低于此处。
        hedged = [eq[0]] * n if n else []
        hedged_beta = hedged_total = hedged_annual = hedged_sharpe = hedged_mdd = 0.0
        if n > 2:
            rp = [eq[i] / eq[i - 1] - 1 for i in range(1, n)]
            rb = [bench[i] / bench[i - 1] - 1 for i in range(1, n)]
            mrp = sum(rp) / len(rp)
            mrb = sum(rb) / len(rb)
            cov = sum((rp[i] - mrp) * (rb[i] - mrb) for i in range(len(rp)))
            var_rb = sum((x - mrb) ** 2 for x in rb)
            hedged_beta = cov / var_rb if var_rb > 0 else 0.0
            rh = [rp[i] - hedged_beta * rb[i] for i in range(len(rp))]
            hedged = [self.initial_cash]
            for r in rh:
                hedged.append(hedged[-1] * (1 + r))
            hedged_total = hedged[-1] / hedged[0] - 1
            hedged_annual = (hedged[-1] / hedged[0]) ** (252.0 / n) - 1
            if rh:
                mh = sum(rh) / len(rh)
                vh = sum((x - mh) ** 2 for x in rh) / (len(rh) - 1)
                hstd = math.sqrt(vh)
                hedged_sharpe = (mh / hstd) * math.sqrt(252.0) if hstd > 0 else 0.0
            hp = hedged[0]
            for e in hedged:
                if e > hp:
                    hp = e
                dd = (e - hp) / hp if hp > 0 else 0.0
                if dd < hedged_mdd:
                    hedged_mdd = dd
        # 把对冲净值回填进 equity_curve（与 dates 对齐）
        self.equity_curve = [
            PEquityPoint(date=self.equity_curve[i].date, equity=eq[i], benchmark=bench[i], hedged=hedged[i])
            for i in range(n)
        ]

        return PortfolioResult(
            universe_size=len(self.universe),
            symbols_used=sum(1 for s in self.universe if self.hist[s]),
            start=start,
            end=end,
            strategy_key=strategy_class.__name__,
            params=params,
            initial_cash=self.initial_cash,
            final_equity=round(eq[-1], 2),
            total_return=round(total_return, 6),
            annual_return=round(annual, 6),
            max_drawdown=round(mdd, 6),
            sharpe=round(sharpe, 4),
            benchmark_total_return=round(bench_ret, 6),
            excess_return=round(excess, 6),
            info_ratio=round(ir, 4),
            trade_count=len(self.trades),
            win_rate=round(win_rate, 4),
            equity_curve=self.equity_curve,
            trades=self.trades,
            holdings=self.holdings_snapshots,
            industry_distribution=industry_dist,
            factor_analysis=self._summarize_factor_analysis(),
            hedged_beta=round(hedged_beta, 4),
            hedged_total_return=round(hedged_total, 6),
            hedged_annual_return=round(hedged_annual, 6),
            hedged_sharpe=round(hedged_sharpe, 4),
            hedged_max_drawdown=round(hedged_mdd, 6),
        )
