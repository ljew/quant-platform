"""从「合集(album)」回补公众号历史文章。

原理：mp.weixin.qq.com/mp/appmsgalbum 是公开页，不需要微信客户端 key。
合集页每个条目带 data-msgid / data-itemidx / data-title / create_time / data-link，
用 from_msgid+from_itemidx 翻页即可完整枚举，属于"公开可访问"，不涉及逆向。

用法（必须容器内跑，knowledge.db 单写者）：
    python backfill_album.py --dry-run          # 只统计，不写库
    python backfill_album.py --since 2026-08-04 # 真正回补
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

DATA = Path(os.environ.get("QUANT_WECHAT_DATA_DIR", "/data/wechat"))
KNOWLEDGE = DATA / "knowledge.db"
SCAN = Path("/tmp/album_scan.json")

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.40"
)

ITEM_RE = re.compile(
    r'data-msgid="(\d+)"\s+data-itemidx="(\d+)"\s+data-link="([^"]+)"[^>]*?data-title="([^"]*)"',
    re.S,
)
TIME_RE = re.compile(r'class="js_article_create_time[^"]*">(\d{9,11})<')


def http(url: str, timeout: int = 30) -> str:
    """只读前 1.6MB：公众号文章页整页约 3.4MB，但 __biz/album/js_content 都在 0.6MB 之前。
    2026-11-02 用 4MB 全读跑批量回补，收尾时把容器顶到 OOM 被 kill（OOMKilled=true）。"""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": "https://mp.weixin.qq.com/"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(1_600_000).decode("utf-8", "ignore")


def album_url(biz: str, album: str, fmid: str = "", fidx: str = "") -> str:
    """翻页必须用 begin_msgid/begin_itemidx —— 实测 from_msgid 会原样返回首页（0 新条目）。"""
    return (
        f"https://mp.weixin.qq.com/mp/appmsgalbum?__biz={biz}&action=getalbum"
        f"&album_id={album}&scene=173&begin_msgid={fmid}&begin_itemidx={fidx}"
        f"&count=20&nolastread=1"
    )


def parse_items(html: str) -> list[dict]:
    times = TIME_RE.findall(html)
    items = []
    for i, (mid, idx, link, title) in enumerate(ITEM_RE.findall(html)):
        ts = int(times[i]) if i < len(times) else 0
        items.append(
            {
                "mid": mid,
                "idx": idx,
                "title": title.replace("&quot;", '"').replace("&amp;", "&"),
                "ts": ts,
                "link": link.replace("&amp;", "&"),
            }
        )
    return items


def collect_album(biz: str, album: str, max_pages: int = 60, gap: float = 1.5) -> list[dict]:
    seen, out, fmid, fidx = set(), [], "", ""
    for _ in range(max_pages):
        try:
            html = http(album_url(biz, album, fmid, fidx))
        except Exception as e:  # noqa: BLE001
            print(f"    合集页抓取失败：{type(e).__name__}: {str(e)[:70]}")
            break
        items = parse_items(html)
        fresh = [it for it in items if (it["mid"], it["idx"]) not in seen]
        if not fresh:
            break
        for it in fresh:
            seen.add((it["mid"], it["idx"]))
        out.extend(fresh)
        fmid, fidx = fresh[-1]["mid"], fresh[-1]["idx"]
        if len(fresh) < 20:
            break
        time.sleep(gap)
    return out


def html_to_text(html: str) -> str:
    i = html.find('id="js_content"')
    if i < 0:
        return ""
    start = html.find(">", i) + 1
    depth, pos, pat = 1, start, re.compile(r"<div\b|</div>", re.I)
    while depth > 0:
        m = pat.search(html, pos)
        if not m:
            frag = html[start:]
            break
        depth += 1 if m.group(0).lower().startswith("<div") else -1
        pos = m.end()
    else:
        frag = html[start:pos]
    frag = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", frag, flags=re.S | re.I)
    frag = re.sub(r"<br\s*/?>|</p>|</section>|</div>|</li>|</h\d>", "\n", frag, flags=re.I)
    text = re.sub(r"<[^>]+>", "", frag)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&ldquo;", "“"), ("&rdquo;", "”")):
        text = text.replace(a, b)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--since", default="2026-08-04", help="只补这个日期之后（YYYY-MM-DD）")
    ap.add_argument("--interval", type=float, default=2.0, help="抓正文间隔")
    ap.add_argument("--limit-per-mp", type=int, default=0, help="每个号最多补几篇")
    args = ap.parse_args()

    since_ts = int(datetime.strptime(args.since, "%Y-%m-%d").timestamp())
    scan = [x for x in json.load(open(SCAN)) if x.get("albums")]
    con = sqlite3.connect(str(KNOWLEDGE), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")

    total_new = 0
    total_done = 0
    for acc in scan:
        name, mp_id, biz = acc["mp_name"], acc["mp_id"], acc["biz"]
        have = {r[0] for r in con.execute("select title from articles where mp_id=?", (mp_id,))}
        picked = []
        for album in acc["albums"]:
            items = collect_album(biz, album)
            print(f"  [{name}] 合集 {album}: 共 {len(items)} 条")
            for it in items:
                if it["ts"] < since_ts:
                    continue
                if it["title"] in have:
                    continue
                picked.append(it)
        # 去重（同一篇可能出现在多个合集）
        uniq = {}
        for it in picked:
            uniq[(it["mid"], it["idx"])] = it
        picked = sorted(uniq.values(), key=lambda x: x["ts"])
        if args.limit_per_mp:
            picked = picked[-args.limit_per_mp:]
        print(f"  [{name}] 缺口内待补 {len(picked)} 篇")
        total_new += len(picked)
        if args.dry_run or not picked:
            for it in picked[:5]:
                print(f"      {datetime.fromtimestamp(it['ts']):%Y-%m-%d} {it['title'][:30]}")
            continue

        new = 0
        for it in picked:
            try:
                html = http(it["link"])
            except Exception as e:  # noqa: BLE001
                print(f"      抓正文失败 {it['title'][:20]}: {type(e).__name__}")
                time.sleep(args.interval)
                continue
            text = html_to_text(html)
            if len(text) < 50:
                print(f"      正文过短跳过 {it['title'][:20]}（{len(text)} 字）")
                time.sleep(args.interval)
                continue
            rid = f"{mp_id}_{it['mid']}_{it['idx']}"
            con.execute(
                "INSERT OR REPLACE INTO articles"
                "(id, mp_id, mp_name, title, author, publish_time, url, cover, content_text, fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rid, mp_id, name, it["title"], "", it["ts"], it["link"], "", text, int(time.time())),
            )
            con.commit()
            new += 1
            print(f"      ✓ {datetime.fromtimestamp(it['ts']):%Y-%m-%d} {it['title'][:32]} ({len(text)}字)")
            time.sleep(args.interval)
        print(f"  [{name}] 实际入库 {new} 篇")
        total_done += new

    con.close()
    if args.dry_run:
        print(f"\n[dry-run] 合计候选 {total_new} 篇（含正文为空的图片型文章）")
    else:
        print(f"\n合计候选 {total_new} 篇，实际入库 {total_done} 篇"
              f"（其余为纯图片/报名页，正文 <50 字已跳过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
