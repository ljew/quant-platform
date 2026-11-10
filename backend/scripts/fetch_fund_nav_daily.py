#!/usr/bin/env python3
"""拉取基金历史日频复权净值，供「净值回归校验」使用。

数据源：Tushare fund_nav，自带 adj_nav（分红再投复权净值）。

两种模式
--------
**按日（默认，日更用）**：`fund_nav(nav_date=YYYYMMDD)` 一次返回全市场当天净值，
25429 只 / 3 次分页 / 实测 1.6 秒。日更 4 天窗口约 10 秒。
原来的逐只模式要 7489 次请求 / 55 分钟，窗口太长，任何一次容器重启都会把它打断，
而被打断后状态记录还留在 running（见 fund_scheduler 的心跳自愈）。

**逐只（`--by-code`，回填历史用）**：按 ts_code 逐只拉，适合首次建库或
按日模式被限流时的降级。实测 5 只约 1.4s，全量 7489 只约 55 分钟。

只取 adj_nav，不做任何插值/补齐：净值缺失的交易日直接不落盘，
回归时按实际可用样本数决定置信度。

用法
----
  python backend/scripts/fetch_fund_nav_daily.py --incremental        # 日更（默认按日）
  python backend/scripts/fetch_fund_nav_daily.py --incremental --by-code  # 降级逐只
  python backend/scripts/fetch_fund_nav_daily.py                      # 全量回填（逐只）
  python backend/scripts/fetch_fund_nav_daily.py --status
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import tushare as ts  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tushare_token import require_token  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "research"
DB = ROOT / "data" / "quant_dev.db"
RESEARCH_DB = ROOT / "data" / "fund_research.db"
RAW = DATA / "nav_raw.csv"
PROGRESS = DATA / "progress_nav.json"
PROGRESS_INC = DATA / "progress_nav_inc.json"
LOG = DATA / "fetch_nav.log"
LOCK = DATA / "fetch_nav.lock"

# token 由 _tushare_token 统一解析（环境变量 → 项目根 .env），源码内不留明文。
FIELDS = ["ts_code", "nav_date", "adj_nav"]
SLEEP = 0.12
RATE_WAIT = 45
PAGE = 10000          # fund_nav 单次返回上限 10500（实测），留余量分页
LOOKBACK = 3          # 日更回看天数：QDII 净值 T+2 才公布，回看 3 天足够


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
    d = datetime.strptime(yyyymmdd, "%Y%m%d").date() + timedelta(days=1)
    return d.strftime("%Y%m%d")


def back_off(yyyymmdd: str, days: int) -> str:
    d = datetime.strptime(yyyymmdd, "%Y%m%d").date() - timedelta(days=days)
    return d.strftime("%Y%m%d")


def date_span(since: str, until: str) -> list[str]:
    """since~until 的每个自然日。周末也查 —— 一次 0.5s，比判断交易日简单可靠。"""
    a = datetime.strptime(since, "%Y%m%d").date()
    b = datetime.strptime(until, "%Y%m%d").date()
    out = []
    while a <= b:
        out.append(a.strftime("%Y%m%d"))
        a += timedelta(days=1)
    return out


def existing_pairs(since: str) -> dict[str, set[str]]:
    """库内 since 之后每一天已有哪些 ts_code，用于跳过重复写入。

    不比对就写：日更回看 3 天 × 池内 7489 只 = 每天往 CSV 里塞两万行重复，
    CSV 越滚越大，nav_import 每次全量扫也跟着变慢。
    """
    con = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True, timeout=10)
    try:
        out: dict[str, set[str]] = {}
        for d, code in con.execute(
            "SELECT nav_date, ts_code FROM fund_nav_daily WHERE nav_date >= ?", (since,)
        ):
            out.setdefault(str(d).replace("-", ""), set()).add(code)
        return out
    except Exception:  # noqa: BLE001
        return {}
    finally:
        con.close()


def fetch_day(pro, day: str) -> "list | None":
    """拉某一天的全市场净值（分页）。返回 (ts_code, nav_date, adj_nav) 三元组列表。

    实测单页 10000 行约 0.6s，全市场 25429 只需 3 页约 1.6s。
    返回 None 表示该日彻底失败（调用方跳过，下一轮再补）。
    """
    acc, off = [], 0
    while True:
        df = None
        for attempt in range(4):
            try:
                df = pro.query("fund_nav", nav_date=day, limit=PAGE, offset=off,
                               fields="ts_code,nav_date,adj_nav")
                break
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                wait = RATE_WAIT if ("每分钟" in msg or "频率" in msg or "限制" in msg) else 2 ** attempt
                log(f"  {day} offset={off} 第{attempt+1}次失败，{wait}s 后重试：{msg[:80]}")
                time.sleep(wait)
        if df is None:
            return None
        if not len(df):
            break
        acc.extend(df[FIELDS].itertuples(index=False, name=None))
        if len(df) < PAGE:
            break
        off += len(df)
        if off > 200000:            # 防御：接口行为异常时不要无限翻页
            log(f"  {day} 翻页超过 20 万行，强制停止")
            break
    return acc


def ensure_trailing_newline() -> None:
    """追加前确保文件以换行结尾。

    CSV 是长期追加的活文件：上一次写到一半被容器重启打断，末尾会留半行，
    后面 append 直接粘上去就成畸形记录。import 侧有格式校验兜底，
    但在源头修掉更省事。
    """
    if not RAW.exists() or RAW.stat().st_size == 0:
        return
    with open(RAW, "rb+") as f:
        f.seek(-1, os.SEEK_END)
        if f.read(1) != b"\n":
            f.write(b"\n")
            log("CSV 末尾缺换行，已补（上次写入被中断）")


def run_by_date(pro, codes: list[str], since: str, until: str) -> None:
    """日更主路径：按日拉全市场，过滤到基金池后落 CSV。"""
    wanted = set(codes)
    have = existing_pairs(since)
    days = date_span(since, until)
    log(f"按日拉取 {len(days)} 天（{since}~{until}），池内 {len(wanted)} 只，"
        f"库内已有 {sum(len(v) for v in have.values())} 条")
    ensure_trailing_newline()
    if not RAW.exists():
        with open(RAW, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(FIELDS)

    started, n_rows, n_days, n_fail = time.time(), 0, 0, 0
    with open(RAW, "a", newline="", encoding="utf-8") as fout:
        writer = csv.writer(fout)
        for d in days:
            rows = fetch_day(pro, d)
            if rows is None:
                n_fail += 1
                log(f"  {d} 拉取失败，跳过（下一轮再补）")
                continue
            if d not in have and not rows:
                log(f"  {d} 无净值（非交易日/未公布）")
                continue
            skip = have.get(d, set())
            new = [r for r in rows if r[0] in wanted and r[0] not in skip and r[2] and r[2] > 0]
            if new:
                writer.writerows(new)
                n_rows += len(new)
                n_days += 1
            log(f"  {d}: 全市场 {len(rows)} 行 → 池内新增 {len(new)} 行"
                f"（该日库内已有 {len(skip)} 只，跳过）")
            fout.flush()

    el = time.time() - started
    # 「落库 N 行」是调度器 _parse_rows 认的格式，别随手改措辞。
    log(f"结束：{n_days} 天有新增 / 落库 {n_rows} 行 / 失败 {n_fail} 天 / 用时 {el:.1f}s")
    save_progress(set(), [])


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
    ap.add_argument("--by-code", action="store_true",
                    help="逐只拉（回填历史/按日模式被限流时的降级）。不给则按日拉全市场")
    ap.add_argument("--lookback", type=int, default=LOOKBACK,
                    help=f"按日模式回看天数（默认 {LOOKBACK}，补迟公布的净值）")
    args = ap.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)

    if args.incremental:
        PROGRESS = PROGRESS_INC
        since = next_day(latest_nav_date() or "20150101")
        if not args.by_code:
            # 回看：QDII 净值 T+2 才公布，只从「次日」往后拉会永久漏掉那几只。
            # 已有数据由 existing_pairs 跳过，回看不会重复写。
            since = min(since, back_off(datetime.now().strftime("%Y%m%d"), args.lookback))
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

    pro = ts.pro_api(require_token())

    # 日更默认按日拉全市场（1.6s/天）；只有显式 --by-code 或全量回填才逐只。
    if args.incremental and not args.by_code:
        try:
            run_by_date(pro, codes, since, until)
        finally:
            LOCK.unlink(missing_ok=True)
        return
    todo = [c for c in codes if c not in done]
    if args.limit:
        todo = todo[:args.limit]
    log(f"基金池 {len(codes)}，已完成 {len(done)}，本次待拉 {len(todo)}，窗口 {since}~{until}")

    ensure_trailing_newline()
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
