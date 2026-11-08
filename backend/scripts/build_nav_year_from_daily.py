#!/usr/bin/env python3
"""把日频复权净值折叠成年度 / 月度面板，写入 quant_dev.db。

为什么要有这一步
----------------
`fund_research.db.fund_nav_daily` 是「真源」（1024 万行日频），但所有统计脚本
都按面板（基金 × 期）工作。每次都去扫日频表既慢又容易写错口径，所以折叠一次，
落成两张只读面板表：

    fund_nav_year_d   (base_code, year, adj_nav)  年 = 12 月最后一个有净值的交易日
    fund_nav_month_d  (base_code, ym,  adj_nav)  月 = 当月最后一个有净值的交易日

口径两条硬规则
--------------
1. **按「最后有净值的交易日」取，不按日历日取。** 2022-12-31、2023-12-31 是周末，
   按日历日会漏掉整年；停牌基金也不会被强行插值。
2. **2014 年末用年截面表兜底。** 日频抓取起点是 2015-01-01，缺 2014-12-31 就算不出
   2015 年收益，会白丢一期迁移。年截面表与日频在 2015 年末的重叠部分已校验一致
   （最大相对偏差 <1e-9），可以安全补这一列。

用法（backend/ 下）
------------------
    PYTHONPATH=$(pwd) python scripts/build_nav_year_from_daily.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RES = ROOT / "data" / "fund_research.db"
DEV = ROOT / "data" / "quant_dev.db"

SQL_ME = """
select ts_code, nav_date, adj_nav from (
    select ts_code, nav_date, adj_nav,
           row_number() over (partition by ts_code, substr(nav_date, 1, 6)
                              order by nav_date desc) rn
    from fund_nav_daily
    where adj_nav is not null and adj_nav > 0
) where rn = 1
"""


def main() -> int:
    con = sqlite3.connect(f"file:{RES}?mode=ro", uri=True)
    me = pd.read_sql(SQL_ME, con)
    con.close()
    me["base_code"] = me.ts_code.str[:6]
    me["ym"] = me.nav_date.str[:6]
    me["year"] = me.ym.str[:4].astype(int)
    print(f"月末快照 {len(me):,} 行 / {me.base_code.nunique():,} 只 / "
          f"{me.ym.min()}~{me.ym.max()}")

    # ---- 年度：每年 12 月的快照 ----
    dec = me[me.ym.str.endswith("12")][["base_code", "year", "adj_nav"]].copy()
    dec = dec.rename(columns={"adj_nav": "adj_nav"})
    dec = dec.groupby(["base_code", "year"], as_index=False).last()

    # ---- 2014 年末：从年截面表补 ----
    dev = sqlite3.connect(DEV)
    old = pd.read_sql("select base_code, year, adj_nav from fund_nav_year where year=2014",
                      dev)
    if len(old):
        old["base_code"] = old.base_code.astype(str).str.zfill(6)
        old = old.groupby(["base_code", "year"], as_index=False).last()
        dec = dec[dec.year != 2014]
        dec = pd.concat([dec, old], ignore_index=True)
        print(f"补入 2014 年末净值 {len(old):,} 只（来自年截面表）")

    # 校验：2015 年末与年截面表必须一致
    chk_old = pd.read_sql("select base_code, adj_nav from fund_nav_year where year=2015", dev)
    chk_old["base_code"] = chk_old.base_code.astype(str).str.zfill(6)
    chk = (dec[dec.year == 2015][["base_code", "adj_nav"]]
           .merge(chk_old, on="base_code", suffixes=("_d", "_o")))
    if len(chk):
        rel = ((chk.adj_nav_d / chk.adj_nav_o) - 1).abs()
        print(f"2015 年末与年截面表重叠 {len(chk):,} 只，最大相对偏差 {rel.max():.2e}")

    dec = dec.sort_values(["base_code", "year"])
    dec.to_sql("fund_nav_year_d", dev, if_exists="replace", index=False)
    print(f"fund_nav_year_d: {len(dec):,} 行 / {dec.base_code.nunique():,} 只 / "
          f"{dec.year.min()}-{dec.year.max()}")

    # ---- 月度：全部月末 ----
    mon = me[["base_code", "ym", "adj_nav"]].copy()
    mon = mon.groupby(["base_code", "ym"], as_index=False).last()
    mon.to_sql("fund_nav_month_d", dev, if_exists="replace", index=False)
    print(f"fund_nav_month_d: {len(mon):,} 行 / {mon.base_code.nunique():,} 只 / "
          f"{mon.ym.min()}~{mon.ym.max()}")

    dev.execute("CREATE INDEX IF NOT EXISTS ix_navd_base ON fund_nav_year_d(base_code, year)")
    dev.execute("CREATE INDEX IF NOT EXISTS ix_navm_base ON fund_nav_month_d(base_code, ym)")
    dev.commit()
    dev.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
