#!/usr/bin/env python3
"""FOF 两层穿透与质量核验。

输入
----
- fund_fof_holdings   FOF 基金层明细（来自定期报告 PDF，见 fetch_fof_reports.py）
- fund_csrc_equity    各基金监管口径 A 股仓位（占净值 %，来自 F10 HYPZ）
- fund_hold_coverage  各基金持仓覆盖度

算法
----
对每个 (FOF, 报告期)：
  w_total      = Σ 基金层占净值比                      —— 这一层看得见多少
  w_pool       = Σ 其中底层基金我们真的有持仓数据的部分  —— 这一层能穿透多少
  w_equity     = Σ w_i × eq_i                         —— 两层穿透后的 A 股敞口
  eq_i         = 底层基金同报告期的监管 A 股仓位%（缺失时取最近一期 ≤ 报告期）
  a_share_decl = FOF 直接持股的 A 股仓位%（fund_csrc_equity）
  equity_total = w_equity + a_share_decl              —— 穿透 + 直接

口径提醒
--------
FOF 自己的行业配置表只统计**直接持有**的股票，不含通过基金间接持有的部分，
所以不能用它来校验穿透结果。真正有意义的硬约束是 equity_total ≤ 100%：
超过就说明底层仓位取值或权重解析有问题。

用法
----
  /usr/bin/python3 backend/scripts/audit_fof_holdings.py            # 只出报告
  /usr/bin/python3 backend/scripts/audit_fof_holdings.py --write    # 落库
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "fund_research.db"

DDL = """
CREATE TABLE IF NOT EXISTS fund_fof_layer_check (
  base_code    TEXT NOT NULL,
  end_date     TEXT NOT NULL,
  report_type  TEXT,
  n_funds      INTEGER,
  w_total      REAL,     -- 基金层合计占净值比(%)
  w_pool       REAL,     -- 其中底层有持仓数据的部分(%)
  w_equity     REAL,     -- 两层穿透后的 A 股敞口(%)
  w_unresolved REAL,     -- 100 - w_total，报告里没有基金明细覆盖的部分
  a_share_decl REAL,     -- FOF 直接持股的 A 股仓位(%)
  eq_total     REAL,     -- 穿透 + 直接，硬约束 ≤100
  n_missing_eq INTEGER,  -- 底层缺仓位数据的只数
  updated_at   TEXT,
  PRIMARY KEY (base_code, end_date));
"""


def med(xs):
    return round(st.median(xs), 2) if xs else None


def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return round(xs[k], 2)


def load_eq(con) -> dict[str, list[tuple[str, float]]]:
    """每只基金按报告期升序的 [(end_date, a_share_pct)]。"""
    out: dict[str, list[tuple[str, float]]] = {}
    for code, end, pct_ in con.execute(
        "SELECT base_code, end_date, a_share_pct FROM fund_csrc_equity "
        "WHERE a_share_pct IS NOT NULL ORDER BY base_code, end_date"
    ):
        out.setdefault(code, []).append((end, pct_))
    return out


def at_or_before(series, end_date, max_gap_days=200):
    best = None
    for d, v in series:
        if d <= end_date:
            best = (d, v)
        else:
            break
    if best is None:
        return None
    d0 = datetime.strptime(end_date, "%Y%m%d")
    d1 = datetime.strptime(best[0], "%Y%m%d")
    if (d0 - d1).days > max_gap_days:
        return None
    return best[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=30000")
    eq = load_eq(con)
    pool = {r[0] for r in con.execute("SELECT DISTINCT base_code FROM fund_hold_coverage")}
    fof_decl = {r[0]: r[1] for r in con.execute(
        "SELECT base_code||'|'||end_date, a_share_pct FROM fund_csrc_equity")}

    # 同一报告期可能同时有 H1（全量）和 Q2（前十名），按行数多的取，别把两者混在一起
    hold: dict[tuple[str, str, str], list[tuple[str, float]]] = {}
    for base, end, rt, code, w in con.execute(
        "SELECT base_code, end_date, report_type, sub_code, weight_pct FROM fund_fof_holdings"
    ):
        hold.setdefault((base, end, rt), []).append((code, w))
    picked: dict[tuple[str, str], tuple[str, list[tuple[str, float]]]] = {}
    for (base, end, rt), subs in hold.items():
        k = (base, end)
        if k not in picked or len(subs) > len(picked[k][1]):
            picked[k] = (rt, subs)
    con.close()

    recs = []
    for (base, end), (rt, subs) in sorted(picked.items()):
        if not subs:
            continue
        w_total = sum(w or 0 for _, w in subs)
        w_pool = w_equity = 0.0
        miss = 0
        for code, w in subs:
            if not w:
                continue
            in_pool = code in pool
            e = at_or_before(eq.get(code, []), end)
            if in_pool:
                w_pool += w
            if e is None:
                miss += 1
                continue
            w_equity += w / 100.0 * e
        decl = fof_decl.get(f"{base}|{end}")
        recs.append(
            {
                "base_code": base,
                "end_date": end,
                "report_type": rt,
                "n_funds": len(subs),
                "w_total": round(w_total, 2),
                "w_pool": round(w_pool, 2),
                "w_equity": round(w_equity, 2),
                "w_unresolved": round(max(0.0, 100 - w_total), 2),
                "a_share_decl": decl,
                "eq_total": (round(w_equity + decl, 2) if decl is not None else None),
                "n_missing_eq": miss,
            }
        )

    # ---------------- 质量报告
    print(f"样本：{len(recs)} 个 (FOF, 报告期)")
    by_rt: dict[str, list[dict]] = {}
    for r in recs:
        by_rt.setdefault(r["report_type"], []).append(r)
    print(f"{'类型':<4}{'n':>6}{'基金层中位':>10}{'可穿透中位':>10}{'权益中位':>9}"
          f"{'未解析中位':>10}{'缺仓位中位':>10}{'权益合计中位':>12}")
    for rt in ("H1", "FY", "Q1", "Q2", "Q3", "Q4"):
        g = by_rt.get(rt)
        if not g:
            continue
        et = [r["eq_total"] for r in g if r["eq_total"] is not None]
        print(
            f"{rt:<4}{len(g):>6}{med([r['w_total'] for r in g]):>10}"
            f"{med([r['w_pool'] for r in g]):>10}{med([r['w_equity'] for r in g]):>9}"
            f"{med([r['w_unresolved'] for r in g]):>10}{med([r['n_missing_eq'] for r in g]):>10}"
            f"{(med(et) if et else '-'):>12}"
        )

    et = [r["eq_total"] for r in recs if r["eq_total"] is not None]
    if et:
        over = sum(1 for x in et if x > 100)
        print(
            f"\n硬约束（{len(et)} 个可对照样本）：穿透+直接权益 中位 {med(et)}%，"
            f"p95 {pct(et,0.95)}%，>100% 的 {over} 个（{over/len(et):.1%}）"
        )

    if args.verbose:
        worst = sorted([r for r in recs if r["eq_total"] is not None],
                       key=lambda x: -x["eq_total"])[:15]
        print("\n权益合计最高的 15 例：")
        for r in worst:
            print(f"  {r['base_code']} {r['end_date']} {r['report_type']:<3} "
                  f"w_total={r['w_total']:6.2f} w_eq={r['w_equity']:6.2f} "
                  f"decl={r['a_share_decl']} total={r['eq_total']}")

    if args.write:
        out = sqlite3.connect(DB, timeout=60)
        out.executescript(DDL)
        out.execute("DELETE FROM fund_fof_layer_check")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out.executemany(
            """INSERT OR REPLACE INTO fund_fof_layer_check
               (base_code,end_date,report_type,n_funds,w_total,w_pool,w_equity,
                w_unresolved,a_share_decl,eq_total,n_missing_eq,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(r["base_code"], r["end_date"], r["report_type"], r["n_funds"],
              r["w_total"], r["w_pool"], r["w_equity"], r["w_unresolved"],
              r["a_share_decl"], r["eq_total"], r["n_missing_eq"], now) for r in recs],
        )
        out.commit()
        out.close()
        print(f"\n已写入 fund_fof_layer_check：{len(recs)} 行")


if __name__ == "__main__":
    main()
