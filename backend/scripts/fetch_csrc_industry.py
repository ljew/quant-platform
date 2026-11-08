#!/usr/bin/env python3
"""拉取天天基金 F10 的「行业配置」——证监会口径的监管披露数据。

数据源与法规
------------
接口：https://api.fund.eastmoney.com/f10/HYPZ/?fundCode=&year=&callback=
      （页面 fundf10.eastmoney.com/hytz_XXXXXX.html 的异步数据源）
法规：《证券投资基金信息披露编报规则第4号》第五条 / 《内容与格式准则第2号》第三十条
      —— 定期报告必须披露「报告期末按行业分类的股票投资组合」，
      列示各行业市值及 **占基金资产净值比例** 及合计。

价值
----
季报虽只披露前十大个股，但行业配置是**按全部持仓计算的真实分布**。
其中 Σ ZJZBL = A 股股票仓位（占净值 %），可直接替换我们「按锚点继承 100% 仓位」
这个最强也最可疑的假设；门类分布本身是独立第四轨验证。

口径注意
--------
- ZJZBL 分母是**基金资产净值**，不是总资产；勿与 SAMMVPCTNV / PCTCP 混用。
- 证监会门类只有约 19 类，「制造业」常占 60%+，粒度远粗于申万一级 31 类，
  两者没有官方一一映射，本脚本只做记录，不做强制映射。
- 港股通标的不在证监会 A 股行业分类内，故 Σ ZJZBL 只代表 A 股部分。

用法
----
  python backend/scripts/fetch_csrc_industry.py --limit 100 --years 2025
  python backend/scripts/fetch_csrc_industry.py --years 2015-2025 --workers 12
  python backend/scripts/fetch_csrc_industry.py --status
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "research"
OUT = DATA / "csrc_industry_raw.csv"
PROGRESS = DATA / "progress_csrc.json"
LOG = DATA / "fetch_csrc.log"
RESEARCH_DB = ROOT / "data" / "fund_research.db"

URL = "https://api.fund.eastmoney.com/f10/HYPZ/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://fundf10.eastmoney.com/",
}
FIELDS = ["base_code", "end_date", "industry_code", "industry_name", "weight_pct", "mkv_wan"]
_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with _lock, open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fund_codes() -> list[str]:
    con = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT base_code FROM fund_hold_coverage ORDER BY base_code"
        ).fetchall()]
    finally:
        con.close()


def parse_years(s: str) -> list[int]:
    if "-" in s:
        a, b = s.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in s.split(",") if x.strip()]


def fetch_one(code: str, year: int) -> list[tuple]:
    for attempt in range(3):
        try:
            r = requests.get(URL, params={"fundCode": code, "year": year,
                                          "callback": "cb"}, headers=HEADERS, timeout=15)
            r.encoding = "utf-8"
            m = re.match(r"^cb\((.*)\)\s*$", r.text.strip(), re.S)
            if not m:
                return []
            d = json.loads(m.group(1))
            if d.get("ErrCode") not in (0, None):
                return []
            out = []
            for q in (d.get("Data") or {}).get("QuarterInfos") or []:
                ed = (q.get("JZRQ") or "").replace("-", "")
                if len(ed) != 8:
                    continue
                for x in q.get("HYPZInfo") or []:
                    try:
                        w = float(x.get("ZJZBL") or 0)
                    except (ValueError, TypeError):
                        continue
                    out.append((code, ed, x.get("HYDM", ""), x.get("HYMC", ""),
                                w, float(x.get("SZ") or 0)))
            return out
        except Exception:
            time.sleep(1.5 ** attempt)
    return []


def load_progress() -> set[str]:
    if PROGRESS.exists():
        return set(json.loads(PROGRESS.read_text(encoding="utf-8")).get("done", []))
    return set()


def save_progress(done: set[str]) -> None:
    tmp = PROGRESS.with_suffix(".tmp")
    tmp.write_text(json.dumps({"done": sorted(done),
                              "updated_at": datetime.now().isoformat(timespec="seconds")},
                             ensure_ascii=False), encoding="utf-8")
    tmp.replace(PROGRESS)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只基金")
    ap.add_argument("--years", type=str, default="2015-2025")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    codes = fund_codes()
    years = parse_years(args.years)
    done = load_progress()

    if args.status:
        n = sum(1 for _ in open(OUT, encoding="utf-8")) - 1 if OUT.exists() else 0
        print(f"基金池 {len(codes)} / 任务 {len(codes)*len(years)} / 已完成 {len(done)} / 明细行 {n}")
        return

    todo_codes = codes[:args.limit] if args.limit else codes
    tasks = [(c, y) for c in todo_codes for y in years if f"{c}:{y}" not in done]
    log(f"基金 {len(todo_codes)} / 年份 {len(years)} / 待拉 {len(tasks)} 任务 / 并发 {args.workers}")
    if not tasks:
        return

    if not OUT.exists():
        with open(OUT, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(FIELDS)

    started, n_ok, n_rows = time.time(), 0, 0
    fout = open(OUT, "a", newline="", encoding="utf-8")
    writer = csv.writer(fout)

    def work(t: tuple[str, int]) -> tuple[str, list[tuple]]:
        return f"{t[0]}:{t[1]}", fetch_one(*t)

    try:
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            for key, rows in ex.map(work, tasks):
                if rows:
                    writer.writerows(rows)
                    n_rows += len(rows)
                done.add(key)
                n_ok += 1
                if n_ok % 500 == 0:
                    fout.flush()
                    save_progress(done)
                    el = time.time() - started
                    rate = n_ok / el if el else 0
                    log(f"进度 {n_ok}/{len(tasks)} | {n_rows:,} 行 | {rate:.1f} 任务/秒 | "
                        f"剩余约 {(len(tasks)-n_ok)/max(rate,1e-6)/60:.1f} 分钟")
    finally:
        fout.flush()
        fout.close()
        save_progress(done)
    log(f"结束：{n_ok} 任务 / {n_rows:,} 行 / 耗时 {time.time()-started:.0f}s / 累计 {len(done)}")


if __name__ == "__main__":
    main()
