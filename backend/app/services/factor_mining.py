"""因子挖掘服务（自定义表达式 → 截面计算 → 有效性检验）。

检验指标（与回测引擎同口径，Spearman 秩相关）：
- IC：每期截面 因子值 vs 未来 N 日收益 的 Spearman 相关
- ICIR = mean(IC) / std(IC)；t 值；IC>0 胜率
- 分组单调性：因子值分 G 组，各组合未来收益均值是否单调
- 多空组合：Top组 − Bottom组 累计收益曲线
- 与现有 14 因子相关性（最新截面，去重判断）

数据：K线/属性/基本面全部从库读（DuckDB 优先由 data_source 处理），
截面属性非 PIT（挖掘探索阶段可接受，落地进策略库后由引擎做 PIT）。
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from sqlalchemy import select, and_
from sqlalchemy.orm import Session

from app.core.engine.factor_expr import eval_factor
from app.datahub.ns_vars import fin_asof, fin_hist_map, make_ns, var_names
from app.models import (
    FACTOR_COLUMNS,
    FactorDaily,
    FinancialsRaw,
    FundamentalsHistory,
    Stock,
)
from app.services.adjust import apply_adjust_series

# 截面抽样间隔（交易日）：20 ≈ 每月一次
DEFAULT_STEP = 20
DEFAULT_FORWARD = 20
DEFAULT_GROUPS = 5


# ============ 工具 ============
def _spearman(a: list[float], b: list[float]) -> float:
    """Spearman 秩相关系数（并列用平均秩）。"""
    n = len(a)
    if n < 5:
        return 0.0

    def ranks(xs: list[float]) -> list[float]:
        idx = sorted(range(len(xs)), key=lambda i: xs[i])
        r = [0.0] * len(xs)
        i = 0
        while i < len(idx):
            j = i
            while j + 1 < len(idx) and xs[idx[j + 1]] == xs[idx[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[idx[k]] = avg
            i = j + 1
        return r

    ra, rb = ranks(a), ranks(b)
    ma = sum(ra) / n
    mb = sum(rb) / n
    cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((x - mb) ** 2 for x in rb))
    if va == 0 or vb == 0:
        return 0.0
    return cov / (va * vb)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def resolve_range(start: str, end: str) -> tuple[date, date]:
    """解析挖掘区间（缺省 end=今天、start=end 往前 400 天）。

    单独抽出来是为了让「结果实际用的区间」与「落库的参数签名」同源 ——
    查重层据此生成签名，不会因为默认值展开规则不一致而误判重复。
    """
    ed = date.fromisoformat(end) if end else date.today()
    sd = date.fromisoformat(start) if start else ed - timedelta(days=400)
    return sd, ed


def snapshot_dates(db: Session, start: str, end: str,
                   forward: int = DEFAULT_FORWARD, step: int = DEFAULT_STEP) -> list[str]:
    """本次挖掘将使用的截面日期列表（与 mine_factor 内部分期规则同源）。

    用于识别「参数签名缺失的历史记录」：签名是后加的列，老记录为空，
    但它们的 ic_series 日期序列由 (区间, forward, step) 唯一决定 ——
    日期序列一致即等价于同一组参数，可据此回填签名并判定重复。
    """
    sd, ed = resolve_range(start, end)
    all_dates = _dates_in_range(db, sd, ed)
    return [d.isoformat() for i, d in enumerate(all_dates)
            if i % step == 0 and i + forward < len(all_dates)]


def _sig(x: float | None) -> float | None:
    """保留 6 位有效数字：因子值量纲横跨 1e-3（fcf_yield）到 1e2（市值），
    固定小数位要么丢信息要么刷屏，有效数字才是量纲无关的写法。"""
    if x is None or not math.isfinite(x):
        return None
    return float(f"{x:.6g}")


def _quantile(sorted_xs: list[float], q: float) -> float:
    """线性插值分位数（sorted_xs 必须已升序）。"""
    if not sorted_xs:
        return 0.0
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    pos = q * (len(sorted_xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def _factor_stats(vals: list[float], n_periods: int) -> dict:
    """因子值本身的分布统计（与 IC 无关）。

    存在的意义：IC 是 Spearman 秩相关，对整体缩放、加减常数、单调变换完全
    免疫 —— 用户调整这类参数时 IC 分毫不动，会误以为「改了没反应」。因子值
    的均值/标准差/分位数对数值变化敏感，是唯一能让参数调整「可见」的反馈。
    """
    if not vals:
        return {}
    s = sorted(vals)
    return {
        "n_values": len(vals),
        "n_per_period": round(len(vals) / n_periods, 1) if n_periods else 0.0,
        "mean": _sig(_mean(vals)),
        "std": _sig(_std(vals)),
        "min": _sig(s[0]),
        "p25": _sig(_quantile(s, 0.25)),
        "p50": _sig(_quantile(s, 0.50)),
        "p75": _sig(_quantile(s, 0.75)),
        "max": _sig(s[-1]),
        "skew": _sig(_skew(vals)),
        "n_unique": len(set(round(v, 10) for v in s)),
    }


def _skew(xs: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    m = _mean(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / n)
    if sd == 0:
        return 0.0
    return sum(((x - m) / sd) ** 3 for x in xs) / n


def validate_expr(expr: str) -> tuple[bool, str, float | None]:
    """校验表达式：AST 预检（函数名/变量名/属性访问）→ 受限命名空间试算。

    白名单与试算命名空间均从 ns_vars 派生，而非在此硬编码：
    变量增删时校验层自动跟随，保证「校验通过」等价于「真实挖掘可求值」。
    """
    import ast
    import random

    from app.core.engine.factor_expr import FUNCS

    # ① AST 预检：非法函数/变量/属性访问在试算前报出
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        return False, f"语法错误: {e.msg}", None
    allowed_vars = var_names()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCS:
                fn = node.func.id if isinstance(node.func, ast.Name) else "<表达式>"
                return False, f"未定义的函数: {fn}", None
        elif isinstance(node, ast.Attribute):
            return False, "不支持属性访问（如 .xxx）", None
        elif isinstance(node, ast.Name) and node.id not in allowed_vars and node.id not in FUNCS:
            return False, f"未定义的变量: {node.id}", None

    # ② 试算：命名空间由 make_ns 构造，与真实挖掘同构
    random.seed(42)
    n = 140
    base = 10.0
    closes: list[float] = []
    for _ in range(n):
        base *= 1 + random.uniform(-0.02, 0.02)
        closes.append(round(base, 3))
    vols = [round(1e6 * (1 + random.uniform(-0.3, 0.3)), 1) for _ in range(n)]
    amts = [round(1e8 * (1 + random.uniform(-0.3, 0.3)), 1) for _ in range(n)]
    ns = make_ns(
        closes, closes,
        {"pe_ttm": 15.0, "pb": 2.0, "market_cap": 100.0, "roe": 0.12,
         "revenue_yoy": 0.10, "profit_yoy": 0.08, "industry": "测试"},
        news=0.02, esv=0.02, vols=vols, amts=amts,
        fin={"ocf": 1e9, "capex": 2e8, "total_assets": 5e9},
    )
    try:
        v = eval_factor(expr, ns)
        if v is None:
            return False, "表达式计算无结果（可能除零/数据不足）", None
        return True, "", v
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", None


# 复杂度权重（QuantaAlpha C(f) = α₁·语法长度 + α₂·参数数 + α₃·log(1+特征数)）
_W_SL, _W_PC, _W_F = 0.15, 0.25, 0.6

# 变量白名单统一由 ns_vars 派生，避免与 make_ns 增删脱节
_ALLOWED_VARS = var_names()


def compute_complexity(expr: str) -> dict:
    """AST 复杂度（QuantaAlpha）：结构长度 + 参数计数 + 信息源多样性，越低越好。"""
    import ast

    try:
        tree = ast.parse(expr, mode="eval")
    except Exception:  # noqa: BLE001
        return {"sl": 0, "pc": 0, "nf": 0, "score": 0.0}
    nodes = sum(1 for _ in ast.walk(tree))
    # 参数计数：数字常量（可调参数）
    pc = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)))
    nf = len({n.id for n in ast.walk(tree)
              if isinstance(n, ast.Name) and n.id in _ALLOWED_VARS and not n.id.startswith("c_")
              } | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in ("c_m", "c_r", "c_v")})
    score = round(_W_SL * (nodes - 1) + _W_PC * pc + _W_F * math.log1p(max(nf, 0)), 3)
    return {"sl": nodes - 1, "pc": pc, "nf": nf, "score": score}


# ============ 数据加载 ============
def _core_universe(db: Session) -> list[str]:
    from app.services.etl import _get_universe

    return _get_universe(db)


def _attrs_map(db: Session, syms: list[str]) -> dict:
    rows = db.execute(
        select(Stock.symbol, Stock.industry, Stock.market_cap, Stock.pe_ttm, Stock.pb,
               Stock.roe, Stock.revenue_yoy, Stock.profit_yoy)
        .where(Stock.symbol.in_(syms))
    ).all()
    return {
        r.symbol: {"industry": r.industry, "market_cap": r.market_cap, "pe_ttm": r.pe_ttm,
                   "pb": r.pb, "roe": r.roe, "revenue_yoy": r.revenue_yoy, "profit_yoy": r.profit_yoy}
        for r in rows
    }


def _es_map(db: Session, syms: list[str]) -> dict:
    """PEAD 盈余惊喜（最新期 − 历史均值，≥2 期；与回测同口径）。"""
    rows = db.execute(
        select(FundamentalsHistory.symbol, FundamentalsHistory.report_date, FundamentalsHistory.profit_yoy)
        .where(FundamentalsHistory.symbol.in_(syms))
        .order_by(FundamentalsHistory.report_date)
    ).all()
    out: dict[str, float | None] = {}
    cur: dict[str, list[float]] = {}
    for sym, _rd, py in rows:
        if py is not None:
            cur.setdefault(sym, []).append(float(py))
    for sym, vals in cur.items():
        if len(vals) >= 2:
            out[sym] = (vals[-1] - _mean(vals[:-1])) / 100.0
        else:
            out[sym] = None
    return out


def _closes_for(db: Session, syms: list[str], sd: date, ed: date) -> dict[str, list[float]]:
    """批量加载区间**复权后**收盘序列（内存复用，避免逐期查库）。

    库内 kline_daily 存的是未复权原价，这里折算成后复权后再交给因子引擎 ——
    动量/反转/波动率这类因子依赖于价格序列的连续性，直接用原始价会把除权
    跳变当成真实涨跌（实测含除权窗口约 1/10 的动量方向会翻转）。
    """
    from app.models import AdjFactorDaily, KlineDaily

    rows = db.execute(
        select(KlineDaily.symbol, KlineDaily.trade_date, KlineDaily.close,
               AdjFactorDaily.adj_factor)
        .outerjoin(AdjFactorDaily, and_(
            AdjFactorDaily.symbol == KlineDaily.symbol,
            AdjFactorDaily.trade_date == KlineDaily.trade_date))
        .where(KlineDaily.symbol.in_(syms), KlineDaily.adj == "none",
               KlineDaily.trade_date >= sd, KlineDaily.trade_date <= ed)
        .order_by(KlineDaily.symbol, KlineDaily.trade_date)
    ).all()

    raw: dict[str, list[float]] = {}
    fac: dict[str, list] = {}
    for sym, _td, close, f in rows:
        raw.setdefault(sym, []).append(float(close))
        fac.setdefault(sym, []).append(f)
    return {s: apply_adjust_series(v, fac[s], "hfq") for s, v in raw.items()}


def _bench_closes(db: Session, sd: date, ed: date) -> list[float]:
    from app.models import IndexKlineDaily

    rows = db.execute(
        select(IndexKlineDaily.close).where(
            IndexKlineDaily.symbol == "sh000906",
            IndexKlineDaily.trade_date >= sd, IndexKlineDaily.trade_date <= ed,
        ).order_by(IndexKlineDaily.trade_date)
    ).all()
    return [float(r[0]) for r in rows]


def _dates_in_range(db: Session, sd: date, ed: date) -> list[date]:
    from app.models import KlineDaily

    rows = db.execute(
        select(KlineDaily.trade_date).where(
            KlineDaily.trade_date >= sd, KlineDaily.trade_date <= ed,
        ).distinct().order_by(KlineDaily.trade_date)
    ).all()
    return [r[0] for r in rows]


def _collect_seg(cmap: dict, vmap: dict, amtmap: dict, dates: list) -> tuple[list, list, list]:
    """按统一日期轴采集收盘与量能序列。

    收盘缺失 → 整点丢弃（保持原有行为）；量能缺失 → 前值填充；
    序列首端仍向量能缺失 → 返回空列表，交由上层把量能变量置 None。
    三者共用同一趟遍历，从根上杜绝「收盘 61 个点、量能 60 个点」的错位。
    """
    seg: list[float] = []
    vseg: list[float] = []
    aseg: list[float] = []
    for d in dates:
        c = cmap.get(d)
        if c is None:
            continue
        seg.append(float(c))
        v = vmap.get(d)
        vseg.append(float(v) if v is not None else (vseg[-1] if vseg else None))
        a = amtmap.get(d)
        aseg.append(float(a) if a is not None else (aseg[-1] if aseg else None))
    if vseg and vseg[0] is None:
        vseg = []
    if aseg and aseg[0] is None:
        aseg = []
    return seg, vseg, aseg


# ============ 挖掘主流程 ============
def mine_factor(db: Session, expr: str, name: str = "自定义因子",
                start: str = "", end: str = "",
                groups: int = DEFAULT_GROUPS, forward: int = DEFAULT_FORWARD,
                step: int = DEFAULT_STEP, universe: list[str] | None = None,
                progress=None) -> dict:
    """因子挖掘：逐期截面因子 vs 未来收益，输出有效性检验报告。"""
    ok, err, sample = validate_expr(expr)
    if not ok:
        return {"ok": False, "error": f"表达式无效：{err}"}

    sd, ed = resolve_range(start, end)
    if progress:
        progress(0.05, "加载股票池与数据…")

    syms = universe or _core_universe(db)
    if len(syms) < 50:
        return {"ok": False, "error": f"股票池过小（{len(syms)} 只），无法做截面检验"}

    attrs = _attrs_map(db, syms)
    es_map = _es_map(db, syms)
    # 财务（PIT 口径）：一次全量预载到内存（14 万行 ≈ 数 MB），
    # 之后按「公告日 ≤ 截面日」逐点选取，避免前视。
    fin_hist = fin_hist_map(db.execute(
        select(FinancialsRaw.symbol, FinancialsRaw.ann_date,
               FinancialsRaw.n_cashflow_act, FinancialsRaw.capex,
               FinancialsRaw.total_assets)
    ).all())

    # 个股新闻情绪 lookup（当日或近3日均值）
    from app.models import NewsStockDaily

    news_hist: dict[str, dict] = {}
    for ns_sym, ns_d, ns_v in db.execute(
        select(NewsStockDaily.symbol, NewsStockDaily.date, NewsStockDaily.net_sentiment)
    ).all():
        if ns_v is not None:
            news_hist.setdefault(ns_sym, {})[ns_d] = float(ns_v)

    def news_lookup(sym: str, asof: date) -> float | None:
        hist = news_hist.get(sym)
        if not hist:
            return None
        vals = [hist[d2] for d2 in hist if 0 <= (asof - d2).days <= 3]
        return _mean(vals) if vals else None

    # 截面日期：区间内每 step 个交易日取 1 个
    all_dates = _dates_in_range(db, sd, ed)
    snapshots = [d for i, d in enumerate(all_dates) if i % step == 0 and i + forward < len(all_dates)]
    if len(snapshots) < 3:
        return {"ok": False, "error": "区间内有效截面不足（建议拉长区间或缩短 forward）"}

    # —— 统一日期轴对齐（K线 + 基准）——
    from app.models import AdjFactorDaily, KlineDaily

    axis = _dates_in_range(db, sd - timedelta(days=300), ed)
    aligned: dict[str, dict[date, float]] = {}
    vol_aligned: dict[str, dict[date, float]] = {}
    amt_aligned: dict[str, dict[date, float]] = {}
    rows = db.execute(
        select(KlineDaily.symbol, KlineDaily.trade_date, KlineDaily.close,
               KlineDaily.volume, KlineDaily.amount, AdjFactorDaily.adj_factor)
        .outerjoin(AdjFactorDaily, and_(
            AdjFactorDaily.symbol == KlineDaily.symbol,
            AdjFactorDaily.trade_date == KlineDaily.trade_date))
        .where(KlineDaily.symbol.in_(syms), KlineDaily.adj == "none",
               KlineDaily.trade_date >= sd - timedelta(days=300), KlineDaily.trade_date <= ed)
        .order_by(KlineDaily.symbol, KlineDaily.trade_date)
    ).all()
    # 先按标的分组折算成后复权（因子必须基于连续价格序列），再回填对齐映射。
    # 量额保持原始值：量能因子取的是相对变化，常数比例折算不影响结果。
    _raw: dict[str, list[float]] = {}
    _fac: dict[str, list] = {}
    _meta: dict[str, list] = {}
    for sym, td, close, vol, amt, f in rows:
        _raw.setdefault(sym, []).append(float(close))
        _fac.setdefault(sym, []).append(f)
        _meta.setdefault(sym, []).append((td, vol, amt))
    for sym, seq in _raw.items():
        for (td, vol, amt), c in zip(_meta[sym],
                                     apply_adjust_series(seq, _fac[sym], "hfq")):
            aligned.setdefault(sym, {})[td] = float(c)
            if vol is not None:
                vol_aligned.setdefault(sym, {})[td] = float(vol)
            if amt is not None:
                amt_aligned.setdefault(sym, {})[td] = float(amt)
    bench_map = {d: c for d, c in zip(
        _dates_in_range(db, sd - timedelta(days=300), ed),
        _bench_closes(db, sd - timedelta(days=300), ed),
    )}
    if len(bench_map) < 60:
        return {"ok": False, "error": "基准数据不足"}
    axis_dates = _dates_in_range(db, sd - timedelta(days=300), ed)
    bench_aligned = [bench_map.get(d) for d in axis_dates]

    ic_list = []
    ic_dates = []
    all_vals: list[float] = []  # 全区间所有参与检验的因子值（供 factor_stats 用）
    group_rets = {g: [] for g in range(1, groups + 1)}
    ls_cum = 0.0
    long_short = []
    for si, snap in enumerate(snapshots):
        if progress:
            progress(0.1 + 0.75 * si / len(snapshots), f"截面 {si + 1}/{len(snapshots)} ({snap})…")
        if snap not in bench_map:
            continue
        idx = axis_dates.index(snap)
        if idx + forward >= len(axis_dates):
            continue
        fwd_date = axis_dates[idx + forward]
        fv: list[tuple[str, float, float]] = []  # (sym, factor, fwd_ret)
        for sym in syms:
            amap = aligned.get(sym)
            if not amap or snap not in amap or fwd_date not in amap:
                continue
            # 因子窗口：snap 往前 131 根（122 交易日缓冲，供 c_m/c_t 窗口切分）
            i0 = max(0, idx - 130)
            seg, vseg, aseg = _collect_seg(
                amap, vol_aligned.get(sym) or {}, amt_aligned.get(sym) or {},
                axis_dates[i0: idx + 1],
            )
            if len(seg) < 60:
                continue
            mkt_b = [c for c in bench_aligned[i0: idx + 1] if c is not None]
            if len(mkt_b) != len(seg):
                mkt_b = bench_aligned[i0: idx + 1][-len(seg):]
                if len(mkt_b) != len(seg):
                    continue
            a = attrs.get(sym, {}) or {}
            ns = make_ns(
                seg, mkt_b, a,
                news=news_lookup(sym, snap) if news_lookup else None,
                esv=es_map.get(sym), vols=vseg, amts=aseg,
                fin=fin_asof(fin_hist, sym, snap),
            )
            try:
                val = eval_factor(expr, ns)
            except Exception:  # noqa: BLE001
                val = None
            if val is None or not math.isfinite(val):
                continue
            ret = amap[fwd_date] / amap[snap] - 1.0
            fv.append((sym, val, ret))
        if len(fv) < 30:
            continue
        vals = [x[1] for x in fv]
        rets = [x[2] for x in fv]
        all_vals.extend(vals)
        ic = _spearman(vals, rets)
        ic_list.append(ic)
        ic_dates.append(snap.isoformat())
        # 分组
        ordered = sorted(fv, key=lambda x: x[1])
        n = len(ordered)
        g_size = max(1, n // groups)
        for g in range(1, groups + 1):
            seg_g = ordered[(g - 1) * g_size: g * g_size]
            if seg_g:
                group_rets[g].append(_mean([x[2] for x in seg_g]))
        # 多空：Top组 − Bottom组（当期）
        top = _mean([x[2] for x in ordered[-g_size:]])
        bottom = _mean([x[2] for x in ordered[:g_size]])
        ls_cum += top - bottom
        long_short.append((snap.isoformat(), round(ls_cum, 5)))

    if len(ic_list) < 3:
        return {"ok": False, "error": "有效截面不足（因子值缺失过多或区间太短）"}

    ic_mean = _mean(ic_list)
    ic_std = _std(ic_list)
    icir = ic_mean / ic_std if ic_std > 0 else 0.0
    t_stat = ic_mean / (ic_std / math.sqrt(len(ic_list))) if ic_std > 0 else 0.0
    ic_win = sum(1 for x in ic_list if x > 0) / len(ic_list)

    # 分组平均收益 + 单调性（首尾组差）
    g_means = {g: _mean(group_rets[g]) if group_rets[g] else 0.0 for g in range(1, groups + 1)}
    monotonic = (g_means[groups] - g_means[1]) if groups >= 2 else 0.0
    # 单调性评分：相邻组差同号占比
    diffs = [g_means[g + 1] - g_means[g] for g in range(1, groups)]
    mono_score = sum(1 for d in diffs if (d > 0) == (monotonic > 0)) / len(diffs) if diffs else 0.0

    # 与现有因子相关性（最新因子截面）
    corr_table = _corr_with_existing(
        db, expr, syms, attrs, es_map, aligned, axis_dates, bench_aligned,
        vol_aligned=vol_aligned, amt_aligned=amt_aligned,
        fin_hist=fin_hist, news_lookup=news_lookup,
    )

    # 有效性评级
    if abs(t_stat) >= 2 and abs(ic_mean) > 0.02 and mono_score >= 0.6 and abs(corr_table.get("max_abs_corr", 0)) < 0.8:
        rating = "优秀"
    elif abs(t_stat) >= 1.5 and abs(ic_mean) > 0.01:
        rating = "可用"
    elif abs(ic_mean) > 0.005:
        rating = "弱"
    else:
        rating = "无效"

    return {
        "ok": True,
        "name": name,
        "expr": expr,
        "ic_mean": round(ic_mean, 5),
        "icir": round(icir, 4),
        "t_stat": round(t_stat, 3),
        "ic_win_rate": round(ic_win, 3),
        "ic_series": [{"date": d, "ic": round(v, 4)} for d, v in zip(ic_dates, ic_list)],
        "groups": groups,
        "group_means": {str(g): round(v, 5) for g, v in g_means.items()},
        "monotonic_spread": round(monotonic, 5),
        "mono_score": round(mono_score, 3),
        "long_short": long_short,
        "corr_with_existing": corr_table["corr"],
        "max_abs_corr": corr_table["max_abs_corr"],
        "rating": rating,
        "n_periods": len(ic_list),
        "complexity": compute_complexity(expr),
        "factor_stats": _factor_stats(all_vals, len(ic_list)),
        "n_stocks": len(syms),
        "forward_days": forward,
    }


def _corr_with_existing(db, expr, syms, attrs, es_map, aligned, axis_dates, bench_aligned,
                        vol_aligned=None, amt_aligned=None, fin_hist=None,
                        news_lookup=None) -> dict:
    """最新截面：新因子与 factor_daily 现有 14 因子 Spearman 相关。"""
    latest = db.execute(
        select(FactorDaily.trade_date).order_by(FactorDaily.trade_date.desc()).limit(1)
    ).scalar()
    if not latest:
        return {"corr": {}, "max_abs_corr": 0.0}
    cols = [FactorDaily.symbol] + [getattr(FactorDaily, c) for c in FACTOR_COLUMNS]
    rows = db.execute(
        select(*cols).where(FactorDaily.trade_date == latest, FactorDaily.symbol.in_(syms))
    ).all()
    existing: dict[str, dict[str, float | None]] = {}
    for r in rows:
        existing[r[0]] = {c: r[i + 1] for i, c in enumerate(FACTOR_COLUMNS)}
    # 新因子值（最新截面）
    if latest not in set(axis_dates):
        idx = len(axis_dates) - 1
        snap = axis_dates[idx]
    else:
        idx = axis_dates.index(latest)
        snap = latest
    new_vals: dict[str, float] = {}
    vol_aligned = vol_aligned or {}
    amt_aligned = amt_aligned or {}
    fin_hist = fin_hist or {}
    for sym in syms:
        amap = aligned.get(sym)
        if not amap or snap not in amap:
            continue
        i0 = max(0, idx - 130)
        seg, vseg, aseg = _collect_seg(
            amap, vol_aligned.get(sym) or {}, amt_aligned.get(sym) or {},
            axis_dates[i0: idx + 1],
        )
        if len(seg) < 60:
            continue
        mkt_b = bench_aligned[i0: idx + 1][-len(seg):]
        if len(mkt_b) != len(seg):
            continue
        ns = make_ns(
            seg, bench_aligned[i0: idx + 1], attrs.get(sym, {}) or {},
            news=news_lookup(sym, snap) if news_lookup else None,
            esv=es_map.get(sym), vols=vseg, amts=aseg,
            fin=fin_asof(fin_hist, sym, snap),
        )
        try:
            v = eval_factor(expr, ns)
        except Exception:  # noqa: BLE001
            v = None
        if v is not None and math.isfinite(v):
            new_vals[sym] = v
    corr: dict[str, float] = {}
    for col in FACTOR_COLUMNS:
        pairs = [(new_vals[s], existing[s][col]) for s in new_vals
                 if s in existing and existing[s][col] is not None]
        if len(pairs) >= 20:
            corr[col] = round(_spearman([p[0] for p in pairs], [p[1] for p in pairs]), 4)
    max_abs = max((abs(v) for v in corr.values()), default=0.0)
    return {"corr": corr, "max_abs_corr": round(max_abs, 3)}

