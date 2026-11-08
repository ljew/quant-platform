"""境外资讯三槽调度（OPS-FN-1a）。

槽：07:00 / 12:30 / 21:00 Asia/Shanghai（本机/容器已按 CST）。
独立于国内 news_feed 10min 循环；QUANT_FOREIGN_NEWS 缺省开。
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime

logger = logging.getLogger("foreign_news_scheduler")

# (hour, minute)
SLOTS: tuple[tuple[int, int], ...] = ((7, 0), (12, 30), (21, 0))

_status = {
    "enabled": False,
    "last_run_at": None,
    "last_result": None,
    "last_error": None,
    "runs_total": 0,
    "last_slot": None,
    "sources": {},          # per-source 快照
}
_lock = threading.Lock()
_sched_thread: threading.Thread | None = None
_done_slots: set[str] = set()   # "YYYY-MM-DD#HH:MM"


def get_status() -> dict:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    return {
        **_status,
        "slots": [f"{h:02d}:{m:02d}" for h, m in SLOTS],
        "timezone": "Asia/Shanghai",
        "done_today": sorted(k for k in _done_slots if k.startswith(today)),
        "next_slot": _next_slot(now),
    }


def _slot_key(day: str, hour: int, minute: int) -> str:
    return f"{day}#{hour:02d}:{minute:02d}"


def _next_slot(now: datetime) -> str:
    for h, m in SLOTS:
        if (now.hour, now.minute) < (h, m):
            return f"{h:02d}:{m:02d}"
    return f"明天 {SLOTS[0][0]:02d}:{SLOTS[0][1]:02d}"


def _due_slot(now: datetime) -> tuple[int, int] | None:
    """当前分钟是否命中某槽，且今日尚未跑过。"""
    for h, m in SLOTS:
        if now.hour == h and now.minute == m:
            key = _slot_key(now.strftime("%Y-%m-%d"), h, m)
            if key not in _done_slots:
                return h, m
    return None


def _run_once() -> dict:
    from app.database import SessionLocal
    from app.services import foreign_news

    db = SessionLocal()
    try:
        return foreign_news.sync(db)
    finally:
        db.close()


def _summarize(res: dict) -> str:
    added = sum(int(v.get("added") or 0) for v in res.values()
                if isinstance(v, dict) and int(v.get("added") or 0) > 0)
    failed = [k for k, v in res.items()
              if isinstance(v, dict) and v.get("status") in ("fail", "degraded")]
    unconf = [k for k, v in res.items()
              if isinstance(v, dict) and v.get("status") == "unconfigured"]
    parts = [f"+{added} 条"]
    if failed:
        parts.append(f"异常 {','.join(failed)}")
    if unconf:
        parts.append(f"未配置 {','.join(unconf)}")
    return "；".join(parts)


def _loop(enabled: bool) -> None:
    while True:
        try:
            if enabled:
                now = datetime.now()
                due = _due_slot(now)
                if due is not None:
                    h, m = due
                    slot_key = _slot_key(now.strftime("%Y-%m-%d"), h, m)
                    try:
                        res = _run_once()
                        summary = _summarize(res)
                        with _lock:
                            _status["last_run_at"] = now.isoformat(timespec="seconds")
                            _status["last_result"] = summary
                            _status["last_error"] = None
                            _status["runs_total"] += 1
                            _status["last_slot"] = f"{h:02d}:{m:02d}"
                            _status["sources"] = {
                                k: {
                                    "added": v.get("added", 0),
                                    "status": v.get("status"),
                                    "error": (v.get("error") or "")[:120],
                                }
                                for k, v in res.items() if isinstance(v, dict)
                            }
                        _done_slots.add(slot_key)
                        # 只留近两天，防集合膨胀
                        from datetime import timedelta as _td
                        keep_prefix = (
                            now.strftime("%Y-%m-%d"),
                            (now - _td(days=1)).strftime("%Y-%m-%d"),
                        )
                        for k in list(_done_slots):
                            if not any(k.startswith(p) for p in keep_prefix):
                                _done_slots.discard(k)
                        logger.info("境外资讯槽 %02d:%02d 完成：%s", h, m, summary)
                    except Exception as e:  # noqa: BLE001
                        with _lock:
                            _status["last_error"] = f"{type(e).__name__}: {e}"[:200]
                        # 失败不记 done，同分钟内循环还会再试；下一分钟放弃
                        logger.warning("境外资讯槽 %02d:%02d 失败：%s", h, m, e)
                        _done_slots.add(slot_key)  # 避免同分钟死循环；下一槽再试
        except Exception as e:  # noqa: BLE001
            with _lock:
                _status["last_error"] = f"{type(e).__name__}: {e}"[:200]
            logger.warning("境外资讯调度循环异常：%s", e)
        threading.Event().wait(60)


def start_foreign_news_scheduler(enabled: bool | None = None) -> threading.Thread:
    """启动守护线程。enabled 缺省读 QUANT_FOREIGN_NEWS（默认开）。"""
    global _sched_thread
    if _sched_thread is not None and _sched_thread.is_alive():
        logger.info("调度器已在运行，跳过重复启动（foreign_news）")
        return _sched_thread
    if enabled is None:
        enabled = os.getenv("QUANT_FOREIGN_NEWS", "1") not in ("0", "", "false", "False")
    with _lock:
        _status["enabled"] = enabled
    t = threading.Thread(target=_loop, args=(enabled,), daemon=True, name="foreign-news")
    t.start()
    _sched_thread = t
    logger.info(
        "境外资讯调度器已启动（enabled=%s，槽 %s Asia/Shanghai）",
        enabled, ", ".join(f"{h:02d}:{m:02d}" for h, m in SLOTS),
    )
    return t
