#!/usr/bin/env python3
"""回归隐含持仓（第五轨）：把约束 WLS 的行业 β 反推到个股级。

纪律（与老刘的口径一致）：
- 这是**独立推断轨**，绝不覆盖 disclosed / carried 主口径，也不写回主明细。
- β 只给到申万行业一级；行业内个股分布仍来自「锚点完整持仓的真实候选股」，
  属于二级推断，置信度低于行业 β，逐行带 quality / confidence 标签。
- 真实披露的前十大权重是硬下界，回归不能推翻。
- 低质量回归（R² 低或行业排序与贝叶斯相反）标 low，页面上只作参考。

输入：
  data/holdings/by_fund/*.csv        主口径双轨明细（disclosed/carried/carried_proxy）
  fund_research.db
    fund_industry_regression         (ts_code,end_date,industry,beta,bayes_w,diff)
    fund_regression_meta             (ts_code,end_date,r2,n_obs,mse,lambda,bayes_l1,spearman)
    fund_hold_coverage              (is_complete / disclosed_cover ...)
输出：
  fund_regression_holdings          逐股对比明细
  fund_regression_holdings_meta    逐截面质量与守恒
  fund_regression_holdings_summary 单行汇总（供 API 直接读）
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "fund_research.db"
BY_FUND = ROOT / "data" / "holdings" / "by_fund"
SUMMARY_JSON = ROOT / "data" / "research" / "regression_holdings.json"

DDL = [
    """CREATE TABLE IF NOT EXISTS fund_regression_holdings (
        ts_code TEXT NOT NULL, end_date TEXT NOT NULL,
        symbol TEXT NOT NULL, industry TEXT NOT NULL,
        beta_w REAL, bayes_w REAL, diff REAL,
        source TEXT, confidence REAL, quality TEXT,
        PRIMARY KEY (ts_code, end_date, symbol, source))""",
    """CREATE TABLE IF NOT EXISTS fund_regression_holdings_meta (
        ts_code TEXT NOT NULL, end_date TEXT NOT NULL,
        r2 REAL, spearman REAL, n_obs INTEGER,
        beta_total REAL, disclosed_total REAL, implied_total REAL,
        bayes_total REAL, l1_vs_bayes REAL,
        n_stocks INTEGER, n_proxy INTEGER, quality TEXT,
        PRIMARY KEY (ts_code, end_date))""",
    """CREATE TABLE IF NOT EXISTS fund_regression_holdings_summary (
        k TEXT PRIMARY KEY, payload_json TEXT, updated_at TEXT)""",
]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def water_fill(cands: list[tuple[str, float]], budget: float, cap: float):
    """按候选股相对权重分配预算，单股不超过 cap；溢出在同行业内反复再分配。"""
    if budget <= 0 or not cands:
        return []
    weights = [max(w, 1e-9) for _, w in cands]
    alloc = [0.0] * len(cands)
    remain = budget
    active = list(range(len(cands)))
    for _ in range(12):
        if remain <= 1e-6 or not active:
            break
        tw = sum(weights[i] for i in active)
        over = 0.0
        for i in active:
            give = remain * weights[i] / tw
            new = alloc[i] + give
            if new > cap:
                over += new - cap
                new = cap
            alloc[i] = new
        remain = over
        active = [i for i in active if alloc[i] < cap - 1e-6]
    if remain > 1e-4:
        # 全部撞上限：剩余交给行业代理占位，不伪造个股
        return [(cands[i][0], alloc[i]) for i in range(len(cands)) if alloc[i] > 1e-6] + \
               [("_UNALLOCATED_", remain)]
    return [(cands[i][0], alloc[i]) for i in range(len(cands)) if alloc[i] > 1e-6]


def quality_of(r2: float, sp: float) -> str:
    if r2 >= 0.70 and sp >= 0.50:
        return "high"
    if r2 >= 0.50 and sp >= 0.20:
        return "medium"
    return "low"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="只处理前 N 只基金（调试）")
    args = ap.parse_args()

    started = time.time()
    con = sqlite3.connect(str(DB), timeout=60)
    con.execute("PRAGMA busy_timeout=30000")
    for ddl in DDL:
        con.execute(ddl)
    if not args.sample:
        con.execute("DELETE FROM fund_regression_holdings")
        con.execute("DELETE FROM fund_regression_holdings_meta")
    con.commit()

    log("读取回归 β / 质量指标 ...")
    beta_map: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for c, d, ind, b in con.execute(
        "SELECT ts_code,end_date,industry,beta FROM fund_industry_regression"
    ):
        beta_map[(c, d)][ind] = float(b)
    meta_map = {
        (c, d): (float(r2), float(sp), int(n))
        for c, d, r2, n, mse, lam, l1, sp in con.execute(
            "SELECT ts_code,end_date,r2,n_obs,mse,lambda,bayes_l1,spearman "
            "FROM fund_regression_meta"
        )
    }
    incomplete = {
        (c, d) for c, d in con.execute(
            "SELECT ts_code,end_date FROM fund_hold_coverage WHERE is_complete=0"
        )
    }
    log(f"回归截面 {len(beta_map):,} / 需补齐截面 {len(incomplete):,}")

    files = sorted(BY_FUND.glob("*.csv"))
    if args.sample:
        files = files[: args.sample]
    log(f"处理 {len(files):,} 个基金分片 ...")

    out_rows: list[tuple] = []
    out_meta: list[tuple] = []
    n_sections = n_proxy = n_stock = 0
    quality_count: dict[str, int] = defaultdict(int)
    l1_all: list[float] = []
    conserve: list[float] = []

    for fi, path in enumerate(files, 1):
        by_date: dict[str, dict[str, list[tuple]]] = defaultdict(
            lambda: {"disclosed": [], "carried": []}
        )
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                ed = r["end_date"]
                rec = (r["symbol"], r["industry"], float(r["weight"] or 0))
                if r["source"] == "disclosed":
                    by_date[ed]["disclosed"].append(rec)
                else:
                    by_date[ed]["carried"].append(rec)

        for ed, groups in by_date.items():
            key = (path.stem, ed)
            if key not in beta_map or key not in incomplete:
                continue
            beta = beta_map[key]
            r2, sp, n_obs = meta_map.get(key, (0.0, 0.0, 0))
            quality = quality_of(r2, sp)
            conf = round(max(0.0, min(1.0, r2 * max(sp, 0.0))), 4)

            disc = groups["disclosed"]
            carr = groups["carried"]
            if not disc or not carr:
                continue

            d_w: dict[str, float] = defaultdict(float)
            for _, ind, w in disc:
                d_w[ind] += w
            cand: dict[str, list[tuple[str, float]]] = defaultdict(list)
            bayes_w: dict[str, float] = defaultdict(float)
            bayes_src: dict[str, str] = {}
            for sym, ind, w in carr:
                # PROXY:行业 是行业占位，不作为个股候选，但保留其贝叶斯权重作对比
                if not sym.startswith("PROXY:"):
                    cand[ind].append((sym, w))
                bayes_w[(sym, ind)] += w
                bayes_src[(sym, ind)] = (
                    "bayes_proxy" if sym.startswith("PROXY:") else "bayes_stock"
                )

            tenth = min(w for _, _, w in disc)
            cap = max(0.5, min(tenth, 100.0))
            implied: dict[tuple[str, str], float] = defaultdict(float)
            implied_src: dict[tuple[str, str], str] = {}
            proxy_n = 0

            for ind, target in beta.items():
                gap = max(0.0, target - d_w.get(ind, 0.0))
                if gap <= 1e-6:
                    continue
                got = water_fill(cand.get(ind, []), gap, cap)
                for sym, w in got:
                    if sym == "_UNALLOCATED_":
                        k = (f"PROXY:{ind}", ind)
                        implied[k] += w
                        implied_src[k] = "regression_proxy"
                        proxy_n += 1
                    else:
                        k = (sym, ind)
                        implied[k] += w
                        implied_src[k] = "regression_implied"
                if not got:
                    k = (f"PROXY:{ind}", ind)
                    implied[k] += gap
                    implied_src[k] = "regression_proxy"
                    proxy_n += 1

            # 非申万行业（港股/其他A股）不在 β 覆盖内：原样保留贝叶斯值，不推断
            for sym, ind, w in carr:
                if ind in beta:
                    continue
                k = (sym, ind)
                implied[k] += w
                implied_src[k] = "unchanged_non_sw"

            keys = set(implied) | set(bayes_w)
            for k in keys:
                sym, ind = k
                bw, iw = bayes_w.get(k, 0.0), implied.get(k, 0.0)
                src = implied_src.get(k, "dropped_by_beta")
                if iw <= 1e-6 and bw <= 1e-6:
                    continue
                out_rows.append((path.stem, ed, sym, ind, round(iw, 4),
                                 round(bw, 4), round(iw - bw, 4),
                                 src, conf, quality))
                if src == "regression_implied" or src == "unchanged_non_sw":
                    if not sym.startswith("PROXY:") and src == "regression_implied":
                        n_stock += 1
                if src == "regression_proxy":
                    n_proxy += 1

            # 目标总权重：申万行业按 β，非申万（港股/其他A股）的披露+继承都原样保留。
            # 早期版本漏掉「非申万披露权重」，守恒 gap 会恰好等于 −该值（港股重仓基金可达 −90pp）。
            non_sw_disc = sum(w for _, ind, w in disc if ind not in beta)
            non_sw_carr = sum(w for _, ind, w in carr if ind not in beta)
            beta_total = sum(beta.values()) + non_sw_disc + non_sw_carr
            implied_total = sum(implied.values())
            bayes_total = sum(bayes_w.values())
            disclosed_total = sum(w for _, _, w in disc)
            l1 = sum(abs(implied.get(k, 0.0) - bayes_w.get(k, 0.0)) for k in keys)
            l1_all.append(l1)
            conserve.append(beta_total - disclosed_total - implied_total)
            quality_count[quality] += 1
            out_meta.append((
                path.stem, ed, round(r2, 4), round(sp, 4), n_obs,
                round(beta_total, 2), round(disclosed_total, 2),
                round(implied_total, 2), round(bayes_total, 2), round(l1, 2),
                sum(1 for k in keys if implied_src.get(k) == "regression_implied"),
                proxy_n, quality,
            ))
            n_sections += 1
            if len(out_rows) >= 200000:
                con.executemany(
                    "INSERT OR REPLACE INTO fund_regression_holdings VALUES (?,?,?,?,?,?,?,?,?,?)",
                    out_rows,
                )
                out_rows = []
            if len(out_meta) >= 20000:
                con.executemany(
                    "INSERT OR REPLACE INTO fund_regression_holdings_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    out_meta,
                )
                out_meta = []

        if fi % 1000 == 0:
            log(f"  {fi:,}/{len(files):,} 分片 · 截面 {n_sections:,} · 用时 {time.time()-started:.0f}s")

    if out_rows:
        con.executemany(
            "INSERT OR REPLACE INTO fund_regression_holdings VALUES (?,?,?,?,?,?,?,?,?,?)",
            out_rows,
        )
    if out_meta:
        con.executemany(
            "INSERT OR REPLACE INTO fund_regression_holdings_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            out_meta,
        )
    con.execute("CREATE INDEX IF NOT EXISTS idx_reghold_key "
                "ON fund_regression_holdings(ts_code,end_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_reghold_meta_key "
                "ON fund_regression_holdings_meta(ts_code,end_date)")

    def med(a):
        if not a:
            return 0.0
        s = sorted(a)
        return round(s[len(s) // 2], 3)

    summary = {
        "ok": True,
        "version": "v5 · 回归隐含个股持仓（第五轨）",
        "n_sections": n_sections,
        "n_rows": con.execute(
            "SELECT COUNT(*) FROM fund_regression_holdings"
        ).fetchone()[0],
        "n_funds": con.execute(
            "SELECT COUNT(DISTINCT ts_code) FROM fund_regression_holdings_meta"
        ).fetchone()[0],
        "quality": dict(quality_count),
        "l1_vs_bayes_median": med(l1_all),
        "conserve_gap_median": med(conserve),
        "conserve_within_1pp": round(
            sum(abs(x) < 1 for x in conserve) / max(len(conserve), 1), 4
        ),
        "n_proxy_rows": n_proxy,
        "n_implied_stock_rows": n_stock,
        "discipline": (
            "β 只到申万行业一级；行业内个股分布仍取锚点真实候选股，属二级推断。"
            "真实披露前十大为硬下界不可推翻；本轨独立存储、不覆盖主口径，"
            "quality=low 的截面仅供参考。"
        ),
        "not_done": (
            "FOF 多层嵌套穿透：Tushare fund_portfolio 与天天基金 F10 对 FOF "
            "均只返回直接持有的股票、不返回被投基金，需 Wind AMS / 聚源 / "
            "基金定期报告 PDF「基金投资明细」章节才能做真正多层穿透。"
        ),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    con.execute(
        "INSERT OR REPLACE INTO fund_regression_holdings_summary VALUES (?,?,?)",
        ("main", json.dumps(summary, ensure_ascii=False), summary["updated_at"]),
    )
    con.commit()
    con.close()

    SUMMARY_JSON.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    log(f"完成：{n_sections:,} 截面 / {summary['n_rows']:,} 行 / 质量 {dict(quality_count)}")
    log(f"权重守恒中位偏差 {summary['conserve_gap_median']}pp，"
        f"|gap|<1pp 占比 {summary['conserve_within_1pp']:.1%}")
    log(f"耗时 {time.time()-started:.0f}s")


if __name__ == "__main__":
    main()
