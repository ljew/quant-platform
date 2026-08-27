"""数据管道调度器（设计 v1.0：定时数据拉取/更新）。

每交易日 17:00 后调用 scripts/etl_daily.py（子进程隔离，崩溃不影响主服务）。
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

# 运行状态（供监控页查询）
_status = {
    "enabled": False,
    "run_hour": f"{RUN_HOUR:02d}:{RUN_MIN:02d}",
    "last_run_at": None,      # 最近一次触发时间
    "last_success": None,     # 最近一次成功时间
    "last_error": None,       # 最近一次失败信息
    "runs_total": 0,
}


def get_status() -> dict:
    """调度器状态快照（监控页用）。"""
    import copy

    return copy.deepcopy(_status)


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _is_trading_day(d: datetime) -> bool:
    """是否为 A 股交易日（周末 + 法定节假日休市）。"""
    from app.core.trading_calendar import is_trading_day as _td

    return _td(d)


def _run_update() -> bool:
    """每日数据任务。

    P5 起：优先执行 datahub 管道（extract→clean→score→mined→factor，
    运行记录入 pipeline_runs 供监控），任一步骤失败自动降级 etl_daily。
    """
    from app.database import SessionLocal

    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
    # —— 新管道优先 ——
    try:
        from app.datahub.runner import run_pipeline

        rid = run_pipeline(trigger="scheduler")
        from app.models import PipelineRun

        with SessionLocal() as db:
            st = db.get(PipelineRun, rid)
        if st and st.status == "SUCCESS":
            logger.info("datahub 管道完成 run_id=%s", rid)
            _status["last_success"] = datetime.now().isoformat(timespec="seconds")
            return True
        # 步骤部分失败 → 降级 etl_daily 兜底
        err = (st.error if st else None) or f"pipeline status={st.status if st else '?'}"
        logger.warning("datahub 管道未完全成功(run_id=%s)，降级 etl_daily: %s", rid, err[:200])
        _status["last_error"] = f"pipeline fallback: {str(err)[:200]}"
    except Exception as e:  # noqa: BLE001
        logger.error("datahub 管道异常(%s)，降级 etl_daily: %s", type(e).__name__, e)
        _status["last_error"] = f"pipeline fallback: {str(e)[:200]}"

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
            _status["last_success"] = datetime.now().isoformat(timespec="seconds")
            return True
        logger.error("ETL 日更失败 rc=%s:\n%s", proc.returncode, proc.stderr[-1200:])
        _status["last_error"] = f"rc={proc.returncode}: {proc.stderr[-300:]}"
        return False
    except Exception as e:  # noqa: BLE001
        logger.error("ETL 日更异常: %s", e)
        _status["last_error"] = str(e)[:300]
        return False


def _loop(enabled: bool) -> None:
    last_run_date = ""
    while True:
        try:
            now = datetime.now()
            if enabled and _is_trading_day(now) and (now.hour, now.minute) >= (RUN_HOUR, RUN_MIN):
                if last_run_date != _today_str():
                    logger.info("触发每日数据更新 %s", _today_str())
                    _status["last_run_at"] = now.isoformat(timespec="seconds")
                    _status["runs_total"] += 1
                    _run_update()
                    last_run_date = _today_str()
        except Exception as e:  # noqa: BLE001
            logger.error("数据调度循环异常: %s", e)
        time.sleep(_CHECK_INTERVAL)


def start_data_scheduler(enabled: bool | None = None) -> threading.Thread:
    """启动调度 daemon 线程。enabled 缺省读 env QUANT_DATA_SCHEDULE。"""
    if enabled is None:
        enabled = os.getenv("QUANT_DATA_SCHEDULE", "0") == "1"
    _status["enabled"] = enabled
    t = threading.Thread(target=_loop, args=(enabled,), daemon=True, name="data-scheduler")
    t.start()
    logger.info("数据调度器已启动（enabled=%s，每交易日 %02d:%02d 后自动日更）", enabled, RUN_HOUR, RUN_MIN)
    return t
