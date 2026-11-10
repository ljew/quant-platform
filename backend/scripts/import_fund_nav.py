#!/usr/bin/env python3
"""把 nav_raw.csv 导入独立研究库 fund_nav_daily。

净值拉取脚本产出的是流式 CSV（只往后追加）；入库后回归脚本与 API 共用同一个
数据源，避免临时文件与库内数据漂移。

**增量模式（默认）**：用字节偏移记住上次导入到的位置，只解析新增行，
`INSERT OR REPLACE` 保证幂等。CSV 已累积 1164 万行、306MB，全量重建实测 273 秒，
而日更实际只增加几千行 —— 增量把这一环压到 1 秒级，顺带把「跑一半被容器重启
打断」的窗口从 4.5 分钟缩到几乎为零。

**全量模式（`--full`）**：清空重建。CSV 被重写（文件变小，偏移失效）或库表需要
修复时用；偏移文件不存在时也会自动走这条。

偏移以**字节**而不是行数记录：文本文件按行 seek 需要从头扫，等于没省。
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "fund_research.db"
CSV = ROOT / "data" / "research" / "nav_raw.csv"
OFFSET = ROOT / "data" / "research" / "nav_import.offset"

BATCH = 20000

# 畸形行防守：偏移落在行中间时 csv.reader 会把残片当一条记录
# （实测截断出过 ('1.OF','20261109',1.729)），必须按格式剔掉，
# 否则脏数据会一路进到净值回归里。
_CODE = re.compile(r"^\d{6}\.(OF|SH|SZ|BJ)$")
_DATE = re.compile(r"^\d{8}$")


def read_offset() -> int:
    """上次导入到的字节位置；-1 表示没有基线（首次/被删除）。"""
    try:
        return int(OFFSET.read_text(encoding="utf-8").strip())
    except Exception:  # noqa: BLE001
        return -1


def write_offset(pos: int) -> None:
    tmp = OFFSET.with_suffix(".tmp")
    tmp.write_text(str(pos), encoding="utf-8")
    tmp.replace(OFFSET)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true",
                    help="清空重建（CSV 被重写或库表修复时用）")
    args = ap.parse_args()

    if not CSV.exists():
        raise SystemExit(f"净值文件不存在：{CSV}")

    size = CSV.stat().st_size
    base = read_offset()
    # 偏移比文件还大 → CSV 被重写过（全量拉取重建），旧基线失效，必须全量。
    full = args.full or base < 0 or base > size
    start = 0 if full else base

    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    if full:
        con.execute("DELETE FROM fund_nav_daily")
        con.commit()

    started, n, batch, pos = time.time(), 0, [], start
    with open(CSV, encoding="utf-8", newline="") as f:
        if start:
            f.seek(start)
            if not full:
                # 偏移必须落在行首。切在行中间时丢掉这段残片，
                # 否则它以合法三元组的样子混进库（已踩过一次）。
                f.seek(start - 1)
                if f.read(1) != "\n":
                    f.readline()
        reader = csv.reader(f)
        if full:
            next(reader, None)              # 跳表头
        for r in reader:
            if len(r) != 3 or not _CODE.match(r[0]) or not _DATE.match(r[1]):
                continue
            try:
                v = float(r[2])
            except (ValueError, TypeError):
                continue
            if v <= 0:
                continue
            batch.append((r[0], r[1], v))
            if len(batch) >= BATCH:
                # 先在提交前取位置：这批已读完，指针正落在下一行开头。
                pos = f.tell()
                con.executemany("INSERT OR REPLACE INTO fund_nav_daily VALUES (?,?,?)", batch)
                n += len(batch)
                batch = []
                if full:
                    con.commit()
                    print(f"  {n:,} 行  {time.time()-started:.0f}s", flush=True)
        pos = f.tell()
    if batch:
        con.executemany("INSERT OR REPLACE INTO fund_nav_daily VALUES (?,?,?)", batch)
        n += len(batch)
    con.commit()

    # 统计按模式分档：全量才做全表 COUNT（1164 万行一次约 8s），
    # 增量每天只进几千行，没必要为了打印一行日志扫全表。
    rng = con.execute("SELECT MIN(nav_date),MAX(nav_date) FROM fund_nav_daily").fetchone()
    if full:
        total = con.execute("SELECT COUNT(*) FROM fund_nav_daily").fetchone()[0]
        funds = con.execute("SELECT COUNT(DISTINCT ts_code) FROM fund_nav_daily").fetchone()[0]
        stat = f"库内 {total:,} 行 / {funds:,} 只基金 / "
    else:
        stat = ""
    con.close()

    write_offset(pos)
    mode = "全量重建" if full else f"增量（偏移 {start:,}→{pos:,}）"
    print(f"导入完成[{mode}] 新增 {n:,} 行 / {stat}{rng[0]}~{rng[1]} / "
          f"耗时 {time.time()-started:.1f}s")


if __name__ == "__main__":
    main()
