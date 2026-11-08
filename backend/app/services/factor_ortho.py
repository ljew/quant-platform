"""截面正交化（对代表风格因子残差）——挖矿主路径与 GP 共享。

W47-2 / M3-4：单一真相，禁止在 factor_mining 再复制一份 OLS。
代表集表达式与历史 GP ORTHO_EXPRS 对齐，本切片禁止改式。
"""
from __future__ import annotations

import math
from datetime import date

from app.core.engine.factor_expr import eval_factor
from app.datahub.ns_vars import fin_asof, make_ns

# 代表因子集（契约写死，与 GP 历史 ORTHO_EXPRS 一致）
ORTHO_EXPRS: dict[str, str] = {
    "momentum": "roc(c_m, 20)",
    "reversal": "-roc(c_r, 5)",
    "low_vol": "std(c_v)",
    "value": "safe_inv(pe_ttm, 0.001, 1000)",
    "size": "log(market_cap + 1)",
}

MIN_CROSS_SECTION = 20

ORTHO_NOTE = (
    "截面股票对全部代表因子对齐数<20或共线时该期降级为raw IC"
)


def ortho_basis_template(
    *,
    n_snaps_resid: int = 0,
    n_snaps_fallback_raw: int = 0,
) -> dict:
    """报告用 ortho_basis 骨架。"""
    return {
        "names": list(ORTHO_EXPRS.keys()),
        "exprs": dict(ORTHO_EXPRS),
        "min_cross_section": MIN_CROSS_SECTION,
        "n_snaps_resid": int(n_snaps_resid),
        "n_snaps_fallback_raw": int(n_snaps_fallback_raw),
        "note": ORTHO_NOTE,
    }


def orthogonalize_cross_section(
    fv: list[tuple[str, float, float]],
    fac_bucket: dict[str, dict[str, float]] | None,
) -> list[tuple[str, float, float]]:
    """候选值对代表因子做横截面 OLS，返回残差列表 [(sym, resid, ret)]。

    失败 / 共线 / 对齐股票数 < MIN_CROSS_SECTION → **原样返回同一 fv 对象**
    （调用方可按 `is` 判断是否降级）。
    """
    if not fac_bucket:
        return fv
    # 须对 ORTHO_EXPRS 每一个 fac 都有桶且该 sym 有值（契约 §5.1）
    if not all(f in fac_bucket for f in ORTHO_EXPRS):
        return fv
    syms_ok = [
        x for x in fv
        if all(x[0] in fac_bucket[f] for f in ORTHO_EXPRS)
    ]
    if len(syms_ok) < MIN_CROSS_SECTION:
        return fv
    # 设计矩阵列序与 ORTHO_EXPRS 插入序一致，保证可复现
    xs = [[1.0] + [fac_bucket[f][x[0]] for f in ORTHO_EXPRS] for x in syms_ok]
    ys = [x[1] for x in syms_ok]
    n = len(xs)
    k = len(xs[0])
    try:
        xtx = [
            [sum(xs[i][a] * xs[i][b_] for i in range(n)) for b_ in range(k)]
            for a in range(k)
        ]
        xty = [sum(xs[i][a] * ys[i] for i in range(n)) for a in range(k)]
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
        out: list[tuple[str, float, float]] = []
        for i, x in enumerate(syms_ok):
            pred = sum(beta[a] * xs[i][a] for a in range(k))
            out.append((x[0], x[1] - pred, x[2]))
        return out
    except Exception:  # noqa: BLE001
        return fv


def build_ortho_cache(
    *,
    snaps: list[tuple[int, date]],
    syms: list[str],
    aligned: dict[str, dict[date, float]],
    vol_aligned: dict[str, dict[date, float]],
    amt_aligned: dict[str, dict[date, float]],
    axis_dates: list[date],
    attrs: dict,
    attrs_tl: dict | None,
    es_map: dict,
    fund_tl: dict | None,
    fin_hist,
    bench_aligned: list | None = None,
    bench_map: dict | None = None,
) -> dict[date, dict[str, dict[str, float]]]:
    """预计算 snap → {fac_name → {sym → val}}。

    数据源与挖矿/GP 同构（make_ns + eval_factor）；禁止另开数据源。
    """
    # 延迟导入，避免与 factor_mining 顶层环依赖
    from app.services.factor_mining import (
        _apply_fund_asof,
        _attrs_asof,
        _collect_seg,
        _es_asof,
    )

    ortho_cache: dict[date, dict[str, dict[str, float]]] = {}
    for fac_name, fexpr in ORTHO_EXPRS.items():
        for idx, snap in snaps:
            if bench_map is not None and snap not in bench_map:
                continue
            i0 = max(0, idx - 130)
            slice_dates = axis_dates[i0: idx + 1]
            if bench_aligned is not None:
                mkt_slice = bench_aligned[i0: idx + 1]
            else:
                mkt_slice = [None] * len(slice_dates)
            bucket = ortho_cache.setdefault(snap, {})
            for sym in syms:
                amap = aligned.get(sym)
                if not amap or snap not in amap:
                    continue
                seg, vseg, aseg = _collect_seg(
                    amap,
                    vol_aligned.get(sym) or {},
                    amt_aligned.get(sym) or {},
                    slice_dates,
                )
                if len(seg) < 60:
                    continue
                a = dict(attrs.get(sym, {}) or {})
                if attrs_tl:
                    _pv = _attrs_asof(attrs_tl, sym, snap)
                    if _pv:
                        a.update(_pv)
                if fund_tl is not None:
                    _apply_fund_asof(a, fund_tl, sym, snap)
                # 与 GP 一致：基准切片与价格窗对齐；长度不齐时截尾
                mkt_b = mkt_slice
                if len(mkt_b) != len(seg):
                    mkt_b = mkt_slice[-len(seg):] if len(mkt_slice) >= len(seg) else list(mkt_slice) + [None] * (len(seg) - len(mkt_slice))
                ns = make_ns(
                    seg,
                    mkt_b,
                    a,
                    esv=_es_asof(es_map, sym, snap),
                    vols=vseg,
                    amts=aseg,
                    fin=fin_asof(fin_hist, sym, snap) if fin_hist is not None else None,
                )
                try:
                    v = eval_factor(fexpr, ns)
                except Exception:  # noqa: BLE001
                    v = None
                if v is not None and math.isfinite(v):
                    bucket.setdefault(fac_name, {})[sym] = float(v)
    return ortho_cache
