"""遗传规划因子自动挖掘引擎（QuantaAlpha 思想的本地化落地，无 LLM 依赖）。

方法借鉴（QuantaAlpha: An Evolutionary Framework for LLM-Driven Alpha Mining）：
- 多样化假设初始化：研究方向模板播种初始种群，抑制同质化
- 进化搜索：μ+λ 变异进化（函数替换/参数扰动/变量轮换/子树重生），以 IC 检验为适应度
- 复杂度控制：C(f)=α₁·AST长度 + α₂·参数数 + α₃·log(1+特征数)，纳入适应度惩罚
- 冗余控制：候选与现有因子的数值相关性

流程：构建评估上下文（一次加载）→ 方向模板播种种群 → 逐代变异淘汰 →
Top-K 因子用完整检验（mine_factor 全核心池）出正式报告。
"""
from __future__ import annotations

import ast
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.engine.factor_expr import eval_factor
from app.models import KlineDaily, Stock, FundamentalsHistory, IndexKlineDaily
from app.services.factor_mining import (
    _spearman,
    _mean,
    _std,
    compute_complexity,
    mine_factor,
    validate_expr,
)

# ============ 表达式文法 ============
LIST_VARS = ["c_m", "c_v", "c_b", "c_r", "c_t"]
ATTR_VARS = ["pe_ttm", "pb", "market_cap", "roe", "revenue_yoy", "profit_yoy", "earnings_surprise"]

# 一元列表→标量 终结函数
TERMINALS_1ARG = ["std", "mean", "min", "max", "sum", "skew", "maxdd"]
# 二元 列表+基准→标量
TERMINALS_MKT = ["beta", "idio_vol"]
# 列表→列表 中间变换
TRANSFORMS = ["zscore", "rank", "winsor", "returns"]
# 标量一元变换
SCALAR_UNARY = ["abs", "sqrt_abs", "log_abs1", "sign"]
BINOPS = ["+", "-", "*"]

# 研究方向模板（多样化初始化：不同方向约束首层构造，抑制同质化）
DIRECTION_TEMPLATES = {
    "momentum": {"prefer_terminal": ["roc"], "prefer_var": ["c_m", "c_r", "c_t"], "note": "动量趋势"},
    "volatility": {"prefer_terminal": ["std", "idio_vol", "maxdd"], "prefer_var": ["c_v", "c_b"], "note": "波动率结构"},
    "value": {"prefer_attr": True, "note": "估值变换"},
    "quality": {"prefer_terminal": [], "prefer_attr_weight": 0.7, "prefer_var": ["c_v"], "note": "质量/成长联动"},
    "reversal": {"prefer_terminal": ["roc"], "invert": True, "prefer_var": ["c_r", "c_m"], "note": "均值回归"},
}


def random_expr(rng: random.Random, direction: str | None = None, depth: int = 2) -> str:
    """按方向模板生成一条合法标量表达式。"""
    tpl = DIRECTION_TEMPLATES.get(direction or "", {})

    def gen(d: int) -> str:
        r = rng.random()
        if d <= 0 or r < 0.3:
            # 终结层
            if tpl.get("prefer_attr") and rng.random() < 0.7:
                return f"safe_inv({rng.choice(ATTR_VARS[:2])}, 0.001, 1000)"
            weight_attr = tpl.get("prefer_attr_weight")
            if weight_attr and rng.random() < weight_attr:
                return f"{rng.choice(['mean', 'max'])}(({rng.choice(LIST_VARS)}))"
            terms = tpl.get("prefer_terminal") or TERMINALS_1ARG
            fn = rng.choice(terms)
            var = rng.choice(tpl.get("prefer_var") or LIST_VARS)
            if fn in TERMINALS_MKT:
                return f"{fn}({var}, mkt_b)"
            if fn == "roc":
                return f"roc({var}, {rng.choice([5, 10, 20, 40, 60])})"
            return f"{fn}({var})" if fn not in ("sum",) else f"sum(({var})[-{rng.choice([10, 20, 40])}:])"
        if r < 0.75:
            op = rng.choice(BINOPS + (["/"] if rng.random() < 0.25 else []))
            a, b = gen(d - 1), gen(d - 1)
            return f"({a} {op} ({b}))" if op == "/" else f"({a} {op} {b})"
        u = rng.choice(SCALAR_UNARY)
        inner = gen(d - 1)
        if u == "sqrt_abs":
            return f"sqrt(abs({inner}))"
        if u == "log_abs1":
            return f"log(abs({inner}) + 1)"
        return f"{u}({inner})"

    e = gen(max(1, depth))
    # 反转方向取负（均值回归）
    if tpl.get("invert") and rng.random() < 0.6:
        e = f"-({e})"
    return e


def mutate_expr(expr: str, rng: random.Random) -> str:
    """语义级变异：AST 节点定位 + 结构化改写（而非盲目字符替换）。"""
    try:
        tree = ast.parse(expr, mode="eval").body
    except Exception:  # noqa: BLE001
        return random_expr(rng, None, 2)

    points: list[tuple[str, object]] = []  # (kind, node)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            points.append(("call", node))
        elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            points.append(("const", node))
        elif isinstance(node, ast.Name) and node.id in set(LIST_VARS) | set(ATTR_VARS):
            points.append(("var", node))
    if not points:
        return random_expr(rng, None, 2)

    kind, node = rng.choice(points)
    t = ast.parse(random_expr(rng, None, 1), mode="eval").body  # 备用片段

    if kind == "call":
        fname = node.func.id
        same_family = {
            "std": TERMINALS_1ARG, "mean": TERMINALS_1ARG, "min": TERMINALS_1ARG,
            "max": TERMINALS_1ARG, "skew": TERMINALS_1ARG, "maxdd": TERMINALS_1ARG,
            "zscore": TRANSFORMS, "rank": TRANSFORMS, "returns": TRANSFORMS,
            "roc": ["roc"],
        }.get(fname)
        if same_family and len(node.args) == 1 and not any(k.keyword for k in node.keywords):
            new_fn = rng.choice([f for f in same_family if f != fname] or [fname])
            node.func.id = new_fn
        elif kind == "call":
            args_to_touch = [a for a in node.args]
            if args_to_touch:
                pick = rng.choice(args_to_touch)
                if isinstance(pick, ast.Constant) and isinstance(pick.value, (int, float)):
                    pick.value = round(pick.value * rng.uniform(0.5, 1.6)) or pick.value
    elif kind == "const":
        node.value = round(node.value * rng.uniform(0.5, 1.6)) if node.value else node.value
    elif kind == "var":
        pool = LIST_VARS if node.id in LIST_VARS else ATTR_VARS
        node.id = rng.choice(pool)

    try:
        out = ast.unparse(ast.Expression(body=tree)).strip()
        out = out.replace("Math.", "").replace("math.", "")
        validate_expr(out)
        return out
    except Exception:  # noqa: BLE001
        return random_expr(rng, None, 2)


# ============ 评估上下文 ============
def build_context(db: Session, start: str, end: str, forward: int, step: int,
                  pool_size: int | None = 200) -> dict:
    """一次性加载评估所需数据（多候选共享）。pool_size 抽样加速。"""
    from app.services.factor_mining import _core_universe, _attrs_map, _es_map, _dates_in_range

    ed = date.fromisoformat(end) if end else date.today()
    sd = date.fromisoformat(start) if start else ed - timedelta(days=400)
    syms_all = _core_universe(db)
    if pool_size:
        syms = syms_all[: min(pool_size, len(syms_all))]
    else:
        syms = syms_all

    attrs = _attrs_map(db, syms)
    es_map = _es_map(db, syms)
    load_from = sd - timedelta(days=300)

    axis_dates = _dates_in_range(db, load_from, ed)
    aligned: dict[str, dict[date, float]] = {}
    rows = db.execute(
        select(KlineDaily.symbol, KlineDaily.trade_date, KlineDaily.close)
        .where(KlineDaily.symbol.in_(syms), KlineDaily.adj == "qfq",
               KlineDaily.trade_date >= load_from, KlineDaily.trade_date <= ed)
    ).all()
    for sym, td, close in rows:
        aligned.setdefault(sym, {})[td] = float(close)
    bench_rows = db.execute(
        select(IndexKlineDaily.trade_date, IndexKlineDaily.close)
        .where(IndexKlineDaily.symbol == "sh000906",
               IndexKlineDaily.trade_date >= load_from, IndexKlineDaily.trade_date <= ed)
        .order_by(IndexKlineDaily.trade_date)
    ).all()
    bench_map = {td: float(c) for td, c in bench_rows}
    bench_aligned = [bench_map.get(d) for d in axis_dates]

    all_in = _dates_in_range(db, sd, ed)
    snapshots = [d for i, d in enumerate(all_in) if i % step == 0 and i + forward < len(all_in)]
    snap_pairs = []
    for snap in snapshots:
        try:
            idx = axis_dates.index(snap)
        except ValueError:
            continue
        if idx + forward >= len(axis_dates):
            continue
        fwd_date = axis_dates[idx + forward]
        # 危机截面判定：持有窗口内基准下跌（风格条件挖掘，事后分类持有环境）
        bench_ret = None
        if bench_map.get(snap) and bench_map.get(fwd_date):
            bench_ret = bench_map[fwd_date] / bench_map[snap] - 1.0
        crisis = bench_ret is not None and bench_ret < -0.03
        snap_pairs.append((idx, snap, fwd_date, bench_ret, crisis))

    # —— 正交化代表因子预计算（增量 alpha 的基准集合）——
    # 代表性表达式：动量/反转/波动/价值/规模，每期截面算一次缓存，候选取用零成本
    ORTHO_EXPRS = {
        "momentum": "roc(c_m, 20)",
        "reversal": "-roc(c_r, 5)",
        "low_vol": "std(c_v)",
        "value": "safe_inv(pe_ttm, 0.001, 1000)",
        "size": "log(market_cap + 1)",
    }
    ortho_cache: dict[date, dict[str, dict[str, float]]] = {}  # snap -> {fac -> {sym: val}}
    for fac_i, (fac_name, fexpr) in enumerate(ORTHO_EXPRS.items()):
        for idx, snap, fwd_date, _br, _crisis in snap_pairs:
            if snap not in bench_map:
                continue
            i0 = max(0, idx - 130)
            slice_dates = axis_dates[i0: idx + 1]
            bucket = ortho_cache.setdefault(snap, {})
            for sym in syms:
                amap = aligned.get(sym)
                if not amap or snap not in amap:
                    continue
                seg = [c for c in (amap.get(d) for d in slice_dates) if c is not None]
                a = attrs.get(sym, {}) or {}
                ns = {"c_m": seg, "c_r": seg, "c_v": seg, "c_b": seg, "c_t": seg,
                      "mkt_b": bench_aligned[i0: idx + 1][-len(seg):],
                      "pe_ttm": a.get("pe_ttm"), "pb": a.get("pb"), "market_cap": a.get("market_cap"),
                      "roe": a.get("roe"), "revenue_yoy": a.get("revenue_yoy"),
                      "profit_yoy": a.get("profit_yoy"), "earnings_surprise": es_map.get(sym),
                      "industry": a.get("industry")}
                try:
                    v = eval_factor(fexpr, ns)
                except Exception:  # noqa: BLE001
                    v = None
                if v is not None and math.isfinite(v):
                    bucket.setdefault(fac_name, {})[sym] = v

    return {
        "syms": syms, "attrs": attrs, "es_map": es_map, "aligned": aligned,
        "axis_dates": axis_dates, "bench_aligned": bench_aligned, "snap_pairs": snap_pairs,
        "ortho_cache": ortho_cache,
    }


def _orthogonalize(fv: list[tuple[str, float, float]], fac_bucket: dict[str, dict[str, float]] | None):
    """候选值对代表因子做横截面 OLS 回归，返回残差列表 [(sym, resid, ret)]。"""
    if not fac_bucket:
        return fv
    syms_ok = [x for x in fv if x[0] in fac_bucket and all(x[0] in v for v in fac_bucket.values())]
    if len(syms_ok) < 20:
        return fv
    xs = [[1.0] + [fac_bucket[f][x[0]] for f in fac_bucket] for x in syms_ok]
    ys = [x[1] for x in syms_ok]
    n = len(xs)
    k = len(xs[0])
    # 正规方程 (X'X) b = X'y（列间近独立，直接解）
    try:
        xtx = [[sum(xs[i][a] * xs[i][b_] for i in range(n)) for b_ in range(k)] for a in range(k)]
        xty = [sum(xs[i][a] * ys[i] for i in range(n)) for a in range(k)]
        # 高斯消元
        m = [row[:] + [xty[j]] for j, row in enumerate(xtx)]
        for col in range(k):
            piv = max(range(col, k), key=lambda r: abs(m[r][col]))
            if abs(m[piv][col]) < 1e-12:
                return fv
            m[col], m[piv] = m[piv], m[col]
            for r2 in range(k):
                if r2 != col and m[col][col]:
                    f = m[r2][col] / m[col][col]
                    m[r2] = [m[r2][c] - f * m[col][c] for c in range(k + 1)]
        beta = [m[i][k] / m[i][i] for i in range(k)]
        out = []
        for i, x in enumerate(syms_ok):
            pred = sum(beta[a] * xs[i][a] for a in range(k))
            out.append((x[0], x[1] - pred, x[2]))
        return out
    except Exception:  # noqa: BLE001
        return fv


def evaluate_candidate(expr: str, ctx: dict, groups: int,
                       orthogonal: bool = False, crisis_only: bool = False) -> dict:
    """单候选轻量评估：IC 族指标 + 单调性 + 复杂度。

    orthogonal=True：候选因子值对代表因子（动量/反转/低波/价值/规模）横截面
                      回归取残差再算 IC —— 度量"增量预测能力"，惩罚重复发现。
    crisis_only=True：仅统计持有窗口内基准下跌(<-3%)的截面 —— 危机 Alpha。
    """
    ok, err, _ = validate_expr(expr)
    if not ok:
        return {"error": err}
    ic_list: list[float] = []
    g_rets: dict[int, list[float]] = {g: [] for g in range(1, groups + 1)}
    crisis_ic_list: list[float] = []
    crisis_windows = 0
    fv_cache: dict[tuple, list[float]] = {}
    for idx, snap, fwd_date, bench_ret, is_crisis in ctx["snap_pairs"]:
        i0 = max(0, idx - 130)
        slice_dates = ctx["axis_dates"][i0: idx + 1]
        bench_seg_raw = ctx["bench_aligned"][i0: idx + 1]
        if bench_ret is not None and bench_ret < 0:
            crisis_windows += 1
        fv: list[tuple[str, float, float]] = []
        for sym in ctx["syms"]:
            amap = ctx["aligned"].get(sym)
            if not amap or snap not in amap or fwd_date not in amap:
                continue
            key = (sym, idx)
            seg_list = fv_cache.get(key)
            if seg_list is None:
                seg_list = [c for c in (amap.get(d) for d in slice_dates) if c is not None]
                fv_cache[key] = seg_list
            seg = seg_list
            if len(seg) < 55:
                continue
            mkt_b = bench_seg_raw[-len(seg):]
            if len(mkt_b) != len(seg):
                continue
            a = ctx["attrs"].get(sym, {}) or {}
            # 差异化窗口（对齐 multi_factor 语义）：动量125/反转25/波动125/Beta125/尾部125
            def tail(n: int) -> list[float]:
                return seg[-n:] if len(seg) >= n else seg
            ns = {"c_m": tail(126), "c_r": tail(26), "c_v": tail(61), "c_b": tail(126),
                  "c_t": tail(121), "mkt_b": mkt_b[-(len(tail(126))):],
                  "pe_ttm": a.get("pe_ttm"), "pb": a.get("pb"), "market_cap": a.get("market_cap"),
                  "roe": a.get("roe"), "revenue_yoy": a.get("revenue_yoy"),
                  "profit_yoy": a.get("profit_yoy"), "earnings_surprise": ctx["es_map"].get(sym),
                  "industry": a.get("industry")}
            try:
                v = eval_factor(expr, ns)
            except Exception:  # noqa: BLE001
                v = None
            if v is None or not math.isfinite(v):
                continue
            ret = amap[fwd_date] / amap[snap] - 1.0
            fv.append((sym, v, ret))
        if len(fv) < 25:
            continue
        vals = [x[1] for x in fv]
        rets = [x[2] for x in fv]
        ic_raw = _spearman(vals, rets)
        # 危机截面单独记录
        if is_crisis:
            crisis_ic_list.append(ic_raw)
        if orthogonal:
            vals = [x[1] for x in _orthogonalize(fv, ctx["ortho_cache"].get(snap))]
            ic_raw = _spearman(vals, rets)
        if crisis_only and not is_crisis:
            continue
        ic_list.append(ic_raw)
        ordered_key = sorted(zip([x[0] for x in fv], vals), key=lambda t: t[1])
        order_idx = {sym: i for i, (sym, _) in enumerate(ordered_key)}
        gs = max(1, len(fv) // groups)
        ordered_sorted = sorted(fv, key=lambda x: order_idx[x[0]])
        for g in range(1, groups + 1):
            part = ordered_sorted[(g - 1) * gs: g * gs]
            if part:
                g_rets[g].append(_mean([x[2] for x in part]))
    min_periods = 2 if crisis_only else 3
    if len(ic_list) < min_periods or (crisis_only and len(crisis_ic_list) == 0):
        return {"error": "有效截面不足"}
    ic_mean = _mean(ic_list)
    ic_stdv = _std(ic_list)
    icir = ic_mean / ic_stdv if ic_stdv > 0 else 0.0
    t_stat = ic_mean / (ic_stdv / math.sqrt(len(ic_list))) if ic_stdv > 0 else 0.0
    win = sum(1 for x in ic_list if x > 0) / len(ic_list)
    gm = {g: (_mean(g_rets[g]) if g_rets[g] else 0.0) for g in range(1, groups + 1)}
    diffs = [gm[g + 1] - gm[g] for g in range(1, groups)]
    mono_score = (sum(1 for d in diffs if d > 0) / len(diffs)) if diffs else 0.0
    cx = compute_complexity(expr)
    fitness = abs(ic_mean) * win + 0.3 * abs(icir) - 0.04 * cx["score"]
    crisis_info = {}
    if crisis_ic_list:
        crisis_info = {
            "crisis_ic": round(_mean(crisis_ic_list), 4),
            "crisis_windows": crisis_windows,
            "crisis_used": len(crisis_ic_list),
        }
    cx = compute_complexity(expr)
    fitness = abs(ic_mean) * win + 0.3 * abs(icir) - 0.04 * cx["score"]
    return {
        "ic_mean": round(ic_mean, 5), "icir": round(icir, 4), "t_stat": round(t_stat, 3),
        "win": win, "mono_score": round(mono_score, 3),
        "group_means": {str(g): round(v, 5) for g, v in gm.items()},
        "complexity": cx, "fitness": fitness,
        "n_periods": len(ic_list),
        "crisis": crisis_info,
        "orthogonal": orthogonal,
    }


def _old_metrics_placeholder():
    """保留以下计算供 mine_factor 使用。"""

def legacy_tail():
    pass
# ============ GP 主搜索 ============
DIRECTIONS = list(DIRECTION_TEMPLATES.keys())


def gp_search(db: Session, directions: list[str] | None = None,
              pop_size: int = 14, generations: int = 6,
              start: str = "", end: str = "", forward: int = 20,
              step: int = 30, pool_size: int | None = 220,
              top_k: int = 3, orthogonal: bool = False, crisis_only: bool = False,
              progress=None) -> dict:
    """遗传规划批量挖掘：返回精英报告列表（top_k 已全池精评）。

    orthogonal=True 挖"增量 alpha"（对代表因子正交后的残差 IC）；
    crisis_only=True 只在基准下跌窗口评 IC（危机 Alpha）。"""
    dirs = [d for d in (directions or DIRECTIONS) if d in DIRECTION_TEMPLATES] or DIRECTIONS
    rng = random.Random(datetime.now().microsecond)

    ctx = build_context(db, start, end, forward, step, pool_size)
    if progress:
        progress(0.08, f"上下文就绪：{len(ctx['syms'])} 只 · {len(ctx['snap_pairs'])} 个快速截面")

    # 初始种群：方向均匀播种（互补初始化）
    population: list[tuple[str, dict]] = []
    seen: set[str] = set()
    while len(population) < pop_size:
        d = dirs[len(population) % len(dirs)]
        e = random_expr(rng, d, depth=rng.choice([1, 2]))
        if e in seen:
            continue
        res = evaluate_candidate(e, ctx, groups=5, orthogonal=orthogonal, crisis_only=crisis_only)
        seen.add(e)
        if "error" not in res:
            population.append((e, res))

    history_best = []
    total_gens = max(generations, 1)
    for gen in range(total_gens):
        if progress:
            done = 0.1 + 0.75 * (gen + 1) / total_gens
            best = max(population, key=lambda p: p[1].get("fitness", -9))[0]
            progress(done, f"第 {gen + 1}/{total_gens} 代 · 种群 {len(population)} · 最优 IC={max(p[1]['ic_mean'] for p in population):.4f}")
        population.sort(key=lambda p: p[1].get("fitness", -9), reverse=True)
        survivors = population[: max(2, pop_size // 2)]
        children: list[tuple[str, dict]] = []
        while len(children) < pop_size - len(survivors):
            parent = rng.choice(survivors)[0]
            child = mutate_expr(parent, rng)
            if child in seen:
                continue
            seen.add(child)
            cres = evaluate_candidate(child, ctx, groups=5, orthogonal=orthogonal, crisis_only=crisis_only)
            if "error" not in cres:
                children.append((child, cres))
        population = survivors + children
        best_ic = max(p[1]["ic_mean"] for p in population)
        history_best.append({"gen": gen + 1, "best_ic": round(best_ic, 4),
                             "avg_fitness": round(_mean([p[1].get("fitness", 0) for p in population]), 4)})
        if progress:
            pass

    population.sort(key=lambda p: p[1]["fitness"], reverse=True)
    elites = population[:top_k]

    # 精评：全核心池完整检验（正式报告口径）
    final_reports = []
    for i, (expr, quick) in enumerate(elites):
        if progress:
            progress(0.88 + 0.1 * i / max(len(elites), 1), f"精英 #{i + 1} 全池精评中…")
        full = mine_factor(db, expr, name=f"GP-elite{i + 1}", start=start, end=end,
                           groups=5, forward=forward, universe=None)
        if full.get("ok"):
            full["quick_ic"] = round(quick["ic_mean"], 4)
            full["quick_icir"] = round(quick.get("icir", 0), 4)
            full["fitness"] = round(quick["fitness"], 4)
            if orthogonal:
                full["residual_ic"] = full["ic_mean"]  # 残差口径提示由前端标注
            if quick.get("crisis"):
                full["crisis_info"] = quick["crisis"]
            final_reports.append(full)
    final_reports.sort(key=lambda r: abs(r.get("icir", 0)), reverse=True)
    return {
        "ok": True,
        "directions": dirs,
        "pop_size": pop_size,
        "generations": total_gens,
        "evolution_log": history_best,
        "elites": final_reports,
        "n_candidates_evaluated": len(seen),
    }
