"""统一资讯流同步调度：快源 10 分钟、慢源每小时、股吧独立节奏（默认 20 分钟）。

为什么三种节奏而不是两档：
- 财联社/东财/新浪是实时流，10 分钟不同步页面上就没新东西
- 公众号每天只采两轮、tushare 靠 ETL 每日落盘，每小时抄一次已足够
- 股吧要抓 10 个页面（10 次请求），比快源重，但帖子更新比公众号快得多，
  所以给它单开一档 —— 老刘 2026-11-07 明确要求「股吧频率可以高一点，tushare 保持现状」

## 股吧的指数退避（必须的）

东财对 `guba.eastmoney.com/list,<code>.html` 有滑块风控（2026-11-07 实测返回
「身份核实」空壳页）。抓不到时若仍按 20 分钟硬刷，只会把封禁拖得更久。
所以连续失败按 20 → 40 → 80 → 160 → 180 分钟退避，成功一次即复位。
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime

logger = logging.getLogger("news_feed_scheduler")

FAST_INTERVAL_MIN = 10       # 财联社 / 东财 / 新浪
SLOW_EVERY_HOUR_AT = 5       # 每小时第 5 分钟顺带公众号 + tushare
GUBA_INTERVAL_MIN = 20       # 股吧基础间隔
GUBA_BACKOFF_MAX_MIN = 180   # 连续失败时最长退到 3 小时

_status = {
    "enabled": False,
    "last_run_at": None,
    "last_result": None,
    "last_error": None,
    "runs_total": 0,
    "last_guba_at": None,
    "guba_fail": 0,
    "guba_next_min": GUBA_INTERVAL_MIN,
}
_lock = threading.Lock()
_sched_thread: threading.Thread | None = None


def get_status() -> dict:
    return {**_status,
            "fast_interval_min": FAST_INTERVAL_MIN,
            "guba_interval_min": GUBA_INTERVAL_MIN}


def _run_once(with_slow: bool, with_guba: bool) -> dict:
    from app.database import SessionLocal
    from app.services import news_feed

    db = SessionLocal()
    try:
        return news_feed.sync(db, with_slow=with_slow, with_guba=with_guba)
    finally:
        db.close()


def _loop(enabled: bool) -> None:
    last_fast: datetime | None = None
    last_slow_hour = ""
    last_guba: datetime | None = None
    guba_fail = 0

    while True:
        try:
            if enabled:
                now = datetime.now()

                slow = (now.minute >= SLOW_EVERY_HOUR_AT
                        and last_slow_hour != now.strftime("%Y-%m-%d#%H"))
                due_fast = (last_fast is None
                            or (now - last_fast).total_seconds() >= FAST_INTERVAL_MIN * 60)
                guba_gap = min(GUBA_INTERVAL_MIN * (2 ** min(guba_fail, 4)),
                               GUBA_BACKOFF_MAX_MIN)
                due_guba = (last_guba is None
                            or (now - last_guba).total_seconds() >= guba_gap * 60)

                if slow or due_fast or due_guba:
                    res = _run_once(with_slow=slow, with_guba=due_guba)

                    # 股吧单独判定成败：其它源成功不代表股吧没被风控
                    if due_guba:
                        got = res.get("guba", 0)
                        last_guba = now
                        if got and got > 0:
                            guba_fail = 0
                        else:
                            guba_fail += 1
                            logger.info(
                                "股吧本轮无新增（连续第 %d 次），下次间隔 %d 分钟",
                                guba_fail,
                                min(GUBA_INTERVAL_MIN * (2 ** min(guba_fail, 4)),
                                    GUBA_BACKOFF_MAX_MIN))
                        res.pop("guba", None)

                    added = sum(v for v in res.values() if v > 0)
                    failed = [k for k, v in res.items() if v < 0]
                    msg = f"+{added} 条" + (f"，失败 {','.join(failed)}" if failed else "")
                    tag = []
                    if slow:
                        tag.append("慢源")
                    if due_fast:
                        tag.append("快源")
                    if due_guba:
                        tag.append("股吧")
                    with _lock:
                        _status["last_run_at"] = now.isoformat(timespec="seconds")
                        _status["last_result"] = f"{'+'.join(tag)} {msg}"
                        _status["last_error"] = None if (not failed or added > 0) else msg
                        _status["runs_total"] += 1
                        _status["last_guba_at"] = (
                            last_guba.isoformat(timespec="seconds") if last_guba else None)
                        _status["guba_fail"] = guba_fail
                        _status["guba_next_min"] = min(
                            GUBA_INTERVAL_MIN * (2 ** min(guba_fail, 4)),
                            GUBA_BACKOFF_MAX_MIN)

                    if slow:
                        last_slow_hour = now.strftime("%Y-%m-%d#%H")
                    if due_fast:
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
    logger.info(
        "资讯同步调度器已启动（enabled=%s，快源 %s 分钟 / 慢源每小时 :%02d / 股吧 %s 分钟）",
        enabled, FAST_INTERVAL_MIN, SLOW_EVERY_HOUR_AT, GUBA_INTERVAL_MIN)
    return t
