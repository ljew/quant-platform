#!/usr/bin/env python3
"""拉取申万 2021 一级行业指数日线，作为净值回归的行业收益因子。

数据源：Tushare index_classify(level='L1', src='SW2021') 取 31 个一级行业代码，
        sw_daily 逐代码拉日线，pct_change 即行业日收益率（单位 %）。

实测：31/31 全部可用，每个 2877 行，2015-01-05 ~ 2026-11-06，合计 89187 行。
覆盖范围与持仓数据（2015Q1-2025Q4）完全对齐，不需要自造行业指数。

输出：data/research/sw_industry_daily.csv
"""
from __future__ import annotations

import os
import time
from pathlib import Path

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import pandas as pd  # noqa: E402
import tushare as ts  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "research" / "sw_industry_daily.csv"
TOKEN = os.getenv(
    "TUSHARE_TOKEN",
    "d2684763c95c5f46e0cf65dee253d0559ad6ed2ec0b05b12ae557e99",
)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    pro = ts.pro_api(TOKEN)
    l1 = pro.index_classify(level="L1", src="SW2021")
    frames = []
    for code, name in zip(l1.index_code, l1.industry_name):
        for attempt in range(3):
            try:
                d = pro.sw_daily(ts_code=code, start_date="20150101", end_date="20261231",
                                 fields="ts_code,trade_date,pct_change,close")
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  {code} {name} FAIL {str(e)[:80]}")
                    d = None
                time.sleep(2 ** attempt)
        if d is None or not len(d):
            continue
        d["industry"] = name
        frames.append(d[["industry", "trade_date", "pct_change", "close"]])
        time.sleep(0.12)
    all_d = pd.concat(frames, ignore_index=True)
    all_d["pct_change"] = pd.to_numeric(all_d["pct_change"], errors="coerce")
    all_d = all_d.dropna(subset=["pct_change"])
    all_d.to_csv(OUT, index=False)
    print(f"申万一级行业指数：{len(all_d):,} 行 / {all_d.industry.nunique()} 个行业 / "
          f"{all_d.trade_date.min()} ~ {all_d.trade_date.max()}")
    print(f"已存 {OUT}")


if __name__ == "__main__":
    main()
