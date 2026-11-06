#!/usr/bin/env python3
"""公众号全文知识库抓取管线。

从 wewe-rss 元数据库读取文章列表，逐篇抓取微信文章正文，
写入 knowledge.db（SQLite + FTS5 全文索引），并同步导出
Markdown 到 vault/ 目录（可用 Obsidian 直接打开）。

用法：
  python3 crawl_content.py --limit 20      # 抓 20 篇（测试）
  python3 crawl_content.py                  # 抓全部缺失的
  python3 crawl_content.py --stats          # 只看统计
  python3 crawl_content.py --retry-failed   # 重试之前失败的
"""
import argparse
import os
import html
import json
import random
import re
import sqlite3
import ssl
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import CRAWL_STATE as STATE_FILE  # noqa: E402
from paths import KNOW_DB, VAULT_DIR, WEWE_DB, ensure_dirs  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CONTENT_RE = re.compile(
    r'<div[^>]+id="js_content"[^>]*>(.*?)</div>\s*(?:<script|<div[^>]+id="js_pc_qr_code")',
    re.S)
AUTHOR_RE = re.compile(r'<meta\s+name="author"\s+content="([^"]*)"', re.S)
OGTITLE_RE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]*)"', re.S)
TAG_RE = re.compile(r"<[^>]+>")
BLOCK_RE = re.compile(r"</(p|div|section|h[1-6]|li|blockquote|tr)>", re.I)
BR_RE = re.compile(r"<br\s*/?>", re.I)


def init_db(con: sqlite3.Connection):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS articles (
        id TEXT PRIMARY KEY,
        mp_id TEXT NOT NULL,
        mp_name TEXT NOT NULL,
        title TEXT NOT NULL,
        author TEXT DEFAULT '',
        publish_time INTEGER NOT NULL,
        url TEXT NOT NULL,
        cover TEXT DEFAULT '',
        content_text TEXT NOT NULL,
        fetched_at INTEGER NOT NULL
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
        title, mp_name, author, content_text,
        content='articles', content_rowid='rowid',
        tokenize='trigram'
    );
    CREATE TABLE IF NOT EXISTS failures (
        id TEXT PRIMARY KEY,
        reason TEXT,
        failed_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY,
        v TEXT
    );
    """)
    con.execute("""
    CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
        INSERT INTO articles_fts(rowid, title, mp_name, author, content_text)
        VALUES (new.rowid, new.title, new.mp_name, new.author, new.content_text);
    END;
    """)
    con.commit()


def html_to_text(raw: str) -> str:
    raw = BR_RE.sub("\n", raw)
    raw = BLOCK_RE.sub("\n", raw)
    txt = TAG_RE.sub("", raw)
    txt = html.unescape(txt)
    lines = [ln.strip() for ln in txt.split("\n")]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def fetch_article(art_id: str):
    """返回 (content_html, author, ok_reason)。"""
    url = f"https://mp.weixin.qq.com/s/{art_id}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        page = resp.read().decode("utf-8", "ignore")
    # 视频类文章（og:type=video 或有视频播放器标识）：跳过不抓
    if re.search(r'<meta\s+property="og:type"\s+content="video"', page, re.I) \
       or re.search(r'id="js_[a-z]*video[a-z_]*"', page, re.I) \
       or '<mp-common-videosnap' in page:
        return None, "", "video_skip"
    if "环境异常" in page or "验证" in page[:2000] and "js_content" not in page:
        return None, "", "blocked"
    m = CONTENT_RE.search(page)
    if not m:
        return None, "", "no_content"
    am = AUTHOR_RE.search(page)
    author = html.unescape(am.group(1)).strip() if am else ""
    return m.group(1), author, "ok"


def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\n\r]+', "_", s).strip()[:80] or "untitled"


def export_markdown(vault: Path, row: dict):
    d = vault / safe_name(row["mp_name"])
    d.mkdir(parents=True, exist_ok=True)
    date = datetime.fromtimestamp(row["publish_time"]).strftime("%Y-%m-%d")
    fn = d / f"{date} {safe_name(row['title'])}.md"
    fm = [
        "---",
        f"公众号: {row['mp_name']}",
        f"作者: {row.get('author') or ''}",
        f"发布时间: {datetime.fromtimestamp(row['publish_time']).strftime('%Y-%m-%d %H:%M')}",
        f"原文链接: {row['url']}",
        f"抓取时间: {datetime.fromtimestamp(row['fetched_at']).strftime('%Y-%m-%d %H:%M')}",
        "---",
        "",
        f"# {row['title']}",
        "",
        row["content_text"],
    ]
    fn.write_text("\n".join(fm), encoding="utf-8")


def get_pending(wcon, kcon, retry_failed=False, since_ts=0):
    done = {r[0] for r in kcon.execute("SELECT id FROM articles")}
    failed = {r[0] for r in kcon.execute("SELECT id FROM failures")}
    rows = wcon.execute("""
        SELECT a.id, a.mp_id, a.title, a.publish_time, a.pic_url, f.mp_name
        FROM articles a JOIN feeds f ON f.id = a.mp_id
        WHERE a.publish_time >= ?
        ORDER BY a.publish_time DESC
    """, (since_ts,)).fetchall()
    pend = []
    for aid, mp_id, title, ts, pic, mp_name in rows:
        if aid in done or (aid in failed and not retry_failed):
            continue
        pend.append({"id": aid, "mp_id": mp_id, "mp_name": mp_name,
                     "title": title, "publish_time": ts, "cover": pic})
    return pend


def save_state(**kw):
    st = {}
    if STATE_FILE.exists():
        st = json.loads(STATE_FILE.read_text())
    st.update(kw)
    st["updated_at"] = datetime.now().isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2))


def stats(kcon, wcon):
    total_meta = wcon.execute("SELECT count(*) FROM articles").fetchone()[0]
    got = kcon.execute("SELECT count(*) FROM articles").fetchone()[0]
    failed = kcon.execute("SELECT count(*) FROM failures").fetchone()[0]
    chars = kcon.execute("SELECT COALESCE(SUM(LENGTH(content_text)),0) FROM articles").fetchone()[0]
    mps = kcon.execute("SELECT count(DISTINCT mp_id) FROM articles").fetchone()[0]
    print(f"元数据文章: {total_meta} | 已抓正文: {got} | 失败: {failed} | "
          f"待抓: {total_meta - got - failed} | 覆盖公众号: {mps} | 总字数: {chars/1e4:.0f} 万")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="本次最多抓多少篇")
    ap.add_argument("--since-days", type=int, default=0,
                    help="只抓最近 N 天内发布的文章（0=不限，每日增量建议 7）")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--min-delay", type=float, default=2.0)
    ap.add_argument("--max-delay", type=float, default=3.5)
    args = ap.parse_args()

    ensure_dirs()
    wcon = sqlite3.connect(f"file:{WEWE_DB}?mode=ro", uri=True)
    kcon = sqlite3.connect(KNOW_DB)
    init_db(kcon)

    if args.stats:
        stats(kcon, wcon)
        return

    since_ts = int(time.time()) - args.since_days * 86400 if args.since_days else 0
    pend = get_pending(wcon, kcon, args.retry_failed, since_ts)
    if args.limit:
        pend = pend[:args.limit]
    print(f"本次待抓 {len(pend)} 篇" + (f"（近 {args.since_days} 天）" if args.since_days else ""))

    ok = fail = blocked = skipped = 0
    consec_fail = 0
    t0 = time.time()
    for i, art in enumerate(pend, 1):
        url = f"https://mp.weixin.qq.com/s/{art['id']}"
        try:
            content_html, author, reason = fetch_article(art["id"])
        except Exception as e:
            content_html, author, reason = None, "", f"error:{type(e).__name__}"
        if content_html:
            text = html_to_text(content_html)
            now = int(time.time())
            kcon.execute(
                "INSERT OR REPLACE INTO articles"
                "(id, mp_id, mp_name, title, author, publish_time, url, cover, content_text, fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (art["id"], art["mp_id"], art["mp_name"], art["title"], author,
                 art["publish_time"], url, art["cover"], text, now))
            kcon.execute("DELETE FROM failures WHERE id=?", (art["id"],))
            kcon.commit()
            try:
                export_markdown(VAULT_DIR, {**art, "author": author, "url": url,
                                            "content_text": text, "fetched_at": now})
            except Exception as e:
                print(f"  [md导出失败] {art['id']}: {e}")
            ok += 1
            consec_fail = 0
            print(f"[{i}/{len(pend)}] OK {art['mp_name']}《{art['title'][:30]}》({len(text)}字)")
        else:
            if reason == "video_skip":
                skipped += 1
                print(f"[{i}/{len(pend)}] SKIP(视频) {art['title'][:30]}")
                continue
            kcon.execute("INSERT OR REPLACE INTO failures(id, reason, failed_at) VALUES (?,?,?)",
                         (art["id"], reason, int(time.time())))
            kcon.commit()
            fail += 1
            consec_fail += 1
            if reason == "blocked":
                blocked += 1
            print(f"[{i}/{len(pend)}] FAIL({reason}) {art['title'][:30]}")
            if blocked and blocked % 3 == 0:
                print("  检测到限流，退避 90 秒…")
                time.sleep(90)
        if consec_fail >= 10:
            print("连续失败 10 篇，本次停止（可稍后重跑，支持断点续抓）")
            break
        if i < len(pend):
            time.sleep(random.uniform(args.min_delay, args.max_delay))

    dur = time.time() - t0
    save_state(last_run={"ok": ok, "fail": fail, "seconds": round(dur)})
    print(f"\n完成：成功 {ok}，失败 {fail}，耗时 {dur/60:.1f} 分钟")
    stats(kcon, wcon)


if __name__ == "__main__":
    main()
