#!/usr/bin/env python3
"""FOF 行业穿透：FOF → 底层基金 → 行业，全链路只用披露值，不估算。

为什么单独一支脚本
------------------
主动基金的行业穿透（build_fund_penetration.py）解的是「基金 → 股票 → 行业」。
FOF 多一层：FOF 的净值 80~95% 投在**别的基金**上，它的行业敞口主要藏在基金层，
直接看 FOF 自己的股票持仓只会得到个位数占比 —— 这就是「未穿透上界 96%」的由来。

口径（三条硬约束）
------------------
1. **只用披露值**。底层基金的行业权重取 fund_industry_exposure 的 disclosed_w
   （不含 carried 影子持仓），FOF 自身的行业取同一表的 disclosed_w。
   任何一环没有披露，就记进 unresolved，绝不用行业均值或指数分布去填。

2. **不做前视**。底层基金报告期 R_i 必须满足「报告期 ≤ FOF 报告期 R」且
   「法定披露可得日 ≤ R」。可得日按监管截止日保守估计：
       0331 → 当年 04-22     （季报：季度结束后 15 个工作日）
       0630 → 当年 08-31     （中期报告）
       0930 → 当年 10-22     （季报）
       1231 → 次年 03-31     （年度报告）
   否则会出现「用 8/31 才披露的底层中报去解释 6/30 的 FOF 净值」，那是穿越。

3. **守恒可核**。每个 (FOF, 报告期) 都算
       w_direct + w_fund_layer + w_residual = 100
   其中 w_fund_layer 再拆成「已穿透 / 未穿透」。对不上就是解析或映射出了问题，
   直接记进 meta 的 residual_w 供核验，不做归一化硬凑。

未穿透的分类
------------
债基、货基、商品、QDII 是**按需求不覆盖**（老刘 2026-11-08 决策），不是数据缺陷；
只有「权益类但缺持仓」才是真缺口。两者必须分开统计，否则质量指标永远报假警。

产物
----
fund_research.db:
  fund_fof_penetration        行业级明细
  fund_fof_penetration_meta   每个 (FOF, 报告期) 的覆盖率与守恒核验

用法
----
  python backend/scripts/build_fof_penetration.py            # 全量重算
  python backend/scripts/build_fof_penetration.py --since 2025
  python backend/scripts/build_fof_penetration.py --dry-run  # 只打印不落库
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "fund_research.db"

# 法定披露可得日（月日, 跨年偏移）。保守取值，宁可少用一期也不前视。
_AVAIL = {"0331": ("0422", 0), "0630": ("0831", 0), "0930": ("1022", 0), "1231": ("0331", 1)}

# 底层基金分类关键词 —— 只用于把「未穿透」拆成「按需求不覆盖」和「真缺口」
_KW_MONEY = ("货币", "现金", "理财", "保证金")
_KW_FOREIGN = ("QDII", "全球", "海外", "美元", "纳斯达克", "标普", "恒生", "港股",
               "美国", "欧洲", "日本", "亚太", "新兴市场", "德国", "法国", "越南", "印度")
_KW_COMM = ("黄金", "白银", "商品", "原油", "豆粕", "有色", "能源化工", "农产品", "饲料")
_KW_DEBT = ("债", "信用", "利率", "短融", "国开", "政金", "城投", "转债", "可转",
            "存单", "票据", "收益")


def classify(name: str) -> str:
    """底层基金粗分类。宁可偏保守：认不出的算「权益/其他」，进真缺口统计。

    对「不覆盖」的类别误判成权益，只会让质量指标更严格（多报缺口）；
    反过来把权益误判成债券，就会掩盖真实缺口 —— 所以债券关键词取窄。
    """
    n = name or ""
    up = n.upper()
    if any(k in n for k in _KW_MONEY):
        return "money"
    if "QDII" in up or any(k in n for k in _KW_FOREIGN):
        return "foreign"
    if any(k in n for k in _KW_COMM):
        return "commodity"
    if any(k in n for k in _KW_DEBT):
        return "debt"
    return "equity"


_COVERED = ("debt", "money", "foreign", "commodity")  # 按需求不覆盖的类别


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def avail_date(end_date: str) -> str:
    """报告期 → 该期数据「最早可能被看到」的日期（YYYYMMDD）。"""
    mmdd = end_date[4:]
    if mmdd not in _AVAIL:
        return end_date
    d, yadd = _AVAIL[mmdd]
    return f"{int(end_date[:4]) + yadd}{d}"


def pick_period(periods: list[str], fof_end: str, code: str = "",
                full: set | None = None) -> tuple[str | None, bool]:
    """挑「最近且未前视」的一期，返回 (报告期, 是否完整披露)。

    完整披露（半年报/年报）优先：Q1/Q3 只披露前十大，把它的行业结构当成全量
    会系统性高估前十大所在行业。判据用 cover/监管仓位 ≥ 0.9，不能用绝对
    cover —— Tushare 的 stk_mkv_ratio 存在「占股票市值比」的量纲问题，
    绝对值和 100 比较没有意义。
    """
    idx = bisect.bisect_right(periods, fof_end)
    fallback = None
    for j in range(idx - 1, -1, -1):
        p = periods[j]
        if avail_date(p) > fof_end:
            continue
        if full is None or (code, p) in full:
            return p, True
        if fallback is None:
            fallback = p
    return (fallback, False) if fallback else (None, False)


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(
        """
CREATE TABLE IF NOT EXISTS fund_fof_penetration (
  base_code   TEXT NOT NULL,
  end_date    TEXT NOT NULL,
  report_type TEXT NOT NULL,
  industry    TEXT NOT NULL,
  direct_w    REAL DEFAULT 0,   -- FOF 直接持股，占 FOF 净值 %
  fund_w      REAL DEFAULT 0,   -- 穿透底层基金得到，占 FOF 净值 %
  total_w     REAL DEFAULT 0,
  PRIMARY KEY (base_code, end_date, report_type, industry));

CREATE TABLE IF NOT EXISTS fund_fof_penetration_meta (
  base_code       TEXT NOT NULL,
  end_date        TEXT NOT NULL,
  report_type     TEXT NOT NULL,
  ts_code         TEXT,
  name            TEXT,
  n_funds         INTEGER,      -- 底层基金只数
  w_fund_layer    REAL,         -- 基金层权重合计（占 FOF 净值 %）
  w_penetrated    REAL,         -- 已穿透到行业的基金层权重
  w_unresolved    REAL,         -- 未穿透基金层权重
  w_unres_debt    REAL,         -- 其中：债/货币/商品/QDII（按需求不覆盖）
  w_unres_equity  REAL,         -- 其中：权益类缺持仓（真缺口）
  w_direct        REAL,         -- FOF 直接持股合计
  w_total         REAL,         -- 直接 + 已穿透
  residual_w      REAL,         -- 100 − w_total（债券/现金/未披露）
  n_ind           INTEGER,
  hhi             REAL,
  top_json        TEXT,         -- 前 5 大行业
  n_funds_partial INTEGER,      -- 用不完整期次（Q1/Q3 只前十大）做穿透的只数
  leveraged       INTEGER,      -- 基金层合计 >101%，债券 FOF 可加杠杆（上限 140%）
  updated_at      TEXT,
  PRIMARY KEY (base_code, end_date, report_type));

CREATE INDEX IF NOT EXISTS ix_ffp_ind ON fund_fof_penetration(industry, end_date);
CREATE INDEX IF NOT EXISTS ix_ffp_meta_end ON fund_fof_penetration_meta(end_date);
"""
    )
    # 旧版建过的表不会因为 CREATE IF NOT EXISTS 自动补列
    cols = {r[1] for r in con.execute("PRAGMA table_info(fund_fof_penetration_meta)")}
    for _c, _t in (("n_funds_partial", "INTEGER"), ("leveraged", "INTEGER")):
        if _c not in cols:
            con.execute(f"ALTER TABLE fund_fof_penetration_meta ADD COLUMN {_c} {_t}")
    con.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="", help="只算该年起（如 2024）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    con = sqlite3.connect(DB, timeout=120)
    con.execute("PRAGMA journal_mode=WAL")
    init_db(con)

    # ---- 1. FOF 名单（先加载，供下面筛直接持股用） ----
    fof_meta = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT base_code, ts_code, name FROM fund_fof_diagnosis")}
    if not fof_meta:
        log("没有 FOF 名单（fund_fof_diagnosis 为空），退出")
        return

    # ---- 2. 一次性载入行业敞口：归一化结构 + 监管仓位锚 ----
    #       237 万行只扫一遍。分两趟读会把 2.6GB 的库读两次，白等几分钟。
    #
    #       为什么不能直接用 disclosed_w 当「占净值比」：Tushare 的 stk_mkv_ratio
    #       在「基金只持有极少数股票」时会退化成「占股票市值比」。
    #       实例 005156（短债 FOF）2021-06-30 只持中国平安一只，Tushare 给 100.0，
    #       而证监会行业配置披露的 A 股仓位是 0.33% —— 直接相乘会把 0.33% 放大成 100%。
    #       处理办法：行业**结构**完全取自披露（不估算），
    #       总量改用监管仓位 reg_equity 锚定，两者相乘得到占净值比。
    log("载入行业敞口（结构归一化 + 监管仓位锚）…")
    acc: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    wsum: dict[tuple[str, str], float] = defaultdict(float)
    for code, end, ind, w in con.execute(
        "SELECT base_code, end_date, industry, disclosed_w FROM fund_industry_exposure "
        "WHERE disclosed_w > 0"
    ):
        acc[(code, end)][ind] = w
        wsum[(code, end)] += w

    # 监管 A 股仓位（%），口径权威，作为总量锚
    reg_eq: dict[tuple[str, str], float] = {}
    for code, end, v in con.execute(
        "SELECT base_code, end_date, reg_equity FROM fund_hold_coverage "
        "WHERE reg_equity IS NOT NULL AND reg_equity > 0"
    ):
        reg_eq[(code, end)] = float(v)

    struct: dict[tuple[str, str], dict[str, float]] = {}
    base_w: dict[tuple[str, str], float] = {}
    n_clamped = 0
    for k, d in acc.items():
        tw = wsum.get(k, 0.0)
        if tw <= 0:
            continue
        struct[k] = {i: v / tw for i, v in d.items()}   # 归一化到 1 的行业结构
        rq = reg_eq.get(k)
        if rq and rq > 0:
            base_w[k] = rq
            if tw > rq * 1.5:   # 披露合计明显大于监管仓位 → 量纲问题被纠正
                n_clamped += 1
        else:
            base_w[k] = min(tw, 100.0)
    periods_of: dict[str, list[str]] = defaultdict(list)
    # 完整披露期（半年报/年报）：cover 应≈监管仓位，用相对判据避开量纲问题
    full_keys: set[tuple[str, str]] = set()
    for (code, end), tw in wsum.items():
        b = base_w.get((code, end), 0.0)
        if b > 0 and tw >= b * 0.9:
            full_keys.add((code, end))
        periods_of[code].append(end)
    for code in periods_of:
        periods_of[code].sort()
    log(f"  覆盖 {len(periods_of)} 只基金 / {len(acc)} 个(基金,期)，"
        f"其中 {n_clamped} 个按监管仓位纠正了量纲，"
        f"完整披露 {len(full_keys)} 个")

    # ---- 3. FOF 报告清单 ----
    where = "WHERE parse_status='ok'"
    params: list = []
    if args.since:
        where += " AND end_date >= ?"
        params.append(f"{args.since}0101")
    reports = con.execute(
        f"SELECT base_code, end_date, report_type FROM fund_fof_reports {where} "
        "ORDER BY base_code, end_date", params
    ).fetchall()
    log(f"待算报告 {len(reports)} 份（{len({r[0] for r in reports})} 只 FOF）")

    # ---- 4. 基金层明细 ----
    hold: dict[tuple[str, str, str], list[tuple[str, str, float]]] = defaultdict(list)
    for base, end, rt, sub, nm, w in con.execute(
        "SELECT base_code, end_date, report_type, sub_code, sub_name, weight_pct "
        "FROM fund_fof_holdings WHERE weight_pct > 0"
    ):
        hold[(base, end, rt)].append((sub, nm or "", float(w)))

    # ---- 5. 逐份计算 ----
    rows_ind: list[tuple] = []
    rows_meta: list[tuple] = []
    skipped = 0
    for base, end, rt in reports:
        subs = hold.get((base, end, rt)) or []
        if not subs:
            skipped += 1
            continue

        # FOF 自身直接持股：同一套「披露结构 × 监管仓位」
        d_key = (base, end)
        d_struct = struct.get(d_key) or {}
        d_base = base_w.get(d_key, 0.0) if d_struct else 0.0
        d_map = {i: d_base * s for i, s in d_struct.items()}
        w_direct = d_base

        agg: dict[str, float] = defaultdict(float)
        w_layer = w_pen = w_debt = w_eq = 0.0
        used = n_partial = 0
        for sub, nm, w in subs:
            w_layer += w
            per = periods_of.get(sub)
            pick, is_full = pick_period(per, end, sub, full_keys) if per else (None, False)
            st = struct.get((sub, pick)) if pick else None
            if not st:
                if classify(nm) in _COVERED:
                    w_debt += w
                else:
                    w_eq += w
                continue
            # w        = 该底层基金占 FOF 净值 %
            # base_w   = 该底层基金的股票仓位（监管口径，占其净值 %）
            # st       = 归一化行业结构（和为 1）
            scale = w / 100.0 * base_w[(sub, pick)]
            for ind, s in st.items():
                agg[ind] += s * scale
            w_pen += w
            used += 1
            if not is_full:
                n_partial += 1

        if not agg and not d_map:
            skipped += 1
            continue

        total: dict[str, float] = defaultdict(float)
        for ind, w in d_map.items():
            total[ind] += w
        for ind, w in agg.items():
            total[ind] += w

        for ind, w in total.items():
            rows_ind.append((base, end, rt, ind,
                             round(d_map.get(ind, 0.0), 4),
                             round(agg.get(ind, 0.0), 4),
                             round(w, 4)))

        w_total = w_direct + w_pen
        hhi = sum((w / 100.0) ** 2 for w in total.values()) if total else 0.0
        top = sorted(total.items(), key=lambda x: -x[1])[:5]
        ts_code, name = fof_meta.get(base, ("", ""))
        rows_meta.append((
            base, end, rt, ts_code, name, len(subs),
            round(w_layer, 2), round(w_pen, 2), round(w_layer - w_pen, 2),
            round(w_debt, 2), round(w_eq, 2),
            round(w_direct, 2), round(w_total, 2), round(100.0 - w_total, 2),
            len(total), round(hhi, 5),
            json.dumps([[i, round(v, 2)] for i, v in top], ensure_ascii=False),
            n_partial,
            1 if w_layer > 101 else 0,
            datetime.now().isoformat(timespec="seconds"),
        ))

    log(f"计算完成：{len(rows_meta)} 份报告 / {len(rows_ind)} 行行业明细（跳过 {skipped}）")

    if args.dry_run:
        for r in rows_meta[:8]:
            # 字段序：6=基金层 7=已穿透 8=未穿透 9=不覆盖类 10=权益缺口
            #        11=直接持股 12=合计 13=残余 14=行业数
            log(f"  {r[0]} {r[1]} {r[2]} 基金层={r[6]:.1f} 已穿透={r[7]:.1f} "
                f"未穿透={r[8]:.1f}（不覆盖类 {r[9]:.1f} + 权益缺口 {r[10]:.1f}）"
                f" 直接={r[11]:.1f} 合计={r[12]:.1f} 残余={r[13]:.1f} 行业={r[14]}")
        con.close()
        return

    with con:
        con.execute("DELETE FROM fund_fof_penetration")
        con.execute("DELETE FROM fund_fof_penetration_meta")
        con.executemany(
            "INSERT OR REPLACE INTO fund_fof_penetration"
            "(base_code,end_date,report_type,industry,direct_w,fund_w,total_w)"
            " VALUES (?,?,?,?,?,?,?)", rows_ind)
        con.executemany(
            "INSERT OR REPLACE INTO fund_fof_penetration_meta"
            "(base_code,end_date,report_type,ts_code,name,n_funds,w_fund_layer,"
            " w_penetrated,w_unresolved,w_unres_debt,w_unres_equity,w_direct,"
            " w_total,residual_w,n_ind,hhi,top_json,n_funds_partial,leveraged,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows_meta)

    # ---- 6. 汇总校验（字段序见上方 rows_meta 的构造顺序）----
    import statistics as st
    ratio = [r[7] / r[6] for r in rows_meta if r[6] > 5]        # 已穿透 / 基金层
    resid = [r[13] for r in rows_meta]                          # 100 − (直接+已穿透)
    eqgap = [r[10] for r in rows_meta]                          # 权益类真缺口
    debt = [r[9] for r in rows_meta]                            # 按需求不覆盖
    if ratio:
        log(f"穿透率（已穿透/基金层）中位 {st.median(ratio):.1%}，"
            f"p25/p75 {sorted(ratio)[len(ratio)//4]:.1%}/{sorted(ratio)[len(ratio)*3//4]:.1%}")
    log(f"守恒：残余权重中位 {st.median(resid):.2f}pp，"
        f"p95 {sorted(resid)[int(len(resid)*0.95)]:.2f}pp")
    log(f"未穿透拆分：不覆盖类中位 {st.median(debt):.2f}pp，"
        f"权益真缺口中位 {st.median(eqgap):.2f}pp")
    log(f"落库完成，用时 {time.time() - t0:.1f}s")
    con.close()


if __name__ == "__main__":
    main()
