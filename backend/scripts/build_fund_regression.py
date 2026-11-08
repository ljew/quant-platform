#!/usr/bin/env python3
"""净值回归校验（v3 第三轨）：以披露持仓为下界，用申万行业日收益做约束 WLS。

原理
----
    y_t = Σ_i β_i · r_{i,t} + ε_t
    s.t. β_i ≥ disclosed_i（真实披露不可推翻）, β_i ≥ 0, Σβ_i = equity
    w_t = exp(−(T−t)/τ)：近期样本权重更高，即 WLS 的 W

变量替换 γ = β − lower 后是单纯形约束最小二乘，用投影梯度求解
（单纯形投影有闭式解），不依赖 scipy——三个环境都没装。

定位
----
**不覆盖主结果**。贝叶斯双轨仍是可解释的主口径，回归是独立第三轨交叉验证：
两者一致 → 补齐可信度提高；分歧大 → 该期行业结构存疑，页面标红。
净值回归本身也有误差（单基金不可信、横截面聚合才可信），故只做校验不做替代。

窗口：报告期 R 之前 60 个交易日（含 R 当日若有净值），严格无前视。
只对 31 个申万一级行业回归；港股 / 其他A股 保持贝叶斯值不动。

用法
----
  python backend/scripts/build_fund_regression.py --sample 200   # 小样本验证
  python backend/scripts/build_fund_regression.py                 # 全量
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "fund_research.db"
NAV_CSV = ROOT / "data" / "research" / "nav_raw.csv"

WINDOW = 60          # 报告期前交易日窗口
MIN_OBS = 30         # 少于这个样本数不回归
TAU = 30.0           # 时间衰减（交易日）
RIDGE = 0.02         # 相对迹的岭正则，仅为数值稳定（行业收益高度共线）
ITERS = 300          # 投影梯度迭代次数


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def proj_simplex(v: np.ndarray, s: float) -> np.ndarray:
    """投影到 {x ≥ 0, Σx = s}（Duchi et al. 闭式解）。"""
    if s <= 0:
        return np.zeros_like(v)
    u = np.sort(v)[::-1]
    css = np.cumsum(u) - s
    idx = np.arange(1, len(u) + 1)
    cond = u - css / idx > 0
    if not cond.any():
        return np.full_like(v, s / len(v))
    rho = np.nonzero(cond)[0][-1]
    theta = css[rho] / idx[rho]
    return np.maximum(v - theta, 0.0)


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    ra -= ra.mean()
    rb -= rb.mean()
    return float(ra @ rb / (np.linalg.norm(ra) * np.linalg.norm(rb) + 1e-12))


def solve(X: np.ndarray, y: np.ndarray, w: np.ndarray,
          lower: np.ndarray, equity: float) -> tuple[np.ndarray, float, float]:
    """带下界 + 总和约束的 WLS；返回 (beta, r2, 加权 mse)。"""
    K = X.shape[1]
    s = max(0.0, equity - float(lower.sum()))
    sw = np.sqrt(w)
    A = X * sw[:, None]
    z = y * sw - A @ lower
    G = A.T @ A
    G = G + RIDGE * (np.trace(G) / K) * np.eye(K)
    L = float(np.linalg.eigvalsh(G)[-1]) or 1.0
    b = A.T @ z
    gamma = np.full(K, s / K)
    step = 1.0 / L
    for _ in range(ITERS):
        gamma = proj_simplex(gamma - step * (G @ gamma - b), s)
    beta = lower + gamma
    resid = y - X @ beta
    ss_res = float(np.sum(w * resid ** 2))
    ybar = float(np.average(y, weights=w))
    ss_tot = float(np.sum(w * (y - ybar) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
    return beta, max(-1.0, min(1.0, r2)), ss_res / max(float(w.sum()), 1e-9)


def load_nav(con: sqlite3.Connection) -> dict[str, dict[str, float]]:
    n = con.execute("SELECT COUNT(*) FROM fund_nav_daily").fetchone()[0]
    if n > 0:
        nav = pd.read_sql(
            "SELECT ts_code,nav_date,adj_nav FROM fund_nav_daily ORDER BY ts_code,nav_date", con)
    else:
        log("库内净值为空，直接读 nav_raw.csv（小样本验证用）")
        nav = pd.read_csv(NAV_CSV, dtype={"ts_code": str, "nav_date": str})
        nav = nav.sort_values(["ts_code", "nav_date"])
    nav["ret"] = nav.groupby("ts_code").adj_nav.pct_change() * 100
    nav["ret"] = nav["ret"].mask((nav["ret"] > 50) | (nav["ret"] < -50))
    return {c: dict(zip(g.nav_date, g.ret)) for c, g in nav.groupby("ts_code", sort=False)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="只处理前 N 只基金（验证用）")
    ap.add_argument("--no-cache", action="store_true", help="不更新页面缓存")
    args = ap.parse_args()

    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA busy_timeout=30000")

    log("读申万行业日收益 ...")
    sw = pd.read_sql("SELECT industry,trade_date,ret FROM sw_industry_daily", con)
    inds = sorted(sw.industry.unique())
    K = len(inds)
    ipos = {v: i for i, v in enumerate(inds)}
    piv = sw.pivot_table(index="trade_date", columns="industry", values="ret",
                         aggfunc="first").sort_index()
    dates = piv.index.tolist()
    X_all = piv.reindex(columns=inds).fillna(0.0).to_numpy(dtype=np.float64)
    log(f"  行业 {K} 个 / 交易日 {len(dates)} 个 / {dates[0]}~{dates[-1]}")

    log("读基金日频净值 ...")
    nav_map = load_nav(con)
    log(f"  {len(nav_map):,} 只基金有净值")

    log("读补齐截面与贝叶斯行业权重 ...")
    sec = pd.read_sql("""
        SELECT ts_code,end_date,anchor_cover,disclosed_cover
        FROM fund_hold_coverage
        WHERE is_complete=0 AND anchor_end_date<>'' AND anchor_cover>0
        ORDER BY ts_code,end_date
    """, con)
    if args.sample:
        codes = sorted(sec.ts_code.unique())[:args.sample]
        sec = sec[sec.ts_code.isin(codes)].copy()
    exp = pd.read_sql(
        "SELECT ts_code,end_date,industry,disclosed_w,total_w FROM fund_industry_exposure", con)
    exp = exp[exp.industry.isin(inds)]
    exp_map = {k: g for k, g in exp.groupby(["ts_code", "end_date"], sort=False)}
    log(f"  待回归截面 {len(sec):,} 个 / 行业权重行 {len(exp):,}")

    reg_rows: list[tuple] = []
    meta_rows: list[tuple] = []
    done = skipped = skipped_low_info = 0
    t0 = time.time()
    for code, grp in sec.groupby("ts_code", sort=False):
        nmap = nav_map.get(code)
        if not nmap:
            skipped += len(grp)
            continue
        for r in grp.itertuples(index=False):
            i = bisect.bisect_right(dates, r.end_date) - 1
            if i < 0:
                skipped += 1
                continue
            lo = max(0, i - WINDOW + 1)
            ys = np.array([nmap.get(d, np.nan) for d in dates[lo:i + 1]], dtype=float)
            mask = np.isfinite(ys)
            if int(mask.sum()) < MIN_OBS:
                skipped += 1
                continue
            X = X_all[lo:i + 1][mask]
            y = ys[mask]
            # 低信息截面：净值长期不更新 / 债基特征时 y 方差趋近 0，
            # 硬约束 Σβ≈100% 会制造巨大残差、R² 变成大负数，必须剔除而非硬算。
            if float(np.std(y)) < 0.15:
                skipped_low_info += 1
                continue
            T = len(y)
            w = np.exp(-np.arange(T)[::-1] / TAU)
            g = exp_map.get((code, r.end_date))
            if g is None:
                skipped += 1
                continue
            lower = np.zeros(K)
            bayes = np.zeros(K)
            for e in g.itertuples(index=False):
                j = ipos[e.industry]
                lower[j] = float(e.disclosed_w or 0)
                bayes[j] = float(e.total_w or 0)
            equity = float(bayes.sum())   # 只回归申万行业；港股/其他保持贝叶斯值
            if equity <= 0 or equity < lower.sum():
                skipped += 1
                continue
            # 量纲：库里权重是百分数（30 表示 30%），回归方程 y = X·β 需要小数权重 0.30。
            # 不换算会让 X·β 放大 100 倍，残差爆炸、R² 变成大负数。
            beta, r2, mse = solve(X, y, w, lower / 100.0, equity / 100.0)
            beta = beta * 100.0
            l1 = float(np.abs(beta - bayes).sum())
            sp = spearman(beta, bayes)
            lam = round(max(0.0, min(1.0, r2)) * max(0.0, 1.0 - l1 / 100.0), 4)
            for j in range(K):
                if beta[j] + bayes[j] > 0.01:
                    reg_rows.append((code, r.end_date, inds[j],
                                     round(float(beta[j]), 4), round(float(bayes[j]), 4),
                                     round(float(beta[j] - bayes[j]), 4)))
            meta_rows.append((code, r.end_date, round(r2, 4), T, round(mse, 4),
                              lam, round(l1, 2), round(sp, 3)))
            done += 1
        if done and done % 10000 < len(grp):
            log(f"  已回归 {done:,} 截面 / 跳过 {skipped:,} / {time.time()-t0:.0f}s")

    log(f"回归完成 {done:,} 截面 / 跳过 {skipped:,}（无净值/样本不足）/"
        f"低信息剔除 {skipped_low_info:,} / 耗时 {time.time()-t0:.0f}s")
    if not meta_rows:
        log("无结果，退出")
        con.close()
        return

    log("写库 ...")
    con.execute("DELETE FROM fund_industry_regression")
    con.execute("DELETE FROM fund_regression_meta")
    con.executemany("INSERT OR REPLACE INTO fund_industry_regression VALUES (?,?,?,?,?,?)", reg_rows)
    con.executemany("INSERT OR REPLACE INTO fund_regression_meta VALUES (?,?,?,?,?,?,?,?)", meta_rows)

    m = pd.DataFrame(meta_rows, columns=["c", "d", "r2", "n", "mse", "lam", "l1", "sp"])
    nav_prog = {"done": 0, "total": 0, "completed": False}
    pf = ROOT / "data" / "research" / "progress_nav.json"
    if pf.exists():
        try:
            obj = json.loads(pf.read_text(encoding="utf-8"))
            dn = len(obj.get("done", []))
            pool = int(con.execute(
                "SELECT COUNT(DISTINCT ts_code) FROM fund_hold_coverage").fetchone()[0])
            nav_prog = {"done": dn, "total": pool, "completed": dn >= pool,
                        "pct": round(dn / max(pool, 1) * 100, 1)}
        except Exception:
            pass
    summary = {
        "version": "v3 · 约束 WLS 净值回归校验",
        "nav_progress": nav_prog,
        "n_sections": int(len(m)), "n_funds": int(m.c.nunique()),
        "skipped_low_info": int(skipped_low_info),
        "r2_median": round(float(m.r2.median()), 3),
        "r2_mean": round(float(m.r2.mean()), 3),
        "l1_median": round(float(m.l1.median()), 1),
        "spearman_median": round(float(m.sp.median()), 3),
        "n_obs_median": int(m.n.median()),
        "agree_share": round(float((m.sp >= 0.6).mean()), 3),
        "high_conflict_share": round(float((m.sp < 0.3).mean()), 3),
        "metric_note": "l1 是 31 个行业合计的双向绝对偏离(pp)，实际错位约为其一半；"
                       "一致性以 Spearman 排序相关为准，>=0.6 记一致、<0.3 记高分歧",
    }
    if not args.no_cache:
        row = con.execute(
            "SELECT payload_json FROM fund_stat_cache WHERE key='penetration'").fetchone()
        if row:
            d = json.loads(row[0])
            d["regression"] = summary
            meth = d.get("method") or {}
            meth["version"] = "v5 · 双轨 + 贝叶斯行业约束 + 监管仓位约束 + 净值回归校验 + 回归隐含个股持仓"
            steps = meth.get("steps", [])
            steps = [s for s in steps if s.get("n") not in (7, 8)]
            steps.append({
                "n": 7, "title": "净值回归校验（第三轨）",
                "desc": "以当期已披露行业权重为下界硬约束，用报告期前 60 个交易日的申万一级行业日收益"
                        "对基金日收益做时间衰减 WLS（单纯形投影梯度求解），得到独立第三轨 β。"
                        "输出 R²、与贝叶斯结果的 L1 距离与排序相关；回归结果不覆盖主口径，只作交叉验证，"
                        "分歧大的期次在页面标红提示。",
            })
            steps.append({
                "n": 8, "title": "回归隐含个股持仓（第五轨）",
                "desc": "把行业 β 当作该行业目标权重，减去真实披露的前十大（硬下界，回归不能推翻），"
                        "得到行业缺口；缺口按锚点完整持仓的真实候选股相对权重分配，单股上限 = 本期"
                        "第 10 大重仓，撞上限用 PROXY:行业 占位。港股等非申万行业不在 β 覆盖内，"
                        "原样保留。结果独立存第五轨，不覆盖主口径；按 R² 与排序相关打 quality 标签，"
                        "low 只作参考。权重守恒 100% 截面落在 1pp 内。",
            })
            meth["steps"] = sorted(steps, key=lambda s: s.get("n", 0))
            meth["not_done"] = (
                "仍未做：FOF 多层嵌套穿透 —— Tushare fund_portfolio 与天天基金 F10 对 FOF "
                "均只返回直接持有的股票、不返回被投基金，样本中 196 只 FOF 的直接 A 股仓位"
                "中位仅 7.4%（非 FOF 81.6%），需 Wind AMS / 聚源或定期报告 PDF 的"
                "「基金投资明细」章节；证监会门类与申万一级无官方映射，监管数据只用于总量约束与独立展示")
            meth["regression_params"] = {
                "window": WINDOW, "min_obs": MIN_OBS, "tau": TAU,
                "ridge": RIDGE, "iters": ITERS,
                "constraint": "β_i ≥ disclosed_i, β_i ≥ 0, Σβ_i = 申万行业合计权重",
            }
            d["method"] = meth
            con.execute("UPDATE fund_stat_cache SET payload_json=?,updated_at=? WHERE key='penetration'",
                        (json.dumps(d, ensure_ascii=False, default=float),
                         datetime.now().isoformat(timespec="seconds")))
    con.commit()
    con.close()
    log("汇总: " + json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
