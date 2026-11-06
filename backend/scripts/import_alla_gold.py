#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全A 历史数据导入（Gold 层）—— 把 Bronze 灌进 SQLite，再同步 DuckDB。

一次性建仓脚本。做完之后 quant 具备：
  · 全A 5553 只（含 334 只已退市）2019-2026 完整日线，未复权原始价
  · adj_factor_daily 全历史复权因子（回测取价用）
  · financials_raw 财务原始三表（现金流/资本开支/总资产/ROE，支撑 FCF/OE 因子）
  · stocks 补齐 list_date / delist_date / list_status（PIT 成分重建的依据）
  · index_membership 新增 index_code='ALLA' 的逐月全A 成分快照

运行：
  cd backend && PYTHONPATH=. python scripts/import_alla_gold.py

注意：会**重建 kline_daily**（原表存的是核心池前复权价，口径不同不能混存）。
重建前自动备份到 data/backup/。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime

import pandas as pd

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
DATA = os.path.join(ROOT, "data")
SQLITE_DB = os.path.join(DATA, "quant_dev.db")
MARKET = os.path.join(DATA, "raw", "market")
ALLA_DIR = os.path.join(MARKET, "bars_daily_alla")
BASIC_PARQUET = os.path.join(MARKET, "stock_basic_all.parquet")
BACKUP_DIR = os.path.join(DATA, "backup")

# 上一轮研究阶段已拉好的数据（复权因子 / 财务三类），搬运进来保持 quant 自洽
LEGACY_CACHE = "/Users/happyljew/WorkBuddy/2026-09-14-13-55-42/backtest/cache_alla"

MIN_LIST_DAYS = 250   # 上市满 250 自然日才纳入池子（与策略口径一致）


def plat(ts_code: str) -> str:
    """tushare 代码 → 平台代码。600519.SH → sh600519"""
    code, ex = str(ts_code).split(".")
    return ex.lower() + code


def drop_bj(df: pd.DataFrame) -> pd.DataFrame:
    """剔除北交所（.BJ）—— 与 Bronze/Silver 口径一致，不纳入策略池。

    ⚠️ 上游 parquet 是按 trade_date 拉的全市场数据（bars_*.parquet /
    adj_all.parquet 等），**含北交所**；fetch_universe 只过滤了成分表。
    这里再加一道防御，保证即使上游过滤失效，重跑导入也不会把 bj 带回库里
    （2026-09-18 定论：北交所不纳入策略池）。
    """
    if "ts_code" not in df.columns:
        return df
    m = ~df.ts_code.astype(str).str.endswith(".BJ")
    n = int((~m).sum())
    if n:
        log("   [filter] 剔除北交所 %d 行" % n)
    return df[m].copy()


def ymdd(s: str) -> str:
    """20190102 → 2019-01-02"""
    s = str(s)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


def log(msg: str):
    print(msg, flush=True)


# ============================ 1. 备份 ============================
def step_backup(con: sqlite3.Connection):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    n = con.execute("select count(*) from kline_daily").fetchone()[0]
    if n == 0:
        log("[备份] kline_daily 为空，跳过")
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(BACKUP_DIR, f"kline_daily_qfq_{ts}.parquet")
    log(f"[备份] kline_daily {n:,} 行 → {os.path.basename(out)}")
    df = pd.read_sql_query("select * from kline_daily", con)
    df.to_parquet(out, index=False)
    log(f"[备份] 完成 {len(df):,} 行")


# ============================ 2. schema 升级 ============================
def step_schema(con: sqlite3.Connection):
    cols = {r[1] for r in con.execute("PRAGMA table_info(stocks)")}
    for col, decl in (("delist_date", "DATE"), ("list_status", "VARCHAR(2)")):
        if col not in cols:
            con.execute(f"ALTER TABLE stocks ADD COLUMN {col} {decl}")
            log(f"[schema] stocks 新增列 {col}")
        else:
            log(f"[schema] stocks.{col} 已存在")
    con.commit()

    # financials_raw 交给 SQLAlchemy 建（保证与 models 一致）
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    from app.database import Base, engine
    from app.models import FinancialsRaw, KlineDaily

    Base.metadata.create_all(engine, tables=[FinancialsRaw.__table__])
    log("[schema] financials_raw 就绪")
    return KlineDaily


# ============================ 3. 标的生命周期 ============================
def step_stocks(con: sqlite3.Connection):
    sb = drop_bj(pd.read_parquet(BASIC_PARQUET))
    log(f"[stocks] 源 {len(sb)} 只（上市 {(sb.list_status=='L').sum()} / "
        f"退市 {(sb.list_status=='D').sum()}）")
    rows, n_new, n_upd = [], 0, 0
    exist = {r[0] for r in con.execute("select symbol from stocks")}
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for r in sb.itertuples(index=False):
        sym = plat(r.ts_code)
        ld = ymdd(r.list_date) if pd.notna(r.list_date) and str(r.list_date) != "nan" else None
        dd = (ymdd(r.delist_date)
              if hasattr(r, "delist_date") and pd.notna(r.delist_date)
              and str(r.delist_date) != "nan" else None)
        ind = r.industry if hasattr(r, "industry") and pd.notna(r.industry) else None
        if sym in exist:
            con.execute("update stocks set name=?, list_date=coalesce(?,list_date), "
                        "delist_date=?, list_status=?, industry=coalesce(?,industry) "
                        "where symbol=?", (r.name, ld, dd, r.list_status, ind, sym))
            n_upd += 1
        else:
            con.execute("insert into stocks (symbol,name,market,raw_code,industry,"
                        "list_date,delist_date,list_status,updated_at) "
                        "values (?,?,?,?,?,?,?,?,?)",
                        (sym, r.name, sym[:2], sym[2:], ind, ld, dd, r.list_status, now))
            n_new += 1
    con.commit()
    log(f"[stocks] 新增 {n_new} 只 / 更新 {n_upd} 只")


# ============================ 4. 日线（未复权，全量替换）============================
def step_kline(con: sqlite3.Connection, KlineDaily):
    files = sorted(f for f in os.listdir(ALLA_DIR) if f.startswith("bars_") and f.endswith(".parquet"))
    if not files:
        raise RuntimeError(f"未找到日线分片：{ALLA_DIR}")
    log(f"[kline] 源 {len(files)} 个年度分片")

    from app.database import engine

    con.execute("DROP TABLE IF EXISTS kline_daily")
    con.commit()
    # 用 SQLAlchemy 建表，保证与 models 定义严格一致（手写 DDL 易漂移）
    KlineDaily.__table__.create(engine)
    log("[kline] 表已重建（未复权口径）")

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    sql = ("insert or replace into kline_daily "
           "(symbol,trade_date,open,high,low,close,volume,amount,adj,created_at) "
           "values (?,?,?,?,?,?,?,?,?,?)")
    total = 0
    t0 = time.time()
    for f in files:
        df = pd.read_parquet(os.path.join(ALLA_DIR, f))
        df = drop_bj(df)
        df = df.dropna(subset=["open", "high", "low", "close"])
        buf = [
            (plat(r.ts_code), ymdd(r.trade_date), float(r.open), float(r.high),
             float(r.low), float(r.close), int(r.vol or 0), float(r.amount or 0.0),
             "none", now)
            for r in df.itertuples(index=False)
        ]
        for i in range(0, len(buf), 50000):
            con.executemany(sql, buf[i:i + 50000])
        con.commit()
        total += len(buf)
        log(f"[kline] {f}  {len(buf):>9,} 行  累计 {total:,}")
    log(f"[kline] 完成 {total:,} 行，{time.time()-t0:.0f}s")
    con.execute("CREATE INDEX IF NOT EXISTS ix_kline_symbol_date ON kline_daily(symbol, trade_date)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_kline_date ON kline_daily(trade_date)")
    con.commit()
    log("[kline] 索引就绪")
    return total


# ============================ 5. 复权因子 ============================
def step_adj(con: sqlite3.Connection):
    src = os.path.join(LEGACY_CACHE, "adj_all.parquet")
    if not os.path.exists(src):
        log(f"[adj] 源不存在，跳过：{src}")
        return 0
    log(f"[adj] 读 {os.path.basename(src)}")
    df = pd.read_parquet(src)
    df = drop_bj(df)
    df = df[df.adj_factor.notna()]
    log(f"[adj] {len(df):,} 行 × {df.ts_code.nunique()} 只")
    con.execute("DELETE FROM adj_factor_daily")
    con.commit()
    sql = ("insert or replace into adj_factor_daily (symbol,trade_date,adj_factor) "
           "values (?,?,?)")
    buf = [(plat(r.ts_code), ymdd(r.trade_date), float(r.adj_factor))
           for r in df.itertuples(index=False)]
    for i in range(0, len(buf), 50000):
        con.executemany(sql, buf[i:i + 50000])
    con.commit()
    con.execute("CREATE INDEX IF NOT EXISTS ix_adj_symbol_date ON adj_factor_daily(symbol, trade_date)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_adj_date ON adj_factor_daily(trade_date)")
    con.commit()
    log(f"[adj] 完成 {len(buf):,} 行")
    return len(buf)


# ============================ 6. 财务原始三表 ============================
def step_financials(con: sqlite3.Connection):
    paths = {k: os.path.join(LEGACY_CACHE, f"{k}_raw.parquet")
             for k in ("roe", "cashflow", "balancesheet")}
    miss = [k for k, p in paths.items() if not os.path.exists(p)]
    if miss:
        log(f"[financials] 缺文件 {miss}，跳过（等取数完成后再跑此步）")
        return 0

    roe = drop_bj(pd.read_parquet(paths["roe"]))
    cf = drop_bj(pd.read_parquet(paths["cashflow"]))
    bs = drop_bj(pd.read_parquet(paths["balancesheet"]))
    log(f"[financials] roe {len(roe):,} / cf {len(cf):,} / bs {len(bs):,}")

    for d in (roe, cf, bs):
        d["ts_code"] = d["ts_code"].astype(str)
        d["end_date"] = d["end_date"].astype(str)
        d["ann_date"] = d["ann_date"].astype(str)

    # 主键 (ts_code, end_date)：一期一记录；ann_date 取该期最新公告
    def uniq(d, cols):
        d = d.dropna(subset=["end_date", "ann_date"]).copy()
        d = d[d.ann_date.str.len() == 8]
        return d.sort_values("ann_date").drop_duplicates(
            subset=["ts_code", "end_date"], keep="last")

    roe = uniq(roe[["ts_code", "end_date", "ann_date", "roe"]], None)
    cf = uniq(cf[["ts_code", "end_date", "ann_date", "n_cashflow_act",
                  "c_pay_acq_const_fiolta"]], None)
    bs = uniq(bs[["ts_code", "end_date", "ann_date", "total_assets"]], None)

    m = cf.merge(bs, on=["ts_code", "end_date"], how="outer",
                 suffixes=("", "_bs"))
    # ann_date 可能来自任一侧，取较晚的一个（该期信息齐备的时点）
    m["ann_date"] = m[["ann_date", "ann_date_bs"]].max(axis=1)
    m = m.drop(columns=["ann_date_bs"])
    m = m.merge(roe[["ts_code", "end_date", "roe"]], on=["ts_code", "end_date"], how="outer")
    m["capex"] = m["c_pay_acq_const_fiolta"].abs()
    m = m.dropna(subset=["ann_date"])
    log(f"[financials] 合并后 {len(m):,} 行 × {m.ts_code.nunique()} 只")

    con.execute("DELETE FROM financials_raw")
    con.commit()
    sql = ("insert or replace into financials_raw "
           "(symbol,end_date,ann_date,roe,n_cashflow_act,capex,total_assets) "
           "values (?,?,?,?,?,?,?)")

    def num(v):
        return None if pd.isna(v) else float(v)

    buf = [(plat(r.ts_code), ymdd(r.end_date), ymdd(r.ann_date), num(r.roe),
            num(r.n_cashflow_act), num(r.capex), num(r.total_assets))
           for r in m.itertuples(index=False)]
    for i in range(0, len(buf), 50000):
        con.executemany(sql, buf[i:i + 50000])
    con.commit()
    con.execute("CREATE INDEX IF NOT EXISTS ix_fin_symbol_ann ON financials_raw(symbol, ann_date)")
    con.commit()
    log(f"[financials] 完成 {len(buf):,} 行")
    return len(buf)


# ============================ 7. 全A PIT 成分快照 ============================
def step_alla_cons(con: sqlite3.Connection):
    """全A 没有官方成分快照，必须用 list_date/delist_date 逐月重建。

    否则 2019 年的池子会包含 2023 年才上市的公司 —— 这是幸存者偏差的典型来源。
    """
    sb = pd.read_parquet(BASIC_PARQUET)
    dates = [r[0] for r in con.execute(
        "select distinct trade_date from kline_daily order by trade_date")]
    if not dates:
        log("[ALLA] kline_daily 为空，跳过")
        return 0
    s = pd.Series(dates)
    month_ends = s.groupby(s.str[:7]).max().tolist()

    ld = pd.to_datetime(sb["list_date"].astype(str), format="%Y%m%d", errors="coerce")
    dd = pd.to_datetime(sb["delist_date"].astype(str), format="%Y%m%d", errors="coerce")
    dd = dd.fillna(pd.Timestamp("2099-12-31"))
    eligible = ld + pd.Timedelta(days=MIN_LIST_DAYS)
    syms = sb["ts_code"].map(plat).values

    con.execute("DELETE FROM index_membership WHERE index_code='ALLA'")
    con.commit()
    sql = ("insert into index_membership (index_code,trade_date,symbol,weight) "
           "values (?,?,?,?)")
    n = 0
    for t in month_ends:
        tdt = pd.to_datetime(t)
        mask = ((eligible <= tdt) & (dd > tdt)).values
        rows = [("ALLA", t, syms[i], 1.0) for i in range(len(syms)) if mask[i]]
        if rows:
            con.executemany(sql, rows)
            n += len(rows)
    con.commit()
    log(f"[ALLA] {len(month_ends)} 期快照 / {n:,} 条成员记录")
    return n


# ============================ 8. 同步 DuckDB ============================
def step_sync():
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    from app.services.duckdb_sync import sync_after_seed
    sync_after_seed(only=["kline_daily", "stocks", "index_membership",
                          "adj_factor_daily", "financials_raw"])


# ============================ main ============================
def main():
    t0 = time.time()
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    log("=" * 78)
    log("全A 历史数据导入 → %s" % SQLITE_DB)
    log("=" * 78)

    con = sqlite3.connect(SQLITE_DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA cache_size=-200000")   # 200MB
    try:
        log("[1/8] 备份现有 kline_daily"); step_backup(con)
        log("[2/8] schema 升级");         KlineDaily = step_schema(con)
        log("[3/8] 标的生命周期");        step_stocks(con)
        log("[4/8] 日线（未复权，全量替换）"); step_kline(con, KlineDaily)
        log("[5/8] 复权因子");            step_adj(con)
        log("[6/8] 财务原始三表");        step_financials(con)
        log("[7/8] 全A PIT 成分快照");    step_alla_cons(con)
    finally:
        con.close()

    log("[8/8] 同步 DuckDB")
    try:
        step_sync()
    except Exception as e:  # noqa: BLE001
        log(f"  ! DuckDB 同步失败（SQLite 数据安全，可稍后 migrate_to_duckdb.py 补齐）: {e}")

    el = time.time() - t0
    log("=" * 78)
    log("[完成] %.0fs（%.1f 分钟）" % (el, el / 60))


if __name__ == "__main__":
    main()
