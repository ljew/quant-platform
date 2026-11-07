"""统一资讯流同步调度：快源每 10 分钟、慢源每小时。

为什么独立成模块：跟 hot_brief（每小时、要调 LLM、可能几秒到几十秒）和
data_scheduler（ETL/验证，重活）分开，谁卡住了一眼能看出来。

为什么快慢分开：财联社/东财/新浪是实时流，10 分钟不同步页面就没新东西；
公众号每天只采两轮、tushare/股吧靠 ETL 每日落盘，每小时抄一次足够 ——
把它们也塞进 10 分钟循环，等于每小时白读 3 次 parquet + 查 400 篇公众号。
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime

logger = logging.getLogger("news_feed_scheduler")

FAST_INTERVAL_MIN = 10      # 快源同步间隔
SLOW_EVERY_HOUR_AT = 5      # 每小时的第 5 分钟顺带同步慢源

_status = {
    "enabled": False,
    "last_run_at": None,
    "last_result": None,
    "last_error": None,
    "runs_total": 0,
}
_lock = threading.Lock()
_sched_thread: threading.Thread | None = None


def get_status() -> dict:
    return {**_status, "fast_interval_min": FAST_INTERVAL_MIN}


def _run_once(with_slow: bool) -> tuple[bool, str]:
    from app.database import SessionLocal
    from app.services import news_feed

    db = SessionLocal()
    try:
        res = news_feed.sync(db, with_slow=with_slow)
    finally:
        db.close()
    added = sum(v for v in res.values() if v > 0)
    failed = [k for k, v in res.items() if v < 0]
    msg = f"+{added} 条" + (f"，失败 {','.join(failed)}" if failed else "")
    return (not failed or added > 0), msg


def _loop(enabled: bool) -> None:
    last_fast: datetime | None = None
    last_slow_hour = ""
    while True:
        try:
            if enabled:
                now = datetime.now()
                slow = (now.minute >= SLOW_EVERY_HOUR_AT
                        and last_slow_hour != now.strftime("%Y-%m-%d#%H"))
                due_fast = (last_fast is None
                            or (now - last_fast).total_seconds() >= FAST_INTERVAL_MIN * 60)
                if slow or due_fast:
                    ok, msg = _run_once(with_slow=slow)
                    with _lock:
                        _status["last_run_at"] = now.isoformat(timespec="seconds")
                        _status["last_result"] = ("慢源+快源 " if slow else "快源 ") + msg
                        _status["last_error"] = None if ok else msg
                        _status["runs_total"] += 1
                    if slow:
                        last_slow_hour = now.strftime("%Y-%m-%d#%H")
                    last_fast = now
        except Exception as e:  # noqa: BLE001
            with _lock:
                _status["last_error"] = f"{type(e).__name__}: {e}"[:200]
            logger.warning("资讯同步循环异常：%s", e)
        threading.Event().wait(60)


def start_news_feed_scheduler(enabled: bool | None = None) -> threading.Thread:
    """启动守护线程。enabled 缺省读 QUANT_NEWS_FEED（默认开）。"""
    global _sched_thread
    if _sched_thread is not None and _sched_thread.is_alive():
        logger.info("调度器已在运行，跳过重复启动（news_feed）")
        return _sched_thread
    if enabled is None:
        enabled = os.getenv("QUANT_NEWS_FEED", "1") not in ("0", "", "false", "False")
    with _lock:
        _status["enabled"] = enabled
    t = threading.Thread(target=_loop, args=(enabled,), daemon=True, name="news-feed")
    t.start()
    _sched_thread = t
    logger.info("资讯同步调度器已启动（enabled=%s，快源每 %s 分钟 / 慢源每小时 :%02d）",
                enabled, FAST_INTERVAL_MIN, SLOW_EVERY_HOUR_AT)
    return t
