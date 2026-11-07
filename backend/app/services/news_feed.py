"""统一资讯流：把各来源的原文条目归一入库，供「全部资讯」页浏览与检索。

## 为什么要单独一张表

之前每个来源各有一个入口：东财/新浪在首页右栏、财联社只喂给简报、
公众号有自己的页面、tushare/股吧落在 bronze parquet 里没有界面。
老刘要的是「一个界面对比所有源头」，所以必须先有统一结构。

## 两条腿：快源 / 慢源

- **快源**（10 分钟一同步）：财联社电报、东财头条、新浪滚动
  —— 这三家是实时流，页面刷新就该看到新的
- **慢源**（每小时一同步）：公众号、tushare 资讯、股吧
  —— 公众号每天只在 06:20/20:40 采两轮；tushare/股吧由 ETL 每日落 parquet，
  抄进来自的是「统一界面能看到」，不是实时性

## 幂等

`uid = sha1(f"{source}|{自然主键}")[:32]`，靠唯一约束 + `ON CONFLICT DO NOTHING`。
重复同步不会长胖，也不需要先查后插（那样每条都要一次往返）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import NewsFeedItem

logger = logging.getLogger("news_feed")

# source key → 展示名。顺序即页面上标签的顺序（快源在前）。
# 不含 cctv：tushare 的 major_news 与 cctv_news 在采集器里已合成一份，
# parquet 层分不开，留一个恒为 0 的标签只会让人以为坏了。
SOURCE_LABELS: dict[str, str] = {
    "cls": "财联社",
    "eastmoney": "东财",
    "sina": "新浪",
    "tushare": "tushare资讯",
    "wechat": "公众号",
    "guba": "股吧",
}

FAST_SOURCES = ("cls", "eastmoney", "sina")
SLOW_SOURCES = ("wechat", "tushare", "guba")

RETENTION_DAYS = 120          # 超过这个天数的条目在同步时清理


def _uid(source: str, key: str) -> str:
    return hashlib.sha1(f"{source}|{key}".encode("utf-8")).hexdigest()[:32]


def _to_dt(v) -> datetime | None:
    """各源时间口径不同，统一成 datetime：unix 秒 / ISO 字符串 / None。"""
    if v is None or v == "" or v == 0:
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(float(v))
        s = str(v).strip()
        if s.isdigit():
            return datetime.fromtimestamp(int(s))
        # "2026-11-07 07:34:40" / "2026-11-07T10:34" 都能被 fromisoformat 吃下
        return datetime.fromisoformat(s)
    except Exception:  # noqa: BLE001
        return None


_CLS_PREFIX = re.compile(r"^财联社\s*\d+月\d+日电[，,：:]\s*")


def _rows(source: str, items: list[dict]) -> list[dict]:
    """把归一化后的条目转成待插入的行（自动补 uid / label / 默认值）。"""
    out = []
    now = datetime.now()
    for it in items:
        title = (it.get("title") or "").strip()
        if len(title) < 4:            # 纯符号、空标题没意义
            continue
        key = it.get("key") or it.get("url") or title
        summary = it.get("summary") or ""
        if source == "cls":
            # 电头在统一流里是冗余的（左侧已有「财联社」标签）
            summary = _CLS_PREFIX.sub("", summary)
        out.append({
            "uid": _uid(source, str(key)),
            "source": source,
            "source_label": SOURCE_LABELS.get(source, source),
            "title": title[:400],
            "summary": summary[:600],
            "url": (it.get("url") or "")[:600],
            "published_at": _to_dt(it.get("published_at")),
            "fetched_at": now,
            "symbols": (it.get("symbols") or "")[:200],
            "extra_json": json.dumps(it.get("extra") or {}, ensure_ascii=False),
        })
    return out


def _insert(db: Session, rows: list[dict]) -> int:
    """批量插入，冲突忽略。返回真正写入的条数。"""
    if not rows:
        return 0
    n = 0
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        stmt = sqlite_insert(NewsFeedItem).values(chunk).on_conflict_do_nothing(
            index_elements=["uid"]
        )
        res = db.execute(stmt)
        n += res.rowcount or 0
    db.commit()
    return n


# ───────────────────────── 各源适配器 ─────────────────────────

def _fetch_cls() -> list[dict]:
    from app.datahub.extractors.cls_telegraph import ClsTelegraphExtractor

    df = ClsTelegraphExtractor().fetch(rn=50, pages=3)
    if df is None or df.empty:
        return []
    return [
        {"key": r.get("article_id"), "title": r.get("title"),
         "summary": r.get("content_text"), "published_at": r.get("publish_time"),
         "extra": {"kind": "快讯"}}
        for _, r in df.iterrows()
    ]


def _fetch_eastmoney() -> list[dict]:
    from app.services.news_agg import eastmoney_headlines

    return [
        {"key": x.get("url") or x.get("title"), "title": x.get("title"),
         "url": x.get("url") or "", "published_at": x.get("time"),
         "extra": {"orig_source": x.get("source") or ""}}
        for x in (eastmoney_headlines() or [])
    ]


def _fetch_sina() -> list[dict]:
    from app.services.news_agg import sina_roll

    return [
        {"key": x.get("url") or x.get("title"), "title": x.get("title"),
         "url": x.get("url") or "", "published_at": x.get("time"),
         "extra": {"orig_source": x.get("source") or ""}}
        for x in (sina_roll() or [])
    ]


def _fetch_wechat(limit: int = 400) -> list[dict]:
    """直接从公众号库读（不再复制一份正文，只留标题/摘要/链接）。"""
    from app.config import settings

    path = os.path.join(settings.wechat_data_dir, "knowledge.db")
    if not os.path.exists(path):
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        rows = con.execute(
            "select id, mp_name, title, url, publish_time, substr(content_text,1,500) "
            "from articles order by publish_time desc limit ?", (limit,)
        ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        title = r[2] or ""
        mp = r[1] or ""
        out.append({
            "key": r[0], "title": title, "url": r[3] or "",
            "published_at": r[4], "summary": r[5] or "",
            # 公众号标题常不带来源，前缀一下方便在统一流里辨识
            "extra": {"mp_name": mp, "kind": "公众号"},
        })
    return out


def _fetch_parquet(kind: str, source: str, limit: int = 600) -> list[dict]:
    """从 bronze parquet 读最近一批（tushare 资讯 / 股吧）。"""
    import glob

    import pandas as pd

    files = sorted(glob.glob(f"/data/raw/text/{kind}/*.parquet"))
    if not files:
        return []
    try:
        df = pd.read_parquet(files[-1])
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 %s parquet 失败：%s", kind, e)
        return []
    if df is None or df.empty:
        return []

    out = []
    for _, r in df.head(limit).iterrows():
        if source == "guba":
            # 股吧的 key 形如 "东山精密:H股公告"，没有正文也没有 URL
            out.append({"key": r.get("key"), "title": r.get("key"),
                        "published_at": r.get("date"),
                        "symbols": str(r.get("code") or ""),
                        "extra": {"heat": r.get("heat"), "kind": "股吧"}})
        else:
            src = str(r.get("source") or "")
            out.append({"key": r.get("article_id"), "title": r.get("title"),
                        "summary": r.get("content_text"),
                        "published_at": r.get("publish_time"),
                        "extra": {"orig_source": src,
                                  "kind": "新闻联播" if "cctv" in src.lower() else "资讯"}})
    return out


# ───────────────────────── 同步 ─────────────────────────

def sync_fast(db: Session) -> dict:
    """快源：财联社 / 东财 / 新浪。任何一路失败不影响其它。"""
    out: dict[str, int] = {}
    for source, fn in (("cls", _fetch_cls), ("eastmoney", _fetch_eastmoney),
                       ("sina", _fetch_sina)):
        try:
            out[source] = _insert(db, _rows(source, fn()))
        except Exception as e:  # noqa: BLE001
            logger.warning("同步 %s 失败：%s", source, e)
            out[source] = -1
    return out


def sync_slow(db: Session) -> dict:
    """慢源：公众号 / tushare（公众号每天采两轮，tushare 靠 ETL 每日落 parquet）。"""
    out: dict[str, int] = {}
    try:
        out["wechat"] = _insert(db, _rows("wechat", _fetch_wechat()))
    except Exception as e:  # noqa: BLE001
        logger.warning("同步公众号失败：%s", e)
        out["wechat"] = -1
    try:
        out["tushare"] = _insert(db, _rows("tushare", _fetch_parquet("tushare_news", "tushare")))
    except Exception as e:  # noqa: BLE001
        logger.warning("同步 tushare 失败：%s", e)
        out["tushare"] = -1
    return out


def sync_guba(db: Session) -> int:
    """股吧：独立节奏（比慢源频繁），失败时由调度器指数退避。

    ⚠️ 2026-11-07 实测：东财对 `guba.eastmoney.com/list,<code>.html` 上了滑块风控，
    容器 IP 拿到的是「身份核实」空壳页（2826 字节、无 `var article_list`），
    旧的内嵌 JSON 抓法失效；`gbapi` 的 Articlelist 也变成 `re: []`。
    所以**抓不到是常态而非异常**：这里失败只返回 0，不抛错、不刷 warning 日志，
    由调度器拉长间隔，等风控解除后自然恢复。
    """
    rows = []
    try:
        from app.datahub.extractors.guba_hot import GubaHotExtractor

        df = GubaHotExtractor().fetch(top_n=10, max_posts=40)
        if df is not None and not df.empty:
            rows = [{"key": r.get("key"), "title": r.get("key"),
                     "published_at": r.get("date"),
                     "symbols": str(r.get("code") or ""),
                     "extra": {"heat": r.get("heat"), "kind": "股吧"}}
                    for _, r in df.iterrows()]
    except Exception as e:  # noqa: BLE001
        logger.info("股吧实时抓取异常（多为风控）：%s", str(e)[:80])
    if not rows:
        # 回退到 ETL 落的 parquet：至少让当日数据进统一流，别整段空着
        try:
            rows = _fetch_parquet("guba_hot", "guba")
        except Exception:  # noqa: BLE001
            rows = []
    try:
        return _insert(db, _rows("guba", rows))
    except Exception as e:  # noqa: BLE001
        logger.warning("写入股吧失败：%s", e)
        return -1


def prune(db: Session, days: int = RETENTION_DAYS) -> int:
    cutoff = datetime.now() - timedelta(days=days)
    res = db.query(NewsFeedItem).filter(NewsFeedItem.published_at < cutoff).delete()
    db.commit()
    if res:
        logger.info("清理 %s 天前的资讯 %d 条", days, res)
    return int(res or 0)


def sync(db: Session, with_slow: bool = False, with_guba: bool = False) -> dict:
    res = sync_fast(db)
    if with_slow:
        res.update(sync_slow(db))
    if with_guba:
        res["guba"] = sync_guba(db)
    prune(db)
    total = sum(v for v in res.values() if v > 0)
    if total:
        logger.info("资讯同步 +%d 条 %s", total, res)
    return res


# ───────────────────────── 查询 ─────────────────────────

def query(db: Session, sources: list[str] | None = None, q: str = "",
          hours: int = 0, limit: int = 50, offset: int = 0) -> dict:
    """按来源 / 关键词 / 时间窗查询，返回 {items, total, has_more}。"""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    conds = []
    if sources:
        conds.append(NewsFeedItem.source.in_(sources))
    if hours and hours > 0:
        conds.append(NewsFeedItem.published_at >= datetime.now() - timedelta(hours=hours))
    if q:
        like = f"%{q.strip()}%"
        conds.append(or_(NewsFeedItem.title.like(like),
                         NewsFeedItem.summary.like(like)))

    base = select(NewsFeedItem)
    count_q = select(func.count()).select_from(NewsFeedItem)
    for c in conds:
        base = base.where(c)
        count_q = count_q.where(c)

    total = int(db.execute(count_q).scalar() or 0)
    rows = db.execute(
        base.order_by(NewsFeedItem.published_at.desc().nullslast(),
                      NewsFeedItem.id.desc())
        .limit(limit).offset(offset)
    ).scalars().all()

    items = [{
        "id": r.id,
        "source": r.source,
        "source_label": r.source_label,
        "title": r.title,
        # 入库时已剥过电头，但历史行是在改动前写的，这里再兜一次，
        # 免得页面上新旧条目一个带「财联社11月7日电」一个不带。
        "summary": _CLS_PREFIX.sub("", r.summary) if r.source == "cls" else r.summary,
        "url": r.url,
        "published_at": r.published_at.isoformat(timespec="minutes") if r.published_at else None,
        "symbols": r.symbols,
    } for r in rows]
    return {"items": items, "total": total, "has_more": offset + len(items) < total}


def source_stats(db: Session, hours: int = 72) -> list[dict]:
    """各来源最近 N 小时的条数，用于页面顶部标签。"""
    cutoff = datetime.now() - timedelta(hours=hours) if hours else None
    stmt = select(NewsFeedItem.source, func.count(),
                  func.max(NewsFeedItem.published_at)).group_by(NewsFeedItem.source)
    if cutoff is not None:
        stmt = stmt.where(NewsFeedItem.published_at >= cutoff)
    got = {r[0]: (int(r[1]), r[2]) for r in db.execute(stmt).all()}
    out = []
    for key, label in SOURCE_LABELS.items():
        cnt, latest = got.pop(key, (0, None))
        out.append({"key": key, "label": label, "count": cnt,
                    "latest": latest.isoformat(timespec="minutes") if latest else None})
    # 未知来源也不丢（以后加源不会静默消失）
    for key, (cnt, latest) in got.items():
        out.append({"key": key, "label": key, "count": cnt,
                    "latest": latest.isoformat(timespec="minutes") if latest else None})
    return out
