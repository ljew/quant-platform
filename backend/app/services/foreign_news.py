"""境外资讯日爬编排（OPS-FN-1a）。

拉 RSS / X → 复用 news_feed.ingest / _rows 入库；整批硬顶 90s；
单源异常记错继续。不改表结构，靠 source 前缀 fn_* / fn_x 区分。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import NewsFeedItem
from app.services import news_feed
from app.services.foreign_news_sources import (
    FOREIGN_SOURCE_LABELS,
    FOREIGN_SOURCES,
    all_foreign_keys,
    get_source,
)

logger = logging.getLogger("foreign_news")

BATCH_TIMEOUT_SEC = 90.0

# 把境外标签并入统一流展示名（含默认关）
for _k, _lab in FOREIGN_SOURCE_LABELS.items():
    news_feed.SOURCE_LABELS.setdefault(_k, _lab)

# 进程内 per-source 运行态（对标调度器内存快照）
_source_state: dict[str, dict[str, Any]] = {}
_state_lock = threading.Lock()


def _set_state(key: str, **kwargs: Any) -> None:
    with _state_lock:
        cur = _source_state.setdefault(key, {})
        cur.update(kwargs)


def get_source_state(key: str) -> dict[str, Any]:
    with _state_lock:
        return dict(_source_state.get(key) or {})


def _normalize_items(source_key: str, items: list[dict], tags: list[str] | None) -> list[dict]:
    """补 extra.lang / tags；主键已由适配器写在 key。"""
    out = []
    for it in items:
        extra = dict(it.get("extra") or {})
        extra.setdefault("lang", "en")
        if tags:
            extra.setdefault("tags", list(tags))
        # 主键优先 url → guid/tweet_id → title+published_at（适配器已尽量给 key）
        key = it.get("key") or it.get("url") or ""
        if not key:
            guid = extra.get("guid") or extra.get("tweet_id") or ""
            pub = it.get("published_at")
            pub_s = pub.isoformat(timespec="seconds") if hasattr(pub, "isoformat") else str(pub or "")
            key = guid or f"{it.get('title') or ''}|{pub_s}"
        out.append({
            "key": key,
            "title": it.get("title") or "",
            "summary": it.get("summary") or "",
            "url": it.get("url") or "",
            "published_at": it.get("published_at"),
            "extra": extra,
        })
    return out


def _fetch_one(src: dict[str, Any]) -> list[dict]:
    kind = src.get("kind")
    if kind == "rss":
        from app.services.foreign_news_rss import fetch_rss
        url = src.get("url") or ""
        if not url:
            raise RuntimeError("缺少 RSS URL")
        return fetch_rss(url)
    if kind == "x":
        from app.services.foreign_news_x import fetch_x
        return fetch_x()
    raise RuntimeError(f"未知源类型：{kind}")


def sync_one(db: Session, source_key: str) -> dict[str, Any]:
    """同步单源，返回 {added, status, error}。"""
    src = get_source(source_key)
    if src is None:
        return {"added": 0, "status": "fail", "error": f"未知源 {source_key}"}
    if not src.get("enabled_default"):
        _set_state(source_key, status="disabled", last_error="")
        return {"added": 0, "status": "disabled", "error": ""}

    if src.get("kind") == "x":
        from app.services.foreign_news_x import bearer_configured
        if not bearer_configured():
            _set_state(source_key, status="unconfigured", last_error="未配置 QUANT_X_BEARER",
                       last_count=0)
            return {"added": 0, "status": "unconfigured",
                    "error": "未配置 X API，RSS 源仍可用"}

    try:
        raw = _fetch_one(src)
        items = _normalize_items(source_key, raw, src.get("tags") or [])
        added = news_feed.ingest(db, source_key, items)
        if added < 0:
            raise RuntimeError("入库失败")
        _set_state(
            source_key,
            status="ok",
            last_ok_at=datetime.now().isoformat(timespec="seconds"),
            last_count=int(added),
            last_error="",
        )
        return {"added": int(added), "status": "ok", "error": ""}
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:200]
        # 脱敏：绝不把 Bearer 片段写进状态
        if "Bearer" in msg or "bearer" in msg:
            msg = "认证失败（细节已脱敏）"
        status = "unconfigured" if "unconfigured" in msg.lower() else "fail"
        if "429" in msg or "配额" in msg:
            status = "degraded"
        _set_state(source_key, status=status, last_error=msg, last_count=0)
        logger.warning("境外源 %s 失败：%s", source_key, msg)
        return {"added": 0, "status": status, "error": msg}


def sync(db: Session, keys: list[str] | None = None,
         batch_timeout: float = BATCH_TIMEOUT_SEC) -> dict[str, Any]:
    """整批同步。超时停后续源，已写入保留。返回 per-source 结果。"""
    t0 = time.monotonic()
    if keys:
        targets = [get_source(k) for k in keys]
        targets = [s for s in targets if s]
    else:
        targets = list(FOREIGN_SOURCES)

    results: dict[str, Any] = {}
    for src in targets:
        key = src["key"]
        # 默认关的也进结果（disabled），但不拉网
        if not src.get("enabled_default") and not (keys and key in keys):
            results[key] = {"added": 0, "status": "disabled", "error": ""}
            _set_state(key, status="disabled", last_error="")
            continue
        if time.monotonic() - t0 >= batch_timeout:
            results[key] = {"added": 0, "status": "degraded",
                            "error": f"整批超时 {int(batch_timeout)}s，本源未跑"}
            _set_state(key, status="degraded",
                       last_error=results[key]["error"])
            # 后续一并标记
            continue
        results[key] = sync_one(db, key)

    # 超时后未跑到的源
    for src in targets:
        key = src["key"]
        if key not in results:
            results[key] = {"added": 0, "status": "degraded",
                            "error": f"整批超时 {int(batch_timeout)}s，本源未跑"}

    total = sum(int(v.get("added") or 0) for v in results.values() if int(v.get("added") or 0) > 0)
    if total:
        logger.info("境外资讯同步 +%d 条", total)
    # 与国内同 prune
    try:
        news_feed.prune(db)
    except Exception as e:  # noqa: BLE001
        logger.warning("境外同步后 prune 失败：%s", e)
    return results


def query_foreign(db: Session, sources: list[str] | None = None, q: str = "",
                  hours: int = 0, limit: int = 50, offset: int = 0) -> dict:
    """只查 fn_* / fn_x。"""
    keys = sources if sources else all_foreign_keys()
    keys = [k for k in keys if k.startswith("fn_")]
    if not keys:
        keys = all_foreign_keys()
    return news_feed.query(db, sources=keys, q=q, hours=hours, limit=limit, offset=offset)


def sources_status(db: Session, hours: int = 72) -> list[dict[str, Any]]:
    """源状态条：注册表 ∪ 运行态 ∪ 近 N 小时条数。密钥永不回传（仅 bool configured）。"""
    from app.services.foreign_news_x import bearer_configured

    x_ok = bearer_configured()
    cutoff = datetime.now() - timedelta(hours=hours) if hours else None
    stmt = (
        select(NewsFeedItem.source, func.count(), func.max(NewsFeedItem.published_at))
        .where(NewsFeedItem.source.in_(all_foreign_keys()))
        .group_by(NewsFeedItem.source)
    )
    if cutoff is not None:
        stmt = stmt.where(NewsFeedItem.published_at >= cutoff)
    got = {r[0]: (int(r[1]), r[2]) for r in db.execute(stmt).all()}

    out: list[dict[str, Any]] = []
    for src in FOREIGN_SOURCES:
        key = src["key"]
        st = get_source_state(key)
        cnt, latest = got.get(key, (0, None))
        enabled = bool(src.get("enabled_default"))
        status = st.get("status")
        if not enabled:
            status = "disabled"
        elif key == "fn_x" and not x_ok:
            status = "unconfigured"
        elif not status:
            status = "ok" if cnt > 0 else ("disabled" if not enabled else "ok")

        row = {
            "source": key,
            "label": src["label"],
            "kind": src.get("kind"),
            "enabled": enabled,
            "last_ok_at": st.get("last_ok_at"),
            "last_count": st.get("last_count", 0),
            "last_error": st.get("last_error") or "",
            "status": status,
            "count": cnt,
            "latest": latest.isoformat(timespec="minutes") if latest else None,
        }
        if key == "fn_x":
            row["configured"] = bool(x_ok)  # 仅布尔，无密钥
        out.append(row)
    return out


def digest(db: Session, hours: int = 24, limit: int = 40) -> dict:
    """境外日摘要：最近 hours 标题列表 + 可选短拼接文本；不强制调 LLM。

    供 WorkBuddy / 人工粘贴 / Reports 链出。1b 交付。
    """
    hours = max(0, min(int(hours or 24), 24 * 120))
    limit = max(1, min(int(limit or 40), 80))
    got = query_foreign(db, hours=hours, limit=limit, offset=0)
    items = []
    for it in got.get("items") or []:
        items.append({
            "title": it.get("title") or "",
            "source": it.get("source") or "",
            "source_label": it.get("source_label") or "",
            "published_at": it.get("published_at"),
            "url": it.get("url") or "",
            "summary": (it.get("summary") or "")[:200],
        })
    lines = [
        f"- [{x['source_label'] or x['source']}] {x['title']}"
        for x in items if x.get("title")
    ]
    text = "\n".join(lines)
    return {
        "hours": hours,
        "count": len(items),
        "total": got.get("total", len(items)),
        "items": items,
        "text": text,
        "llm": False,  # 本接口不调 LLM
    }

