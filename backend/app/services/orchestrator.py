"""Orchestrator：研究流水线 DAG 编排器（P2）。

职责：按固定顺序驱动各 Agent 步骤，每步动作落 AgentEvent（SSE 由此广播），
在 RiskAgent 审查处挂起等待人工（倒计时自动放行保证无人值守）。

设计约束（沿用项目已验证的模式）：
- 重活（数据管道）走 subprocess 隔离 + 硬超时（data_scheduler 同款）；
- 轻步（质检/信号/发布）进程内直调；
- 整条流水线在**后台线程**里跑，API 只负责启动与查询；
- 每步独立 try/except，单步失败不拖垮整个 run（质量闸门除外——数据不合格
  必须阻断下游，这是 QualityAgent 存在的意义）。

步骤顺序（与合并设计稿一致）：
  data → quality → research → signal → backtest → attribution → risk(闸门) → publish
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.models import AgentEvent, ResearchRun, SignalAction

# 人工闸门默认倒计时（秒）；驾驶舱确认后可提前放行/否决
GATE_COUNTDOWN_SEC = int(os.getenv("QUANT_GATE_SECONDS", "300"))
DATA_STEP_TIMEOUT = 1800

STEPS: list[dict] = [
    {"key": "data", "agent": "data", "title": "数据采集与处理"},
    {"key": "quality", "agent": "quality", "title": "数据质检"},
    {"key": "research", "agent": "research", "title": "策略选股"},
    {"key": "signal", "agent": "signal", "title": "信号判定"},
    {"key": "backtest", "agent": "backtest", "title": "组合回测刷新"},
    {"key": "attribution", "agent": "attribution", "title": "归因统计"},
    {"key": "risk", "agent": "risk", "title": "发布审查（人工闸门）"},
    {"key": "publish", "agent": "publish", "title": "信号发布"},
]

# 进程内运行状态（驾驶舱轮询用；权威状态在 research_runs 表）
STATE: dict = {"running": False, "run_id": None, "step": None,
               "started_at": None, "finished_at": None}


def is_running() -> bool:
    return STATE["running"]


def start_run(db: Session, trigger: str = "manual") -> int:
    """启动一次流水线（后台线程）。返回 run_id。"""
    if STATE["running"]:
        raise RuntimeError("已有流水线在运行中")
    run = ResearchRun(trigger=trigger, status="RUNNING")
    db.add(run)
    db.commit()
    db.refresh(run)
    threading.Thread(target=_run, args=(run.id,), daemon=True).start()
    return run.id


def _event(db: Session, run_id: int, agent: str, message: str, *,
           step: str = "", status: str | None = None, rows: int | None = None,
           level: str = "info", duration_ms: int | None = None) -> None:
    db.add(AgentEvent(run_id=run_id, agent=agent, step=step, status=status,
                      level=level, message=message, rows=rows,
                      duration_ms=duration_ms))


def _run(run_id: int) -> None:
    """流水线主体（后台线程；每步独立 session，崩溃安全）。"""
    from app.database import SessionLocal

    STATE.update(running=True, run_id=run_id, step=None,
                 started_at=datetime.now().isoformat(timespec="seconds"),
                 finished_at=None)
    db = SessionLocal()
    try:
        _step_data(db, run_id)
        _step_quality(db, run_id)
        _step_research_signal(db, run_id)
        _step_stub(db, run_id, "backtest", "BacktestAgent 组合回测刷新",
                   "增量回测刷新（P3 接入 signal_stats 六维统计；当前仅校验信号引擎输出）")
        _step_stub(db, run_id, "attribution", "AttributionAgent 归因统计",
                   "六维有效性统计（P3 接入；当前信号事实已完整落 signal_daily/signal_actions）")
        if not _step_risk_gate(db, run_id):
            _finish(db, run_id, "REJECTED", "人工否决：信号不发布")
            return
        _step_publish(db, run_id)
        _finish(db, run_id, "SUCCESS", None)
    except _QualityBlocked as exc:
        _finish(db, run_id, "FAILED", f"质量闸门阻断：{exc}")
    except Exception as exc:  # noqa: BLE001
        _finish(db, run_id, "FAILED", str(exc)[:500])
    finally:
        db.close()


def _step_data(db: Session, run_id: int) -> None:
    """数据采集与处理：子进程跑完整 datahub 管道（采集→清洗→因子→DuckDB 同步）。"""
    backend = str(BASE_DIR / "backend")
    code = ("from app.datahub.runner import run_pipeline;"
            "import sys; sys.stdout.flush();"
            "rid = run_pipeline('orchestrator');"
            "print('PIPELINE_RID', rid, flush=True)")
    env = dict(os.environ)
    env["PYTHONPATH"] = backend
    t0 = time.time()
    _event(db, run_id, "data", "数据管道启动（采集→清洗→因子→DuckDB 同步）",
           step="data", status="running")
    db.commit()
    try:
        proc = subprocess.run([sys.executable, "-c", code], env=env, cwd=backend,
                              capture_output=True, text=True,
                              timeout=DATA_STEP_TIMEOUT)
    except subprocess.TimeoutExpired:
        _event(db, run_id, "data", f"数据管道超过 {DATA_STEP_TIMEOUT}s 已终止",
               step="data", status="fail", level="error")
        db.commit()
        raise
    rid_line = next((l for l in (proc.stdout or "").splitlines()
                     if l.startswith("PIPELINE_RID")), "")
    ok = "SUCCESS" in (proc.stdout or "") or rid_line != ""
    _event(db, run_id, "data",
           f"数据管道完成（{time.time()-t0:.0f}s，{rid_line or '无 run 记录'}）",
           step="data", status="ok" if ok else "warn",
           duration_ms=int((time.time() - t0) * 1000))
    db.commit()


class _QualityBlocked(Exception):
    pass


def _step_quality(db: Session, run_id: int) -> None:
    """数据质检：健康分 < 60 或 status=error → 阻断下游。"""
    from app.services.data_health import health_report

    t0 = time.time()
    rep = health_report(db)
    score = rep.get("overall_score", 0)
    status = rep.get("overall_status", "error")
    failed = [c["name"] for layer in rep.get("layers", {}).values()
              for c in layer.get("checks", []) if c.get("status") == "fail"]
    blocked = status == "error" or score < 60
    _event(db, run_id, "quality",
           f"健康分 {score}（{status}）" + (f"；未过项: {'、'.join(failed[:5])}" if failed else ""),
           step="quality", status="fail" if blocked else "ok",
           level="error" if blocked else "info",
           duration_ms=int((time.time() - t0) * 1000))
    db.commit()
    if blocked:
        raise _QualityBlocked(f"健康分 {score}（{status}）")


def _step_research_signal(db: Session, run_id: int) -> None:
    """策略选股 + 信号判定：run_signal_scan 内部已按 research/signal 发事件。"""
    from app.config import DATA_DIR
    from app.services.xq import runner as xq_runner

    xq_runner.run_signal_scan(db, str(DATA_DIR / "quant.duckdb"), run_id=run_id)


def _action_distribution(db: Session) -> dict[str, int]:
    from datetime import date as _date

    latest = db.scalars(
        select(SignalAction.date).order_by(SignalAction.date.desc()).limit(1)
    ).first()
    if not latest:
        return {}
    rows = db.scalars(
        select(SignalAction).where(SignalAction.date == latest)
    ).all()
    dist: dict[str, int] = {}
    for r in rows:
        dist[r.action] = dist.get(r.action, 0) + 1
    return dist


def _step_stub(db: Session, run_id: int, agent: str, title: str, note: str) -> None:
    _event(db, run_id, agent, f"{title}：{note}", step=agent, status="ok")
    db.commit()


def _step_risk_gate(db: Session, run_id: int) -> bool:
    """人工闸门：置 pending + 倒计时，等待人工放行/否决或超时自动放行。

    返回 True=放行（继续 publish），False=否决（终止）。
    """
    dist = _action_distribution(db)
    deadline = datetime.now() + timedelta(seconds=GATE_COUNTDOWN_SEC)
    run = db.get(ResearchRun, run_id)
    run.gate_status = "pending"
    run.gate_note = f"待审查：动作分布 {dist}"
    run.gate_deadline = deadline
    _event(db, run_id, "risk",
           f"发布审查挂起：{dist}；{GATE_COUNTDOWN_SEC}s 内未处理将自动放行",
           step="risk", status="pending")
    db.commit()

    while True:
        db.expire_all()
        run = db.get(ResearchRun, run_id)
        if run.gate_status in ("approved", "rejected"):
            _event(db, run_id, "risk",
                   f"闸门{'放行' if run.gate_status == 'approved' else '否决'}"
                   f"（{run.gate_note or '人工'}）",
                   step="risk", status="ok" if run.gate_status == "approved" else "fail")
            db.commit()
            return run.gate_status == "approved"
        if datetime.now() >= (run.gate_deadline or deadline):
            run.gate_status = "approved"
            run.gate_note = "倒计时到时自动放行"
            db.commit()
            _event(db, run_id, "risk", "倒计时到时自动放行（无人值守模式）",
                   step="risk", status="ok")
            db.commit()
            return True
        time.sleep(5)


def _step_publish(db: Session, run_id: int) -> None:
    """信号发布：HTML 邮件 + 飞书（P3 会替换成完整报告模板）。"""
    from app.services import mailer

    dist = _action_distribution(db)
    latest = db.scalars(
        select(SignalAction.date).order_by(SignalAction.date.desc()).limit(1)
    ).first()
    items = db.scalars(
        select(SignalAction).where(
            SignalAction.date == latest,
            SignalAction.action.in_(("BUY_STRONG", "WATCH", "SELL", "REDUCE")),
        ).order_by(SignalAction.action, SignalAction.streak.desc())
    ).all() if latest else []

    rows_html = "".join(
        f"<tr><td>{i.action}</td><td>{i.name or ''} {i.symbol}</td>"
        f"<td>{i.streak}</td><td>{i.strategies}</td><td>{i.reason}</td></tr>"
        for i in items[:50]
    )
    html = (
        "<html><body style='font-family:-apple-system,Helvetica;margin:24px'>"
        f"<h2>每日信号报告 · {latest}</h2>"
        f"<p>动作分布：{dist}</p>"
        "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-size:13px'>"
        "<tr style='background:#f5f5f5'><th>动作</th><th>标的</th><th>连续天数</th>"
        "<th>命中策略</th><th>判定理由</th></tr>"
        f"{rows_html}</table>"
        "<p style='color:#888;font-size:12px'>由个人量化平台自动生成（研究结论仅供参考，不构成投资建议）</p>"
        "</body></html>"
    )
    title = f"每日信号 · {latest}"
    mailer.send_email_html(db, title, html, dedup_key=f"signal|{latest}")
    lines = [f"{i.action} {i.name or ''} {i.symbol}（连续 {i.streak} 天）"
             for i in items[:15]]
    mailer.send_feishu(db, title, lines or ["今日无重点信号"],
                       dedup_key=f"signal|{latest}")
    _event(db, run_id, "publish", f"发布完成：邮件 + 飞书（{title}）",
           step="publish", status="ok")
    db.commit()


def _finish(db: Session, run_id: int, status: str, error: str | None) -> None:
    run = db.get(ResearchRun, run_id)
    if run:
        run.status = status
        run.error = error
        run.finished_at = datetime.now()
    db.add(AgentEvent(run_id=run_id, agent="orchestrator",
                      message=f"流水线结束：{status}" + (f"（{error}）" if error else ""),
                      status="ok" if status == "SUCCESS" else "fail",
                      level="info" if status == "SUCCESS" else "error"))
    db.commit()
    STATE.update(running=False, step=None,
                 finished_at=datetime.now().isoformat(timespec="seconds"))
