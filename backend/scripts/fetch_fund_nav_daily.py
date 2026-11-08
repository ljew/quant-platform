#!/usr/bin/env python3
"""拉取基金历史日频复权净值，供「净值回归校验」使用。

数据源：Tushare fund_nav，按 ts_code 逐只拉，自带 adj_nav（分红再投复权净值）。
实测 000001.OF 返回 2688 行（2015-01-05 ~ 2025-12-31），5 只约 1.4s。

只取 adj_nav，不做任何插值/补齐：净值缺失的交易日直接不落盘，
回归时按实际可用样本数决定置信度。

用法
----
  python backend/scripts/fetch_fund_nav_daily.py --limit 100   # 小样本
  python backend/scripts/fetch_fund_nav_daily.py                # 全量（断点续传）
  python backend/scripts/fetch_fund_nav_daily.py --status
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import tushare as ts  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "research"
DB = ROOT / "data" / "quant_dev.db"
RESEARCH_DB = ROOT / "data" / "fund_research.db"
RAW = DATA / "nav_raw.csv"
PROGRESS = DATA / "progress_nav.json"
PROGRESS_INC = DATA / "progress_nav_inc.json"
LOG = DATA / "fetch_nav.log"
LOCK = DATA / "fetch_nav.lock"

TOKEN = os.getenv(
    "TUSHARE_TOKEN",
    "d2684763c95c5f46e0cf65dee253d0559ad6ed2ec0b05b12ae557e99",
)
FIELDS = ["ts_code", "nav_date", "adj_nav"]
SLEEP = 0.12
RATE_WAIT = 45


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def latest_nav_date() -> str:
    """独立库里已有净值的最大交易日（YYYYMMDD）。增量抓取从这里次日开始。

    以**库**而不是 CSV 为准：CSV 可能比库新（import 没跑），
    从 CSV 推会跳过一批尚未入库的数据。
    """
    try:
        con = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True, timeout=10)
        try:
            v = con.execute("SELECT MAX(nav_date) FROM fund_nav_daily").fetchone()[0]
            return (v or "").replace("-", "")
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return ""


def next_day(yyyymmdd: str) -> str:
    from datetime import timedelta
    d = datetime.strptime(yyyymmdd, "%Y%m%d").date() + timedelta(days=1)
    return d.strftime("%Y%m%d")


def fund_codes() -> list[str]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT ts_code FROM fund_hold_coverage ORDER BY ts_code"
        ).fetchall()]
    finally:
        con.close()


def load_progress() -> tuple[set[str], list[str]]:
    if PROGRESS.exists():
        o = json.loads(PROGRESS.read_text(encoding="utf-8"))
        return set(o.get("done", [])), o.get("failed", [])
    return set(), []


def save_progress(done: set[str], failed: list[str]) -> None:
    tmp = PROGRESS.with_suffix(".tmp")
    tmp.write_text(json.dumps({"done": sorted(done), "failed": failed,
                              "updated_at": datetime.now().isoformat(timespec="seconds")},
                             ensure_ascii=False), encoding="utf-8")
    tmp.replace(PROGRESS)


def main() -> None:
    global PROGRESS

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--since", default="", help="起始日 YYYYMMDD（默认全量 20150101）")
    ap.add_argument("--until", default="", help="结束日 YYYYMMDD（默认今天）")
    ap.add_argument("--incremental", action="store_true",
                    help="日更模式：从库内最新净值次日开始抓全池，独立进度文件")
    args = ap.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)

    if args.incremental:
        PROGRESS = PROGRESS_INC
        since = next_day(latest_nav_date() or "20150101")
    else:
        since = args.since or "20150101"
    until = args.until or datetime.now().strftime("%Y%m%d")

    codes = fund_codes()
    done, failed = load_progress()
    if args.incremental:
        # 日更要的是「全池最近一段」，progress 里的 done 是历史批次的，
        # 不清空就一只都不会抓。
        done = set()

    if args.status:
        n = sum(1 for _ in open(RAW, encoding="utf-8")) - 1 if RAW.exists() else 0
        print(f"基金池 {len(codes)} / 已完成 {len(done)} / 失败 {len(failed)} / 净值行 {n}"
              f" / 窗口 {since}~{until}")
        return

    if since > until:
        log(f"无需更新：库内净值已到 {until}（窗口 {since}~{until}）")
        return

    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 3600:
        raise SystemExit(f"已有实例在跑：{LOCK}")
    LOCK.write_text(str(os.getpid()), encoding="utf-8")

    pro = ts.pro_api(TOKEN)
    todo = [c for c in codes if c not in done]
    if args.limit:
        todo = todo[:args.limit]
    log(f"基金池 {len(codes)}，已完成 {len(done)}，本次待拉 {len(todo)}，窗口 {since}~{until}")

    if not RAW.exists():
        with open(RAW, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(FIELDS)

    fout = open(RAW, "a", newline="", encoding="utf-8")
    writer = csv.writer(fout)
    started, n_ok, n_rows = time.time(), 0, 0
    try:
        for code in todo:
            df = None
            for attempt in range(4):
                try:
                    df = pro.fund_nav(ts_code=code, start_date=since,
                                      end_date=until,
                                      fields="ts_code,nav_date,adj_nav")
                    break
                except Exception as e:
                    msg = str(e)
                    wait = RATE_WAIT if ("每分钟" in msg or "频率" in msg or "限制" in msg) else 2 ** attempt
                    log(f"  {code} 第{attempt+1}次失败，{wait}s 后重试：{msg[:80]}")
                    time.sleep(wait)
            if df is None:
                failed.append(code)
                log(f"  {code} 最终失败")
                continue
            if len(df):
                df = df[df.adj_nav.notna() & (df.adj_nav > 0)]
                writer.writerows(df[FIELDS].itertuples(index=False, name=None))
                n_rows += len(df)
            done.add(code)
            n_ok += 1
            LOCK.write_text(str(os.getpid()), encoding="utf-8")
            if n_ok % 100 == 0:
                fout.flush()
                save_progress(done, failed)
                el = time.time() - started
                rate = n_ok / el if el else 0
                log(f"进度 {len(done)}/{len(codes)} | 本次 {n_rows} 行 | {rate:.1f} 只/秒 | "
                    f"剩余约 {(len(codes)-len(done))/max(rate,1e-6)/60:.1f} 分钟")
            time.sleep(SLEEP)
    except KeyboardInterrupt:
        log("收到中断，保存进度")
    finally:
        fout.flush()
        fout.close()
        save_progress(done, failed)
        LOCK.unlink(missing_ok=True)
        log(f"结束：本次 {n_ok} 只 / {n_rows} 行，累计 {len(done)}/{len(codes)}，失败 {len(failed)}")


if __name__ == "__main__":
    main()
