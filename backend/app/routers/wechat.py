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
    """上游链路体检：wewe-rss 服务 / 二维码中转平台 / 已绑定的读书账号状态。

    为什么需要：扫码失败时原因可能在三处（本平台、wewe-rss、微信读书中转站），
    页面只看到「二维码加载中」时无从判断。这里一次性探明，直接给结论。

    ⚠️ 容器里访问宿主机 wewe-rss 必须用 host.docker.internal，127.0.0.1 是容器自身。
    """
    import json as _json
    import urllib.error
    import urllib.request

    from app.config import BASE_DIR

    def _probe(url: str, timeout: float = 4.0) -> tuple[bool, int | None, str]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "quant-platform/upstream-probe"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return True, r.status, ""
        except urllib.error.HTTPError as e:
            return False, e.code, f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            return False, None, f"{type(e).__name__}: {str(e)[:80]}"

    # ① wewe-rss 本体（宿主机 :4000）
    wewe_url = "http://host.docker.internal:4000"
    wewe_ok, wewe_code, wewe_err = _probe(f"{wewe_url}/dash")

    # ② 二维码中转平台：从 vendor 的 .env 读 PLATFORM_URL，读不到用默认
    platform_url = "https://weread.111965.xyz"
    env_path = BASE_DIR / "vendor" / "wechat-rss" / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("PLATFORM_URL="):
                platform_url = line.split("=", 1)[1].strip().strip('"').strip("'") or platform_url
                break
    except OSError:
        pass
    # 不要直接探 https://weread...：容器走 Cloudflare 会 403/502 抖动，宿主机却是另
    # 一个结果，探不准。改为**让 wewe-rss（宿主机进程）发起真实的取二维码请求** ——
    # 这正是用户点「添加账号」时走的同一条链路，成败即真相。
    qr_ok, qr_code, qr_err = False, None, ""
    try:
        body = _json.dumps({"0": {"json": None}}).encode()
        req = urllib.request.Request(
            f"{wewe_url}/trpc/platform.createLoginUrl?batch=1",
            data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
        payload = _json.loads(raw)
        if isinstance(payload, list) and payload and payload[0].get("result"):
            qr_ok, qr_code = True, r.status
        else:
            msg = (payload[0].get("error", {}) or {}).get("message", "") if isinstance(payload, list) else ""
            qr_code, qr_err = 502, msg or "取二维码失败"
    except urllib.error.HTTPError as e:
        qr_code, qr_err = e.code, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        qr_err = f"{type(e).__name__}: {str(e)[:80]}"

    # ③ 已绑定账号（tRPC account.list；status=1 有效、0 失效）
    accounts: list[dict] = []
    try:
        q = "%7B%220%22%3A%7B%22json%22%3Anull%7D%7D"
        with urllib.request.urlopen(f"{wewe_url}/trpc/account.list?batch=1&input={q}", timeout=6) as r:
            payload = _json.loads(r.read().decode("utf-8"))
        for it in payload[0]["result"]["data"]["items"]:
            accounts.append({"name": it.get("name"), "status": it.get("status"),
                             "updated_at": it.get("updatedAt")})
    except Exception:  # noqa: BLE001
        pass

    alive = [a for a in accounts if a["status"] == 1]
    if not wewe_ok:
        verdict = f"wewe-rss 不可用（{wewe_err}）—— 公众号抓取与扫码都停摆"
        level = "fail"
    elif not qr_ok:
        verdict = (f"二维码生成失败（{platform_url} → {qr_err or qr_code}）—— "
                   f"扫码弹不出二维码；这是第三方中转站的问题，等它恢复或改 PLATFORM_URL")
        level = "warn"
    elif accounts and not alive:
        verdict = f"{len(accounts)} 个读书账号全部失效，需要重新扫码登录"
        level = "warn"
    else:
        verdict = "上游正常"
        level = "ok"
    return {
        "level": level, "verdict": verdict,
        "wewe": {"ok": wewe_ok, "url": wewe_url, "status": wewe_code, "error": wewe_err},
        "qr_platform": {"ok": qr_ok, "url": platform_url, "status": qr_code, "error": qr_err},
        "accounts": accounts, "accounts_alive": len(alive),
    }
