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
