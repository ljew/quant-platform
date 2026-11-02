"""公众号（微信订阅号）阅读 API —— 只读 knowledge.db。

数据链路：
    wewe-rss（宿主机 :4000，微信读书接口拉列表）
        → crawl_content.py 逐篇抓正文
        → data/wechat/knowledge.db（SQLite + FTS5 trigram）
        → 本 API → /wechat 页面

为什么全只读：抓取要连微信读书、有风控节奏（每篇间隔 2-3.5s），放在宿主机定时跑；
API 侧只负责查，所以这里没有任何写操作，也不需要锁。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.services import wechat_svc

router = APIRouter(prefix="/wechat", tags=["wechat"])


@router.get("/stats")
def stats():
    """总览：总篇数 / 覆盖号数 / 时间跨度 / 今日与近 7 天新发 / 数据新鲜度。"""
    return wechat_svc.stats()


@router.get("/mps")
def mps(limit: int = Query(300, ge=1, le=1000)):
    """公众号列表，按最新发文时间倒序。"""
    items = wechat_svc.list_mps(limit=limit)
    return {"items": items, "n": len(items)}


@router.get("/articles")
def articles(
    mp_id: str = Query("", description="按公众号过滤（mp_id，留空=全部）"),
    q: str = Query("", description="关键词；≥3 字走 FTS5 全文索引，<3 字走 LIKE"),
    since_days: int = Query(0, ge=0, le=3650, description="只取最近 N 天，0=不限"),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """文章流（不含全文，带 160 字摘要）。"""
    return wechat_svc.list_articles(
        mp_id=mp_id, q=q, since_days=since_days, limit=limit, offset=offset)


@router.get("/article/{art_id}")
def article(art_id: str):
    """单篇全文。"""
    d = wechat_svc.get_article(art_id)
    if d is None:
        raise HTTPException(status_code=404, detail=f"未找到文章：{art_id}")
    return d


@router.get("/daily")
def daily(date: str = Query("", description="YYYY-MM-DD，留空=今天")):
    """某一天的新发文（「每天看公众号」的主视图）。"""
    return wechat_svc.daily(date)


@router.get("/trend")
def trend(days: int = Query(14, ge=1, le=90)):
    """最近 N 天每日发文量。"""
    items = wechat_svc.recent_days(days)
    return {"items": items, "days": days}


@router.get("/schedule")
def schedule():
    """每日抓取调度状态（上次何时跑、成功没、抓了多少）。"""
    from app.core.wechat_scheduler import get_status
    return get_status()


@router.get("/upstream")
def upstream():
    """采集链路体检：微信读书凭证 + 最近一次采集结果 + 订阅号数量。

    为什么需要：这条路有三个失败面（Cookie 过期、微信改版、订阅清单丢了），
    页面上只看到「很久没更新」时无从判断。这里一次性探明并给出结论。

    2026-11-02 起改为**直连微信读书官方接口**（wewe-rss 的中转站已停摆）：
      /api/mp/cover?bookId=MP_WXS_xxx  → 该号最新一篇
      /web/mp/content?reviewId=xxx     → 正文
    """
    import json as _json
    import urllib.error
    import urllib.request

    from app.config import settings

    data_dir = Path(settings.wechat_data_dir)
    cookie_file = data_dir / "weread_cookie.json"
    feeds_file = data_dir / "feeds.json"
    state_file = data_dir / "state" / "weread_state.json"

    # ① 最近一次采集结果
    last: dict = {}
    if state_file.exists():
        try:
            last = _json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            last = {}

    # ② 订阅号数量
    feeds = 0
    if feeds_file.exists():
        try:
            feeds = len(_json.loads(feeds_file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            feeds = 0

    # ③ 凭证是否还有效：拿第一个号实测一次 cover（不写库、代价极小）
    cookie_ok, cookie_err, cookie_vid = False, "", ""
    if not cookie_file.exists():
        cookie_err = "未配置 Cookie（data/wechat/weread_cookie.json）"
    else:
        try:
            jar = _json.loads(cookie_file.read_text(encoding="utf-8"))
            if isinstance(jar, str):
                ck = jar
            else:
                ck = "; ".join(f"{c['name']}={c['value']}" for c in jar)
                cookie_vid = next((c["value"] for c in jar if c["name"] == "wr_vid"), "")
            probe = ""
            if feeds_file.exists():
                try:
                    probe = _json.loads(feeds_file.read_text(encoding="utf-8"))[0]["mp_id"]
                except (OSError, ValueError, IndexError, KeyError):
                    probe = ""
            if not probe:
                cookie_err = "订阅清单为空"
            else:
                req = urllib.request.Request(
                    f"https://weread.qq.com/api/mp/cover?bookId={probe}",
                    headers={"Cookie": ck, "User-Agent": "Mozilla/5.0 quant-platform/probe",
                             "Referer": "https://weread.qq.com/"},
                )
                with urllib.request.urlopen(req, timeout=8) as r:
                    payload = _json.loads(r.read().decode("utf-8", "ignore"))
                if payload.get("statusCode") == 401 or payload.get("errcode") in (-2010, -2012, -2041):
                    cookie_err = f"{payload.get('errcode')} {payload.get('errmsg', '')}".strip()
                else:
                    cookie_ok = True
        except urllib.error.HTTPError as e:
            cookie_err = f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            cookie_err = f"{type(e).__name__}: {str(e)[:80]}"

    # ④ 结论
    if not cookie_ok:
        level = "fail"
        verdict = f"微信读书凭证不可用（{cookie_err}）—— 公众号采集会全线失败，需重新扫码"
    elif not feeds:
        level = "warn"
        verdict = "订阅清单为空（data/wechat/feeds.json），采集不会跑任何号"
    elif not last:
        level = "warn"
        verdict = "尚未跑过采集（等 08:20 / 20:40 定时，或手动触发一次）"
    elif last.get("error"):
        level = "warn"
        verdict = f"上次采集凭证失效：{last['error']}"
    elif last.get("ok"):
        level = "ok"
        verdict = (f"正常 · 上次 {last.get('at', '')} 新增 {last.get('new', 0)} 篇"
                   f"（已最新 {last.get('skip', 0)}，失败 {last.get('fail', 0)}）")
    else:
        level = "warn"
        verdict = "上次采集未成功，详见 last_run"

    return {
        "level": level, "verdict": verdict, "source": "weread",
        "cookie": {"ok": cookie_ok, "vid": cookie_vid, "error": cookie_err},
        "feeds": feeds, "last_run": last,
    }


@router.post("/collect")
def collect(limit: int = 0):
    """手动触发一次采集（子进程执行，接口立即返回）。

    ⚠️ 必须子进程：采集要 6-8 分钟且带 3 秒限频，放进 uvicorn 线程会拖垮服务
    （本项目定过规矩：长任务一律 subprocess + 独立进程）。
    """
    import subprocess
    import sys
    from pathlib import Path as _P

    here = _P(__file__).resolve()
    script = None
    for d in (_P("/app/scripts/wechat"), here.parents[2] / "scripts" / "wechat",
              here.parents[2] / "backend" / "scripts" / "wechat"):
        if (d / "collect_weread.py").exists():
            script = d / "collect_weread.py"
            break
    if script is None:
        raise HTTPException(status_code=500, detail="找不到 collect_weread.py")

    cmd = [sys.executable, str(script), "--interval", "3"]
    if limit:
        cmd += ["--limit", str(limit)]
    subprocess.Popen(cmd, cwd=str(script.parent),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return {"ok": True, "note": "采集已在后台启动，60 秒后刷新本页看结果"}
