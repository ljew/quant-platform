#!/usr/bin/env python3
"""公众号历史回溯脚本（补爬 2025/2026 年文章）。

背景：
  wewe-rss 的定时任务(CRON)只抓每个号「最新一页」(page=1)，历史文章从未被拉取，
  导致 knowledge.db 里 2025 年几乎为空。wewe-rss 自带 getHistoryMpArticles 本可往前翻，
  但源码有起始页 bug（对非 20 整数倍的账号停在残缺页→误判无历史→立即终止）。
  已在 vendor/wewe-rss 源码与 dist 中修复（起始页改为 ceil(total/20)+1），重启 live server 后生效。

本脚本职责（不修改 wewe-rss 任何代码）：
  1) 对每个已订阅公众号，调用 live wewe-rss 的 getHistoryArticles 接口触发历史回溯；
  2) 轮询 inProgressHistoryMp 直到该号回溯完成，再触发下一个（接口全局串行，必须逐号等待）；
  3) 断点续跑：已完成的号写入 backfill_state.json，重跑可跳过；
  4) 全部完成后（或每 N 个号）调用 crawl_content.py 把新文章拉进 knowledge.db。

用法：
  python3 backfill_history.py                 # 全量回溯（断点续跑）
  python3 backfill_history.py --dry-run        # 只打印将要处理的号，不触发
  python3 backfill_history.py --force          # 忽略已完成记录，全部重来
  python3 backfill_history.py --crawl-only     # 只跑 crawl_content，不触发回溯

依赖：live wewe-rss 服务需在 4000 端口运行，且已应用起始页修复（重启过）。
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import BACKFILL_STATE as STATE_FILE  # noqa: E402
from paths import WEWE_DB as LIVE_DB  # noqa: E402
from paths import WEWE_ENV as LIVE_ENV  # noqa: E402
from paths import WEWE_URL as LIVE_URL  # noqa: E402
from paths import read_auth_code  # noqa: E402

CRAWL_SCRIPT = Path(__file__).resolve().parent / "crawl_content.py"
CRAWL_EVERY = 10          # 每处理 N 个号跑一次 crawl_content
PER_FEED_TIMEOUT = 4 * 3600  # 单个号回溯超时（秒），超时视为异常并跳过
POLL_INTERVAL = 30        # 轮询间隔（秒）


def log(*a):
    print(f"[{datetime.now().strftime('%H:%M:%S')}]", *a, flush=True)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"done": [], "failed": []}


def save_state(st):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2))


def api_get(path):
    req = urllib.request.Request(f"{LIVE_URL}/trpc/{path}",
                                 headers={"Authorization": AUTH_CODE})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def api_post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{LIVE_URL}/trpc/{path}", data=data,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": AUTH_CODE})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def list_feeds():
    w = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    rows = w.execute(
        """SELECT f.id, f.mp_name, f.has_history,
                  (SELECT COUNT(*) FROM articles a WHERE a.mp_id=f.id) AS n
           FROM feeds f WHERE f.status=1
           ORDER BY n DESC"""
    ).fetchall()
    w.close()
    return [{"id": r[0], "name": r[1], "has_history": r[2], "n": r[3]} for r in rows]


def in_progress():
    try:
        res = api_get("feed.getInProgressHistoryMp")
        return res.get("result", {}).get("data", {})
    except Exception as e:
        log("  轮询失败:", e)
        return {"id": "__unknown__", "page": 0}


def wait_done(mp_id, timeout):
    """轮询直到 inProgressHistoryMp.id 变为空（该号回溯结束），或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ip = in_progress()
            if ip.get("id") in (None, "", mp_id):
                # id 为空，或已被别的号占用（说明本号已结束/被打断）
                if ip.get("id") in (None, ""):
                    return True
                # 被别的号占用：本号已结束
                return True
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)
    return False


def run_crawl():
    log(">>> 运行 crawl_content.py（把新文章拉进 knowledge.db）...")
    env = dict(os.environ)
    env["WEWE_DB_PATH"] = str(LIVE_DB)
    try:
        subprocess.run([sys.executable, str(CRAWL_SCRIPT)], env=env,
                       check=True, timeout=3600)
    except Exception as e:
        log("   crawl_content 失败:", e)


def main():
    global AUTH_CODE
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--crawl-only", action="store_true")
    args = ap.parse_args()

    AUTH_CODE = read_auth_code()

    if args.crawl_only:
        run_crawl()
        return

    # 检查 live server 是否可达
    try:
        api_get("feed.getInProgressHistoryMp")
    except Exception as e:
        log(f"live wewe-rss({LIVE_URL}) 不可达：{e}")
        log("请确认服务已启动且已应用起始页修复（重启过）。中止。")
        return

    feeds = list_feeds()
    log(f"live 库共 {len(feeds)} 个已订阅号")

    st = load_state()
    if args.force:
        st = {"done": [], "failed": []}

    done = set(st.get("done", []))
    failed = set(st.get("failed", []))

    if args.dry_run:
        for f in feeds:
            tag = "✓已完成" if f["id"] in done else ("✗曾失败" if f["id"] in failed else "→待处理")
            log(f"  {tag}  {f['name']}  (当前 {f['n']} 篇, has_history={f['has_history']})")
        return

    processed = 0
    for f in feeds:
        fid, name = f["id"], f["name"]
        if fid in done:
            log(f"跳过(已完成): {name}")
            continue
        log(f"触发历史回溯: {name} (当前 {f['n']} 篇)")
        try:
            api_post("feed.getHistoryArticles", {"mpId": fid})
        except Exception as e:
            log(f"  触发失败: {e}")
            failed.add(fid)
            save_state({"done": list(done), "failed": list(failed)})
            continue
        ok = wait_done(fid, PER_FEED_TIMEOUT)
        if ok:
            done.add(fid)
            log(f"  完成: {name}")
        else:
            log(f"  超时/异常: {name}，标记失败稍后重试")
            failed.add(fid)
        processed += 1
        save_state({"done": list(done), "failed": list(failed)})
        if processed % CRAWL_EVERY == 0:
            run_crawl()

    log(f"全部号处理完毕。已完成 {len(done)}，失败 {len(failed)}。")
    if failed:
        log("失败号(可重跑本脚本重试): " + ", ".join(failed))
    run_crawl()
    log("历史回溯流程结束。")


if __name__ == "__main__":
    main()
