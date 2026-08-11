"""研究级多因子选股策略（横截面打分 + 行业/市值中性 + 月度调仓 + IC自适应加权）。

因子体系对齐 2025-2026 年 A 股主流金工研报实证有效的风格/质量/成长因子，全部通过
『因子表达式引擎（app.core.engine.factor_expr + factor_library）』计算 —— 新增因子只需在
factor_library 登记一条定义，无需改动本文件。本文件只负责：取数 → 计算因子 → 缺失填充 →
市值/行业中性 → 方向自适应 + IC加权 → 横截面 z-score 合成 → 行业中性选股 → 调仓下单。

========= 关键设计：方向自适应 + IC加权 =========
1) 因子先取**中性原始值**（表达式直接求值，不预设方向，如动量=原始涨幅、规模=ln市值）；
2) 每期用「截至上一期的滚动 IC 序列」自动判定该因子**当前方向**
   （mean(IC)>0 则越大越优，<0 则翻转）；历史样本不足时用 FactorDef.default_dir 兜底；
3) 合成权重 = 用户偏好权重 × |滚动 IC|，再归一化 —— 有效因子自动获更高权重、失效因子自动降权；
4) 所有因子（取向后的最终值）通过 ctx.report_factor 上报，供引擎做 IC/IR 与分层研究。

质量(ROE)、成长(营收/利润同比) 类因子已登记在 factor_library，需 seed_fundamentals 入库
对应基本面字段后方能生效；未入库时其值为 None → 截面中位数填充 → 对合成无贡献（不影响既有 10 因子）。
该策略继承 PortfolioStrategy，在 rebalance() 中完成横截面选股与调仓。
"""
from __future__ import annotations

import math
import statistics
from collections import Counter

from app.core.engine.base_strategy import PortfolioStrategy
from app.core.engine.factor_expr import eval_factor
from app.core.engine.factor_library import (
    FACTORS, FACTOR_NAMES, FACTOR_MAP, CN_MAP,
)


def _zscore(vals):
    mm = statistics.fmean(vals)
    sd = statistics.pstdev(vals)
    return [(x - mm) / sd if sd > 0 else 0.0 for x in vals]


def _neutralize_by_group(values: list[float], group_key: list, n_groups: int = 5) -> list[float]:
    """按 group_key（如市值）分位分组，组内 demean，剥离该风格暴露。"""
    n = len(values)
    if n == 0:
        return values[:]
    valid_keys = [k for k in group_key if k is not None]
    fill = statistics.median(valid_keys) if valid_keys else 0.0
    keys = [k if k is not None else fill for k in group_key]
    order = sorted(range(n), key=lambda i: keys[i])
    groups = [order[i::n_groups] for i in range(n_groups)]
    out = values[:]
    for g in groups:
        if len(g) < 2:
            continue
        mean = statistics.fmean(values[i] for i in g)
        for i in g:
            out[i] = values[i] - mean
    return out


def _select_by_industry_quota(composite: dict, sym_ind: dict, valid: list, top_n: int) -> set:
    """行业中性选股：按各成分股行业数量占比分配名额，行业内按 composite 排序。"""
    inds = [sym_ind[s] for s in valid]
    ind_count = Counter(inds)
    total = len(valid)
    quota = {ind: max(1, round(top_n * cnt / total)) for ind, cnt in ind_count.items()}
    ranked = sorted(composite.items(), key=lambda x: -x[1])
    selected: set = set()
    for ind in ind_count:
        cands = [s for s, _ in ranked if sym_ind.get(s) == ind and s not in selected]
        selected.update(cands[: quota[ind]])
        if len(selected) >= top_n:
            break
    return set(list(selected)[:top_n])


class EnhancedFactorStrategy(PortfolioStrategy):
    def init(self, ctx) -> None:
        p = ctx.params
        self.top_n = int(p.get("top_n", 50))
        self.rebalance_period = int(p.get("rebalance_period", 21))
        self.momentum_lookback = int(p.get("momentum_lookback", 120))
        self.reversal_lookback = int(p.get("reversal_lookback", 5))
        self.vol_lookback = int(p.get("vol_lookback", 60))
        self.beta_lookback = int(p.get("beta_lookback", 120))
        self.tail_lookback = int(p.get("tail_lookback", 120))
        self.max_weight = float(p.get("max_weight", 0.05))
        self.neutralize_industry = int(p.get("neutralize_industry", 1)) == 1
        self.neutralize_marketcap = int(p.get("neutralize_marketcap", 1)) == 1
        # 加权方式：'ic'（默认，按 |滚动IC| 自适应权重）/ 'fixed'（纯用户权重）
        self.weight_method = str(p.get("weight_method", "ic")).lower()
        # 用户偏好权重（先验），会再乘 |滚动IC| 归一化；参数键 w_<因子名> 未传则回退默认权重
        self.w = {f.name: float(p.get(f"w_{f.name}", f.default_weight)) for f in FACTORS}

    def _rolling_direction_and_weight(self, ctx):
        """返回 (direction: {因子:±1}, eff_w: {因子:归一化权重})。

        direction：有 ≥3 期滚动 IC 历史时取 mean(IC) 的符号；否则用 FactorDef.default_dir。
        eff_w：'ic' 模式 = 用户权重 × |mean(IC)| 后归一化；'fixed' 模式 = 用户权重归一化。
        """
        ic_series = ctx.engine.factor_ic_series
        window = ic_series[-12:] if len(ic_series) >= 3 else []
        mean_ic = {}
        for f in FACTORS:
            ics = [s[f.name] for s in window if f.name in s]
            mean_ic[f.name] = statistics.fmean(ics) if len(ics) >= 3 else None

        direction = {}
        for f in FACTORS:
            if mean_ic[f.name] is not None:
                # 取向 IC<0 → 当前 DEFAULT_DIR 取向与本期市场背离，翻转到反方向
                direction[f.name] = f.default_dir if mean_ic[f.name] > 0 else -f.default_dir
            else:
                direction[f.name] = f.default_dir

        if self.weight_method == "fixed" or all(v is None for v in mean_ic.values()):
            raw_w = {f.name: abs(self.w[f.name]) for f in FACTORS}
        else:
            raw_w = {f.name: abs(self.w[f.name]) * abs(mean_ic[f.name]) for f in FACTORS}
        tot = sum(raw_w.values())
        eff_w = {nm: (raw_w[nm] / tot if tot > 0 else 1.0 / len(FACTORS)) for nm in raw_w}
        return direction, eff_w

    def rebalance(self, ctx, date: str) -> None:
        universe = ctx.universe()

        raw_neutral: dict[str, dict] = {}
        valid: list[str] = []
        sym_ind: dict[str, str] = {}
        mcap_list: list = []

        for sym in universe:
            closes_m = ctx.history(sym, self.momentum_lookback + 1)
            closes_r = ctx.history(sym, self.reversal_lookback + 1)
            closes_v = ctx.history(sym, self.vol_lookback + 1)
            closes_b = ctx.history(sym, self.beta_lookback + 1)
            closes_t = ctx.history(sym, self.tail_lookback + 1)
            mkt = ctx.benchmark_aligned(sym)
            if not (closes_m and closes_r and closes_v and closes_b and closes_t and mkt):
                continue
            mkt_b = mkt[-len(closes_b):]
            # 截面属性（估值/市值/质量/成长）
            attrs = ctx.engine.attributes.get(sym, {}) or {}
            ns = {
                "c_m": closes_m, "c_r": closes_r, "c_v": closes_v,
                "c_b": closes_b, "c_t": closes_t, "mkt_b": mkt_b,
                "pe_ttm": attrs.get("pe_ttm"),
                "pb": attrs.get("pb"),
                "market_cap": attrs.get("market_cap"),
                "roe": attrs.get("roe"),
                "revenue_yoy": attrs.get("revenue_yoy"),
                "profit_yoy": attrs.get("profit_yoy"),
                "industry": attrs.get("industry"),
            }
            fvals = {}
            for f in FACTORS:
                fvals[f.name] = eval_factor(f.expr, ns)
            raw_neutral[sym] = fvals
            valid.append(sym)
            sym_ind[sym] = attrs.get("industry") or "未知"
            mcap_list.append(attrs.get("market_cap"))

        if not valid:
            return

        # 缺失值用该因子截面中位数填充（保持中性，不引入偏置）
        for f in FACTORS:
            vals = [raw_neutral[s][f.name] for s in valid if raw_neutral[s][f.name] is not None]
            med = statistics.median(vals) if vals else 0.0
            for s in valid:
                if raw_neutral[s][f.name] is None:
                    raw_neutral[s][f.name] = med

        # 市值中性：对除规模(size)外的因子剥离规模暴露
        if self.neutralize_marketcap:
            for f in FACTORS:
                if f.neutralize_exempt:
                    continue
                series = [raw_neutral[s][f.name] for s in valid]
                neutralized = _neutralize_by_group(series, mcap_list)
                for i, s in enumerate(valid):
                    raw_neutral[s][f.name] = neutralized[i]

        # —— 方向自适应 + IC 加权 ——
        direction, eff_w = self._rolling_direction_and_weight(ctx)

        # 取向：中性原始值 × 方向 → 越大越优的取向值
        oriented: dict[str, dict] = {}
        for s in valid:
            oriented[s] = {f.name: raw_neutral[s][f.name] * direction[f.name] for f in FACTORS}

        # 逐因子 z-score
        zscores = {}
        for f in FACTORS:
            zscores[f.name] = _zscore([oriented[s][f.name] for s in valid])

        composite = {}
        for i, sym in enumerate(valid):
            score = 0.0
            for f in FACTORS:
                score += eff_w[f.name] * zscores[f.name][i]
            composite[sym] = score
            # 上报取向后的最终值，供 IC/IR/分层研究（均已取「越大越优」取向）
            for f in FACTORS:
                ctx.report_factor(sym, f.name, oriented[sym][f.name])

        if self.neutralize_industry:
            selected = _select_by_industry_quota(composite, sym_ind, valid, self.top_n)
        else:
            ranked = sorted(composite.items(), key=lambda x: -x[1])
            selected = set(s for s, _ in ranked[: self.top_n])

        if not selected:
            return

        # 排名（综合得分降序），用于成交说明
        ranked = sorted(composite.items(), key=lambda x: -x[1])
        rank_map = {sym: i + 1 for i, (sym, _) in enumerate(ranked)}
        N = len(valid)

        w = 1.0 / len(selected)
        w = min(w, self.max_weight)

        # 清仓范围 = 当前股票池 ∪ 已持仓标的：确保已退出指数的持仓也能被卖出
        held = set(ctx.positions().keys())
        all_syms = set(universe) | held
        for sym in all_syms:
            if sym in selected:
                i = valid.index(sym)
                comp = composite[sym]
                rk = rank_map[sym]
                contribs = sorted(
                    ((f.name, eff_w[f.name] * zscores[f.name][i]) for f in FACTORS),
                    key=lambda x: -abs(x[1]),
                )
                top3 = "、".join(f"{CN_MAP[nm]}{c:+.2f}" for nm, c in contribs[:3])
                reason = (f"综合得分 {comp:.2f}（第{rk}/{N}名，前{rk / N * 100:.0f}%）；"
                          f"主要因子贡献: {top3}。入选 Top{self.top_n}，等权配置。")
                ctx.order_target_percent(sym, w, "选股买入", reason)
            elif sym in held:
                if sym in composite:
                    i = valid.index(sym)
                    comp = composite[sym]
                    rk = rank_map[sym]
                    reason = (f"综合得分 {comp:.2f}（第{rk}/{N}名），未进 Top{self.top_n}，调出清仓。")
                else:
                    reason = "本期无有效因子数据，调出清仓。"
                ctx.order_target_percent(sym, 0.0, "调出清仓", reason)
