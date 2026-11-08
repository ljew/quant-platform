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

from app.core.eval_profile import build_eval_profile_for_mining

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

# 截面抽样间隔（交易日）
# B2（2026-10-08）：由 20（月频）→ 5（周频）。原因见下——
#   原 step=20 时截面数 = 区间交易日数 / 20，实测 3 年仅 36 个、7 年 85 个。
#   t 检验的自由度 = 截面数 - 1，85 个截面的约束极松，导致
#   「IC=0.038、t=1.55」这类噪声也能被判「可用」（M1-2 审计发现 37 个因子
#   里 26 个负 IC、7 个负 IC 被误判，正是评级误判 + 检验功效不足叠加的结果）。
#   step=5 → 3 年 145 个 / 7 年 340 个截面，统计功效提升约 4 倍。
DEFAULT_STEP = 5
# 前瞻期保持 20 个交易日（约一个月）—— **不随 step 改变**。
# 这样「因子值 → 未来一个月收益」的经济含义不变，只是采样更密；
# 若同步改成 forward=5，因子含义就从「月频预测」变成「周频预测」，
# 历史 IC 不可与旧结果直接比较。
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


def _nw_lags(forward: int = DEFAULT_FORWARD, step: int = DEFAULT_STEP) -> int:
    """重叠前瞻收益所需的 Newey–West 滞后阶数。

    例如 20 日收益每 5 日采样，相邻 3 个残差仍共享持有窗口；不能把这些
    截面当成独立样本计算普通 t 值。
    """
    if forward <= 0 or step <= 0:
        raise ValueError("forward 和 step 必须为正数")
    return max(0, math.ceil(forward / step) - 1)


def _newey_west_t(values: list[float], lags: int) -> float | None:
    """均值的 Newey–West HAC t 值；方差非正或样本不足时返回 None。"""
    n = len(values)
    if n < 3:
        return None
    m = _mean(values)
    resid = [x - m for x in values]
    long_run_var = sum(x * x for x in resid) / n
    for lag in range(1, min(max(lags, 0), n - 2) + 1):
        cov = sum(resid[i] * resid[i - lag] for i in range(lag, n)) / n
        weight = 1.0 - lag / (lags + 1.0)
        long_run_var += 2.0 * weight * cov
    if long_run_var <= 1e-18:
        return None
    return m / math.sqrt(long_run_var / n)


def _oos_metrics(ic_seq: list[float], nw_lags: int = 0) -> dict:
    """IC 描述统计与 HAC t 值；同时保留普通 t 值用于审计重叠修正幅度。"""
    n = len(ic_seq)
    if n < 3:
        return {"n": n, "ic_mean": None, "ic_std": None, "icir": None,
                "t_stat": None, "t_stat_naive": None, "nw_lags": nw_lags,
                "win_rate": None}
    m = _mean(ic_seq)
    s = _std(ic_seq)
    degenerate = s < 1e-9
    naive_t = None if degenerate else m / (s / math.sqrt(n))
    hac_t = None if degenerate else _newey_west_t(ic_seq, nw_lags)
    return {
        "n": n,
        "ic_mean": round(m, 5),
        "ic_std": round(s, 5),
        "icir": None if degenerate else round(m / s, 4),
        "t_stat": None if hac_t is None else round(hac_t, 3),
        "t_stat_naive": None if naive_t is None else round(naive_t, 3),
        "nw_lags": nw_lags,
        "win_rate": round(sum(1 for x in ic_seq if x > 0) / n, 4),
    }


def _is_oos_split(ic_dates: list[str], ic_seq: list[float],
                  oos_ratio: float = 0.3, forward: int = DEFAULT_FORWARD,
                  step: int = DEFAULT_STEP, embargo_snapshots: int = 1) -> dict:
    """按时间切出最终 OOS，并清除切点两侧的标签重叠。

    IS 尾部 purge 一个完整前瞻窗口；OOS 首部再做 embargo。两侧均使用
    Newey–West t 值，避免把重叠的 20 日收益当作独立观测。
    """
    if len(ic_dates) != len(ic_seq):
        raise ValueError("IC 日期与值序列长度不一致")
    n = len(ic_seq)
    nw = _nw_lags(forward, step)
    purge = nw + 1
    if n < 6:
        return {"enabled": True, "verdict": "样本不足",
                "note": f"总截面 {n} < 6，无法切分",
                "is": _oos_metrics(ic_seq, nw), "oos": _oos_metrics([], nw),
                "oos_ratio": oos_ratio, "purged_is": 0,
                "embargoed_oos": 0, "nw_lags": nw}

    k = min(max(3, int(round(n * oos_ratio))), n - 3)
    boundary = n - k
    # 不为凑样本而保留与 OOS 重叠的 IS 尾部；不足时由下方统一判样本不足。
    purge_n = min(purge, max(0, boundary - 3))
    embargo_n = min(max(0, embargo_snapshots), max(0, n - boundary - 3))
    is_end = boundary - purge_n
    oos_begin = boundary + embargo_n
    is_seq, oos_seq = ic_seq[:is_end], ic_seq[oos_begin:]
    is_dates, oos_dates = ic_dates[:is_end], ic_dates[oos_begin:]
    is_m, oos_m = _oos_metrics(is_seq, nw), _oos_metrics(oos_seq, nw)

    if oos_m["ic_mean"] is None or is_m["ic_mean"] is None:
        verdict, note = "样本不足", "purge/embargo 后 IS 或 OOS 截面 < 3"
    elif is_m["ic_mean"] * oos_m["ic_mean"] < 0:
        verdict = "过拟合"
        note = (f"IS ic={is_m['ic_mean']:+.4f} 与 OOS ic={oos_m['ic_mean']:+.4f} "
                f"**符号相反** —— 样本内有效、样本外失效")
    elif oos_m["t_stat"] is None:
        verdict = "样本不足"
        note = "OOS 段 IC 零方差或 HAC 方差非正，t 检验不可用"
    elif abs(oos_m["t_stat"]) < 2.0:
        verdict = "未通过"
        note = (f"OOS Newey–West |t|={abs(oos_m['t_stat']):.2f} < 2.0，"
                f"样本外区分力不足（IS |t|={abs(is_m['t_stat'] or 0):.2f}）")
    else:
        verdict = "通过"
        _it = "n/a" if is_m["t_stat"] is None else f"{is_m['t_stat']:+.2f}"
        _ot = "n/a" if oos_m["t_stat"] is None else f"{oos_m['t_stat']:+.2f}"
        note = (f"IS ic={is_m['ic_mean']:+.4f} (NW t={_it}) → "
                f"OOS ic={oos_m['ic_mean']:+.4f} (NW t={_ot})，同向且显著")

    return {
        "enabled": True, "verdict": verdict, "note": note,
        "is": is_m, "oos": oos_m,
        "split_date": is_dates[-1] if is_dates else None,
        "boundary_date": ic_dates[boundary - 1] if boundary else None,
        "oos_start": oos_dates[0] if oos_dates else None,
        "oos_ratio": oos_ratio, "purged_is": purge_n,
        "embargoed_oos": embargo_n, "nw_lags": nw,
    }


def _walk_forward_report(ic_dates: list[str], ic_seq: list[float],
                         forward: int = DEFAULT_FORWARD,
                         step: int = DEFAULT_STEP, n_folds: int = 3) -> dict:
    """仅在 IS 内做扩展窗口验证，最终 OOS 不参与调参或折叠判定。"""
    nw = _nw_lags(forward, step)
    purge = nw + 1
    n = len(ic_seq)
    initial = max(12, int(n * 0.5))
    test_size = (n - initial) // n_folds if n_folds > 0 else 0
    if n < 24 or test_size < 4:
        return {"enabled": True, "verdict": "样本不足", "folds": [],
                "n_folds": 0, "note": "IS 至少需要 24 个截面及每折 4 个验证截面",
                "nw_lags": nw}

    folds, stitched = [], []
    for fold in range(n_folds):
        raw_start = initial + fold * test_size
        raw_end = raw_start + test_size if fold + 1 < n_folds else n
        train = ic_seq[:max(0, raw_start - purge)]
        test = ic_seq[min(raw_start + 1, raw_end):raw_end]
        if len(train) < 3 or len(test) < 3:
            continue
        tm = _oos_metrics(train, nw)
        vm = _oos_metrics(test, nw)
        stitched.extend(test)
        folds.append({"fold": fold + 1, "train_n": len(train),
                      "test_start": ic_dates[raw_start + 1],
                      "test_end": ic_dates[raw_end - 1],
                      "test": vm, "train_ic": tm["ic_mean"]})
    overall = _oos_metrics(stitched, nw)
    positive = sum(1 for f in folds if (f["test"]["ic_mean"] or 0) > 0)
    enough = len(folds) == n_folds and overall["t_stat"] is not None
    passed = (enough and positive >= math.ceil(n_folds * 2 / 3)
              and (overall["ic_mean"] or 0) > 0 and abs(overall["t_stat"]) >= 1.0)
    return {"enabled": True, "verdict": "通过" if passed else "未通过",
            "folds": folds, "n_folds": len(folds), "positive_folds": positive,
            "overall": overall, "purged_train_per_fold": purge,
            "embargoed_test_per_fold": 1, "nw_lags": nw,
            "note": f"{positive}/{len(folds)} 折 IC 为正，拼接验证 NW t={overall['t_stat']}"}


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


def _core_universe_pit(db: Session, sd: date, ed: date) -> tuple[list[str], dict]:
    """加载研究区间内的历史成分；返回数据加载超集与逐指数快照。

    不在历史研究中回退到“当前成分并集”。缺少历史快照必须显式失败，
    否则会把幸存者偏差包装成样本外 alpha。
    """
    from app.services.etl import CORE_INDEXES
    from app.services.membership_store import get_membership

    timeline: dict[str, list[tuple[date, set[str]]]] = {}
    union: set[str] = set()
    for code in CORE_INDEXES:
        snaps = get_membership(db, code, sd - timedelta(days=45), ed)
        parsed = [(date.fromisoformat(d), set(members)) for d, members in snaps]
        if parsed:
            timeline[code] = parsed
            union |= set().union(*(members for _, members in parsed))
    return sorted(union), timeline


def _pit_symbols(timeline: dict, snap: date) -> set[str]:
    """各核心指数取 snap 当日可见的最近快照，再求成分并集。"""
    members: set[str] = set()
    for snaps in timeline.values():
        visible = [item for item in snaps if item[0] <= snap]
        if visible:
            members |= visible[-1][1]
    return members


def _lifecycle_map(db: Session, syms: list[str]) -> dict[str, tuple[date | None, date | None]]:
    rows = db.execute(
        select(Stock.symbol, Stock.list_date, Stock.delist_date)
        .where(Stock.symbol.in_(syms))
    ).all()
    return {symbol: (list_date, delist_date) for symbol, list_date, delist_date in rows}


def _eligible_symbol(lifecycle: dict, sym: str, snap: date) -> bool:
    listed, delisted = lifecycle.get(sym, (None, None))
    return not ((listed and listed > snap) or (delisted and delisted <= snap))


def _attrs_map(db: Session, syms: list[str]) -> dict:
    """静态属性底稿（行业 + stocks 最新估值/成长快照）。

    估值与市值：截面上必须经 `_attrs_asof`（StockValuationDaily）做 PIT 时序覆盖。
    成长三字段 roe/revenue_yoy/profit_yoy：截面上必须经 `_fund_asof`
    （FundamentalsHistory，W45-4）覆盖——禁止把本函数返回的最新快照
    直接复用到历史截面（前视）。industry 仍可取最新。
    """
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


def _attrs_timeline(db: Session, syms: list[str], lo: date, hi: date) -> dict[str, list]:
    """估值时序 PIT（B1，2026-10-08）：{symbol: [(trade_date, {pe_ttm,pb,total_mv,circ_mv}), ...]}，
    按日期升序。`_attrs_asof` 从此按截面日二分取「≤ 截面日 的最近一条」。

    **为什么必须这样**：`stocks` 表的 pe_ttm/pb/market_cap 是最新一列快照，
    旧实现复用于所有历史截面 → 2019 年截面用 2026 年的 PE，属明确前视。
    实测茅台 2019-01-10 pe_ttm=24.42/pb=7.59，与今日值完全不同。

    若 stock_valuation_daily 无数据（未回填），返回空 dict，调用方自动退回
    `_attrs_map` 的最新值口径并标注「估值口径=最新快照（有前视）」。
    """
    from app.models import StockValuationDaily

    try:
        rows = db.execute(
            select(StockValuationDaily.symbol, StockValuationDaily.trade_date,
                   StockValuationDaily.pe_ttm, StockValuationDaily.pb,
                   StockValuationDaily.total_mv, StockValuationDaily.circ_mv)
            .where(StockValuationDaily.symbol.in_(syms),
                   StockValuationDaily.trade_date >= lo,
                   StockValuationDaily.trade_date <= hi)
            .order_by(StockValuationDaily.symbol, StockValuationDaily.trade_date)
        ).all()
    except Exception:                      # 表不存在（未迁移）
        return {}
    out: dict[str, list] = {}
    for sym, d, pe, pb, tmv, cmv in rows:
        out.setdefault(sym, []).append(
            (d, {"pe_ttm": pe, "pb": pb, "market_cap": tmv, "circ_mv": cmv})
        )
    return out


def _attrs_asof(tl: dict[str, list], sym: str, snap: date) -> dict | None:
    """取该股票「截至 snap 日」的最近一条估值（PIT 正确口径）。"""
    seq = tl.get(sym)
    if not seq:
        return None
    # seq 按 trade_date 升序（SQL ORDER BY 保证）
    lo, hi = 0, len(seq) - 1
    ans = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if seq[mid][0] <= snap:
            ans = seq[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def _es_map(db: Session, syms: list[str]) -> dict[str, list[tuple[date, float]]]:
    """预载 PEAD 所需的利润增速时序（按报告期升序）。

    返回 {symbol: [(report_date, profit_yoy), ...]}。真正计算必须经 `_es_asof`
    按截面日做 PIT 过滤——禁止把「全样本最新一期」复用到历史截面（前视）。
    口径对齐 multi_factor._pit_earnings_surprise：仅用 report_date <= as_of。
    """
    if not syms:
        return {}
    rows = db.execute(
        select(FundamentalsHistory.symbol, FundamentalsHistory.report_date, FundamentalsHistory.profit_yoy)
        .where(FundamentalsHistory.symbol.in_(syms))
        .order_by(FundamentalsHistory.symbol, FundamentalsHistory.report_date)
    ).all()
    out: dict[str, list[tuple[date, float]]] = {}
    for sym, rd, py in rows:
        if py is None or rd is None:
            continue
        out.setdefault(sym, []).append((rd, float(py)))
    return out


def _es_asof(tl: dict[str, list[tuple[date, float]]], sym: str, as_of: date) -> float | None:
    """Point-in-time PEAD：as_of 及以前 ≥2 期 → 最新期 yoy − 历史均值。

    量纲对齐 multi_factor._pit_earnings_surprise：**原始百分点差，不除以 100**。
    内部复用该函数，单测锁死挖矿与回测同输入同结果。
    """
    from app.core.strategies.multi_factor import _pit_earnings_surprise

    pts = tl.get(sym) or []
    if not pts:
        return None
    snaps = [{"report_date": rd, "profit_yoy": py} for rd, py in pts]
    as_of_s = as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of)
    return _pit_earnings_surprise(snaps, as_of_s)


# 设计稿别名：_es_timeline ≡ _es_map（返回时序，禁止当静态值用）
_es_timeline = _es_map

_GROWTH_KEYS = ("roe", "revenue_yoy", "profit_yoy")


def _fund_timeline(db: Session, syms: list[str]) -> dict[str, list[tuple[date, dict]]]:
    """预载成长三因子时序（FundamentalsHistory，按 report_date 升序）。

    返回 {symbol: [(report_date, {roe, revenue_yoy, profit_yoy}), ...]}。
    真正取值必须经 `_fund_asof` 按截面日过滤——禁止把 stocks 最新快照
    复用到历史截面（W45-4 / P1-1）。
    """
    if not syms:
        return {}
    rows = db.execute(
        select(
            FundamentalsHistory.symbol,
            FundamentalsHistory.report_date,
            FundamentalsHistory.roe,
            FundamentalsHistory.revenue_yoy,
            FundamentalsHistory.profit_yoy,
        )
        .where(FundamentalsHistory.symbol.in_(syms))
        .order_by(FundamentalsHistory.symbol, FundamentalsHistory.report_date)
    ).all()
    out: dict[str, list[tuple[date, dict]]] = {}
    for sym, rd, roe, rev, prof in rows:
        if rd is None:
            continue
        fields = {}
        if roe is not None:
            fields["roe"] = float(roe)
        if rev is not None:
            fields["revenue_yoy"] = float(rev)
        if prof is not None:
            fields["profit_yoy"] = float(prof)
        if not fields:
            continue
        out.setdefault(sym, []).append((rd, fields))
    return out


def _fund_asof(tl: dict[str, list[tuple[date, dict]]], sym: str, as_of: date) -> dict | None:
    """截面 as-of：取 report_date ≤ as_of 最近一期的 roe/yoy。

    对齐 multi_factor._pit_fund_attrs；无可见报告则 None（调用方应清空
    stocks 最新值，禁止前视回退）。
    """
    from app.core.strategies.multi_factor import _pit_fund_attrs

    pts = tl.get(sym) or []
    if not pts:
        return None
    snaps = [{"report_date": rd, **fields} for rd, fields in pts]
    as_of_s = as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of)
    out = _pit_fund_attrs(snaps, as_of_s)
    return out or None


def _apply_fund_asof(attrs: dict, fund_tl: dict, sym: str, snap: date) -> dict:
    """覆盖 attrs 成长三字段为 PIT；无历史则置 None（禁止 Stock 最新前视）。"""
    for k in _GROWTH_KEYS:
        attrs[k] = None
    fv = _fund_asof(fund_tl, sym, snap)
    if fv:
        attrs.update(fv)
    return attrs


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
                n_tested: int = 1,
                oos_ratio: float = 0.3,
                progress=None) -> dict:
    """因子挖掘：逐期截面因子 vs 未来收益，输出有效性检验报告。

    n_tested: 本次批量挖掘累计测试的候选数（多重检验校正，默认 1=单次手动检验）。
    oos_ratio: 样本外占比（M3-1，默认 0.3；0=关闭切分）。
    """
    ok, err, sample = validate_expr(expr)
    if not ok:
        return {"ok": False, "error": f"表达式无效：{err}"}

    sd, ed = resolve_range(start, end)
    if progress:
        progress(0.05, "加载股票池与数据…")

    if universe is not None:
        syms = sorted(set(universe))
        pit_timeline = {}
        universe_mode = "custom_lifecycle"
    else:
        syms, pit_timeline = _core_universe_pit(db, sd, ed)
        universe_mode = "pit_index_membership"
        if not pit_timeline:
            return {"ok": False, "error": "缺少历史指数成分快照，拒绝用当前成分回测历史（幸存者偏差）"}
    if len(syms) < 50:
        return {"ok": False, "error": f"历史股票池并集过小（{len(syms)} 只），无法做截面检验"}
    lifecycle = _lifecycle_map(db, syms)

    attrs = _attrs_map(db, syms)
    # 估值 PIT 时序（B1）：按「截至截面日」取值，替换旧的最新快照口径
    try:
        _sd, _ed = resolve_range(start, end)
        attrs_tl = _attrs_timeline(db, syms, _sd, _ed)
    except Exception:
        attrs_tl = {}
    if attrs_tl:
        _cover = len(attrs_tl) / max(1, len(syms))
        _val_mode = "asof"
        _val_note = None
        if progress:
            progress(0.06, f"估值 PIT 时序已载入：{len(attrs_tl)} 只（覆盖 {_cover:.0%}）")
    else:
        # W45-5：无估值日频时退回 stocks 最新快照 —— 显式标 fallback，禁止静默
        _cover = 0.0
        _val_mode = "fallback"
        _val_note = "stock_valuation_daily 无数据，估值因子退回 stocks 最新快照（有前视）"
        if progress:
            progress(0.06, "⚠️ 估值口径=最新快照（有前视）；请回填 stock_valuation_daily")
    es_map = _es_map(db, syms)
    # W45-4：成长三因子 FundamentalsHistory 时序（report_date as-of）
    fund_tl = _fund_timeline(db, syms)
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
    universe_sizes: list[int] = []
    for si, snap in enumerate(snapshots):
        if progress:
            progress(0.1 + 0.75 * si / len(snapshots), f"截面 {si + 1}/{len(snapshots)} ({snap})…")
        if snap not in bench_map:
            continue
        idx = axis_dates.index(snap)
        if idx + forward >= len(axis_dates):
            continue
        fwd_date = axis_dates[idx + forward]
        active = _pit_symbols(pit_timeline, snap) if pit_timeline else set(syms)
        active = {s for s in active if _eligible_symbol(lifecycle, s, snap)}
        universe_sizes.append(len(active))
        fv: list[tuple[str, float, float]] = []  # (sym, factor, fwd_ret)
        for sym in syms:
            if sym not in active:
                continue
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
            a = dict(attrs.get(sym, {}) or {})
            # 估值/市值按 PIT 覆盖（截面日 as-of）；无时序数据时保留最新快照口径
            if attrs_tl:
                _pv = _attrs_asof(attrs_tl, sym, snap)
                if _pv:
                    a.update(_pv)
            # W45-4：成长三因子截面 as-of（无历史则清空，禁止 Stock 最新前视）
            _apply_fund_asof(a, fund_tl, sym, snap)
            ns = make_ns(
                seg, mkt_b, a,
                news=news_lookup(sym, snap) if news_lookup else None,
                esv=_es_asof(es_map, sym, snap), vols=vseg, amts=aseg,
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

    nw = _nw_lags(forward, step)
    full_metrics = _oos_metrics(ic_list, nw)
    ic_mean = _mean(ic_list)
    ic_std = _std(ic_list)
    icir = ic_mean / ic_std if ic_std > 0 else 0.0
    t_stat = full_metrics["t_stat"] or 0.0
    ic_win = sum(1 for x in ic_list if x > 0) / len(ic_list)

    # 最终 OOS 只做一次评估；walk-forward 严格限制在 purge 后的 IS 内。
    oos_report = (_is_oos_split(ic_dates, ic_list, oos_ratio=oos_ratio,
                                forward=forward, step=step)
                  if oos_ratio > 0 else
                  {"enabled": False, "verdict": "未启用",
                   "note": "oos_ratio=0，已关闭样本外切分"})
    is_n = (oos_report.get("is") or {}).get("n", 0) if oos_ratio > 0 else 0
    walk_forward = (_walk_forward_report(ic_dates[:is_n], ic_list[:is_n],
                                         forward=forward, step=step)
                    if is_n else {"enabled": False, "verdict": "未启用", "folds": []})

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
        fin_hist=fin_hist, news_lookup=news_lookup, fund_tl=fund_tl,
        pit_timeline=pit_timeline, lifecycle=lifecycle,
    )

    # 有效性评级（含多重检验校正：批量挖掘时抬高 t 门槛）
    # 思想来源：《量化交易》2.8.5 + Bailey-López de Prado deflated Sharpe——
    # N 个独立零假设检验的期望最大 |t| ≈ √(2·ln N)。挖 56 个候选时期望最大值
    # 已达 2.84，即 t≥2 的「优秀」可能纯属运气。因此批量挖掘的评级门槛
    # 随 N 上浮：宁可错过，不可错信（风控优先，2026-10-08）。
    luck_bar = math.sqrt(2 * math.log(max(n_tested, 2)))
    t_excellent = max(2.0, luck_bar + 0.3)
    t_usable = max(1.5, luck_bar - 0.3)
    # ⚠️ 方向不可用 abs() 抹掉（M1-2，2026-10-08）：旧实现对 t_stat 与 ic_mean
    # 都取绝对值，导致 ic=-0.06/t=1.8 这类反向因子被判「可用」。实测 37 个已挖
    # 因子中 26 个负 IC，其中 7 个负 IC 被标为可用/优秀；factor_registry 的
    # enabled 记录即由该函数选出，等于把反向信号当正向建仓。
    # 修复：只有 ic_mean > 0 才可能正向评级；ic_mean < 0 但显著者单列
    # 「反向」评级（可研究取负号使用，但禁止自动进生产）。
    has_pos = ic_mean > 0
    if (has_pos and t_stat >= t_excellent and ic_mean > 0.02 and mono_score >= 0.6
            and abs(corr_table.get("max_abs_corr", 0)) < 0.8):
        rating = "优秀"
    elif has_pos and t_stat >= t_usable and ic_mean > 0.01:
        rating = "可用"
    elif (not has_pos) and abs(t_stat) >= t_usable and abs(ic_mean) > 0.01:
        # 反向因子：统计上显著但方向为负。取负号即成正因子，但需人工确认
        # 经济含义后方可使用（不自动进生产）。
        rating = "反向"
    elif abs(ic_mean) > 0.005:
        rating = "弱"
    else:
        rating = "无效"

    # 生产评级硬门禁：最终 OOS 与 IS 内 walk-forward 任一未通过，都不得保留
    # “优秀/可用/反向”。不再把 OOS 未通过降半档后仍标成“可用”。
    oos_verdict = (oos_report or {}).get("verdict")
    wf_verdict = (walk_forward or {}).get("verdict")
    rating_before_oos = rating
    if rating in ("优秀", "可用", "反向") and oos_verdict == "过拟合":
        rating = "弱"
    elif (rating in ("优秀", "可用", "反向") and oos_verdict != "未启用"
          and (oos_verdict != "通过" or wf_verdict != "通过")):
        rating = "弱"
    if rating != rating_before_oos:
        oos_report = {
            **oos_report,
            "demoted_from": rating_before_oos,
            "demote_note": (f"样本外硬门禁未通过（OOS={oos_verdict}，"
                            f"walk-forward={wf_verdict}），评级 {rating_before_oos} → {rating}"),
        }

    _eval = build_eval_profile_for_mining(
        valuation_mode=_val_mode,
        valuation_coverage=_cover,
        valuation_note=_val_note,
    )
    return {
        "ok": True,
        "name": name,
        "expr": expr,
        "ic_mean": round(ic_mean, 5),
        "icir": round(icir, 4),
        "t_stat": round(t_stat, 3),
        "t_stat_naive": full_metrics["t_stat_naive"],
        "nw_lags": nw,
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
        "n_tested": n_tested,
        "luck_bar": round(luck_bar, 3),
        "t_thresholds": {"优秀": round(t_excellent, 2), "可用": round(t_usable, 2)},
        "n_periods": len(ic_list),
        "oos": oos_report,
        "walk_forward": walk_forward,
        "universe_pit": {
            "mode": universe_mode, "indexes_loaded": len(pit_timeline),
            "lifecycle_filter": True, "historical_union_size": len(syms),
            "mean_size": round(_mean(universe_sizes), 1) if universe_sizes else 0,
            "min_size": min(universe_sizes) if universe_sizes else 0,
            "max_size": max(universe_sizes) if universe_sizes else 0,
        },
        "complexity": compute_complexity(expr),
        "factor_stats": _factor_stats(all_vals, len(ic_list)),
        "n_stocks": len(syms),
        "forward_days": forward,
        **_eval,
    }


def _corr_with_existing(db, expr, syms, attrs, es_map, aligned, axis_dates, bench_aligned,
                        vol_aligned=None, amt_aligned=None, fin_hist=None,
                        news_lookup=None, fund_tl=None, pit_timeline=None,
                        lifecycle=None) -> dict:
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
    active = _pit_symbols(pit_timeline, snap) if pit_timeline else set(syms)
    lifecycle = lifecycle or {}
    for sym in syms:
        if sym not in active or not _eligible_symbol(lifecycle, sym, snap):
            continue
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
        a = dict(attrs.get(sym, {}) or {})
        if fund_tl is not None:
            _apply_fund_asof(a, fund_tl, sym, snap)
        ns = make_ns(
            seg, bench_aligned[i0: idx + 1], a,
            news=news_lookup(sym, snap) if news_lookup else None,
            esv=_es_asof(es_map, sym, snap), vols=vseg, amts=aseg,
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

# ───────── 非结构化（新闻情绪）择时检验 ─────────
# 2026-09-18 随「非结构化链路整体下线」一并删除；2026-11-07 恢复。
# 恢复依据：市场级情绪数据已连续 838 天、日均 66 篇，足以做事件检验
# （当初下线的原因是**个股**情绪稀疏，与市场级无关）。
# ⚠️ 只恢复市场级；`news_senti` 个股截面仍保持 TEXT_FACTOR_ENABLED=False —— 
# 近 60 天每天仅 58 只 / 核心池 1808 只 ≈ 3.2%，进截面 IC 依然是噪声。
def _sentiment_rows(db: Session, source: str, min_articles: int) -> list[tuple]:
    """按来源取 (date, net_sentiment, n_finance)。

    source：
      - news（默认）：东财新闻/公告聚合，`news_market_daily`
      - mp          ：公众号语料聚合，`mp_sentiment_daily`
      - both        ：两表按日合并，多空分相加后重新归一
    """
    from app.models import MpSentimentDaily, NewsMarketDaily

    if source == "mp":
        rows = db.execute(
            select(MpSentimentDaily.date, MpSentimentDaily.net_sentiment,
                   MpSentimentDaily.n_finance)
            .where(MpSentimentDaily.n_finance >= min_articles, MpSentimentDaily.n_finance > 0)
            .order_by(MpSentimentDaily.date)
        ).all()
        return [(r[0], float(r[1]), int(r[2])) for r in rows if r[1] is not None]

    if source == "both":
        agg: dict = {}
        for model in (NewsMarketDaily, MpSentimentDaily):
            for d, bull, bear in db.execute(
                select(model.date, model.bull_score, model.bear_score)
            ).all():
                rec = agg.setdefault(d, [0, 0])
                rec[0] += int(bull or 0)
                rec[1] += int(bear or 0)
        out = []
        for d in sorted(agg):
            bull, bear = agg[d]
            total = bull + bear
            if total < min_articles:      # 用「多空词总命中数」当活跃度门槛
                continue
            out.append((d, (bull - bear) / total, total))
        return out

    rows = db.execute(
        select(NewsMarketDaily.date, NewsMarketDaily.net_sentiment, NewsMarketDaily.n_finance)
        .where(NewsMarketDaily.n_finance >= min_articles)
        .order_by(NewsMarketDaily.date)
    ).all()
    return [(r[0], float(r[1]), int(r[2])) for r in rows if r[1] is not None]


def news_event_test(db: Session, extreme_pct: float = 0.10, horizon: int = 5,
                    min_articles: int = 5, source: str = "news") -> dict:
    """极端情绪日的未来指数收益检验。

    看多日 = net_sentiment ≥ 分位数(1-extreme_pct)；看空日 ≤ extreme_pct。
    收益基准 = 中证800(sh000906) 之后 horizon 个交易日收盘收益。
    source 见 `_sentiment_rows`：news / mp / both。
    """
    from app.models import IndexKlineDaily

    if source not in ("news", "mp", "both"):
        source = "news"
    rows = _sentiment_rows(db, source, min_articles)
    if len(rows) < 50:
        return {"ok": False, "error": f"情绪样本不足（{len(rows)} 天 < 50，来源={source}）"}

    bench = db.execute(
        select(IndexKlineDaily.trade_date, IndexKlineDaily.close)
        .where(IndexKlineDaily.symbol == "sh000906", IndexKlineDaily.trade_date >= rows[0][0])
        .order_by(IndexKlineDaily.trade_date)
    ).all()
    dates = [r[0] for r in bench]
    closes = [float(r[1]) for r in bench]
    pos_of = {d: i for i, d in enumerate(dates)}

    def fwd_return(d: date) -> float | None:
        i = pos_of.get(d)
        if i is None or i + horizon >= len(closes):
            return None
        return closes[i + horizon] / closes[i] - 1.0

    sentis = [r[1] for r in rows]
    s_sorted = sorted(sentis)
    hi_th = s_sorted[int(len(s_sorted) * (1 - extreme_pct))]
    lo_th = s_sorted[int(len(s_sorted) * extreme_pct)]

    bull_rets, bear_rets, all_rets = [], [], []
    for d, senti, _nf in rows:
        fr = fwd_return(d)
        if fr is None:
            continue
        all_rets.append(fr)
        if senti >= hi_th:
            bull_rets.append(fr)
        elif senti <= lo_th:
            bear_rets.append(fr)

    base = _mean(all_rets) if all_rets else 0.0
    win = lambda xs: (sum(1 for x in xs if x > 0) / len(xs)) if xs else 0.0
    edge_l = (_mean(bull_rets) - base) if bull_rets else None
    edge_s = (base - _mean(bear_rets)) if bear_rets else None

    # 结论必须按实测给，**不能预设方向**。2026-11-07 首次恢复实测：
    # 看多日未来 5 日 -1.07%（胜率 37%）、基线 -0.47% → edge=-0.60%，是**反向**的。
    # 旧代码把「看多日跑赢基线」写死在 note 里，等于用文案骗自己。
    # 数据覆盖警告：2026-11-07 实测 news_market_daily 有 838 天记录，但
    # n_finance≥5 的只有 254 天，且 **245 天集中在 2026 年**（2020-2025 基本空）。
    # 一年样本算出来的 edge 是噪声，必须写在结论里，否则容易被当成有效信号。
    coverage = ""
    if len(rows) < 500:
        coverage = (f"｜⚠️ 有效样本仅 {len(rows)} 天（要求每日新闻≥{min_articles} 篇），"
                    "且集中在 2026 年 —— 仅供观察，不足以支撑实盘决策")

    if edge_l is None or edge_s is None:
        verdict = "样本不足，无法判别"
    elif edge_l > 0.005 and edge_s > 0.005:
        verdict = "两个方向都有正超额 → 情绪指标具择时价值"
    elif edge_l < -0.005 and edge_s < -0.005:
        verdict = "两个方向都是负超额 → 指标反向或无信息，不建议直接使用"
    elif edge_l > 0.005 and edge_s <= 0.005:
        verdict = ("仅多头方向有效：看多日跑赢基线，但看空日不跌 → "
                   "只能当加仓/减仓参考，不能反向做空")
    elif edge_s > 0.005 and edge_l <= 0.005:
        verdict = ("仅空头方向有效：看空日确实下跌，但看多日不涨 → "
                   "只能当风险预警，不能当买入信号")
    elif edge_l < -0.005 and edge_s >= -0.005:
        verdict = "多头方向负超额：看多日反而跑输，该方向反向（不宜追情绪高点的多）"
    elif edge_s < -0.005 and edge_l >= -0.005:
        verdict = "空头方向负超额：看空日反而上涨，该方向反向（不宜按情绪低点做空）"
    else:
        verdict = "超额不显著（|edge|<0.5%）→ 无可用择时信号"

    return {
        "ok": True,
        "source": source,
        "extreme_pct": extreme_pct,
        "horizon": horizon,
        "hi_threshold": round(hi_th, 4),
        "lo_threshold": round(lo_th, 4),
        "baseline_ret": round(base, 5),
        "n_days_all": len(all_rets),
        "bull": {"n_days": len(bull_rets), "avg_ret": round(_mean(bull_rets), 5),
                 "win_rate": round(win(bull_rets), 3)},
        "bear": {"n_days": len(bear_rets), "avg_ret": round(_mean(bear_rets), 5),
                 "win_rate": round(win(bear_rets), 3)},
        "edge_long_vs_base": round(edge_l, 5) if edge_l is not None else None,
        "edge_short_vs_base": round(edge_s, 5) if edge_s is not None else None,
        "verdict": verdict + coverage,
        "note": verdict + coverage,
        "n_effective_days": len(rows),
        "coverage_warning": coverage.lstrip("｜") or None,
    }
