"""每小时热点简报调度：整点后 2 分钟跑一次。

为什么独立成模块而不是塞进 data_scheduler：那位的循环里已经压了 ETL、策略验证、
成分快照、估值快照、财报补更五件事，再加一件会让「谁卡住了」更难定位。

幂等：按「日期#小时」去重，同一小时最多跑一次；失败不记 done，下一个整点再试
（简报是有时效的，错过这一小时就不补了 —— 补上个小时的热点没有意义）。
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime

logger = logging.getLogger("hot_brief_scheduler")

SLOT_MINUTE = 2                 # 每小时的 :02 触发（避开整点其他任务）
_CHECK_INTERVAL = 60            # 秒
_MAX_ATTEMPTS = 2               # 同一小时最多试 2 次（约 :02 / :05）

_done: set[str] = set()
_attempts: dict[str, int] = {}
_status = {"enabled": False, "last_run_at": None, "last_summary": None,
           "last_error": None, "runs_total": 0}
_lock = threading.Lock()
_sched_thread: threading.Thread | None = None


def get_status() -> dict:
    return {**_status, "slot_minute": SLOT_MINUTE,
            "done_today": sorted(k for k in _done if k.startswith(datetime.now().strftime("%Y-%m-%d")))}


def _run_once() -> tuple[bool, str]:
    from app.database import SessionLocal
    from app.services.hot_brief import run_brief

    db = SessionLocal()
    try:
        r = run_brief(db, minutes=60)
    finally:
        db.close()
    if r.get("ok"):
        return True, f"输入 {r.get('n_input')} 条 → 保留 {r.get('n_items')} 条"
    return False, str(r.get("error") or "未知错误")


def _loop(enabled: bool) -> None:
    while True:
        try:
            if enabled:
                now = datetime.now()
                key = f"{now:%Y-%m-%d}#{now.hour:02d}"
                if now.minute >= SLOT_MINUTE and key not in _done:
                    if _attempts.get(key, 0) < _MAX_ATTEMPTS:
                        _attempts[key] = _attempts.get(key, 0) + 1
                        with _lock:
                            _status["last_run_at"] = now.isoformat(timespec="seconds")
                            _status["runs_total"] += 1
                        ok, msg = _run_once()
                        with _lock:
                            if ok:
                                _done.add(key)
                                _status["last_summary"] = msg
                                _status["last_error"] = None
                                logger.info("热点简报完成：%s", msg)
                            else:
                                _status["last_error"] = msg
                                logger.warning("热点简报失败（第 %s/%s 次）：%s",
                                               _attempts[key], _MAX_ATTEMPTS, msg)
        except Exception as e:  # noqa: BLE001
            logger.warning("热点简报调度循环异常：%s", e)
        threading.Event().wait(_CHECK_INTERVAL)


def start_hot_brief_scheduler(enabled: bool | None = None) -> threading.Thread:
    """启动守护线程。enabled 缺省读 QUANT_HOT_BRIEF（默认开）。"""
    global _sched_thread
    if _sched_thread is not None and _sched_thread.is_alive():
        logger.info("调度器已在运行，跳过重复启动（hot_brief）")
        return _sched_thread
    if enabled is None:
        enabled = os.getenv("QUANT_HOT_BRIEF", "1") not in ("0", "", "false", "False")
    with _lock:
        _status["enabled"] = enabled
    t = threading.Thread(target=_loop, args=(enabled,), daemon=True, name="hot-brief")
    t.start()
    _sched_thread = t
    logger.info("热点简报调度器已启动（enabled=%s，每小时 :%02d 生成一次）", enabled, SLOT_MINUTE)
    return t
