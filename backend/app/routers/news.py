"""资讯聚合 API（东财要闻 / 新浪快讯 / 腾讯外盘 / 雪球热榜 / 个股新闻公告）。

设计要点：
1. **并发拉取** —— 4 个外部源串行要 3~5s，线程池并行后降到最慢那个（约 1s）。
2. **TTL 缓存** —— 外部源都不是关键路径，且雪球/东财对频率敏感。缓存 5 分钟，
   进页面秒开、反复刷新不会打爆对方；上游全挂时缓存再兜 1 小时。
3. **不阻断** —— 任一源失败只返回空列表（news_agg 内部已吞异常），
   页面显示「该来源暂不可用」而不是整页报错。
"""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, Query

from app.database import get_db
from app.services import news_agg

router = APIRouter(prefix="/news", tags=["news"])

_TTL_SEC = 300.0          # 正常缓存有效期
_STALE_SEC = 3600.0       # 上游全失败时，旧缓存最多再兜 1 小时
_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def _get_cached(key: str):
    with _lock:
        hit = _cache.get(key)
    return hit if hit else (None, None)


def _put_cached(key: str, value: dict) -> None:
    with _lock:
        _cache[key] = (time.time(), value)


@router.get("/feed")
def feed(refresh: bool = Query(False, description="忽略缓存强制刷新")):
    """聚合资讯流：要闻 + 快讯 + 全球行情 + 雪球热榜。"""
    key = "feed"
    ts, val = _get_cached(key)
    if not refresh and val is not None and (time.time() - ts) < _TTL_SEC:
        return {**val, "cached": True, "age_sec": round(time.time() - ts, 1)}

    with ThreadPoolExecutor(max_workers=4) as ex:
        f_hl = ex.submit(news_agg.eastmoney_headlines, 20)
        f_sina = ex.submit(news_agg.sina_roll, 30)
        f_quote = ex.submit(news_agg.global_quotes)
        f_hot = ex.submit(news_agg.xueqiu_hot, 20)
        try:
            headlines = f_hl.result(timeout=20)
        except Exception:  # noqa: BLE001
            headlines = []
        try:
            sina = f_sina.result(timeout=20)
        except Exception:  # noqa: BLE001
            sina = []
        try:
            quotes = f_quote.result(timeout=20)
        except Exception:  # noqa: BLE001
            quotes = []
        try:
            hot = f_hot.result(timeout=20)
        except Exception:  # noqa: BLE001
            hot = []

    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "headlines": headlines,
        "sina_roll": sina,
        "global_quotes": quotes,
        "xueqiu_hot": hot,
        "sources_ok": {
            "eastmoney": bool(headlines), "sina": bool(sina),
            "tencent": bool(quotes), "xueqiu": bool(hot),
        },
        "cached": False,
        "age_sec": 0.0,
    }
    # 上游一个都没通 → 不覆盖旧缓存，直接把上次成功的结果继续供出去
    if not any((headlines, sina, quotes, hot)):
        if ts and (time.time() - ts) < _STALE_SEC:
            return {**val, "cached": True, "stale": True,
                    "age_sec": round(time.time() - ts, 1)}
    else:
        _put_cached(key, payload)
    return payload


@router.get("/stock")
def stock(symbol: str = Query(..., description="平台符号 sh600519 / 纯 6 位 600519"),
          num: int = Query(12, ge=1, le=30)):
    """个股新闻 + 公告（东财搜索 / 公告接口）。"""
    digits = re.sub(r"\D", "", symbol)[-6:]
    if len(digits) != 6:
        return {"ok": False, "error": f"无法解析代码：{symbol}", "items": []}
    key = f"stock:{digits}"
    ts, val = _get_cached(key)
    if val is not None and (time.time() - ts) < _TTL_SEC:
        return {**val, "cached": True, "age_sec": round(time.time() - ts, 1)}
    items = news_agg.stock_news(digits, num=num)
    payload = {"ok": True, "symbol": digits, "items": items, "cached": False, "age_sec": 0.0}
    if items:
        _put_cached(key, payload)
    return payload

# ───────────────────── 统一资讯流（全部来源归一）─────────────────────
# 老刘要求：各来源原始信息要能在「一个界面」里看，首页只留简报。
# 数据在 news_feed 表（services/news_feed.py 负责同步与查询）。

@router.get("/items")
def feed_items(
    sources: str = Query("", description="逗号分隔的来源 key，空=全部"),
    q: str = Query("", description="标题/摘要关键词"),
    hours: int = Query(0, ge=0, le=24 * 120, description="只看最近 N 小时，0=不限"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
):
    """统一资讯流分页查询（按发布时间倒序）。"""
    from app.services import news_feed

    srcs = [x.strip() for x in sources.split(",") if x.strip()]
    return news_feed.query(db, sources=srcs or None, q=q, hours=hours,
                           limit=limit, offset=offset)


@router.get("/sources")
def feed_sources(hours: int = Query(72, ge=0, le=24 * 120), db=Depends(get_db)):
    """各来源条数与最新时间（页面顶部标签）。"""
    from app.services import news_feed

    return news_feed.source_stats(db, hours=hours)


@router.post("/sync")
def feed_sync(with_slow: bool = True, with_guba: bool = True, db=Depends(get_db)):
    """手动触发一次同步（调度器自动跑：快源 10 分钟 / 慢源每小时 / 股吧 20 分钟）。"""
    from app.services import news_feed

    return news_feed.sync(db, with_slow=with_slow, with_guba=with_guba)


@router.get("/sync-status")
def feed_sync_status():
    """同步调度器状态（含股吧退避轮次，便于判断是不是被风控）。"""
    from app.core.news_feed_scheduler import get_status

    return get_status()


# ───────────────────── 境外资讯（OPS-FN-1a）─────────────────────
# 专用路径便于状态条与权限语义清晰；底层仍走 news_feed 表（source=fn_*）。


@router.get("/foreign/items")
def foreign_items(
    sources: str = Query("", description="逗号分隔的 fn_* 源，空=全部境外"),
    q: str = Query("", description="标题/摘要关键词"),
    hours: int = Query(24, ge=0, le=24 * 120, description="只看最近 N 小时，0=不限"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
):
    """境外资讯列表（按发布时间倒序）。"""
    from app.services import foreign_news

    srcs = [x.strip() for x in sources.split(",") if x.strip()]
    return foreign_news.query_foreign(
        db, sources=srcs or None, q=q, hours=hours, limit=limit, offset=offset,
    )


@router.get("/foreign/sources")
def foreign_sources(hours: int = Query(72, ge=0, le=24 * 120), db=Depends(get_db)):
    """境外源状态条：注册表 ∪ 运行态 ∪ 近 N 小时条数。密钥只回传 configured 布尔。"""
    from app.services import foreign_news

    return foreign_news.sources_status(db, hours=hours)


@router.post("/foreign/sync")
def foreign_sync(db=Depends(get_db)):
    """强制全源一轮（整批硬顶 90s）；返回 per-source {added, status, error}。"""
    from app.services import foreign_news

    return foreign_news.sync(db)


@router.get("/foreign/sync-status")
def foreign_sync_status():
    """境外资讯调度器状态（三槽 07:00/12:30/21:00）。"""
    from app.core.foreign_news_scheduler import get_status

    return get_status()
