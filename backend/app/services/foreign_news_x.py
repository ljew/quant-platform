"""境外 X（Twitter）API v2 适配器（OPS-FN-1a）。

密钥仅读环境变量 QUANT_X_BEARER；空 → unconfigured（由编排层标状态）。
禁止在日志中打印 Bearer。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

import httpx

from app.services.foreign_news_sources import DEFAULT_X_HANDLES

logger = logging.getLogger("foreign_news")

SOURCE_TIMEOUT = 10.0
_API = "https://api.twitter.com/2"


def bearer_configured() -> bool:
    return bool(os.getenv("QUANT_X_BEARER", "").strip())


def _token() -> str:
    return os.getenv("QUANT_X_BEARER", "").strip()


def _parse_created(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        iso = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:  # noqa: BLE001
        return None


def fetch_x(
    handles: list[str] | None = None,
    max_per_user: int = 5,
    timeout: float = SOURCE_TIMEOUT,
) -> list[dict[str, Any]]:
    """拉默认宏观账号最近推文，归一为 news_feed 条目。

    无 Bearer 时抛 RuntimeError("unconfigured")，由编排层转 status。
    """
    token = _token()
    if not token:
        raise RuntimeError("unconfigured")

    names = [h.strip().lstrip("@") for h in (handles or DEFAULT_X_HANDLES) if h and h.strip()]
    if not names:
        return []

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "QuantDesk/foreign-news",
    }
    items: list[dict[str, Any]] = []

    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        # 批量查 user id（一次最多 100）
        for i in range(0, len(names), 100):
            chunk = names[i:i + 100]
            r = client.get(
                f"{_API}/users/by",
                params={"usernames": ",".join(chunk), "user.fields": "username"},
            )
            if r.status_code == 401:
                raise RuntimeError("X API 401：Bearer 无效或已过期")
            if r.status_code == 429:
                raise RuntimeError("X API 429：配额超限")
            r.raise_for_status()
            payload = r.json() or {}
            users = payload.get("data") or []
            # errors 里可能有 not found；不中断
            by_name = {u.get("username", "").lower(): u for u in users if u.get("id")}

            for handle in chunk:
                u = by_name.get(handle.lower())
                if not u:
                    continue
                uid = u["id"]
                tr = client.get(
                    f"{_API}/users/{uid}/tweets",
                    params={
                        "max_results": max(5, min(max_per_user, 100)),
                        "tweet.fields": "created_at,text",
                        "exclude": "retweets,replies",
                    },
                )
                if tr.status_code == 429:
                    raise RuntimeError("X API 429：配额超限")
                if tr.status_code >= 400:
                    # 单账号失败继续
                    logger.info("X 拉取 @%s 失败：HTTP %s", handle, tr.status_code)
                    continue
                tweets = (tr.json() or {}).get("data") or []
                for tw in tweets:
                    tid = str(tw.get("id") or "")
                    text = (tw.get("text") or "").strip()
                    if len(text) < 4:
                        continue
                    # 标题取首行/前 120 字
                    title = text.splitlines()[0].strip() if text else ""
                    if len(title) > 120:
                        title = title[:117] + "..."
                    url = f"https://x.com/{handle}/status/{tid}" if tid else ""
                    items.append({
                        "key": tid or url or title,
                        "title": title,
                        "summary": text,
                        "url": url,
                        "published_at": _parse_created(tw.get("created_at")),
                        "extra": {
                            "lang": "en",
                            "handle": handle,
                            "tweet_id": tid,
                            "tags": ["x", "macro"],
                        },
                    })
    return items
