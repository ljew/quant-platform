#!/usr/bin/env python3
"""按 ts_code 分片拉取主动权益基金全报告期持仓，绕开 Tushare 的 offset 硬截断。

背景
----
fm_analysis/fetch_portfolio.py 按 period 全市场翻页，`off > 400000` 会被接口拒掉，
2020-2025 每年只拿到恰好 416000 行（52 页 × 8000，最后一页仍是满页），
每只基金 stk_mkv_ratio 权重和只有约 48%。
这不是「季报只披露前十大」，而是**数据被截断**：2015-2019 未截断年份权重和是 100%。

逐只基金调用 fund_portfolio(ts_code=...) 可一次拿到该基金全部报告期且持仓完整
（实测 000001.OF / 20211231 返回 142 行、权重和 99.95%，报告期覆盖到 20260930）。

范围与口径
----------
- 基金池：fund_master 中 group_tag='active' AND std_ret>=8（7602 只，与既有研究口径一致）
- 报告期：end_date 落在 20150101-20251231；ann_date 一并保留，供无前视校验
- 不做任何估算、不补权重，只忠实落盘原始披露值

用法（宿主机，需 tushare + pandas）
----------------------------------
  python backend/scripts/fetch_fund_holdings.py --limit 200   # 小样本先跑通链路
  python backend/scripts/fetch_fund_holdings.py               # 全量（自动跳过已完成）
  python backend/scripts/fetch_fund_holdings.py --status      # 查看进度

增量补抓（季报窗口过后只补最新期次，不动历史）
----------------------------------------------
  python backend/scripts/fetch_fund_holdings.py \
      --since 20251001 --until 20261231 \
      --out holdings_2026.csv --reset-done

  为什么需要：旧版本在过滤时把上界硬编码成 20251231，导致 2026 年的
  季报/中报永远抓不到；而 progress.json 记录的是「这只基金抓过」，
  不清空就不会重抓。--reset-done 只影响本次运行的内存视图，
  不会删掉历史 CSV。

输出（data/holdings/）
---------------------
  holdings_raw.csv   逐只追加的明细，表头固定 8 列
  progress.json      已完成的 ts_code（断点续传依据）
  fetch.log          日志
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# Tushare 必须直连；本机代理会让它超时
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import tushare as ts  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "holdings"
DB = ROOT / "data" / "quant_dev.db"
RESEARCH_DB = ROOT / "data" / "fund_research.db"
RAW = DATA / "holdings_raw.csv"
PROGRESS = DATA / "progress.json"
LOG = DATA / "fetch.log"
LOCK = DATA / "fetch.lock"

# 抓取窗口：Tushare fund_portfolio 单次返回该基金全部报告期，
# 这里只是把不需要的期次过滤掉，减少 CSV 体积。
SINCE = "20150101"
UNTIL = "20261231"

TOKEN = os.getenv(
    "TUSHARE_TOKEN",
    "d2684763c95c5f46e0cf65dee253d0559ad6ed2ec0b05b12ae557e99",
)
FIELDS = [
    "ts_code", "ann_date", "end_date", "symbol",
    "mkv", "amount", "stk_mkv_ratio", "stk_float_ratio",
]
BATCH_SLEEP = 0.15          # 基础节流，避免撞每分钟限频
RATE_LIMIT_WAIT = 45        # 撞限频后的等待秒数


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fund_codes() -> list[str]:
    """抓取池 = 主库活跃高流动性池 ∪ 独立库里链路上真正用得上的基金。

    为什么取并集而不是「主库优先」：FOF 定期报告披露的底层基金里，有相当一部分
    （ETF、指数基金、老基金）不在主库的 group_tag='active' AND std_ret>=8 池内。
    只按主库池抓，这些底层基金永远补不上，FOF 穿透率就卡在 25% 上不去。
    两侧取并集后约 1.2 万只，仍是可接受的抓取量。

    主库不可用时（历史真损坏过：2026-11-08 malformed）只用独立库那一侧，
    基金链路不至于因为主库挂掉整条停摆。
    """
    codes: set[str] = set()
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            codes.update(r[0] for r in con.execute(
                "SELECT ts_code FROM fund_master "
                "WHERE group_tag='active' AND std_ret>=8 AND ts_code LIKE '%.OF'"
            ))
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        log(f"主库不可读，仅用独立库取抓取池：{DB}")

    con = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
    try:
        # 只取「链路上真用得上的」两类，不要全量 fund_master_lite——
        # 全量 2 万只里大半是债基/货基，对本链路零贡献却要各花一次 API 调用。
        #   fund_hold_coverage.base_code：历史上抓过、属于原抓取池的基金
        #   fund_fof_holdings.sub_code：FOF 定期报告披露的底层基金（穿透必需）
        codes.update(r[0] for r in con.execute(
            "SELECT DISTINCT base_code||'.OF' FROM fund_hold_coverage "
            "WHERE base_code IS NOT NULL AND length(base_code)=6 "
            "UNION "
            "SELECT DISTINCT sub_code||'.OF' FROM fund_fof_holdings "
            "WHERE sub_code IS NOT NULL AND length(sub_code)=6"
        ))
    finally:
        con.close()
    return sorted(codes)


def load_progress() -> tuple[set[str], list[str]]:
    if PROGRESS.exists():
        obj = json.loads(PROGRESS.read_text(encoding="utf-8"))
        return set(obj.get("done", [])), obj.get("failed", [])
    return set(), []


def save_progress(done: set[str], failed: list[str]) -> None:
    tmp = PROGRESS.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"done": sorted(done), "failed": failed,
                    "updated_at": datetime.now().isoformat(timespec="seconds")},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(PROGRESS)


def acquire_lock() -> None:
    """防止两个实例同时追加同一个 CSV。"""
    if LOCK.exists():
        age = time.time() - LOCK.stat().st_mtime
        if age < 3600:
            raise SystemExit(f"已有实例在跑（锁文件 {age:.0f}s 前更新），退出：{LOCK}")
        log("发现过期锁文件，接管")
    LOCK.write_text(str(os.getpid()), encoding="utf-8")


def main() -> None:
    global RAW, PROGRESS, LOCK, SINCE, UNTIL

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="本次最多拉多少只（0=全量）")
    ap.add_argument("--status", action="store_true", help="只看进度")
    ap.add_argument("--since", default=SINCE, help=f"报告期下界（默认 {SINCE}）")
    ap.add_argument("--until", default=UNTIL, help=f"报告期上界（默认 {UNTIL}）")
    ap.add_argument("--out", default="", help="输出文件名（默认 holdings_raw.csv）")
    ap.add_argument("--reset-done", action="store_true",
                    help="忽略 progress 里已完成的基金，全部重拉（用于补抓新期次）")
    args = ap.parse_args()

    SINCE, UNTIL = args.since, args.until
    if args.out:
        RAW = DATA / args.out
        # 进度/锁按输出文件隔离，否则补抓会篡改主进度
        PROGRESS = RAW.with_suffix(".progress.json")
        LOCK = RAW.with_suffix(".lock")

    DATA.mkdir(parents=True, exist_ok=True)
    codes = fund_codes()
    done, failed = load_progress()
    if args.reset_done:
        done = set()

    if args.status:
        n_rows = sum(1 for _ in open(RAW, encoding="utf-8")) - 1 if RAW.exists() else 0
        print(f"基金池 {len(codes)} / 已完成 {len(done)} / 失败 {len(failed)} / 明细行 {n_rows}"
              f" / 窗口 {SINCE}~{UNTIL} / 输出 {RAW.name}")
        return

    acquire_lock()
    pro = ts.pro_api(TOKEN)
    todo = [c for c in codes if c not in done]
    if args.limit:
        todo = todo[: args.limit]
    log(f"基金池 {len(codes)}，已完成 {len(done)}，本次待拉 {len(todo)}")

    # 首次创建带表头的 CSV
    if not RAW.exists():
        with open(RAW, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(FIELDS)

    fout = open(RAW, "a", newline="", encoding="utf-8")
    writer = csv.writer(fout)
    started = time.time()
    n_ok = 0
    n_rows = 0
    try:
        for i, code in enumerate(todo, 1):
            df = None
            for attempt in range(4):
                try:
                    df = pro.fund_portfolio(
                        ts_code=code, start_date=SINCE, end_date=UNTIL
                    )
                    break
                except Exception as e:
                    msg = str(e)
                    wait = RATE_LIMIT_WAIT if ("每分钟" in msg or "频率" in msg or "限制" in msg) else 2 ** attempt
                    log(f"  {code} 第{attempt+1}次失败，{wait}s 后重试：{msg[:90]}")
                    time.sleep(wait)
            if df is None:
                failed.append(code)
                log(f"  {code} 最终失败，记入 failed")
                continue

            if len(df):
                df = df[(df.end_date >= SINCE) & (df.end_date <= UNTIL)]
                if len(df):
                    rows = df.reindex(columns=FIELDS).itertuples(index=False, name=None)
                    writer.writerows(rows)
                    n_rows += len(df)
            done.add(code)
            n_ok += 1
            LOCK.write_text(str(os.getpid()), encoding="utf-8")  # 心跳

            if n_ok % 50 == 0:
                fout.flush()
                save_progress(done, failed)
                elapsed = time.time() - started
                rate = n_ok / elapsed if elapsed else 0
                remain = len(codes) - len(done)
                log(f"进度 {len(done)}/{len(codes)} | 本次 {n_ok} 只 {n_rows} 行 | "
                    f"{rate:.1f} 只/秒 | 预计剩余 {remain/rate/60:.1f} 分钟" if rate else
                    f"进度 {len(done)}/{len(codes)}")
            time.sleep(BATCH_SLEEP)
    except KeyboardInterrupt:
        log("收到中断，保存进度后退出")
    finally:
        fout.flush()
        fout.close()
        save_progress(done, failed)
        LOCK.unlink(missing_ok=True)
        log(f"结束：本次 {n_ok} 只 / {n_rows} 行，累计完成 {len(done)}/{len(codes)}，失败 {len(failed)}")


if __name__ == "__main__":
    main()
