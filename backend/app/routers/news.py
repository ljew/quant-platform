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


# 资讯合流：设计文档 08 节要的是「一条时间倒序的流 + 顶部来源筛选器」。
# 原来四个 tab 的差别只是来源和加工深度，不是用法 —— 查一条消息不该先做分类题。
#
# 三个物理来源，四类语义分组：
#   news_feed      → domestic（cls/sina/guba/eastmoney/tushare/research/deepseek）
#                    foreign （fn_* 境外源）
#   market_reports → report  （四时段报告，是「加工过的观点」，不是原始快讯）
#   knowledge.db   → wechat  （公众号全文，news_feed 里那 567 条只是摘要）
_GROUP_CN = {"domestic": "国内快讯", "foreign": "境外", "wechat": "公众号", "report": "时段报告"}


def _group_of(source: str) -> str:
    if source.startswith("fn_"):
        return "foreign"
    if source == "wechat":
        return "wechat"
    return "domestic"


@router.get("/stream")
def stream(days: int = 2, groups: str = "", limit: int = 200, db=Depends(get_db)):
    """统一资讯流：一条时间倒序的流，来源用 groups 过滤。

    groups 逗号分隔（domestic,foreign,wechat,report），留空 = 全要。
    返回的每一条都带 group —— 前端筛选器靠它，不靠猜来源名。
    """
    import sqlite3
    from datetime import datetime, timedelta
    from pathlib import Path

    from sqlalchemy import text as _sql

    from app.config import BASE_DIR

    want = {g.strip() for g in (groups or "").split(",") if g.strip()}
    days = max(1, min(days, 30))
    limit = max(1, min(limit, 500))
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    items: list[dict] = []
    counts = {"domestic": 0, "foreign": 0, "wechat": 0, "report": 0}

    # ① news_feed：国内 + 境外快讯
    if not want or want & {"domestic", "foreign"}:
        rows = db.execute(
            _sql("select id, source, source_label, title, summary, url, published_at "
                 "from news_feed where published_at >= :since "
                 "order by published_at desc limit :lim"),
            {"since": since, "lim": limit * 2},
        ).mappings().all()
        for r in rows:
            g = _group_of(r["source"] or "")
            if want and g not in want:
                continue
            counts[g] += 1
            items.append({
                "id": f"nf{r['id']}", "group": g,
                "source": r["source"] or "", "source_cn": r["source_label"] or r["source"] or "",
                "title": r["title"] or "", "summary": (r["summary"] or "")[:300],
                "url": r["url"] or "", "at": (r["published_at"] or "")[:16],
            })

    # ② 四时段报告
    if not want or "report" in want:
        for r in db.execute(
            _sql("select report_id, report_date, slot, title, data_asof, generated_at "
                 "from market_reports where report_date >= :since "
                 "order by generated_at desc limit 20"),
            {"since": since[:10]},
        ).mappings().all():
            counts["report"] += 1
            items.append({
                "id": f"rp{r['report_id']}", "group": "report",
                "source": "report", "source_cn": f"四时段报告 · {r['slot']}",
                "title": r["title"] or "", "summary": f"数据截至 {r['data_asof'] or '—'}",
                "url": f"/api/v1/reports/{r['report_id']}/html",
                "at": (r["generated_at"] or "")[:16],
            })

    # ③ 公众号全文（knowledge.db 只读；它是另一个库，用独立连接）
    if not want or "wechat" in want:
        kdb = Path(BASE_DIR) / "data" / "wechat" / "knowledge.db"
        if kdb.exists():
            since_ts = int((datetime.now() - timedelta(days=days)).timestamp())
            try:
                c = sqlite3.connect(f"file:{kdb}?mode=ro", uri=True, timeout=5)
                try:
                    for r in c.execute(
                        "select id, mp_name, title, content_text, url, publish_time "
                        "from articles where publish_time >= ? order by publish_time desc limit ?",
                        (since_ts, limit),
                    ):
                        counts["wechat"] += 1
                        items.append({
                            "id": f"wx{r[0]}", "group": "wechat",
                            "source": "wechat", "source_cn": r[1] or "公众号",
                            "title": r[2] or "",
                            "summary": (r[3] or "").strip().replace("\n", " ")[:300],
                            "url": r[4] or "",
                            "at": datetime.fromtimestamp(r[5] or 0).strftime("%Y-%m-%d %H:%M"),
                        })
                finally:
                    c.close()
            except sqlite3.Error:
                pass  # 公众号库打不开不影响另外两类

    items.sort(key=lambda x: x["at"], reverse=True)
    return {
        "count": len(items[:limit]), "total": len(items),
        "since": since[:16], "days": days,
        "groups": {k: v for k, v in counts.items() if v},
        "group_cn": _GROUP_CN,
        "items": items[:limit],
    }


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


@router.get("/foreign/digest")
def foreign_digest(
    hours: int = Query(24, ge=1, le=24 * 120, description="最近 N 小时"),
    limit: int = Query(40, ge=1, le=80),
    db=Depends(get_db),
):
    """境外日摘要：标题列表 + 短拼接文本；不强制调 LLM（OPS-FN-1b）。"""
    from app.services import foreign_news

    return foreign_news.digest(db, hours=hours, limit=limit)

