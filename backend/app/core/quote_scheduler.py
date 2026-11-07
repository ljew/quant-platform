"""行情收盘快照定时任务：每交易日 18:30 后从 tushare 拉一次收盘截面落库。

为什么独立成模块：data_scheduler 的循环里已经压了 ETL、策略验证、指数成分、
估值快照、财报补更五件事（见 hot_brief_scheduler 的说明），再加一件会让
「谁卡住了」更难定位；而且快照只依赖 tushare 当日日线，不必等 19:00 ETL 跑完。

幂等键是 **trade_date** 而不是自然日：周五收盘数据生成后，整个周末都不会
重复跑，也不会重复消耗 tushare 配额。启动即补跑一次（容器重启 / 首次部署 /
断供自愈），保证页面随时有数据可读。
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, timedelta

logger = logging.getLogger("quote_scheduler")

RUN_HOUR, RUN_MIN = 18, 30       # 收盘后数据稳定，且早于 19:00 ETL 避开配额争抢
_CHECK_INTERVAL = 600            # 10 分钟
_MAX_ATTEMPTS = 3                # 同一自然日最多试 3 次（tushare 当日数据偶发延迟）

_status = {"enabled": False, "last_run_at": None, "last_summary": None,
           "last_error": None, "runs_total": 0}
_lock = threading.Lock()
_sched_thread: threading.Thread | None = None
_checked_day: str | None = None
_attempts: dict[str, int] = {}


def get_status() -> dict:
    from app.services import quote_snapshot
    with _lock:
        base = dict(_status)
    base["snapshot"] = quote_snapshot.get_status()
    base["run_at"] = f"{RUN_HOUR:02d}:{RUN_MIN:02d}"
    return base


def _expected_trade_date(now: datetime) -> str:
    """按交易日历推「此刻应该已经有的最新收盘日」——不联网、不查 tushare。

    18:30 之前期望的是上一个交易日（当天收盘数据尚未就绪），之后才是当天。
    """
    from app.core.trading_calendar import is_trading_day
    after = now.hour > RUN_HOUR or (now.hour == RUN_HOUR and now.minute >= RUN_MIN)
    d: date = now.date() if after else now.date() - timedelta(days=1)
    for i in range(12):
        c = d - timedelta(days=i)
        if is_trading_day(c):
            return c.isoformat()
    return d.isoformat()


def _due(now: datetime, first: bool) -> bool:
    """启动补跑立即执行；之后只在**交易日** 18:30 之后、且当天还没成功过时跑。"""
    from app.core.trading_calendar import is_trading_day
    if first:
        return True
    if now.hour < RUN_HOUR or (now.hour == RUN_HOUR and now.minute < RUN_MIN):
        return False
    if not is_trading_day(now.date()):   # 周末/节假日不重复消耗 tushare 配额
        return False
    return _checked_day != now.strftime("%Y-%m-%d")


def _try_refresh() -> tuple[bool, str, str | None]:
    from app.services import quote_snapshot
    r = quote_snapshot.refresh()
    if r.get("ok"):
        return True, f"{r['trade_date']} · {r['n_rows']} 条 · {r['source']}", r["trade_date"]
    return False, str(r.get("error") or "未知错误"), None


def _loop(enabled: bool, need_bootstrap: bool) -> None:
    global _checked_day
    first = need_bootstrap
    while True:
        try:
            if enabled:
                now = datetime.now()
                day = now.strftime("%Y-%m-%d")
                if _due(now, first) and _attempts.get(day, 0) < _MAX_ATTEMPTS:
                    first = False
                    _attempts[day] = _attempts.get(day, 0) + 1
                    with _lock:
                        _status["last_run_at"] = now.isoformat(timespec="seconds")
                        _status["runs_total"] += 1
                    ok, msg, trade_date = _try_refresh()
                    with _lock:
                        _status["last_summary"] = msg if ok else _status["last_summary"]
                        _status["last_error"] = None if ok else msg
                    if ok:
                        # tushare 当日数据偶发延迟时，refresh 会回溯到上一交易日。
                        # 只有拿到「此刻应有的收盘日」才算完成，否则下个 tick 继续重试。
                        if trade_date and trade_date >= _expected_trade_date(now):
                            _checked_day = day
                            logger.info("行情快照完成：%s", msg)
                        else:
                            logger.warning("tushare 当日数据尚未就绪（拿到 %s），稍后重试", trade_date)
                    else:
                        logger.warning("行情快照失败（第 %s/%s 次）：%s",
                                       _attempts[day], _MAX_ATTEMPTS, msg)
        except Exception as e:  # noqa: BLE001
            logger.warning("行情快照调度循环异常：%s", e)
        threading.Event().wait(_CHECK_INTERVAL)


def start_quote_scheduler(enabled: bool | None = None) -> threading.Thread:
    """启动守护线程。enabled 缺省读 QUANT_QUOTE_SCHEDULE（默认开）。"""
    global _sched_thread, _checked_day
    if _sched_thread is not None and _sched_thread.is_alive():
        logger.info("调度器已在运行，跳过重复启动（quote_scheduler）")
        return _sched_thread
    if enabled is None:
        enabled = os.getenv("QUANT_QUOTE_SCHEDULE", "1") not in ("0", "", "false", "False")
    with _lock:
        _status["enabled"] = enabled

    # 从库里恢复「已生成到哪个交易日」：容器重启不会重复拉同一天的数据。
    need_bootstrap = True
    try:
        from app.services import quote_snapshot
        latest = quote_snapshot.get_status().get("trade_date")
        expected = _expected_trade_date(datetime.now())
        need_bootstrap = not (latest and latest >= expected)
        if need_bootstrap:
            logger.info("行情快照待补跑：库内 %s，应为 %s", latest, expected)
        else:
            logger.info("沿用库中已有行情快照：%s", latest)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取已有行情快照失败（不影响启动，将补跑一次）：%s", e)

    t = threading.Thread(target=_loop, args=(enabled, need_bootstrap),
                         daemon=True, name="quote-scheduler")
    t.start()
    _sched_thread = t
    logger.info("行情快照调度器已启动（enabled=%s，每交易日 %02d:%02d 后自动刷新）",
                enabled, RUN_HOUR, RUN_MIN)
    return t
