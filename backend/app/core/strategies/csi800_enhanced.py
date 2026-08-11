"""中证800 / 沪深300 指数增强策略（多因子打分 + 行业/市值中性 + 月度调仓）。

思路：
- 股票池：指数成分股（由回测引擎注入 universe）；
- 因子（均基于前复权收盘价，横截面计算）：
    * 动量 momentum     —— 过去 momentum_lookback 日收益率 (ROC)
    * 反转 reversal     —— 过去 reversal_lookback 日收益率（取原值，做空反转）
    * 低波动 low_vol    —— 过去 vol_lookback 日收益率标准差的倒数（越大越优）
- 风格中性（可选，默认开启）：
    * 市值中性：对每个原始因子按市值分位分组 demean，剥离规模风格暴露；
    * 行业中性：选股时按成分股行业数量占比分配名额，使组合行业分布与基准一致；
- 合成得分 = w_mom*mom_z + w_rev*rev_z + w_vol*vol_z；
- 选得分最高的 top_n 只，等权配置（各 1/top_n 仓位），不在选中集合的清仓；
- 约束：单只最大权重 max_weight（默认 5%）；
- 基准：对应指数（引擎传入）。

该策略继承 PortfolioStrategy，在 rebalance() 中完成横截面选股与调仓。
"""
from __future__ import annotations

import statistics
from collections import Counter

from app.core.engine.base_strategy import PortfolioStrategy


def _zscore(vals):
    mm = statistics.fmean(vals)
    sd = statistics.pstdev(vals)
    return [(x - mm) / sd if sd > 0 else 0.0 for x in vals]


def _neutralize_by_group(values: list[float], group_key: list, n_groups: int = 5) -> list[float]:
    """按 group_key（如市值）分位分组，组内 demean，剥离该风格暴露。

    group_key 中缺失值用中位数填充，避免分组崩溃。
    """
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


def _select_by_industry_quota(
    composite: dict, sym_ind: dict, valid: list, top_n: int
) -> set:
    """行业中性选股：按各成分股行业数量占比分配名额，行业内按 composite 排序。

    成分股行业分布≈基准行业分布，因此按数量占比分配名额即可实现行业中性。
    """
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


class Csi800EnhancedStrategy(PortfolioStrategy):
    def init(self, ctx) -> None:
        p = ctx.params
        self.top_n = int(p.get("top_n", 50))
        self.momentum_lookback = int(p.get("momentum_lookback", 60))
        self.reversal_lookback = int(p.get("reversal_lookback", 5))
        self.vol_lookback = int(p.get("vol_lookback", 20))
        self.w_mom = float(p.get("w_mom", 0.5))
        self.w_rev = float(p.get("w_rev", 0.2))
        self.w_vol = float(p.get("w_vol", 0.3))
        self.w_value = float(p.get("w_value", 0.2))
        self.max_weight = float(p.get("max_weight", 0.05))
        # 风格中性开关（1=开启，0=关闭）
        self.neutralize_industry = int(p.get("neutralize_industry", 1)) == 1
        self.neutralize_marketcap = int(p.get("neutralize_marketcap", 1)) == 1

    def rebalance(self, ctx, date: str) -> None:
        universe = ctx.universe()

        mom_raw, rev_raw, vol_raw, val_raw = [], [], [], []
        valid = []
        sym_ind: dict[str, str] = {}
        mcap_list: list = []

        for sym in universe:
            m = ctx.history(sym, self.momentum_lookback + 1)
            r = ctx.history(sym, self.reversal_lookback + 1)
            v = ctx.history(sym, self.vol_lookback + 1)
            if not m or not r or not v:
                continue
            mom_val = m[-1] / m[0] - 1.0
            rev_val = r[-1] / r[0] - 1.0
            rets = [v[i] / v[i - 1] - 1.0 for i in range(1, len(v))]
            vol_val = statistics.pstdev(rets) if len(rets) > 1 else 1e9
            lowvol_val = 1.0 / vol_val if vol_val > 0 else 0.0
            # 价值因子：低估值更优 → 取负值使“越大越优”（pe_ttm 优先，缺失退化为 pb）
            pe = ctx.attribute(sym, "pe_ttm")
            pb = ctx.attribute(sym, "pb")
            if pe is not None and pe > 0:
                val_val = -pe
            elif pb is not None and pb > 0:
                val_val = -pb
            else:
                val_val = 0.0
            mom_raw.append(mom_val)
            rev_raw.append(rev_val)
            vol_raw.append(lowvol_val)
            val_raw.append(val_val)
            valid.append(sym)
            sym_ind[sym] = ctx.attribute(sym, "industry") or "未知"
            mcap_list.append(ctx.attribute(sym, "market_cap"))

        if not valid:
            return

        # 市值中性：剥离规模风格
        if self.neutralize_marketcap:
            mom_raw = _neutralize_by_group(mom_raw, mcap_list)
            rev_raw = _neutralize_by_group(rev_raw, mcap_list)
            vol_raw = _neutralize_by_group(vol_raw, mcap_list)
            val_raw = _neutralize_by_group(val_raw, mcap_list)

        zm, zr, zv, zval = _zscore(mom_raw), _zscore(rev_raw), _zscore(vol_raw), _zscore(val_raw)

        composite = {}
        for i, sym in enumerate(valid):
            composite[sym] = (
                self.w_mom * zm[i] + self.w_rev * zr[i]
                + self.w_vol * zv[i] + self.w_value * zval[i]
            )
            # 上报因子暴露，供因子研究（IC/IR）使用：传入经风格中性化后的原始因子值
            ctx.report_factor(sym, "momentum", mom_raw[i])
            ctx.report_factor(sym, "reversal", rev_raw[i])
            ctx.report_factor(sym, "low_vol", vol_raw[i])
            ctx.report_factor(sym, "value", val_raw[i])

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

        for sym in universe:
            if sym in selected:
                i = valid.index(sym)
                comp = composite[sym]
                rk = rank_map[sym]
                reason = (f"综合得分 {comp:.2f}（第{rk}/{N}名，前{rk / N * 100:.0f}%）；"
                          f"因子z: 动量{zm[i]:+.2f}/反转{zr[i]:+.2f}/低波{zv[i]:+.2f}/价值{zval[i]:+.2f}。"
                          f"入选 Top{self.top_n}，等权配置。")
                ctx.order_target_percent(sym, w, "选股买入", reason)
            elif ctx.position(sym) > 0:
                if sym in composite:
                    i = valid.index(sym)
                    comp = composite[sym]
                    rk = rank_map[sym]
                    reason = (f"综合得分 {comp:.2f}（第{rk}/{N}名），未进 Top{self.top_n}，调出清仓。")
                else:
                    reason = "本期无有效因子数据，调出清仓。"
                ctx.order_target_percent(sym, 0.0, "调出清仓", reason)
