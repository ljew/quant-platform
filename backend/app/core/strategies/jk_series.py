"""jk 系列策略（jk001 / jk002）—— 从聚宽 jk001_live_v1.py 移植。

策略原型：沪深300 大盘股 ·「自由现金流 + 低波动」双因子 · 月频调仓 + 阶梯移动止损。
交易逻辑逐条对齐原版（参数名与取值一一对应），只替换数据访问层与下单接口：

  原版（聚宽）                       本平台
  get_index_stocks(PIT)         →   ctx.universe()（引擎注入的时点成分快照）
  get_fundamentals(批量)         →   ctx.financial(sym)（financials_raw，按 ann_date 做 PIT）
  get_price(260日 close/volume) →   ctx.history(sym, n) / ctx.history_high(sym, n)
  get_industry(申万一级)         →   ctx.attribute(sym, 'industry')（东财细分行业）
  pos.avg_cost / init_time      →   ctx.cost(sym) / 策略自维护持仓天数
  order_target_value_safe       →   ctx.order_target_percent(sym, pct, signal_type, reason)

与原版**不可避免**的三处差异（已尽量缩小，都会在验证报告里标注）：
  1. 成交价：原版在 14:45/14:55 盘中价成交，平台是日频 bar → 统一用**当日收盘价**。
  2. 行业口径：原版用申万一级（31 个），平台是东财细分行业（约 100+ 个），
     因此「单一行业 ≤ max_industry_num 只」这条约束在平台侧几乎不触发（更宽松）；
     「剔除金融」改为按东财行业名 {银行, 证券, 保险, 多元金融} 匹配。
  3. ST 过滤：平台无历史 ST 标记，只有当前名称 → 参数 exclude_st 关闭时不做该过滤，
     开启时用当前名称匹配（存在轻微前视，默认关闭）。

月频调仓由引擎的 rebalance_period（默认 21 交易日）驱动；止损等日频逻辑走引擎的
on_bar 回调（本文件配合 portfolio_backtest 的每日钩子使用）。
"""
from __future__ import annotations

import logging
import statistics
from datetime import date as _date

logger = logging.getLogger(__name__)

from app.core.engine.base_strategy import PortfolioStrategy

# 东财行业里属于「金融」的门类（对应原版剔除申万一级 J 金融业）
FINANCIAL_INDUSTRIES = {"银行", "证券", "保险", "多元金融", "综合金融"}


def _zscore(d: dict) -> dict:
    """截面 z-score；样本不足或标准差为 0 时返回全 0（与原版 _zscore 一致）。"""
    if not d:
        return {}
    vals = list(d.values())
    if len(vals) > 1:
        sd = statistics.stdev(vals)
        if sd > 1e-12:
            mean = statistics.fmean(vals)
            return {k: (v - mean) / sd for k, v in d.items()}
    return {k: 0.0 for k in d}


class JKFactorStrategy(PortfolioStrategy):
    """jk 系列：FCF/OE + 低波动双因子选股 + 月频调仓 + 阶梯移动止损 + 周线趋势定仓位。"""

    def init(self, ctx) -> None:
        p = ctx.params
        # —— 选股 ——
        self.stock_num = int(p.get("stock_num", 30))
        self.max_stock_weight = float(p.get("max_stock_weight", 0.10))
        self.max_industry_num = int(p.get("max_industry_num", 10))
        self.w_fundamental = float(p.get("w_fundamental", 0.5))
        self.w_lowvol = float(p.get("w_lowvol", 0.5))
        self.w_price_volume = float(p.get("w_price_volume", 0.0))
        self.w_momentum = float(p.get("w_momentum", 0.0))
        # 'filter'=动量只做准入；'score'=动量进打分；'both'=两者都要
        self.momentum_mode = str(p.get("momentum_mode", "filter"))
        self.enable_trend_filter = int(p.get("enable_trend_filter", 1)) == 1
        self.max_intraday_chg = float(p.get("max_intraday_chg", 0.05))
        # —— 股票池 ——
        self.allow_star_market = int(p.get("allow_star_market", 0)) == 1
        self.exclude_financial = int(p.get("exclude_financial", 1)) == 1
        self.exclude_st = int(p.get("exclude_st", 0)) == 1
        self.min_list_days = int(p.get("min_list_days", 250))
        # —— 止损 ——
        self.stop_loss_pct = float(p.get("stop_loss_pct", 0.15))
        self.enable_ladder_stop = int(p.get("enable_ladder_stop", 1)) == 1
        self.ladder_hold_days = int(p.get("ladder_hold_days", 10))
        self.ladder_trailing_pct = float(p.get("ladder_trailing_pct", 0.15))
        self.trailing_stop_pct = float(p.get("trailing_stop_pct", 0.10))
        # —— 仓位（按周线趋势三档）——
        self.bullish_position = float(p.get("bullish_position", 1.00))
        self.neutral_position = float(p.get("neutral_position", 0.90))
        self.bearish_position = float(p.get("bearish_position", 0.00))
        # —— 调仓 ——
        self.reb_thresh = float(p.get("reb_thresh", 0.02))

        # —— 运行时状态 ——
        self.market_trend = "neutral"
        self.position_ratio = self.neutral_position
        self._last_trend_week = None
        self._peak: dict[str, float] = {}       # 建仓以来最高价（移动止损基准）
        self._hold_days: dict[str, int] = {}    # 已持仓交易日数（阶梯止损用）

    # ==================== 日频：趋势 + 止损 ====================
    def on_bar(self, ctx, date: str) -> None:
        """非调仓日的日频回调：周线趋势检查 + 每日止损。"""
        self._update_trend(ctx, date)
        self._check_stop_loss(ctx, date)
        if date[8:] <= "03":      # 每月头几天打一条，便于确认日频钩子在跑
            pos = ctx.positions()
            logger.warning("[jk] on_bar %s 持仓%d 成本=%s", date, len(pos),
                           {s: round(ctx.cost(s), 2) for s in list(pos)[:2]})

    def _week_key(self, date: str):
        try:
            return _date.fromisoformat(date).isocalendar()[:2]
        except Exception:
            return None

    def _update_trend(self, ctx, date: str) -> None:
        """按自然周检查基准（沪深300/中证全指）趋势 → 决定目标总仓位。

        对齐原版 check_market_trend：MA20/60/120/250 排列 + 250 日偏离 + 1/3 月涨幅
        + 60 日年化波动，打分 ≥4 看多、≤-2 看空，其余中性。
        """
        wk = self._week_key(date)
        if wk is not None and self._last_trend_week == wk:
            return
        self._last_trend_week = wk

        c = ctx.benchmark_history(260)
        if not c or len(c) < 200:
            return
        last = c[-1]
        ma20 = statistics.fmean(c[-20:])
        ma60 = statistics.fmean(c[-60:])
        ma120 = statistics.fmean(c[-120:])
        ma250 = statistics.fmean(c[-250:])

        score = 0
        if last > ma20 > ma60 > ma120:
            score += 3
        elif last > ma20 and ma20 > ma60:
            score += 2
        elif last > ma60:
            score += 1
        if last > ma250 * 1.2:
            score += 2
        elif last < ma250 * 0.8:
            score -= 2
        if last / c[-21] - 1 > 0.05:
            score += 1
        if len(c) >= 61 and last / c[-61] - 1 > 0.10:
            score += 2

        rets = [c[i] / c[i - 1] - 1 for i in range(len(c) - 60, len(c))]
        if len(rets) >= 20:
            vol60 = statistics.stdev(rets) * (252 ** 0.5)
            if vol60 < 0.15:
                score += 1
            elif vol60 > 0.35:
                score -= 1

        if score >= 4:
            self.market_trend, self.position_ratio = "bullish", self.bullish_position
        elif score <= -2:
            self.market_trend, self.position_ratio = "bearish", self.bearish_position
        else:
            self.market_trend, self.position_ratio = "neutral", self.neutral_position

    def _forget(self, sym: str) -> None:
        self._peak.pop(sym, None)
        self._hold_days.pop(sym, None)

    def _check_stop_loss(self, ctx, date: str) -> None:
        """固定止损（相对成本价）+ 阶梯移动止损（峰值取建仓以来最高价）。"""
        worst = 0.0
        for sym in list(ctx.positions().keys()):
            try:
                px = ctx.price(sym)
                if not px:
                    continue
                cost = ctx.cost(sym)
                if cost <= 0:
                    continue
                worst = min(worst, px / cost - 1)

                # 持仓天数 + 峰值（每日更新一次）
                self._hold_days[sym] = self._hold_days.get(sym, 0) + 1
                highs = ctx.history_high(sym, 1)
                hi = highs[-1] if highs else px
                self._peak[sym] = max(self._peak.get(sym, cost), hi, px)

                profit = px / cost - 1
                if profit <= -self.stop_loss_pct:
                    ctx.order_target_percent(
                        sym, 0.0, "jk_stop_loss", f"固定止损 {profit * 100:.1f}%")
                    self._forget(sym)
                    continue

                hd = self._hold_days.get(sym, 0)
                trail = (self.ladder_trailing_pct
                         if (self.enable_ladder_stop and hd >= self.ladder_hold_days)
                         else self.trailing_stop_pct)
                peak = self._peak.get(sym, cost)
                if peak > cost and px / peak - 1 <= -trail:
                    ctx.order_target_percent(
                        sym, 0.0, "jk_trailing_stop",
                        f"移动止损 持有{hd}日 自峰值{(px / peak - 1) * 100:.1f}%")
                    self._forget(sym)
            except Exception:  # noqa: BLE001 —— 单只止损失败不影响其余持仓
                continue
        if date[8:] <= "03":
            logger.warning("[jk] %s 最深浮亏 %.1f%% (阈值 -%.0f%%)", date, worst * 100,
                           self.stop_loss_pct * 100)

    # ==================== 调仓日 ====================
    def _build_pool(self, ctx, date: str) -> list[str]:
        """时点成分 → 剔除科创板 / 金融 / ST / 上市不足 min_list_days。"""
        out = []
        try:
            d0 = _date.fromisoformat(date)
        except Exception:
            d0 = None
        for sym in ctx.universe():
            if not self.allow_star_market and "688" in sym:
                continue
            if self.exclude_financial:
                ind = ctx.attribute(sym, "industry") or ""
                if ind in FINANCIAL_INDUSTRIES:
                    continue
            if self.exclude_st:
                name = str(ctx.attribute(sym, "name") or "")
                if "ST" in name.upper():
                    continue
            if self.min_list_days > 0 and d0 is not None:
                # 优先用真实上市日（stocks.list_date）；缺失时退回数据窗口首日
                fd = ctx.attribute(sym, "list_date") or ctx.first_date(sym)
                if fd:
                    try:
                        if (d0 - _date.fromisoformat(str(fd)[:10])).days < self.min_list_days:
                            continue
                    except Exception:
                        pass
            out.append(sym)
        return out

    def _pit_financial(self, ctx, sym: str, date: str):
        """取公告日 <= 今日的最新一期财务（杜绝前视）。"""
        rows = ctx.financial(sym) or []
        best, best_ad = None, ""
        for r in rows:
            ad = str(r.get("ann_date") or "")[:10]
            if ad and ad <= date and ad >= best_ad:
                best, best_ad = r, ad
        return best

    def _screen_fundamentals(self, ctx, pool: list[str], date: str):
        """FCF/OE 与 ROE 准入：ROE > 0 且 FCF/OE > -0.1。

        FCF/OE = (经营现金流净额 − |资本开支|) / 总资产，与原版分子口径一致。
        """
        passed, data = [], {}
        for sym in pool:
            r = self._pit_financial(ctx, sym, date)
            if not r:
                continue
            try:
                roe = r.get("roe")
                ta = r.get("total_assets")
                ocf = r.get("ocf")
                capex = r.get("capex")
                if roe is None or ta is None or ocf is None:
                    continue
                ta = float(ta)
                if ta <= 0:
                    continue
                fcfoe = (float(ocf) - abs(float(capex or 0.0))) / ta
                if not (float(roe) > 0):
                    continue
                if not (fcfoe > -0.1):
                    continue
                data[sym] = {"roe": float(roe), "fcfoe": fcfoe}
                passed.append(sym)
            except Exception:
                continue
        return passed, data

    def _calc_pv_and_momentum(self, ctx, syms: list[str]):
        """量价/波动/动量：返回 (pv, m121, m61, trend, volad, chg)。

        动量口径同原版：m121 = P[-22]/P[-253]−1，m61 = P[-22]/P[-127]−1（剔除最近 21 日）。
        波动率 = 近 20 日日收益标准差（样本 std，与 pandas 默认 ddof=1 一致）。
        """
        pv, m121, m61, trend, volad, chg = {}, {}, {}, {}, {}, {}
        for sym in syms:
            try:
                c = ctx.history(sym, 260)
                if not c or len(c) < 130:
                    continue
                last = c[-1]
                rets = [c[i] / c[i - 1] - 1 for i in range(len(c) - 20, len(c))]
                vola = statistics.stdev(rets) if len(rets) > 1 else 0.0
                ma20 = statistics.fmean(c[-20:])
                ma60 = statistics.fmean(c[-60:]) if len(c) >= 60 else ma20

                mom20 = last / c[-21] - 1 if len(c) >= 21 else 0.0
                ts = 1.0 if last > ma20 else 0.0
                # 量比（vr）原版用成交量，本平台 history 只有收盘价 → 权重为 0 时取 1
                vr = 1.0
                pv[sym] = mom20 * 0.3 + vr * 0.2 + ts * 0.3 - vola * 0.2
                trend[sym] = 1.0 if (last > ma20 > ma60) else (0.5 if last > ma20 else 0.0)
                volad[sym] = vola
                chg[sym] = last / c[-2] - 1 if len(c) >= 2 else 0.0
                p1m = c[-22] if len(c) >= 22 else c[0]
                p6m = c[-127] if len(c) >= 127 else c[0]
                p12m = c[-253] if len(c) >= 253 else c[0]
                m121[sym] = (p1m / p12m - 1) if p12m > 0 else 0.0
                m61[sym] = (p1m / p6m - 1) if p6m > 0 else 0.0
            except Exception:
                continue
        return pv, m121, m61, trend, volad, chg

    def _select(self, ctx, filtered: list[str], fdata: dict, date: str) -> list[str]:
        """打分 + 准入 + 行业上限（不足时放宽准入兜底）。"""
        if not filtered:
            return []
        fund = {s: fdata[s]["fcfoe"] for s in filtered if fdata.get(s, {}).get("fcfoe") is not None}
        fz = _zscore(fund)
        pv, m121, m61, trend, volad, chg = self._calc_pv_and_momentum(ctx, filtered)
        pz = _zscore(pv)
        vz = _zscore({s: -v for s, v in volad.items()})          # 低波动：取负，越大越优
        mz = _zscore({s: 0.5 * m121.get(s, 0.0) + 0.5 * m61.get(s, 0.0) for s in m121})

        use_mom_score = self.momentum_mode in ("score", "both")
        base = self.w_fundamental + self.w_lowvol + self.w_price_volume
        if use_mom_score:
            base += self.w_momentum
        if base <= 0:
            base = 1.0
        wf = self.w_fundamental / base
        wv = self.w_lowvol / base
        wp = self.w_price_volume / base
        wm = self.w_momentum / base if use_mom_score else 0.0

        scored = []
        for s in filtered:
            if s not in vz:
                continue
            sc = (wf * fz.get(s, 0.0) + wv * vz.get(s, 0.0)
                  + wp * pz.get(s, 0.0) + wm * mz.get(s, 0.0))
            scored.append((s, sc, 0.5 * m121.get(s, 0.0) + 0.5 * m61.get(s, 0.0)))
        scored.sort(key=lambda x: x[1], reverse=True)

        def _ind(sym: str) -> str:
            return ctx.attribute(sym, "industry") or "NA"

        picked, icnt = [], {}
        for s, _sc, mom in scored:
            if len(picked) >= self.stock_num:
                break
            if self.momentum_mode in ("filter", "both") and mom <= 0:
                continue                                   # 动量准入
            if self.enable_trend_filter and trend.get(s, 0.0) < 0.5:
                continue                                   # 站上 MA20
            if chg.get(s, 0.0) > self.max_intraday_chg:
                continue                                   # 不追高
            iname = _ind(s)
            if icnt.get(iname, 0) >= self.max_industry_num:
                continue
            picked.append(s)
            icnt[iname] = icnt.get(iname, 0) + 1

        # 兜底：约束过严时放宽准入（保留行业上限，否则仓位会被迫压低）
        if len(picked) < self.stock_num:
            for s, _sc, _mom in scored:
                if len(picked) >= self.stock_num:
                    break
                if s in picked:
                    continue
                iname = _ind(s)
                if icnt.get(iname, 0) >= max(self.max_industry_num, 1):
                    continue
                picked.append(s)
                icnt[iname] = icnt.get(iname, 0) + 1
        return picked

    def _weights(self, selected: list[str]) -> dict[str, float]:
        """等权 + 单只上限，再整体按目标仓位缩放（同原版 allocate_weights）。"""
        if not selected or self.position_ratio <= 0.01:
            return {}
        w = 1.0 / len(selected)
        cap = self.max_stock_weight if self.max_stock_weight > 0 else w
        weights = {s: min(w, cap) for s in selected}
        tot = 0.0
        for v in weights.values():
            tot += v
        if tot <= 0:
            return {}
        return {s: v / tot * self.position_ratio for s, v in weights.items()}

    def rebalance(self, ctx, date: str) -> None:
        self._update_trend(ctx, date)

        if self.position_ratio <= 0.01:
            for s in list(ctx.positions().keys()):
                ctx.order_target_percent(s, 0.0, "jk_bearish", "趋势看空清仓观望")
                self._forget(s)
            return

        pool = self._build_pool(ctx, date)
        if len(pool) < self.stock_num:
            return
        filtered, fdata = self._screen_fundamentals(ctx, pool, date)
        if len(filtered) < self.stock_num:
            return
        logger.warning("[jk] rebalance %s 池%d 基本面通过%d 趋势=%s 仓位%.0f%%",
                       date, len(pool), len(filtered), self.market_trend, self.position_ratio * 100)
        final = self._select(ctx, filtered, fdata, date)
        if not final:
            return
        weights = self._weights(final)
        if not weights:
            return

        cur_w = ctx.attributes_snapshot()
        # 卖出不在目标名单里的持仓
        for sym in list(ctx.positions().keys()):
            if sym not in weights:
                ctx.order_target_percent(sym, 0.0, "jk_exit", "调仓卖出（落选）")
                self._forget(sym)
        # 买入/调整：偏离小于 min(总资产×reb_thresh, 目标×50%) 则不动，省手续费
        for sym, w in weights.items():
            cw = cur_w.get(sym, 0.0)
            if abs(cw - w) <= min(self.reb_thresh, w * 0.5):
                continue
            ctx.order_target_percent(sym, w, "jk_rebalance", f"目标仓位 {w * 100:.1f}%")
            if sym not in self._hold_days:      # 新建仓位：重置峰值与持仓天数
                self._peak[sym] = ctx.price(sym) or 0.0
                self._hold_days[sym] = 0
