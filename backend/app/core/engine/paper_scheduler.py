"""模拟盘后台调度器。

- 单标的任务：交易时段（工作日 9:30-11:30 / 13:00-15:00）每轮用实时价跑一次；
- 组合任务：日频，**当日 K 线入库后**（>=19:00）跑一次 rebalance；
- 常驻 daemon 线程，每 30s 检查一次；由 app.main 启动。
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from sqlalchemy import select

from app.database import SessionLocal
from app.models import PaperTask
from app.core.engine.paper_engine import run_paper_task

logger = logging.getLogger("quant.paper")

_stop = False
_thread: threading.Thread | None = None

# 组合任务的失败重试节流（进程内即可，重启清零无所谓）。
# 键 task_id → 上次尝试的本地时间。
_attempt_at: dict[int, dt.datetime] = {}

# 组合任务最早触发时刻：ETL 19:00 把当日 K 线写进来之前跑，看到的是昨天的数据，
# 跑完 last_bar_date 还是昨天 → 当日净值永远缺一天。
PORTFOLIO_AFTER_HOUR = 19
# 数据还没到位时的重试间隔（秒）
RETRY_SECONDS = 1800


def _trading_now(now: dt.datetime) -> bool:
    """是否处于 A 股交易时段（交易日 + 9:30-11:30 / 13:00-15:00）。"""
    from app.core.trading_calendar import is_trading_day

    if not is_trading_day(now):
        return False
    t = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= t <= 11 * 60 + 30:
        return True
    if 13 * 60 <= t <= 15 * 60:
        return True
    return False


def _loop() -> None:
    while not _stop:
        try:
            now = dt.datetime.now()
            db = SessionLocal()
            try:
                tasks = db.execute(
                    select(PaperTask).where(PaperTask.enabled == True)  # noqa: E712
                ).scalars().all()
                for t in tasks:
                    if t.kind == "single":
                        if _trading_now(now):
                            run_paper_task(db, t)
                    else:  # portfolio：日频，当日 K 线入库后跑一次
                        from app.core.trading_calendar import is_trading_day

                        if now.hour >= PORTFOLIO_AFTER_HOUR and is_trading_day(now):
                            # ⚠️ 判「今天是否已跑过」不能用 last_run_at.date()：
                            # 盘前/盘中手动跑一次会把 last_run_at 打到今天，
                            # 旧逻辑因此跳过了收盘后那次（2026-11-09 实锤：
                            # 快照只到 11-06，11-09 净值整天缺失）。
                            # 正确口径是 **数据跑到哪一天** —— last_bar_date。
                            today = now.date().isoformat()
                            stale = (not t.last_bar_date) or (t.last_bar_date < today)
                            last_try = _attempt_at.get(t.id)
                            cooled = (
                                last_try is None
                                or (now - last_try).total_seconds() >= RETRY_SECONDS
                            )
                            if stale and cooled:
                                _attempt_at[t.id] = now
                                logger.info(
                                    "paper portfolio run: task=%s last_bar=%s today=%s",
                                    t.id, t.last_bar_date, today,
                                )
                                r = run_paper_task(db, t)
                                if not r.get("ok"):
                                    logger.warning(
                                        "paper portfolio failed: task=%s %s",
                                        t.id, r.get("error"),
                                    )
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"paper scheduler error: {e}")
        time.sleep(30)


def start_paper_scheduler() -> None:
    global _thread, _stop
    if _thread and _thread.is_alive():
        return
    _stop = False
    _thread = threading.Thread(target=_loop, daemon=True, name="paper-scheduler")
    _thread.start()
    logger.info("paper scheduler started")


def stop_paper_scheduler() -> None:
    global _stop
    _stop = True
