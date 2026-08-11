"""模拟盘后台调度器。

- 单标的任务：交易时段（工作日 9:30-11:30 / 13:00-15:00）每轮用实时价跑一次；
- 组合任务：日频，每个交易日收盘后（>=15:00）跑一次 rebalance；
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


def _trading_now(now: dt.datetime) -> bool:
    """是否处于 A 股交易时段（本地时间，沙箱为 GMT+8）。"""
    if now.weekday() >= 5:  # 周末
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
                    else:  # portfolio：日频，收盘后跑一次
                        if now.hour >= 15 and (
                            t.last_run_at is None
                            or t.last_run_at.date() != now.date()
                        ):
                            run_paper_task(db, t)
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
