#!/usr/bin/env python3
"""公众号采集（微信读书渠道）—— wewe-rss 中转站停摆后的替代方案。

为什么换：
    2026-11-02 实测确认 wewe-rss 依赖的闭源中转 `weread.111965.xyz` 源站下线
    （Cloudflare 502），项目本身 2026-03 已归档。它连拉文章列表都要过中转，
    所以扫码成功也拿不到新文章。

本脚本直连**微信读书官方接口**，不经任何第三方：
    GET https://weread.qq.com/api/mp/cover?bookId=MP_WXS_xxx
        → {"name","title","pic","reviewId","avatar",...}  该号「最新一篇」
    GET https://weread.qq.com/web/mp/content?reviewId=xxx
        → 文章 HTML（含 `var ct = <发布时间戳>` 与 `#js_content` 正文）

⚠️ 官方只暴露「最新一篇」，**无法回补历史**（旧列表接口 /web/mp/articles 已废弃，
   恒返回 -2041）。所以本采集器是「日更增量」，不是「历史下载器」。
⚠️ 幂等：以 reviewId 作主键，重复跑只跳过，不会写重。
⚠️ 风控：正文请求间隔默认 3 秒（WEREAD_CONTENT_INTERVAL >= 2），勿调低。

Cookie 从 `data/wechat/weread_cookie.json` 读（浏览器登录后抓的 cookie 数组）。
过期会返回 -2010 / -2012 / -2041 —— 脚本以退出码 2 明确失败，由看门狗告警。

用法：
    python3 collect_weread.py                    # 全量（111 个号，约 6-8 分钟）
    python3 collect_weread.py --limit 3          # 只跑前 3 个号（试跑）
    python3 collect_weread.py --mp MP_WXS_xxx    # 只跑某号
    python3 collect_weread.py --dry-run          # 只取列表不入库
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DATA = Path(os.environ.get("QUANT_WECHAT_DATA_DIR", "/data/wechat"))
if not DATA.exists():  # 宿主机直跑时回落到仓库路径
    DATA = Path(__file__).resolve().parents[3] / "data" / "wechat"

COOKIE_FILE = DATA / "weread_cookie.json"     # 浏览器登录后抓的 cookie 数组
FEEDS_FILE = DATA / "feeds.json"
KNOWLEDGE = DATA / "knowledge.db"
WEWE_DB = DATA / "wewe-rss.db"
STATE_FILE = DATA / "state" / "weread_state.json"

AUTH_ERR = {-2010, -2012, -2041, -2011}


# ─────────────────────────── 基础 IO ───────────────────────────

def load_cookie() -> str:
    if not COOKIE_FILE.exists():
        raise SystemExit(f"缺少 Cookie 文件：{COOKIE_FILE}（先扫码登录并导出 cookie）")
    raw = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    if isinstance(raw, str):
        return raw
    return "; ".join(f"{c['name']}={c['value']}" for c in raw)


def load_feeds() -> list[dict]:
    """订阅号清单：优先 feeds.json；没有就从 wewe-rss.db 导一次（它是静态清单了）。"""
    if FEEDS_FILE.exists():
        return json.loads(FEEDS_FILE.read_text(encoding="utf-8"))
    if not WEWE_DB.exists():
        raise SystemExit("既无 feeds.json 也无 wewe-rss.db，无法确定要采哪些号")
    con = sqlite3.connect(f"file:{WEWE_DB}?mode=ro", uri=True)
    rows = con.execute("SELECT id, mp_name, mp_cover FROM feeds ORDER BY mp_name").fetchall()
    con.close()
    feeds = [{"mp_id": r[0], "mp_name": r[1], "cover": r[2]} for r in rows]
    FEEDS_FILE.write_text(json.dumps(feeds, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已从 wewe-rss.db 导出订阅清单：{len(feeds)} 个号 → {FEEDS_FILE}")
    return feeds


def http_json(url: str, cookie: str, timeout: float = 20) -> dict:
    req = urllib.request.Request(url, headers={
        "Cookie": cookie, "User-Agent": UA, "Referer": "https://weread.qq.com/",
        "Accept": "application/json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def http_text(url: str, cookie: str, timeout: float = 30) -> str:
    req = urllib.request.Request(url, headers={
        "Cookie": cookie, "User-Agent": UA, "Referer": "https://weread.qq.com/",
        "Accept": "text/html,application/xhtml+xml,*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


# ─────────────────────────── 解析 ───────────────────────────

def html_to_text(html: str) -> str:
    m = re.search(r'id="js_content"[^>]*>(.*?)</div>\s*(?:<script|<div id="js_pc_qr_code")',
                  html, re.S) or re.search(r'id="js_content"[^>]*>(.*)', html, re.S)
    body = m.group(1) if m else ""
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
    body = re.sub(r"<br\s*/?>|</p>|</section>|</div>", "\n", body, flags=re.I)
    text = re.sub(r"<[^>]+>", "", body)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&ldquo;", "“"), ("&rdquo;", "”")):
        text = text.replace(a, b)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_publish_ts(html: str) -> int:
    for pat in (r"var\s+ct\s*=\s*[\"'](\d+)[\"']", r"var\s+create_time\s*=\s*[\"'](\d+)[\"']"):
        m = re.search(pat, html)
        if m:
            return int(m.group(1))
    return int(time.time())


# ─────────────────────────── 入库 ───────────────────────────

def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS articles (
        id TEXT PRIMARY KEY, mp_id TEXT NOT NULL, mp_name TEXT NOT NULL,
        title TEXT NOT NULL, author TEXT, publish_time INTEGER NOT NULL,
        url TEXT, cover TEXT, content_text TEXT, fetched_at INTEGER NOT NULL
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
        title, mp_name, author, content_text, content='articles', content_rowid='rowid'
    );
    CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
        INSERT INTO articles_fts(rowid, title, mp_name, author, content_text)
        VALUES (new.rowid, new.title, new.mp_name, new.author, new.content_text);
    END;
    """)


def save_state(**kw) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    kw["at"] = datetime.now().isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(kw, ensure_ascii=False, indent=1), encoding="utf-8")


# ─────────────────────────── 主流程 ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=6.0, help="正文请求间隔（秒，>=2）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个号（试跑）")
    ap.add_argument("--mp", default="", help="只处理指定 mp_id")
    ap.add_argument("--dry-run", action="store_true", help="只取列表与正文，不写库")
    args = ap.parse_args()

    # —— 单写者锁：定时任务与「立即采集」可能撞车，同时写 knowledge.db 不划算
    lock = STATE_FILE.parent / "collect.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists() and time.time() - lock.stat().st_mtime < 900:
        print("另一个采集进程正在运行（锁未释放），本次跳过", file=sys.stderr)
        return 3
    lock.write_text(str(os.getpid()), encoding="utf-8")

    t0 = time.time()
    cookie = load_cookie()
    feeds = load_feeds()
    if args.mp:
        feeds = [f for f in feeds if f["mp_id"] == args.mp]
    if args.limit:
        feeds = feeds[: args.limit]
    print(f"开始采集：{len(feeds)} 个号，间隔 {args.interval}s")

    con = sqlite3.connect(str(KNOWLEDGE), timeout=60)
    # ⚠️ 必须 WAL + busy_timeout：这个库同时被后端只读打开。
    # 2026-11-02 曾在**宿主机**跑本脚本、同时容器在读 → macOS bind mount 的
    # 文件锁不可靠，导致 quick_check 报 freelist 不一致 / page 二次引用（真损坏，
    # 靠 .recover 救回 7016 篇）。**本脚本只允许在容器内运行**（读写同域）。
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    ensure_schema(con)
    known = {r[0] for r in con.execute("SELECT id FROM articles").fetchall()}

    new = skip = fail = 0
    auth_error = ""
    for i, f in enumerate(feeds, 1):
        mp_id, mp_name = f["mp_id"], f.get("mp_name", "")
        try:
            cover = http_json(f"https://weread.qq.com/api/mp/cover?bookId={mp_id}", cookie)
        except urllib.error.HTTPError as e:
            fail += 1
            print(f"[{i}/{len(feeds)}] HTTP {e.code} {mp_name}")
            continue
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"[{i}/{len(feeds)}] ERR {mp_name}: {type(e).__name__}")
            continue

        if cover.get("errcode") in AUTH_ERR or cover.get("statusCode") == 401:
            auth_error = f"{cover.get('errcode')} {cover.get('errmsg', '')}"
            print(f"❌ 凭证失效：{auth_error}（第 {i} 个号）")
            break

        rid = cover.get("reviewId") or ""
        title = (cover.get("title") or "").strip()
        if not rid:
            skip += 1
            print(f"[{i}/{len(feeds)}] 空 {mp_name}")
            continue
        if rid in known:
            skip += 1
            print(f"[{i}/{len(feeds)}] 已最新 {mp_name}")
            continue

        # 抓正文：微信读书有频率限制，命中去重/冷却页时页面里没有 #js_content，
        # 而是 `.cooldown_tips`（"操作频繁"）。策略：退避 60 秒重试一次，仍失败则跳过——
        # 不入库空正文，下一轮 cover 还会返回同一篇，自然会重试。
        html = ""
        for attempt in (1, 2):
            try:
                html = http_text(f"https://weread.qq.com/web/mp/content?reviewId={rid}", cookie)
            except Exception as e:  # noqa: BLE001
                print(f"[{i}/{len(feeds)}] 正文请求失败 {mp_name}: {type(e).__name__}（第 {attempt} 次）")
            if html and "js_content" in html:
                break
            if attempt == 1:
                print(f"[{i}/{len(feeds)}] 命中冷却页 {mp_name}，退避 60s 后重试")
                time.sleep(60)
                html = ""
        if not html or "js_content" not in html:
            fail += 1
            print(f"[{i}/{len(feeds)}] 仍无正文 {mp_name}，跳过")
            time.sleep(args.interval)
            continue

        text = html_to_text(html)
        if len(text) < 50:
            # 正文没抽出来（微信改版/风控页）就不写库，避免页面上出现 0 字文章；
            # 下轮 cover 仍会返回同一篇，届时重试
            fail += 1
            print(f"[{i}/{len(feeds)}] 正文过短({len(text)}字) {mp_name}，跳过")
            time.sleep(args.interval)
            continue
        ts = parse_publish_ts(html)
        url = f"https://weread.qq.com/web/mp/content?reviewId={rid}"
        if args.dry_run:
            print(f"[{i}/{len(feeds)}] DRY {mp_name}《{title[:26]}》 {len(text)} 字 "
                  f"{datetime.fromtimestamp(ts):%Y-%m-%d}")
        else:
            con.execute(
                "INSERT OR REPLACE INTO articles"
                "(id, mp_id, mp_name, title, author, publish_time, url, cover, content_text, fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rid, mp_id, mp_name or cover.get("name", ""), title, cover.get("name", ""),
                 ts, url, cover.get("pic", ""), text, int(time.time())),
            )
            con.commit()
            print(f"[{i}/{len(feeds)}] OK {mp_name}《{title[:26]}》 {len(text)} 字")
        new += 1
        time.sleep(args.interval)

    con.close()
    lock.unlink(missing_ok=True)
    secs = round(time.time() - t0, 1)
    save_state(ok=not auth_error, new=new, skip=skip, fail=fail,
               feeds=len(feeds), seconds=secs, error=auth_error or None)
    print(f"完成：新增 {new} / 已最新 {skip} / 失败 {fail}，耗时 {secs}s")
    if auth_error:
        print("凭证失效 —— 需要重新扫码登录微信读书并更新 Cookie", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
