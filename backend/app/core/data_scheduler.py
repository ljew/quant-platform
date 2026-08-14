"""数据管道调度器（设计 v1.0：定时数据拉取/更新）。

每交易日 15:30 后调用 scripts/update_daily.py（子进程隔离，崩溃不影响主服务）。
由 main.startup 启动（env QUANT_DATA_SCHEDULE=1 启用，默认关——避免沙箱频繁
在线拉取；生产服务器建议开启）。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

logger = logging.getLogger("data_scheduler")

# 每个交易日 17:00 后执行一次 ETL（tushare 收盘数据就绪 + 因子计算）
RUN_HOUR, RUN_MIN = 17, 0
_CHECK_INTERVAL = 60  # 秒


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _is_trading_day(d: datetime) -> bool:
    """是否为 A 股交易日（周末 + 法定节假日休市）。"""
    from app.core.trading_calendar import is_trading_day as _td

    return _td(d)


def _run_update() -> bool:
    """子进程执行 ETL 每日管道（tushare 采集→增量入库→因子落库；独立进程隔离崩溃）。"""
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
    script = os.path.join(backend, "scripts", "etl_daily.py")
    env = dict(os.environ)
    env["PYTHONPATH"] = backend
    try:
        proc = subprocess.run(
            [sys.executable, script],
            env=env,
            cwd=backend,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        if proc.returncode == 0:
            logger.info("ETL 日更成功:\n%s", proc.stdout[-1200:])
            return True
        logger.error("ETL 日更失败 rc=%s:\n%s", proc.returncode, proc.stderr[-1200:])
        return False
    except Exception as e:  # noqa: BLE001
        logger.error("ETL 日更异常: %s", e)
        return False


def _loop(enabled: bool) -> None:
    last_run_date = ""
    while True:
        try:
            now = datetime.now()
            if enabled and _is_trading_day(now) and (now.hour, now.minute) >= (RUN_HOUR, RUN_MIN):
                if last_run_date != _today_str():
                    logger.info("触发每日数据更新 %s", _today_str())
                    _run_update()
                    last_run_date = _today_str()
        except Exception as e:  # noqa: BLE001
            logger.error("数据调度循环异常: %s", e)
        time.sleep(_CHECK_INTERVAL)


def start_data_scheduler(enabled: bool | None = None) -> threading.Thread:
    """启动调度 daemon 线程。enabled 缺省读 env QUANT_DATA_SCHEDULE。"""
    if enabled is None:
        enabled = os.getenv("QUANT_DATA_SCHEDULE", "0") == "1"
    t = threading.Thread(target=_loop, args=(enabled,), daemon=True, name="data-scheduler")
    t.start()
    logger.info("数据调度器已启动（enabled=%s，每交易日 %02d:%02d 后自动日更）", enabled, RUN_HOUR, RUN_MIN)
    return t
