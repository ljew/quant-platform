#!/usr/bin/env python3
"""FOF 可穿透性诊断：量化「多层嵌套穿透」在当前数据源下的覆盖边界。

结论先行（2026-11-08 实测）：
  Tushare fund_portfolio 与天天基金 F10 jjcc 对 FOF 都只返回「报告期末
  股票投资明细」，不返回被投基金。因此 active 样本里 196 只 FOF 的持仓明细
  全是股票代码，被投基金层（通常占净值大头）是黑箱。
  本脚本不伪造穿透，只做：识别 → 量化缺口 → 给出排除建议 → 写入页面标注。

输出：
  fund_fof_diagnosis        每只 FOF 一行的可穿透性诊断
  fund_fof_summary          单行汇总（API 直接读）
  data/research/fof_diagnosis.json
"""
from __future__ import annotations

import json
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN_DB = ROOT / "data" / "quant_dev.db"
RES_DB = ROOT / "data" / "fund_research.db"
OUT_JSON = ROOT / "data" / "research" / "fof_diagnosis.json"


def med(a):
    return round(st.median(a), 2) if a else 0.0


def main() -> None:
    main_con = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
    con = sqlite3.connect(str(RES_DB), timeout=60)
    con.execute("PRAGMA busy_timeout=30000")

    con.execute("""CREATE TABLE IF NOT EXISTS fund_fof_diagnosis (
        base_code TEXT, ts_code TEXT PRIMARY KEY, name TEXT, fund_type TEXT,
        n_sections INTEGER, disclosed_cover_med REAL,
        reg_equity_med REAL, our_a_share_med REAL, hk_med REAL,
        unpenetrated_upper REAL, verdict TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS fund_fof_summary (
        k TEXT PRIMARY KEY, payload_json TEXT, updated_at TEXT)""")
    con.commit()

    fofs = main_con.execute(
        "SELECT base_code,ts_code,name,fund_type FROM fund_master "
        "WHERE group_tag='active' AND std_ret>=8 AND upper(name) LIKE '%FOF%'"
    ).fetchall()
    main_con.close()
    print(f"active 样本中的 FOF：{len(fofs)} 只")

    rows = []
    for base, ts, name, ftype in fofs:
        cov = con.execute(
            "SELECT COUNT(*), AVG(disclosed_cover), AVG(reg_equity) "
            "FROM fund_hold_coverage WHERE ts_code=?", (ts,)
        ).fetchone()
        n_sec = int(cov[0] or 0)
        reg_rows = con.execute(
            "SELECT a_share_pct FROM fund_csrc_equity WHERE base_code=?", (base,)
        ).fetchall()
        reg_med = med([float(r[0]) for r in reg_rows if r[0] is not None])

        exp = con.execute(
            "SELECT end_date, "
            "SUM(CASE WHEN industry NOT IN ('港股','其他A股') THEN total_w ELSE 0 END), "
            "SUM(CASE WHEN industry='港股' THEN total_w ELSE 0 END) "
            "FROM fund_industry_exposure WHERE ts_code=? GROUP BY end_date", (ts,)
        ).fetchall()
        our_med = med([float(r[1] or 0) for r in exp])
        hk_med = med([float(r[2] or 0) for r in exp])
        disc_med = med([float(r[0]) for r in con.execute(
            "SELECT disclosed_cover FROM fund_hold_coverage WHERE ts_code=?", (ts,)
        ).fetchall() if r[0] is not None])

        # 上界：净值中不属于「直接 A 股 + 港股」的部分。
        # 含被投基金 + 债券 + 现金，现有数据源无法再拆，故只能作为上界。
        unpen = round(max(0.0, 100.0 - reg_med - hk_med), 2) if reg_med else None
        rows.append((base, ts, name, ftype or "", n_sec, disc_med,
                     reg_med, our_med, hk_med, unpen,
                     "not_applicable" if (unpen or 0) > 30 else "partial"))

    con.executemany(
        "INSERT OR REPLACE INTO fund_fof_diagnosis VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
    )

    # 对照：FOF vs 非 FOF 的直接 A 股仓位 / 披露覆盖
    fof_codes = {r[1] for r in rows}
    all_cov = con.execute(
        "SELECT ts_code,disclosed_cover,reg_equity FROM fund_hold_coverage "
        "WHERE is_complete=0"
    ).fetchall()
    fof_reg = [float(r[2]) for r in all_cov if r[0] in fof_codes and r[2] is not None]
    non_reg = [float(r[2]) for r in all_cov if r[0] not in fof_codes and r[2] is not None]
    fof_disc = [float(r[1]) for r in all_cov if r[0] in fof_codes and r[1] is not None]
    non_disc = [float(r[1]) for r in all_cov if r[0] not in fof_codes and r[1] is not None]

    # 排除 FOF 前后：全市场行业均值（最近一个报告期）
    last_ed = con.execute("SELECT MAX(end_date) FROM fund_industry_exposure").fetchone()[0]
    incl = defaultdict(list)
    excl = defaultdict(list)
    funds_all = set()
    fof_at_ed = set()
    for c, ind, w in con.execute(
        "SELECT ts_code,industry,total_w FROM fund_industry_exposure WHERE end_date=?",
        (last_ed,),
    ):
        funds_all.add(c)
        if c in fof_codes:
            fof_at_ed.add(c)
        incl[ind].append(float(w))
        if c not in fof_codes:
            excl[ind].append(float(w))
    # 全池均值：分母是该报告期全部基金（未配置该行业按 0 计入），
    # 不是「只持有该行业」的条件均值 —— 后者对港股这类品种会虚高到 25%。
    den_in = max(len(funds_all), 1)
    den_ex = max(len(funds_all) - len(fof_at_ed), 1)
    top = sorted(incl, key=lambda k: -sum(incl[k]) / den_in)[:8]
    impact = [
        {"industry": k,
         "with_fof": round(sum(incl[k]) / den_in, 2),
         "without_fof": round(sum(excl[k]) / den_ex, 2),
         "delta": round(sum(excl[k]) / den_ex - sum(incl[k]) / den_in, 2)}
        for k in top
    ]

    summary = {
        "ok": True,
        "n_fof": len(rows),
        "n_fof_sections": sum(r[4] for r in rows),
        "last_end_date": last_ed,
        "fof": {
            "reg_equity_median": med(fof_reg), "disclosed_cover_median": med(fof_disc),
            "n_sections": len(fof_reg),
        },
        "non_fof": {
            "reg_equity_median": med(non_reg), "disclosed_cover_median": med(non_disc),
            "n_sections": len(non_reg),
        },
        "unpenetrated_upper_median": med([r[9] for r in rows if r[9] is not None]),
        "unpenetrated_note": (
            "上界 = 100% − 直接 A 股仓位 − 港股仓位，含被投基金 + 债券 + 现金 + 其他资产；"
            "债券与现金本就无行业属性，因此这个数是「无法用现有数据源穿透的资产比例上界」，"
            "不等于「被投基金占比」。"
        ),
        "industry_mean_note": (
            "全池均值口径：分母为该报告期全部基金，未配置该行业的基金按 0 计入。"
        ),
        "industry_impact_excluding_fof": impact,
        "verdict": (
            "现有数据源无法做 FOF 多层嵌套穿透：Tushare fund_portfolio 与天天基金 "
            "F10 jjcc 对 FOF 均只返回直接持有的股票，不返回被投基金。"
            "样本中的 FOF 持仓明细 0 行被投基金代码，被投基金层是黑箱。"
        ),
        "recommendation": (
            "① 行业配置/超配的全市场均值建议排除这 %d 只 FOF；"
            "② 单基金页面对 FOF 打「穿透不适用」标记，直接股票仓位之外的部分不推断行业；"
            "③ 真要多层穿透需 Wind AMS / 聚源，或解析基金定期报告 PDF 的"
            "「基金投资明细」章节，再按被投基金报告期做无前视递归展开。" % len(rows)
        ),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    con.execute("INSERT OR REPLACE INTO fund_fof_summary VALUES (?,?,?)",
                ("main", json.dumps(summary, ensure_ascii=False), summary["updated_at"]))
    con.commit()
    con.close()

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"FOF A股仓位中位 {summary['fof']['reg_equity_median']}% vs "
          f"非FOF {summary['non_fof']['reg_equity_median']}%")
    print(f"未穿透部分上界中位 {summary['unpenetrated_upper_median']}%")
    print("排除 FOF 前后行业均值 Top8:", impact[:4])


if __name__ == "__main__":
    main()
