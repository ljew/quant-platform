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

from fastapi import APIRouter, Query

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
