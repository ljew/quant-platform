"""平台监控 API（数据情况 + 系统服务状态）。

- GET /monitor/status   聚合监控数据（前端监控页轮询）
  - data: SQLite/DuckDB 各表行数 + 数据新鲜度（最新日期/距今天数）
  - services: 后端/数据源/调度器/任务队列/模拟盘
  - disk: data 目录占用
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime

from fastapi import APIRouter
from sqlalchemy import select, func

from app.database import SessionLocal
from app.services.data_health import health_report
from app.models import (
    FactorDaily,
    KlineDaily,
    IndexKlineDaily,
    IndexMembership,
    PipelineRun,
    PipelineStepLog,
)
from app.config import settings

router = APIRouter(prefix="/monitor", tags=["monitor"])

_START_TIME = time.time()
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "data")


def _dir_size_mb(path: str) -> float:
    total = 0.0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return round(total / 1024 / 1024, 1)


def _table_counts_sqlite() -> dict:
    import sqlite3

    db_path = os.path.join(_DATA_DIR, "quant_dev.db")
    out: dict[str, int] = {}
    if not os.path.exists(db_path):
        return out
    con = sqlite3.connect(db_path)
    try:
        for (t,) in con.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
        ).fetchall():
            try:
                out[t] = con.execute(f'select count(*) from "{t}"').fetchone()[0]
            except Exception:  # noqa: BLE001
                out[t] = -1
    finally:
        con.close()
    return out


def _table_counts_duckdb() -> dict:
    from app.services import duckdb_store

    out = {}
    for t in ("kline_daily", "index_kline_daily", "fundamentals_history", "stocks",
              "index_membership", "factor_daily"):
        try:
            out[t] = duckdb_store.count(t)
        except Exception:  # noqa: BLE001
            out[t] = -1
    return out


def _latest(db, model, col) -> str | None:
    try:
        r = db.execute(select(func.max(col))).scalar()
        return r.isoformat() if hasattr(r, "isoformat") else str(r)
    except Exception:  # noqa: BLE001
        return None


def _freshness(db) -> dict:
    today = date.today().isoformat()

    def fresh(label: str, latest: str | None) -> dict:
        days = None
        if latest:
            try:
                days = (date.today() - date.fromisoformat(latest[:10])).days
            except Exception:  # noqa: BLE001
                days = None
        return {"label": label, "latest": latest, "days_ago": days,
                "stale": (days is not None and days > 5)}

    return {
        "kline": fresh("个股K线", _latest(db, KlineDaily, KlineDaily.trade_date)),
        "index": fresh("指数K线", _latest(db, IndexKlineDaily, IndexKlineDaily.trade_date)),
        "factor": fresh("因子截面", _latest(db, FactorDaily, FactorDaily.trade_date)),
        "membership": fresh("成分快照", _latest(db, IndexMembership, IndexMembership.trade_date)),
        "today": today,
    }


def _paper_stats(db) -> dict:
    from app.models import PaperTask

    total = db.execute(select(func.count()).select_from(PaperTask)).scalar() or 0
    enabled = db.execute(
        select(func.count()).select_from(PaperTask).where(PaperTask.enabled == True)  # noqa: E712
    ).scalar() or 0
    return {"tasks": total, "enabled": enabled}


@router.get("/health-report")
def data_health_endpoint():
    """数据健康度报告。"""
    return health_report()


@router.get("/dataflow")
def data_flow():
    """数据流全景（源头/Bronze/Silver/Gold）。"""
    from app.services.dataflow_svc import dataflow_report

    return dataflow_report(SessionLocal())
def data_health():
    """数据健康度：采集/处理/应用三层评分 + 告警列表。"""
    return health_report()


@router.get("/status")
def monitor_status():
    db = SessionLocal()
    try:
        # —— 数据 ——
        sqlite_counts = _table_counts_sqlite()
        duckdb_counts = _table_counts_duckdb()
        freshness = _freshness(db)

        # —— 服务 ——
        from app.services import data_source
        from app.core import task_queue

        # 调度器状态
        from app.core.data_scheduler import get_status as ds_status

        etl = ds_status()
        paper_alive = False
        try:
            from app.core.engine import paper_scheduler

            paper_alive = bool(paper_scheduler._thread and paper_scheduler._thread.is_alive())
        except Exception:  # noqa: BLE001
            paper_alive = False

        tasks = task_queue.list_tasks(limit=10)
        running = sum(1 for t in tasks if t["status"] == "running")

        return {
            "server": {
                "name": settings.app_name,
                "version": "0.2.0",
                "time": datetime.now().isoformat(timespec="seconds"),
                "uptime_sec": round(time.time() - _START_TIME),
                "db": "sqlite" if settings.is_sqlite else "postgres",
            },
            "data": {
                "sqlite": sqlite_counts,
                "sqlite_total": sum(v for v in sqlite_counts.values() if v > 0),
                "duckdb": duckdb_counts,
                "duckdb_total": sum(v for v in duckdb_counts.values() if v > 0),
                "freshness": freshness,
            },
            "services": {
                "data_source": {
                    "tushare": data_source.check_tushare(),
                    "akshare": data_source.check_akshare(),
                },
                "schedulers": {
                    "etl": etl,
                    "paper": {"alive": paper_alive, "interval_sec": 30},
                },
                "tasks": {"running": running, "recent": tasks},
                "paper": _paper_stats(db),
                "pipeline": _pipeline_stats(db),
            },
            "disk": {
                "data_dir_mb": _dir_size_mb(_DATA_DIR),
                "data_dir": _DATA_DIR,
            },
        }
    finally:
        db.close()


def _pipeline_stats(db) -> dict:
    """数据管道最近运行记录（监控页任务执行情况）。"""
    from app.datahub.runner import init_models

    init_models()
    runs = db.execute(
        select(PipelineRun).order_by(PipelineRun.id.desc()).limit(8)
    ).scalars().all()
    out = []
    for r in runs:
        steps = db.execute(
            select(PipelineStepLog).where(PipelineStepLog.run_id == r.id)
            .order_by(PipelineStepLog.id)
        ).scalars().all()
        out.append({
            "run_id": r.id, "trigger": r.trigger, "status": r.status,
            "started_at": r.started_at.isoformat(timespec="seconds") if r.started_at else None,
            "finished_at": r.finished_at.isoformat(timespec="seconds") if r.finished_at else None,
            "error": (r.error or "")[:200] or None,
            "steps": [
                {"name": st.name, "status": st.status, "duration_sec": st.duration_sec,
                 "rows": st.rows}
                for st in steps
            ],
        })
    return {"runs": out}
