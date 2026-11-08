#!/usr/bin/env python3
"""把监管披露的证监会行业配置导入独立研究库。

输入：data/research/csrc_industry_raw.csv（fetch_csrc_industry.py 产出）
输出：fund_csrc_industry（门类明细）、fund_csrc_equity（每基金-期的 A 股仓位合计）

Σ weight_pct = A 股股票仓位占基金资产净值比例（监管口径，按全部持仓计算），
不含港股通标的。这是替换「按锚点继承 100% 仓位」假设的真实约束。
"""
from __future__ import annotations

import csv
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "fund_research.db"
CSV = ROOT / "data" / "research" / "csrc_industry_raw.csv"


def main() -> None:
    if not CSV.exists():
        raise SystemExit(f"监管数据文件不存在：{CSV}")
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("""CREATE TABLE IF NOT EXISTS fund_csrc_industry (
        base_code TEXT NOT NULL, end_date TEXT NOT NULL,
        industry_code TEXT NOT NULL, industry_name TEXT,
        weight_pct REAL, mkv_wan REAL,
        PRIMARY KEY (base_code, end_date, industry_code))""")
    con.execute("""CREATE TABLE IF NOT EXISTS fund_csrc_equity (
        base_code TEXT NOT NULL, end_date TEXT NOT NULL,
        a_share_pct REAL, n_industries INTEGER,
        PRIMARY KEY (base_code, end_date))""")
    con.execute("DELETE FROM fund_csrc_industry")
    con.execute("DELETE FROM fund_csrc_equity")

    eq = defaultdict(float)
    cnt = defaultdict(int)
    totals: dict[tuple[str, str], float] = {}
    batch, n, skipped_dup = [], 0, 0
    with open(CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            code, ed = r["base_code"], r["end_date"]
            ic = (r["industry_code"] or "").strip()
            iname = (r["industry_name"] or "").strip()
            try:
                w = float(r["weight_pct"] or 0)
            except ValueError:
                continue
            # 部分年份同一报告期混了两套分类：证监会单字母门类(C/I/J…)、
            # 数字码(45 信息技术)、中文码(信息技术)，外加一行「合计」。
            # 直接全加会把仓位放大 2-3 倍（实测出现 262%）。
            if ic == "合计" or iname == "合计":
                totals[(code, ed)] = w
                continue
            if not (len(ic) == 1 and ic.isascii() and ic.isalpha()):
                skipped_dup += 1
                continue
            batch.append((code, ed, ic, iname, w, float(r["mkv_wan"] or 0)))
            eq[(code, ed)] += w
            cnt[(code, ed)] += 1
            if len(batch) >= 20000:
                con.executemany("INSERT OR REPLACE INTO fund_csrc_industry VALUES (?,?,?,?,?,?)", batch)
                n += len(batch)
                batch = []
    if batch:
        con.executemany("INSERT OR REPLACE INTO fund_csrc_industry VALUES (?,?,?,?,?,?)", batch)
        n += len(batch)
    # 「合计」行是报告原文给出的股票仓位合计，比逐门类求和更准
    for k, v in totals.items():
        if 0 < v <= 105:
            eq[k] = v
    eq_rows = [(c, ed, round(v, 2), cnt.get((c, ed), 0))
               for (c, ed), v in eq.items() if 0 < v <= 105]
    con.executemany("INSERT OR REPLACE INTO fund_csrc_equity VALUES (?,?,?,?)", eq_rows)
    con.commit()
    print(f"排除非证监会分类行 {skipped_dup:,} 条 / 采用合计行 {len(totals):,} 个 / "
          f"过滤异常仓位 {len(eq) - len(eq_rows):,} 个")
    import statistics
    a = [x[2] for x in eq_rows]
    print(f"导入监管明细 {n:,} 行 / 截面 {len(eq_rows):,} 个 / 基金 "
          f"{len({c for c, _, _, _ in eq_rows}):,} 只")
    if a:
        print(f"A股仓位: 中位 {statistics.median(a):.1f}% / 均值 {sum(a)/len(a):.1f}% / "
              f"min {min(a):.1f}% / max {max(a):.1f}%")
    con.close()


if __name__ == "__main__":
    main()
