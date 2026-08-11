"""研究级多因子选股策略（横截面打分 + 行业/市值中性 + 月度调仓 + IC自适应加权）。

因子体系对齐 2025-2026 年 A 股主流金工研报（方正/广发/国信/华福/东北/民生等）中
实证有效的风格因子，全部基于『前复权日K + 市值/估值截面属性』计算，无需高频或
另类数据即可落地：

  价值类（估值，越大越优）
    * ep    —— 盈利收益率 = 1 / PE_TTM
    * bp    —— 账面价值比 = 1 / PB

  动量/反转（原始取中期/短期涨幅，方向由滚动 IC 自适应）
    * momentum —— 中期动量 = 过去 momentum_lookback 日收益率(ROC)
    * reversal —— 短期反转 = 过去 reversal_lookback 日收益率

  风险类
    * low_vol        —— 收益率标准差（低波更优）
    * beta           —— 个股对基准的系统性风险（CAPM 斜率）
    * idio_vol       —— 特异度 = CAPM 残差波动率（越低越优）
    * skewness       —— 收益率偏度（彩票型高正偏长期跑输）
    * tail_risk      —— 区间内最大回撤（越低越优）

  规模类
    * size           —— 总市值（小市值溢价）

========= 关键设计：方向自适应 + IC加权 =========
A股风格切换剧烈（如 2023-2025 大盘/红利占优，小市值与动量长期失效）。
若把因子死死钉在「固定取向 + 固定权重」，失效因子会持续贡献负 alpha。

本策略：
1) 因子先取**中性原始值**（不预设方向，如动量=原始涨幅、规模=ln市值）；
2) 每期用「截至上一期的滚动 IC 序列」自动判定该因子**当前方向**
   （mean(IC)>0 则越大越优，<0 则翻转）；历史样本不足时用 DEFAULT_DIR
   （基于长期研报共识的兜底方向，如 A股反转>动量、小市值长期溢价）；
3) 合成权重 = 用户偏好权重 × |滚动 IC|，再归一化 —— 有效因子自动获更高
   权重、失效因子自动降权（无前视：只用历史 IC，不含未来信息）；
4) 所有因子（取向后的最终值）通过 ctx.report_factor 上报，供引擎做 IC/IR。

质量(ROE/现金流)、成长(营收/利润同比)、分析师预期类因子在研报中同样突出，
但需扩充基本面字段后方可接入，见 research 文档。
该策略继承 PortfolioStrategy，在 rebalance() 中完成横截面选股与调仓。
"""
from __future__ import annotations

import math
import statistics
from collections import Counter

from app.core.engine.base_strategy import PortfolioStrategy


# 各因子的「长期共识默认方向」（+1=越大越优, -1=越小越优）。
# 仅在滚动 IC 样本不足时作为兜底；一旦有足够的本期 IC，以 IC 方向为准。
DEFAULT_DIR = {
    "momentum": -1,   # A股中期动量弱、反转强 → 默认取反转取向
    "reversal": -1,   # 短期反转
    "low_vol": -1,    # 低波动溢价
    "size": -1,       # 小市值溢价（长期；短期可能失效，由IC自适应翻转）
    "beta": 1,        # 上涨市高 beta 优；方向不稳，靠 IC 自适应
    "idio_vol": -1,   # 低特异度（残差波动）更优
    "skewness": -1,   # 低正偏（反彩票）更优
    "tail_risk": 1,   # 最大回撤为负值，越接近0越优 → 越大越优
    "ep": 1,          # 盈利收益率越大越优
    "bp": 1,          # 账面价值比越大越优
}

# 默认权重（用户偏好先验）。实际合成会再乘 |滚动IC| 归一化，
# 因此有效因子自动获更高权重，失效因子自动降权。
DEFAULT_W = {
    "momentum": 0.10, "reversal": 0.12, "low_vol": 0.20, "size": 0.10,
    "beta": 0.05, "idio_vol": 0.15, "skewness": 0.05, "tail_risk": 0.05,
    "ep": 0.15, "bp": 0.10,
}

FACTOR_NAMES = list(DEFAULT_W.keys())


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


def _returns(closes: list[float]) -> list[float]:
    return [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]


def _beta_idio(stock_closes: list[float], mkt_closes: list[float]) -> tuple[float, float]:
    """对股票与基准收盘价序列做 CAPM 回归，返回 (beta, 残差波动率)。"""
    xs, ys = [], []
    for i in range(1, len(stock_closes)):
        sc0, sc1 = stock_closes[i - 1], stock_closes[i]
        mc0 = mkt_closes[i - 1] if i - 1 < len(mkt_closes) else None
        mc1 = mkt_closes[i] if i < len(mkt_closes) else None
        if mc0 and mc1 and mc0 > 0 and sc0 > 0:
            xs.append(sc1 / sc0 - 1.0)
            ys.append(mc1 / mc0 - 1.0)
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


def _skewness(vals: list[float]) -> float:
    n = len(vals)
    if n < 3:
        return 0.0
    m = statistics.fmean(vals)
    sd = statistics.pstdev(vals)
    if sd <= 0:
        return 0.0
    return sum((x - m) ** 3 for x in vals) / n / (sd ** 3)


def _max_drawdown(closes: list[float]) -> float:
    """区间内最大回撤（返回负数，越接近 0 越好）。"""
    if len(closes) < 2:
        return 0.0
    peak = closes[0]
    mdd = 0.0
    for c in closes:
        if c > peak:
            peak = c
        if peak > 0:
            dd = c / peak - 1.0
            if dd < mdd:
                mdd = dd
    return mdd


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
        # 用户偏好权重（先验），会再乘 |滚动IC| 归一化
        self.w = {nm: float(p.get(f"w_{nm}", DEFAULT_W[nm])) for nm in FACTOR_NAMES}

    def _rolling_direction_and_weight(self, ctx):
        """返回 (direction: {因子:±1}, eff_w: {因子:归一化权重})。

        direction：有 ≥3 期滚动 IC 历史时取 mean(IC) 的符号；否则用 DEFAULT_DIR。
        eff_w：'ic' 模式 = 用户权重 × |mean(IC)| 后归一化；'fixed' 模式 = 用户权重归一化。
        """
        ic_series = ctx.engine.factor_ic_series
        # 用最近最多 12 期滚动窗口判定方向：A股风格切换缓慢，短窗口(3期)易对动量/
        # 尾部等高噪声因子误判方向，更长窗口更能捕捉主导风格。样本不足 3 期用默认方向。
        window = ic_series[-12:] if len(ic_series) >= 3 else []
        mean_ic = {}
        for nm in FACTOR_NAMES:
            ics = [s[nm] for s in window if nm in s]
            mean_ic[nm] = statistics.fmean(ics) if len(ics) >= 3 else None

        direction = {}
        for nm in FACTOR_NAMES:
            if mean_ic[nm] is not None:
                # oriented IC<0 → 当前 DEFAULT_DIR 取向与本期市场背离，翻转到反方向
                direction[nm] = DEFAULT_DIR[nm] if mean_ic[nm] > 0 else -DEFAULT_DIR[nm]
            else:
                direction[nm] = DEFAULT_DIR[nm]

        if self.weight_method == "fixed" or all(v is None for v in mean_ic.values()):
            raw_w = {nm: abs(self.w[nm]) for nm in FACTOR_NAMES}
        else:
            raw_w = {nm: abs(self.w[nm]) * abs(mean_ic[nm]) for nm in FACTOR_NAMES}
        tot = sum(raw_w.values())
        eff_w = {nm: (raw_w[nm] / tot if tot > 0 else 1.0 / len(FACTOR_NAMES)) for nm in FACTOR_NAMES}
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

            # 价值（越大越优，PE/PB 为负或缺失则退化为 None）
            pe = ctx.attribute(sym, "pe_ttm")
            pb = ctx.attribute(sym, "pb")
            ep_val = (1.0 / pe) if (pe is not None and 0 < pe < 1000) else None
            bp_val = (1.0 / pb) if (pb is not None and 0 < pb < 1000) else None

            # 动量 / 反转（原始涨幅，方向后续由滚动 IC 自适应）
            mom_val = closes_m[-1] / closes_m[0] - 1.0
            rev_raw = closes_r[-1] / closes_r[0] - 1.0

            # 低波动（原始=波动率）
            rets_v = _returns(closes_v)
            vol_val = statistics.pstdev(rets_v) if len(rets_v) > 1 else 1e9

            # 规模（原始=ln总市值）
            mcap = ctx.attribute(sym, "market_cap")
            size_val = math.log(mcap) if (mcap is not None and mcap > 0) else None

            # BETA / 特异度（CAPM）
            beta_val, idio_val = _beta_idio(closes_b, mkt[-len(closes_b):])

            # 偏度（原始）
            skew_val = _skewness(rets_v) if len(rets_v) > 3 else 0.0

            # 尾部风险（最大回撤，负值，越接近0越优）
            tail_val = _max_drawdown(closes_t)

            raw_neutral[sym] = {
                "momentum": mom_val, "reversal": rev_raw, "low_vol": vol_val,
                "size": size_val, "beta": beta_val, "idio_vol": idio_val,
                "skewness": skew_val, "tail_risk": tail_val,
                "ep": ep_val, "bp": bp_val,
            }
            valid.append(sym)
            sym_ind[sym] = ctx.attribute(sym, "industry") or "未知"
            mcap_list.append(mcap)

        if not valid:
            return

        # 缺失值用该因子截面中位数填充（保持中性，不引入偏置）
        for nm in FACTOR_NAMES:
            vals = [raw_neutral[s][nm] for s in valid if raw_neutral[s][nm] is not None]
            med = statistics.median(vals) if vals else 0.0
            for s in valid:
                if raw_neutral[s][nm] is None:
                    raw_neutral[s][nm] = med

        # 市值中性：对价格/估值类因子（剔除规模本身）剥离规模暴露
        if self.neutralize_marketcap:
            for nm in ["momentum", "reversal", "low_vol", "beta", "idio_vol",
                       "skewness", "tail_risk", "ep", "bp"]:
                series = [raw_neutral[s][nm] for s in valid]
                neutralized = _neutralize_by_group(series, mcap_list)
                for i, s in enumerate(valid):
                    raw_neutral[s][nm] = neutralized[i]

        # —— 方向自适应 + IC 加权 ——
        direction, eff_w = self._rolling_direction_and_weight(ctx)

        # 取向：中性原始值 × 方向 → 越大越优的取向值
        oriented: dict[str, dict] = {}
        for s in valid:
            oriented[s] = {nm: raw_neutral[s][nm] * direction[nm] for nm in FACTOR_NAMES}

        # 逐因子 z-score
        zscores = {}
        for nm in FACTOR_NAMES:
            zscores[nm] = _zscore([oriented[s][nm] for s in valid])

        composite = {}
        for i, sym in enumerate(valid):
            score = 0.0
            for nm in FACTOR_NAMES:
                score += eff_w[nm] * zscores[nm][i]
            composite[sym] = score
            # 上报取向后的最终值，供 IC/IR 研究（均已取「越大越优」取向）
            for nm in FACTOR_NAMES:
                ctx.report_factor(sym, nm, oriented[sym][nm])

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
        FACTOR_CN = {
            "momentum": "动量", "reversal": "反转", "low_vol": "低波", "size": "规模",
            "beta": "Beta", "idio_vol": "特异度", "skewness": "偏度",
            "tail_risk": "尾部", "ep": "EP", "bp": "BP",
        }

        w = 1.0 / len(selected)
        w = min(w, self.max_weight)

        # 清仓范围 = 当前股票池 ∪ 已持仓标的：确保已退出指数的持仓也能被卖出，
        # 否则 PIT 过滤后它们不在 universe 内，会一直空仓占用资金。
        held = set(ctx.positions().keys())
        all_syms = set(universe) | held
        for sym in all_syms:
            if sym in selected:
                i = valid.index(sym)
                comp = composite[sym]
                rk = rank_map[sym]
                contribs = sorted(
                    ((nm, eff_w[nm] * zscores[nm][i]) for nm in FACTOR_NAMES),
                    key=lambda x: -abs(x[1]),
                )
                top3 = "、".join(f"{FACTOR_CN[nm]}{c:+.2f}" for nm, c in contribs[:3])
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
