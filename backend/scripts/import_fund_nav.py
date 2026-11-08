#!/usr/bin/env python3
"""把 nav_raw.csv 导入独立研究库 fund_nav_daily。

净值拉取脚本产出的是流式 CSV；入库后回归脚本与 API 共用同一个数据源，
避免临时文件与库内数据漂移。幂等：每次全量替换。
"""
from __future__ import annotations

import csv
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "fund_research.db"
CSV = ROOT / "data" / "research" / "nav_raw.csv"


def main() -> None:
    if not CSV.exists():
        raise SystemExit(f"净值文件不存在：{CSV}")
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("DELETE FROM fund_nav_daily")
    started, n, batch = time.time(), 0, []
    with open(CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                v = float(r["adj_nav"])
            except (ValueError, TypeError):
                continue
            if v <= 0:
                continue
            batch.append((r["ts_code"], r["nav_date"], v))
            if len(batch) >= 20000:
                con.executemany("INSERT OR REPLACE INTO fund_nav_daily VALUES (?,?,?)", batch)
                n += len(batch)
                batch = []
                if n % 1000000 < 20000:
                    con.commit()
                    print(f"  {n:,} 行  {time.time()-started:.0f}s", flush=True)
    if batch:
        con.executemany("INSERT OR REPLACE INTO fund_nav_daily VALUES (?,?,?)", batch)
        n += len(batch)
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM fund_nav_daily").fetchone()[0]
    funds = con.execute("SELECT COUNT(DISTINCT ts_code) FROM fund_nav_daily").fetchone()[0]
    rng = con.execute("SELECT MIN(nav_date),MAX(nav_date) FROM fund_nav_daily").fetchone()
    con.close()
    print(f"导入完成 {n:,} 行 / 库内 {total:,} 行 / {funds:,} 只基金 / {rng[0]}~{rng[1]} / "
          f"耗时 {time.time()-started:.0f}s")


if __name__ == "__main__":
    main()
