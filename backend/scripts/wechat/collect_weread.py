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

def _proc_starttime(pid: int) -> str:
    """读进程启动时刻（/proc/<pid>/stat 第 22 字段），用作「同一个进程」的唯一标识。

    为什么需要：容器重启后 PID 从 1 重新分配，只比对 pid 会把「上一个容器里的
    同号进程」误认成自己 —— 2026-11-03 实测残留锁写的是 pid 31，而新容器的采集
    进程自己也拿到 pid 31，于是锁被判定为「被别人占用」，采集一整个上午没跑起来。
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            fields = f.read().decode("utf-8", "ignore").rsplit(")", 1)[1].split()
        return fields[19]      # starttime：第 22 字段减去前 3 个定位项
    except Exception:  # noqa: BLE001
        return ""


def _lock_owner_alive(pid: int, starttime: str) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False           # 空 pid / 锁指向自己 → 一定是残留
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if not os.path.isdir("/proc"):
        return True            # 非 Linux（宿主机 macOS 无 /proc）：只能判存活
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "ignore")
    except OSError:
        return False           # 内核线程没有 cmdline，不可能是本脚本
    if "collect_weread" not in cmd:
        return False
    if starttime and _proc_starttime(pid) != starttime:
        return False           # 同号不同命：PID 被复用给了别的进程
    return True


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
    # 判锁要「存活 + 身份」双重校验，不能只看 mtime：
    # 2026-11-02 容器被 OOM Killer 干掉时采集进程一起死，锁留了下来，
    # 之后 900 秒内所有调度都回「另一个采集进程正在运行」而跳过（静默漏采）。
    # 又因为容器重启后 PID 从 1 重新分配，光 os.kill(pid,0) 可能命中无关进程，
    # 所以还要核对 /proc/<pid>/cmdline 里确实是本脚本。
    if lock.exists() and time.time() - lock.stat().st_mtime < 900:
        raw = ""
        try:
            raw = lock.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
        pid_s, _, st_s = raw.partition(":")
        try:
            pid = int(pid_s or 0)
        except ValueError:
            pid = 0
        if _lock_owner_alive(pid, st_s):
            print("另一个采集进程正在运行（锁未释放），本次跳过", file=sys.stderr)
            return 3
        print(f"清理失效锁（pid={pid} 已不在运行 / PID 被复用），继续采集", file=sys.stderr)
    elif lock.exists():
        # 绝对超时兜底：单轮最长 ~42 分钟，超过 2 小时的锁一定是残留（进程被 kill、
        # 容器重启、或机器睡眠导致时断时续），不清理就会一直挡着。
        print("清理超期锁（>2h），继续采集", file=sys.stderr)
    lock.write_text(f"{os.getpid()}:{_proc_starttime(os.getpid())}", encoding="utf-8")

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
    consec_fail = 0      # 连续「取列表」失败数（HTTP 层），>=5 判为被限流
    consec_net = 0       # 连续网络错误（DNS/连接），>=3 判为网络不可用
    throttle = ""
    net_error = ""
    for i, f in enumerate(feeds, 1):
        mp_id, mp_name = f["mp_id"], f.get("mp_name", "")
        try:
            cover = http_json(f"https://weread.qq.com/api/mp/cover?bookId={mp_id}", cookie)
        except Exception as e:  # noqa: BLE001
            # 2026-11-02 教训：interval=3s 时跑到第 22 个号就被限流，之后 89 个 cover
            # 请求全部秒失败，而原代码跳过 sleep 直接 continue —— 259s 就把 111 个号
            # 跑完并报「完成」，scheduler 判成功、last_error=null，是最坏的静默失败。
            # 2026-11-03 教训：Mac 睡眠唤醒瞬间 DNS 未就绪（Name or service not known）
            # 也被当成"限流"熔断，且该 slot 被记成 done → 整个上午不再重试。故两类分开。
            fail += 1
            msg = str(e)
            is_net = not isinstance(e, urllib.error.HTTPError) and (
                isinstance(e, urllib.error.URLError) or "Name or service not known" in msg
            )
            if is_net:
                consec_net += 1
                print(f"[{i}/{len(feeds)}] 网络不可用 {mp_name}: {msg[:70]}")
                if consec_net >= 3:
                    net_error = f"网络不可用（DNS/连接失败）：{msg[:90]}"
                    break
            else:
                consec_fail += 1
                print(f"[{i}/{len(feeds)}] 取列表失败 {mp_name}: {type(e).__name__}: {msg[:80]}")
                if consec_fail >= 5:
                    throttle = (f"连续 {consec_fail} 个号取列表失败，疑似被限流"
                                f"（已处理 {i}/{len(feeds)} 个号）")
                    break
            time.sleep(args.interval)   # 失败也要间隔，别把限流窗口打得更长
            continue
        consec_fail = 0
        consec_net = 0

        # 微信读书实际返回驼峰 errCode（2026-11-04 实测 {"errCode":-2012,"errMsg":"登录超时"}）。
        # 旧代码只认小写 errcode → Cookie 过期被当成普通失败：65 个号各退避 60s，
        # 空转 73 分钟、最后还报 ok=true（静默失败）。两种拼写都认。
        cover_ec = cover.get("errCode", cover.get("errcode"))
        if cover_ec in AUTH_ERR or cover.get("statusCode") == 401:
            auth_error = f"{cover_ec} {cover.get('errMsg') or cover.get('errmsg', '')}"
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
            # 未登录/登录超时时，正文接口返回 JSON 而非 HTML：
            # {"errCode":-2012,"errMsg":"登录超时"}。必须立刻识别并结束整轮，
            # 否则每个号都白等 60 秒退避（2026-11-04：65 个号空转 73 分钟）。
            if html and html.lstrip().startswith("{"):
                try:
                    ej = json.loads(html)
                except Exception:  # noqa: BLE001
                    ej = {}
                ec = ej.get("errCode", ej.get("errcode"))
                if ec in AUTH_ERR:
                    auth_error = f"{ec} {ej.get('errMsg') or ej.get('errmsg', '')}"
                    print(f"❌ 凭证失效：{auth_error}（正文接口，第 {i} 个号）")
                    break
            if html and "js_content" in html:
                break
            if attempt == 1:
                print(f"[{i}/{len(feeds)}] 命中冷却页 {mp_name}，退避 60s 后重试")
                time.sleep(60)
                html = ""
        if auth_error:
            break            # 凭证失效与具体号无关，直接结束整轮
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
    # 限流/网络故障也算失败：111 个号里 89 个没取到，绝不能报「完成 = 成功」
    ok = not auth_error and not throttle and not net_error
    err = auth_error or throttle or net_error or None
    save_state(ok=ok, new=new, skip=skip, fail=fail,
               feeds=len(feeds), seconds=secs, error=err)
    tail = f"｜{throttle or net_error}" if (throttle or net_error) else ""
    print(f"完成：新增 {new} / 已最新 {skip} / 失败 {fail}，耗时 {secs}s{tail}")
    if auth_error:
        print("凭证失效 —— 需要重新扫码登录微信读书并更新 Cookie", file=sys.stderr)
        return 2
    if net_error:
        print(net_error, file=sys.stderr)
        return 4
    if throttle:
        print(throttle, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
