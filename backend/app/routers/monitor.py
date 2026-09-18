"""平台监控 API（数据情况 + 系统服务状态）。

- GET /monitor/status   聚合监控数据（前端监控页轮询）
  - data: SQLite/DuckDB 各表行数 + 数据新鲜度（最新日期/距今天数）
  - services: 后端/数据源/调度器/任务队列/模拟盘
  - disk: data 目录占用
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import date, datetime

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select, func

from app.database import SessionLocal
from app.services.assets_svc import assets_report
from app.services.data_health import health_report
from app.services.health_engine import run_rules, METRIC_DOCS, ensure_default_rules
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

logger = logging.getLogger(__name__)

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
    """数据健康度报告（规则引擎驱动）。"""
    return run_rules(SessionLocal())


@router.get("/health/rules")
def health_rules():
    ensure_default_rules()
    db = SessionLocal()
    try:
        from app.models import HealthRule

        rows = db.execute(select(HealthRule).order_by(HealthRule.layer, HealthRule.id)).scalars().all()
        return [{"id": r.id, "name": r.name, "layer": r.layer, "metric": r.metric,
                 "params": r.params, "comparator": r.comparator, "threshold": r.threshold,
                 "level": r.level, "weight": r.weight, "enabled": bool(r.enabled),
                 "last_value": r.last_value, "last_status": r.last_status,
                 "metric_doc": METRIC_DOCS.get(r.metric, "")} for r in rows]
    finally:
        db.close()


class RulePayload(BaseModel):
    name: str
    layer: str = "process"
    metric: str
    params: str = "{}"
    comparator: str = ">="
    threshold: float | None = None
    level: str = "warn"
    weight: float = 1.0
    enabled: bool = True


@router.post("/health/rules")
def health_rule_add(payload: RulePayload):
    from app.models import HealthRule

    db = SessionLocal()
    try:
        row = HealthRule(name=payload.name, layer=payload.layer, metric=payload.metric,
                         params=payload.params, comparator=payload.comparator,
                         threshold=payload.threshold, level=payload.level,
                         weight=payload.weight, enabled=int(payload.enabled))
        db.add(row)
        db.commit()
        return {"ok": True, "id": row.id}
    finally:
        db.close()


@router.put("/health/rules/{rid}")
def health_rule_update(rid: int, payload: dict):
    from app.models import HealthRule

    db = SessionLocal()
    try:
        row = db.get(HealthRule, rid)
        if not row:
            raise HTTPException(status_code=404, detail="规则不存在")
        for k in ("name", "layer", "metric", "params", "comparator", "threshold",
                  "level", "weight"):
            if k in payload:
                setattr(row, k, payload[k])
        if "enabled" in payload:
            row.enabled = int(bool(payload["enabled"]))
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.delete("/health/rules/{rid}")
def health_rule_delete(rid: int):
    from app.models import HealthRule

    db = SessionLocal()
    try:
        row = db.get(HealthRule, rid)
        if row:
            db.delete(row)
            db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.post("/health/run")
def health_run_now():
    return run_rules(SessionLocal())


import threading

_running_pipeline = {"locked": False}


@router.post("/pipeline/run")
def pipeline_run_now():
    """手动立即运行数据管道（后台线程）。"""
    if _running_pipeline["locked"]:
        return {"ok": False, "error": "已有管道在运行中"}
    from app.datahub.runner import run_pipeline

    def _bg():
        _running_pipeline["locked"] = True
        try:
            rid = run_pipeline(trigger="manual")
            logger.info("手动管道完成 run_id=%s", rid)
        finally:
            _running_pipeline["locked"] = False

    threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True}


# ——— 一键修复：把「资产清单里的异常项」翻译成可执行的修复动作 ———

# 数据集 → 需要重跑的管道步骤（执行时按 STEPS 原始顺序排序，保证依赖）
# 说明：文本类数据集已从资产清单摘除（非结构化因子暂时下线），对应映射一并移除。
_DATASET_STEPS: dict[str, list[str]] = {
    # 日K 补齐后因子与 DuckDB 也要跟着补，否则页面仍显示因子滞后
    "kline_daily": ["extract_stock_kline", "clean_bars", "compute_factors", "sync_duckdb"],
    "index_kline_daily": ["extract_index_kline", "sync_duckdb"],
    "factor_daily": ["compute_factors", "sync_duckdb"],
    "factor_mined_daily": ["compute_mined_factors", "sync_duckdb"],
    "stocks": ["extract_attributes"],
}

# 没有对应管道步骤的数据集 → 走独立脚本（均为幂等补缺脚本）
_CORE_INDEXES = "000906,000300,000905,000852,000016,399006"
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REPAIR_TIMEOUT = 1800

_repair_state: dict = {
    "running": False, "job_id": None, "actions": [], "done": 0, "total": 0,
    "current": None, "started_at": None, "finished_at": None, "ok": None, "targets": [],
}


class ReprocessRequest(BaseModel):
    items: list[str] = Field(default_factory=list,
                             description="要修复的数据集 key；留空=自动修复全部异常项")
    include_warn: bool = True


def _bad_datasets(include_warn: bool = True) -> tuple[dict, dict]:
    """返回 (全部数据集, 异常数据集)。

    异常 = status ∈ {stale, empty}（+ warn 可选）。另外「日K 某天只抓到一部分」
    的残缺日不一定表现为滞后，这里单独纳入 kline_daily。
    """
    rep = assets_report(SessionLocal(), force=True)
    flat = {i["key"]: i for g in rep["groups"] for i in g["items"]}
    bad = {}
    for k, v in flat.items():
        if v["status"] in ("stale", "empty") or (include_warn and v["status"] == "warn"):
            bad[k] = v
    if rep.get("coverage", {}).get("partial_count") and "kline_daily" in flat:
        bad["kline_daily"] = flat["kline_daily"]
    return flat, bad


def _plan_repair(items: list[str], include_warn: bool) -> list[dict]:
    """把异常数据集翻译为修复动作列表（管道步骤 / 独立脚本）。"""
    _flat, bad = _bad_datasets(include_warn)
    if items:
        wanted = set(items)
        bad = {k: v for k, v in bad.items() if k in wanted}

    steps: list[str] = []
    for k in bad:
        for s in _DATASET_STEPS.get(k, []):
            if s not in steps:
                steps.append(s)
    # 按管道原始顺序排序，避免依赖倒置（如先算因子再补 K 线）
    try:
        from app.datahub.runner import STEPS

        order = [n for n, _ in STEPS]
        steps.sort(key=lambda s: order.index(s) if s in order else 999)
    except Exception:  # noqa: BLE001
        pass

    actions: list[dict] = []
    if steps:
        actions.append({
            "kind": "pipeline",
            "label": "重跑管道步骤 " + " → ".join(steps),
            "steps": steps,
            "datasets": [k for k in bad if _DATASET_STEPS.get(k)],
        })
    if "index_membership" in bad:
        actions.append({
            "kind": "script",
            "label": "补指数成分快照（PIT）",
            "cmd": [sys.executable, "scripts/seed_membership.py",
                    "--index", _CORE_INDEXES,
                    "--start", f"{date.today().year - 6}-01-01",
                    "--end", date.today().isoformat()],
        })
    if "fundamentals_history" in bad:
        actions.append({
            "kind": "script",
            "label": "补财报历史（仅已披露报告期）",
            "cmd": [sys.executable, "scripts/seed_fundamentals.py", "--history"],
        })
    return actions


def _exec_repair(actions: list[dict]) -> None:
    """串行执行修复动作（每个动作独立子进程 + 超时，卡死不影响 API）。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = _BACKEND_DIR
    for act in actions:
        act["status"] = "RUNNING"
        act["started_at"] = datetime.now().isoformat(timespec="seconds")
        _repair_state["current"] = act["label"]
        try:
            if act["kind"] == "pipeline":
                code = (
                    "from app.datahub.runner import run_pipeline;"
                    "import sys;"
                    "rid = run_pipeline('repair', only=%r, stop_on_fail=False);"
                    "print('PIPELINE_RID', rid, flush=True)"
                ) % (act["steps"],)
                cmd = [sys.executable, "-c", code]
            else:
                cmd = act["cmd"]
            proc = subprocess.run(cmd, env=env, cwd=_BACKEND_DIR, capture_output=True,
                                  text=True, timeout=_REPAIR_TIMEOUT)
            out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            m = re.search(r"PIPELINE_RID\s+(\d+)", out)
            if m:
                act["run_id"] = int(m.group(1))
            if proc.returncode == 0:
                act["status"] = "OK"
                act["message"] = (f"管道 run_id={act['run_id']}" if act.get("run_id")
                                  else out[-200:] or "完成")
            else:
                act["status"] = "FAIL"
                act["message"] = f"rc={proc.returncode}: {out[-300:]}"
        except subprocess.TimeoutExpired:
            act["status"] = "FAIL"
            act["message"] = f"超过 {_REPAIR_TIMEOUT}s 未完成，子进程已终止"
        except Exception as e:  # noqa: BLE001
            act["status"] = "FAIL"
            act["message"] = f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            act["finished_at"] = datetime.now().isoformat(timespec="seconds")
            _repair_state["done"] += 1
    _repair_state["running"] = False
    _repair_state["current"] = None
    _repair_state["finished_at"] = datetime.now().isoformat(timespec="seconds")
    _repair_state["ok"] = all(a.get("status") == "OK" for a in actions)
    logger.info("一键修复完成 ok=%s，共 %s 个动作", _repair_state["ok"], len(actions))


@router.post("/reprocess")
def reprocess(req: ReprocessRequest):
    """一键修复资产清单中的异常数据集（warn/stale/empty）。

    后台线程 + 子进程隔离：单个步骤卡死有超时兜底，不会拖垮 API 进程。
    修复动作 = 重跑对应管道步骤（幂等，ETL 自己识别缺口/残缺日重补），
    指数成分与财报走独立补数脚本。
    """
    if _repair_state["running"]:
        return {"ok": False, "error": "已有修复任务在运行中"}
    if _running_pipeline["locked"]:
        return {"ok": False, "error": "数据管道正在运行，请稍后再试"}
    actions = _plan_repair(req.items, req.include_warn)
    if not actions:
        return {"ok": False, "error": "没有需要修复的数据集（全部正常，或该数据集无对应修复动作）"}
    now = datetime.now().isoformat(timespec="seconds")
    _repair_state.update(running=True, job_id=now, actions=actions, done=0,
                         total=len(actions), current=None, started_at=now,
                         finished_at=None, ok=None,
                         targets=sorted({k for a in actions for k in a.get("datasets", [])}
                                        | {k for a in actions
                                           for k in (["index_membership"] if "membership" in a["label"]
                                                     else ["fundamentals_history"] if "财报" in a["label"]
                                                     else [])}))
    threading.Thread(target=_exec_repair, args=(actions,), daemon=True).start()
    return {"ok": True, "job_id": now, "total": len(actions),
            "actions": [{"label": a["label"], "kind": a["kind"]} for a in actions]}


@router.get("/reprocess/status")
def reprocess_status():
    """查询一键修复任务进度（前端轮询）。"""
    return {
        "running": _repair_state["running"],
        "job_id": _repair_state["job_id"],
        "done": _repair_state["done"],
        "total": _repair_state["total"],
        "current": _repair_state["current"],
        "ok": _repair_state["ok"],
        "started_at": _repair_state["started_at"],
        "finished_at": _repair_state["finished_at"],
        "actions": [
            {"label": a["label"], "kind": a["kind"], "status": a.get("status"),
             "message": a.get("message"), "run_id": a.get("run_id")}
            for a in _repair_state["actions"]
        ],
    }


@router.get("/lineage")
def lineage():
    """数据血缘全景：源 → 步骤 → 层 → 运行时间线。"""
    from app.services.lineage_svc import lineage_report

    return lineage_report(SessionLocal())


@router.get("/dataflow")
def data_flow():
    """数据流全景（源头/Bronze/Silver/Gold）。"""
    from app.services.dataflow_svc import dataflow_report

    return dataflow_report(SessionLocal())


@router.get("/data-health")
def data_health():
    """数据健康度：采集/处理/应用三层评分 + 告警列表。"""
    return health_report()


@router.get("/assets")
def assets(force: bool = False):
    """数据资产清单：每张表的行数 / 覆盖标的 / 起止日期 / 滞后交易日 / 新鲜度状态。"""
    from app.services.assets_svc import assets_report

    return assets_report(SessionLocal(), force=force)


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
                "repair": {
                    "running": _repair_state["running"], "done": _repair_state["done"],
                    "total": _repair_state["total"], "current": _repair_state["current"],
                    "ok": _repair_state["ok"], "finished_at": _repair_state["finished_at"],
                },
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
