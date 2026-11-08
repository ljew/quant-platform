#!/usr/bin/env python3
"""初始化独立研究库 data/fund_research.db，并从主库迁移穿透数据。

为什么拆
--------
quant_dev.db 是 bind mount，容器内 uvicorn 调度线程持续写；宿主机离线脚本
再直接写同一个文件会踩 WAL 视图错乱（docker-compose.yml 第 11 行已警告），
表现为容器侧 `database disk image is malformed`。

拆分后：
  - fund_research.db 由宿主机离线脚本**独占写**
  - 容器内 API 只用 sqlite3 mode=ro 只读
  - quant_dev.db 不再被本链路写入，并发冲突消失

表
--
  fund_hold_coverage / fund_industry_exposure / fund_stat_cache(penetration)
      从 quant_dev.db 迁移（主库旧表保留作备份，不再更新）
  fund_master_lite     主库 fund_master 的名称轻量副本，避免跨库 JOIN
  sw_industry_daily    申万一级行业指数日收益（fetch_sw_daily.py 产出）
  fund_nav_daily       基金历史日频复权净值（fetch_fund_nav_daily.py 产出，单独导入）
  fund_industry_regression / fund_regression_meta   净值回归校验结果

用法：python backend/scripts/init_fund_research_db.py   （幂等，可重复跑）
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "data" / "quant_dev.db"
RESEARCH = ROOT / "data" / "fund_research.db"
SW_CSV = ROOT / "data" / "research" / "sw_industry_daily.csv"

DDL = [
    """CREATE TABLE IF NOT EXISTS fund_hold_coverage (
        base_code TEXT NOT NULL, ts_code TEXT NOT NULL, end_date TEXT NOT NULL,
        ann_date TEXT, report_type TEXT,
        n_disclosed INTEGER, disclosed_cover REAL, is_complete INTEGER,
        anchor_end_date TEXT, anchor_ann_date TEXT, anchor_gap_days INTEGER,
        lookahead INTEGER, n_carried INTEGER, carried_cover REAL,
        confidence REAL, hhi REAL, top3_share REAL, anchor_cover REAL,
        PRIMARY KEY (ts_code, end_date))""",
    """CREATE TABLE IF NOT EXISTS fund_industry_exposure (
        base_code TEXT NOT NULL, ts_code TEXT NOT NULL, end_date TEXT NOT NULL,
        industry TEXT NOT NULL, disclosed_w REAL, carried_w REAL, total_w REAL,
        benchmark_w REAL, active_w REAL,
        PRIMARY KEY (ts_code, end_date, industry))""",
    """CREATE TABLE IF NOT EXISTS fund_stat_cache (
        key TEXT PRIMARY KEY, payload_json TEXT, updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS fund_master_lite (
        ts_code TEXT PRIMARY KEY, base_code TEXT, name TEXT)""",
    """CREATE TABLE IF NOT EXISTS sw_industry_daily (
        industry TEXT NOT NULL, trade_date TEXT NOT NULL, ret REAL, close REAL,
        PRIMARY KEY (industry, trade_date))""",
    """CREATE TABLE IF NOT EXISTS fund_nav_daily (
        ts_code TEXT NOT NULL, nav_date TEXT NOT NULL, adj_nav REAL,
        PRIMARY KEY (ts_code, nav_date))""",
    """CREATE TABLE IF NOT EXISTS fund_industry_regression (
        ts_code TEXT NOT NULL, end_date TEXT NOT NULL, industry TEXT NOT NULL,
        beta REAL, bayes_w REAL, diff REAL,
        PRIMARY KEY (ts_code, end_date, industry))""",
    """CREATE TABLE IF NOT EXISTS fund_regression_meta (
        ts_code TEXT NOT NULL, end_date TEXT NOT NULL,
        r2 REAL, n_obs INTEGER, mse REAL, lambda REAL,
        bayes_l1 REAL, spearman REAL,
        PRIMARY KEY (ts_code, end_date))""",
    # 监管披露（证监会《编报规则第4号》第五条）：季报按全部持仓计算的真实行业配置
    """CREATE TABLE IF NOT EXISTS fund_csrc_industry (
        base_code TEXT NOT NULL, end_date TEXT NOT NULL,
        industry_code TEXT NOT NULL, industry_name TEXT,
        weight_pct REAL, mkv_wan REAL,
        PRIMARY KEY (base_code, end_date, industry_code))""",
    """CREATE TABLE IF NOT EXISTS fund_csrc_equity (
        base_code TEXT NOT NULL, end_date TEXT NOT NULL,
        a_share_pct REAL, n_industries INTEGER,
        PRIMARY KEY (base_code, end_date))""",
    "CREATE INDEX IF NOT EXISTS ix_fhc_base ON fund_hold_coverage(base_code,end_date)",
    "CREATE INDEX IF NOT EXISTS ix_fie_base ON fund_industry_exposure(base_code,end_date)",
    "CREATE INDEX IF NOT EXISTS ix_fie_ind ON fund_industry_exposure(industry,end_date)",
    "CREATE INDEX IF NOT EXISTS ix_sw_date ON sw_industry_daily(trade_date)",
    "CREATE INDEX IF NOT EXISTS ix_nav_date ON fund_nav_daily(nav_date)",
]


def main() -> None:
    con = sqlite3.connect(RESEARCH, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    for ddl in DDL:
        con.execute(ddl)

    # ATTACH 主库只读搬运（ATTACH 不支持 mode=ro，但本脚本对 src 只做 SELECT）
    con.execute("ATTACH DATABASE ? AS src", (str(MAIN),))
    for t in ("fund_hold_coverage", "fund_industry_exposure"):
        con.execute(f"DELETE FROM {t}")
        n = con.execute(f"INSERT INTO {t} SELECT * FROM src.{t}").rowcount
        print(f"  迁移 {t}: {n:,} 行")
    con.execute("DELETE FROM fund_stat_cache WHERE key='penetration'")
    con.execute("INSERT INTO fund_stat_cache SELECT * FROM src.fund_stat_cache WHERE key='penetration'")
    print("  迁移 fund_stat_cache(penetration): 1 行")
    con.execute("DELETE FROM fund_master_lite")
    n = con.execute(
        "INSERT INTO fund_master_lite SELECT ts_code, base_code, name FROM src.fund_master"
    ).rowcount
    print(f"  复制 fund_master_lite: {n:,} 行")

    # 申万行业指数日收益（pct_change 单位 %，与基金收益统一为 %）
    if SW_CSV.exists():
        con.execute("DELETE FROM sw_industry_daily")
        with open(SW_CSV, newline="", encoding="utf-8") as f:
            rows = [(r["industry"], r["trade_date"], float(r["pct_change"]),
                     float(r["close"] or 0)) for r in csv.DictReader(f)]
        con.executemany("INSERT OR REPLACE INTO sw_industry_daily VALUES (?,?,?,?)", rows)
        print(f"  导入 sw_industry_daily: {len(rows):,} 行")
    # 不显式 DETACH：SELECT 事务未结束时会报 "database src is locked"，
    # 且事务回滚会把迁移结果一起丢掉；连接关闭时 SQLite 自动分离。
    con.commit()

    print("\n=== 独立库表行数 ===")
    for (t,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall():
        print(f"  {t:30s} {con.execute(f'SELECT COUNT(*) FROM \"{t}\"').fetchone()[0]:,}")
    con.close()
    print(f"\n独立库: {RESEARCH}  ({RESEARCH.stat().st_size/1024/1024:.1f} MB)")


if __name__ == "__main__":
    main()
