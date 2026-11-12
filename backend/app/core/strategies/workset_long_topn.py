"""工作集成员：单表达式 → 中证800 多头等权 TopN（W48-2 内部薄壳）。

不进入公开策略注册表（registry.STRATEGIES）。编排层自造 meta 后只调
`_run_portfolio`。禁止继承已有增强/固定风格策略类，禁止行业/市值中性，禁止空头。
"""
from __future__ import annotations

import math
from typing import Any, Optional

from app.core.engine.base_strategy import PortfolioStrategy
from app.core.engine.factor_expr import eval_factor
from app.core.strategies.multi_factor import (
    _pit_earnings_surprise,
    _pit_fund_attrs,
)
from app.datahub.ns_vars import as_date, fin_asof, make_ns

# 与契约写死规则一致；实例参数可被 params 覆盖，但 API 不允许改这些值。
DEFAULT_TOP_N = 50
DEFAULT_REBALANCE_PERIOD = 21
DEFAULT_MAX_WEIGHT = 0.05
MIN_CLOSE_BARS = 60  # 与实验室最新截面「len(seg)<60 跳过」对齐
MAKE_NS_WINDOW = 126

# 最近一次 engine.run 的可观察统计（一次 run 对应一次 init）。
# 编排层在 `_run_portfolio` 返回后读取；不改引擎签名。
_LAST_RUN_STATS: dict[str, Any] = {
    "n_valid_max": 0,
    "n_valid_last": 0,
    "n_held_max": 0,
    "n_held_last": 0,
    "n_rebalances": 0,
    "n_zero_valid": 0,
}


def reset_last_run_stats() -> None:
    _LAST_RUN_STATS.update({
        "n_valid_max": 0,
        "n_valid_last": 0,
        "n_held_max": 0,
        "n_held_last": 0,
        "n_rebalances": 0,
        "n_zero_valid": 0,
    })


def peek_last_run_stats() -> dict[str, Any]:
    return dict(_LAST_RUN_STATS)


def _is_finite(v: Any) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


def _date_key(d: Any) -> Optional[str]:
    if d is None:
        return None
    if hasattr(d, "isoformat"):
        return d.isoformat()[:10]
    s = str(d)
    return s[:10] if s else None


def _vol_amt_aligned(ctx: Any, sym: str, n: int) -> tuple[Optional[list], Optional[list]]:
    """从已载入 bars 按 hist_dates 对齐量额；无字段则 None。不改成交路径。"""
    dates = ctx.history_dates(sym, n) if hasattr(ctx, "history_dates") else None
    data = getattr(getattr(ctx, "engine", None), "data", None) or {}
    bars = data.get(sym) or []
    if not dates or not bars:
        return None, None
    by_date: dict[str, Any] = {}
    for b in bars:
        if isinstance(b, dict):
            key = _date_key(b.get("date"))
            rec = b
        else:
            key = _date_key(getattr(b, "date", None))
            rec = b
        if key:
            by_date[key] = rec

    vols: list[Any] = []
    amts: list[Any] = []
    has_v = has_a = False
    for d in dates:
        rec = by_date.get(_date_key(d) or "")
        if rec is None:
            vols.append(None)
            amts.append(None)
            continue
        if isinstance(rec, dict):
            v, a = rec.get("volume"), rec.get("amount")
        else:
            v, a = getattr(rec, "volume", None), getattr(rec, "amount", None)
        vols.append(v)
        amts.append(a)
        if v is not None:
            has_v = True
        if a is not None:
            has_a = True
    return (vols if has_v else None), (amts if has_a else None)


def _fin_asof_for(ctx: Any, sym: str, as_of: str) -> dict | None:
    recs = ctx.financial(sym) if hasattr(ctx, "financial") else []
    if not recs:
        return None
    pairs = []
    for r in recs:
        if not isinstance(r, dict):
            continue
        ann = as_date(r.get("ann_date"))
        if ann is None:
            continue
        pairs.append((ann, {
            "ocf": r.get("ocf"),
            "capex": r.get("capex"),
            "total_assets": r.get("total_assets"),
        }))
    pairs.sort(key=lambda t: t[0])
    if not pairs:
        return None
    return fin_asof({sym: pairs}, sym, as_of)


def _mkt_aligned(ctx: Any, sym: str, n: int) -> list[float]:
    aligned = ctx.benchmark_aligned(sym) if hasattr(ctx, "benchmark_aligned") else None
    if aligned and len(aligned) >= n:
        raw = aligned[-n:]
    else:
        raw = ctx.benchmark_history(n) if hasattr(ctx, "benchmark_history") else None
        raw = list(raw) if raw else []
    out: list[float] = []
    last = None
    for x in raw:
        if x is not None and _is_finite(x):
            last = float(x)
            out.append(last)
        elif last is not None:
            out.append(last)
        else:
            out.append(0.0)
    return out


class WorksetMemberLongTopN(PortfolioStrategy):
    """单成员表达式：PIT 成分内降序取 TopN，等权且单只 ≤ max_weight，只做多。"""

    params: dict[str, Any] = {}

    def init(self, ctx) -> None:
        reset_last_run_stats()
        p = getattr(ctx, "params", {}) or {}
        self.expr = str(p.get("expr") or "")
        self.top_n = int(p.get("top_n", DEFAULT_TOP_N) or DEFAULT_TOP_N)
        self.max_weight = float(p.get("max_weight", DEFAULT_MAX_WEIGHT) or DEFAULT_MAX_WEIGHT)
        # 不读取 neutralize_* 为真；即便传入也不实现中性。
        self._n_valid_last = 0
        self._n_held_last = 0

    def _eval_one(self, ctx, sym: str, date: str) -> Optional[float]:
        hist = getattr(getattr(ctx, "engine", None), "hist", {}).get(sym) if getattr(ctx, "engine", None) else None
        n_avail = len(hist) if hist is not None else 0
        if n_avail < MIN_CLOSE_BARS:
            # 无 engine.hist 时用 history 探测
            probe = ctx.history(sym, MIN_CLOSE_BARS) if hasattr(ctx, "history") else None
            if not probe or len(probe) < MIN_CLOSE_BARS:
                return None
            n_avail = max(len(probe), MIN_CLOSE_BARS)
        n = min(max(n_avail, MIN_CLOSE_BARS), MAKE_NS_WINDOW)
        seg = ctx.history(sym, n)
        if not seg or len(seg) < MIN_CLOSE_BARS:
            return None
        n = len(seg)
        mkt_all = _mkt_aligned(ctx, sym, n)
        attrs = {
            "pe_ttm": ctx.attribute(sym, "pe_ttm") if hasattr(ctx, "attribute") else None,
            "pb": ctx.attribute(sym, "pb") if hasattr(ctx, "attribute") else None,
            "market_cap": ctx.attribute(sym, "market_cap") if hasattr(ctx, "attribute") else None,
            "industry": ctx.attribute(sym, "industry") if hasattr(ctx, "attribute") else None,
        }
        snaps = []
        engine = getattr(ctx, "engine", None)
        if engine is not None:
            snaps = (getattr(engine, "fundamentals", None) or {}).get(sym, []) or []
        attrs.update(_pit_fund_attrs(snaps, date) or {})
        esv = _pit_earnings_surprise(snaps, date)
        vols, amts = _vol_amt_aligned(ctx, sym, n)
        fin = _fin_asof_for(ctx, sym, date)
        try:
            ns = make_ns(list(seg), list(mkt_all), attrs, esv=esv, vols=vols, amts=amts, fin=fin)
        except Exception:
            return None
        if not ns:
            return None
        return eval_factor(self.expr, ns)

    def rebalance(self, ctx, date: str) -> None:
        universe = list(ctx.universe() or [])
        scores: list[tuple[str, float]] = []
        for sym in universe:
            val = self._eval_one(ctx, sym, date)
            if val is None or not _is_finite(val):
                continue
            fv = float(val)
            scores.append((sym, fv))
            if hasattr(ctx, "report_factor"):
                try:
                    ctx.report_factor(sym, "workset_member", fv)
                except Exception:
                    pass

        n_valid = len(scores)
        scores.sort(key=lambda x: x[1], reverse=True)
        selected = [s for s, _ in scores[: self.top_n]]
        n_held = len(selected)
        w = min(1.0 / n_held, self.max_weight) if n_held else 0.0

        held = set()
        if hasattr(ctx, "positions"):
            pos = ctx.positions()
            if isinstance(pos, dict):
                held = {k for k, v in pos.items() if v and float(v) > 0}
        all_syms = set(universe) | held
        sel_set = set(selected)

        for sym in all_syms:
            if n_held == 0:
                # 0 有效值：不清新开多，只把已持仓（含已退出指数的）清到 0
                if sym in held:
                    ctx.order_target_percent(sym, 0.0, "调出清仓", "无有效因子值")
                continue
            if sym in sel_set:
                ctx.order_target_percent(sym, w, "选股买入", f"expr top{self.top_n}")
            else:
                ctx.order_target_percent(sym, 0.0, "调出清仓", "未入选或已退出成分")

        self._n_valid_last = n_valid
        self._n_held_last = n_held
        _LAST_RUN_STATS["n_valid_last"] = n_valid
        _LAST_RUN_STATS["n_held_last"] = n_held
        _LAST_RUN_STATS["n_valid_max"] = max(int(_LAST_RUN_STATS["n_valid_max"]), n_valid)
        _LAST_RUN_STATS["n_held_max"] = max(int(_LAST_RUN_STATS["n_held_max"]), n_held)
        _LAST_RUN_STATS["n_rebalances"] = int(_LAST_RUN_STATS["n_rebalances"]) + 1
        if n_valid == 0:
            _LAST_RUN_STATS["n_zero_valid"] = int(_LAST_RUN_STATS["n_zero_valid"]) + 1
