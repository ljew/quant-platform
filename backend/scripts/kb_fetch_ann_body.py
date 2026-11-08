"""回补东财公告正文 → doc_document.body。

## 为什么要做

`doc_document` 里 20,619 条公告的 `body_len` 中位数只有 **43 字** ——
那是「分类|标题」的拼接，不是正文。`extract_status` 全是 `pending`。
等于库里存了两万条书名，没有书。

## 怎么拿正文

东财公告正文有独立接口（不需要爬详情页 HTML）：

    GET https://np-cnotice-stock.eastmoney.com/api/content/ann
        ?art_code=AN202611071831523200&client_source=web&page_index=1

返回 JSON，`data.notice_content` 就是正文（实测 4890 字）。
art_code 从 `doc_document.source_doc_id` 去掉 `ann_` 前缀即得。

## 两类例外

- **纯 PDF 公告**（attach_list 非空、notice_content 为空）：正文在附件里，
  这里不动，标记 `extract_status='pdf_only'`，避免以后反复重试。
- **已抽取过的**（status != pending）：跳过。

用法：
    python scripts/kb_fetch_ann_body.py --limit 200
    python scripts/kb_fetch_ann_body.py --limit 5000 --sleep 0.2
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal  # noqa: E402

API = ("https://np-cnotice-stock.eastmoney.com/api/content/ann"
       "?art_code={art}&client_source=web&page_index=1")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_RE_TAG = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"[ \t\u3000]+")


def _clean(s: str) -> str:
    """公告正文里夹着 HTML 标签和大量排版空格，清一遍再入库。"""
    s = _RE_TAG.sub("", s or "")
    s = s.replace("\r\n", "\n").replace("\xa0", " ")
    s = _RE_WS.sub(" ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def fetch_body(art_code: str, timeout: int = 20) -> tuple[str, bool]:
    """返回 (正文, 是否只有 PDF)。网络失败抛异常由调用方处理。"""
    req = urllib.request.Request(
        API.format(art=art_code),
        headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "ignore"))
    data = d.get("data") or {}
    body = _clean(data.get("notice_content") or "")
    if body:
        return body, False
    # notice_content 为空但带附件 → 正文在 PDF 里
    has_attach = bool(data.get("attach_list") or data.get("attach_url"))
    return "", has_attach


def _one(row):
    """抓单条：返回 (doc_id, body, status)。

    ⚠️ 东财对高频请求返回 **HTTP 567**（非标准状态码，就是限流）。
    遇到 567/429 先退避 5 秒再重试一次，仍失败才记 fail —— 否则并发一高，
    几千条会被瞬间打成失败（2026-11-08 实测：并发 10 时 3490 条 fail）。
    """
    doc_id, src_id = row[0], row[1]
    art = str(src_id or "").replace("ann_", "").strip()
    if not art.startswith("AN"):
        return doc_id, "", "skip"
    for attempt in range(2):
        try:
            body, pdf_only = fetch_body(art)
        except urllib.error.HTTPError as e:
            if e.code in (429, 567) and attempt == 0:
                time.sleep(5)
                continue
            return doc_id, "", "fail"
        except Exception:  # noqa: BLE001
            if attempt == 0:
                time.sleep(2)
                continue
            return doc_id, "", "fail"
        if body:
            return doc_id, body, "ok"
        return doc_id, "", ("pdf_only" if pdf_only else "no_content")
    return doc_id, "", "fail"


def main() -> int:
    ap = argparse.ArgumentParser(description="回补公告正文")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--since-days", type=int, default=0,
                    help="只补最近 N 天发布的公告（0=不限）")
    ap.add_argument("--workers", type=int, default=4, help="并发抓取数（>5 易被东财限流）")
    ap.add_argument("--min-len", type=int, default=100,
                    help="body_len 小于此值视为只有标题")
    args = ap.parse_args()

    db = SessionLocal()
    stat = {"ok": 0, "pdf_only": 0, "no_content": 0, "skip": 0, "fail": 0}
    try:
        sql = ("SELECT doc_id, source_doc_id, title FROM doc_document "
               "WHERE doc_type = 'announcement' AND body_len < :n "
               "AND extract_status = 'pending' ")
        params = {"n": args.min_len, "lim": args.limit}
        if args.since_days > 0:
            sql += "AND published_at >= :since "
            params["since"] = (datetime.now()
                               - timedelta(days=args.since_days)).strftime("%Y-%m-%d")
        sql += "ORDER BY published_at DESC LIMIT :lim"
        rows = db.execute(text(sql), params).fetchall()
        print(f"待补 {len(rows)} 条（并发 {args.workers}）", flush=True)
        if not rows:
            return 0

        buf = []
        done = 0

        def flush():
            nonlocal buf
            if not buf:
                return
            db.execute(text(
                "UPDATE doc_document SET body = :b, body_len = :l, "
                "extract_status = 'done' WHERE doc_id = :d"),
                [{"b": b, "l": len(b), "d": d} for d, b in buf])
            db.commit()
            buf = []

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futs = [ex.submit(_one, r) for r in rows]
            for fut in as_completed(futs):
                doc_id, body, status = fut.result()
                if status == "ok":
                    buf.append((doc_id, body))
                    stat["ok"] += 1
                    if len(buf) >= 100:
                        flush()
                else:
                    stat[status] = stat.get(status, 0) + 1
                    db.execute(text(
                        "UPDATE doc_document SET extract_status = :s WHERE doc_id = :d"),
                        {"s": status, "d": doc_id})
                done += 1
                if done % 500 == 0:
                    flush()
                    db.commit()
                    print(f"  进度 {done}/{len(rows)}  已补 {stat['ok']}", flush=True)
        flush()
        db.commit()

        left = db.execute(text(
            "SELECT COUNT(*) FROM doc_document WHERE doc_type='announcement' "
            "AND body_len < :n AND extract_status = 'pending'"), {"n": args.min_len}
        ).scalar()
        stat["pending_left"] = int(left or 0)
        print(json.dumps(stat, ensure_ascii=False), flush=True)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
