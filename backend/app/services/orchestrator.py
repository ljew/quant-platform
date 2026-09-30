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
        _step_backtest(db, run_id)
        _step_attribution(db, run_id)
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


def _step_backtest(db: Session, run_id: int) -> None:
    """BacktestAgent：信号有效性统计（组合回测的量化底座）。

    口径 = Sequoia signal_stats：T+1 开盘建仓、持有 T+N、超额对全市场等权。
    统计结果带缓存，归因页与发布报告共用。
    """
    from app.config import DATA_DIR
    from app.services.xq import stats as xq_stats

    t0 = time.time()
    result = xq_stats.compute_stats(db, str(DATA_DIR / "quant.duckdb"))
    if not result.get("ok"):
        _event(db, run_id, "backtest", f"有效性统计不可用：{result.get('error')}",
               step="backtest", status="warn", level="warn")
        db.commit()
        return
    _event(db, run_id, "backtest",
           f"有效性统计完成：{result['n_signals']} 信号 / {result['n_obs']} 观测 · "
           f"{xq_stats.top_line(result)}",
           step="backtest", status="ok", rows=result["n_obs"],
           duration_ms=int((time.time() - t0) * 1000))
    db.commit()


def _step_attribution(db: Session, run_id: int) -> None:
    """AttributionAgent：六维归因速览（明细由归因分析页展示）。"""
    from app.config import DATA_DIR
    from app.services.xq import stats as xq_stats

    result = xq_stats.compute_stats(db, str(DATA_DIR / "quant.duckdb"))
    if not result.get("ok"):
        _event(db, run_id, "attribution", "归因统计不可用（样本不足）",
               step="attribution", status="warn", level="warn")
        db.commit()
        return
    strong = [r for r in result.get("by_strategy", []) if r.get("exc_10") is not None]
    best = max(strong, key=lambda r: r["exc_10"]) if strong else None
    worst = min(strong, key=lambda r: r["exc_10"]) if strong else None
    _event(db, run_id, "attribution",
           "六维归因完成：最强 {strategy} {exc_10:+.2f}% / 最弱 {w_strategy} {w_exc:+.2f}%"
           "（T+10 超额，vs 全市场等权）".format(
               **best, w_strategy=worst["strategy"], w_exc=worst["exc_10"])
           if best and worst else "六维归因完成（样本不足）",
           step="attribution", status="ok")
    db.commit()


def _action_distribution(db: Session) -> dict[str, int]:
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


def build_report(db: Session) -> tuple[str, str]:
    """渲染正式 HTML 报告，返回 (标题, html)。publish 步骤与 /agent/report 预览共用。"""
    from app.config import DATA_DIR
    from app.services.news_agg import eastmoney_headlines, global_quotes, xueqiu_hot
    from app.services.xq import stats as xq_stats

    dist = _action_distribution(db)
    latest = db.scalars(
        select(SignalAction.date).order_by(SignalAction.date.desc()).limit(1)
    ).first()
    items = db.scalars(
        select(SignalAction).where(SignalAction.date == latest)
        .order_by(SignalAction.action, SignalAction.streak.desc())
    ).all() if latest else []

    stats = xq_stats.compute_stats(db, str(DATA_DIR / "quant.duckdb"))
    gq = global_quotes()
    xq_hot = xueqiu_hot()
    news = eastmoney_headlines(6)

    up, dn = "#c0392b", "#1e8449"  # A 股配色：红涨绿跌

    def _chg(v: float) -> str:
        color = up if v >= 0 else dn
        return f"<span style='color:{color}'>{v:+.2f}%</span>"

    def _rows(action: str) -> str:
        return "".join(
            f"<tr>"
            f"<td style='padding:5px 10px'>{i.name or ''} <b>{i.symbol}</b></td>"
            f"<td style='padding:5px 10px'>{i.streak} 天</td>"
            f"<td style='padding:5px 10px'>{i.strategies}</td>"
            f"<td style='padding:5px 10px;color:#555'>{i.reason}</td>"
            f"</tr>"
            for i in items if i.action == action
        ) or "<tr><td colspan='4' style='color:#999;padding:5px 10px'>无</td></tr>"

    action_cn = {"BUY_STRONG": "重点买入", "WATCH": "关注（确认中）", "NEW": "新出现",
                 "REDUCE": "减仓", "SELL": "卖出", "EXIT": "移出"}

    def _section(title: str, action: str, color: str) -> str:
        return (f"<h3 style='margin:18px 0 6px;color:{color}'>{action_cn[action]}"
                f"（{dist.get(action, 0)}）</h3>"
                "<table style='border-collapse:collapse;width:100%;font-size:13px'>"
                "<tr style='background:#f2f2f2'><th align='left' style='padding:5px 10px'>标的</th>"
                "<th align='left' style='padding:5px 10px'>连续</th>"
                "<th align='left' style='padding:5px 10px'>命中策略</th>"
                "<th align='left' style='padding:5px 10px'>判定理由</th></tr>"
                f"{_rows(action)}</table>")

    strat_rows = "".join(
        f"<tr><td style='padding:4px 10px'>{r['strategy']}</td>"
        f"<td style='padding:4px 10px'>{r.get('n_10', 0)}</td>"
        f"<td style='padding:4px 10px'>{_chg(r['exc_10'])}</td>"
        f"<td style='padding:4px 10px'>{_chg(r['exc_5'])}</td></tr>"
        for r in stats.get("by_strategy", []) if r.get("exc_10") is not None
    )
    gq_rows = "".join(
        f"<tr><td style='padding:4px 10px'>{q['name']}</td>"
        f"<td style='padding:4px 10px'>{q['price']}</td>"
        f"<td style='padding:4px 10px'>{_chg(q['change_pct'])}</td></tr>"
        for q in gq
    )
    xq_rows = "".join(
        f"<tr><td style='padding:4px 10px'>{i+1}. {h['name']}</td>"
        f"<td style='padding:4px 10px;color:#888'>热度 {int(h['heat'])}</td></tr>"
        for i, h in enumerate(xq_hot[:10])
    )
    news_rows = "".join(
        f"<li style='margin:3px 0'><a href='{n['url']}' style='color:#2471a3;text-decoration:none'>"
        f"{n['title']}</a> <span style='color:#aaa;font-size:11px'>{n['time'][:16]}</span></li>"
        for n in news
    )

    html = f"""<html><body style="font-family:-apple-system,'PingFang SC',Helvetica;margin:0;background:#fafafa">
<div style="max-width:860px;margin:0 auto;padding:24px">
  <div style="background:#1a1a2e;color:#fff;border-radius:12px 12px 0 0;padding:20px 28px">
    <div style="font-size:22px;font-weight:700">每日信号报告</div>
    <div style="opacity:.75;font-size:13px;margin-top:4px">{latest} · 个人量化投研平台自动生成 ·
      动作分布 BUY_STRONG {dist.get('BUY_STRONG', 0)} / WATCH {dist.get('WATCH', 0)} / NEW {dist.get('NEW', 0)}</div>
  </div>
  <div style="background:#fff;border:1px solid #eee;border-top:none;padding:8px 28px 24px;border-radius:0 0 12px 12px">
    <h2 style="font-size:16px;border-left:4px solid #c0392b;padding-left:10px">信号明细</h2>
    {_section('重点买入', 'BUY_STRONG', up)}
    {_section('关注', 'WATCH', '#b9770e')}
    {_section('新出现', 'NEW', '#2471a3')}
    {_section('减仓', 'REDUCE', dn)}
    {_section('卖出', 'SELL', dn)}

    <h2 style="font-size:16px;border-left:4px solid #8e44ad;padding-left:10px;margin-top:26px">策略归因速览（T+N 超额 vs 全市场等权）</h2>
    <table style="border-collapse:collapse;width:100%;font-size:13px">
      <tr style="background:#f2f2f2"><th align="left" style="padding:4px 10px">策略</th>
      <th align="left" style="padding:4px 10px">样本</th>
      <th align="left" style="padding:4px 10px">T+10 超额</th>
      <th align="left" style="padding:4px 10px">T+5 超额</th></tr>
      {strat_rows}
    </table>

    <div style="display:flex;gap:24px;flex-wrap:wrap;margin-top:26px">
      <div style="flex:1;min-width:240px">
        <h2 style="font-size:15px;border-left:4px solid #2471a3;padding-left:10px">全球市场（腾讯）</h2>
        <table style="border-collapse:collapse;width:100%;font-size:13px">{gq_rows}</table>
      </div>
      <div style="flex:1;min-width:240px">
        <h2 style="font-size:15px;border-left:4px solid #2471a3;padding-left:10px">雪球热榜</h2>
        <table style="border-collapse:collapse;width:100%;font-size:13px">{xq_rows}</table>
      </div>
    </div>

    <h2 style="font-size:15px;border-left:4px solid #2471a3;padding-left:10px;margin-top:26px">东财要闻</h2>
    <ul style="padding-left:18px;font-size:13px">{news_rows}</ul>

    <p style="color:#999;font-size:11px;margin-top:26px;border-top:1px solid #eee;padding-top:12px">
      本报告由个人量化投研平台 Orchestrator 自动生成（多 Agent 流水线：数据→质检→选股→信号→归因→发布）。
      研究结论仅供参考，不构成投资建议。如需人工复核请前往平台「研究驾驶舱」。</p>
  </div>
</div></body></html>"""

    title = f"每日信号 · {latest}"
    return title, html


def _step_publish(db: Session, run_id: int) -> None:
    """信号发布：正式排版 HTML 报告（信号 + 归因 + 外盘 + 雪球热榜 + 要闻）。"""
    from app.services import mailer

    title, html = build_report(db)
    latest = db.scalars(
        select(SignalAction.date).order_by(SignalAction.date.desc()).limit(1)
    ).first()
    items = db.scalars(
        select(SignalAction).where(SignalAction.date == latest)
        .order_by(SignalAction.action, SignalAction.streak.desc())
    ).all() if latest else []
    lines = [f"{i.action} {i.name or ''} {i.symbol}（连续 {i.streak} 天）"
             for i in items[:15]]
    mailer.send_email_html(db, title, html, dedup_key=f"signal|{latest}")
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


# ────────────────────────── 每日定时（17:30，交易日）──────────────────────────

DAILY_HOUR = int(os.getenv("QUANT_ORCHESTRATOR_HOUR", "17"))
DAILY_MINUTE = int(os.getenv("QUANT_ORCHESTRATOR_MINUTE", "30"))
_daily_state: dict = {"last_run_date": "", "last_success": None, "last_error": None}


def start_daily_scheduler() -> None:
    """启动每日定时线程（守护线程，60s 一跳）。

    幂等：每交易日只触发一次；错过的时点（如机器 17:30 未开机）在当天
    之后检测到「今天还没跑」时立即补跑（catch-up），重启进程亦会补跑。
    与 19:00 的 data_scheduler 不冲突（均幂等，管道步骤对已新数据快速跳过）。
    """
    def _loop() -> None:
        while True:
            try:
                now = datetime.now()
                today = now.date().isoformat()
                due = (now.hour, now.minute) >= (DAILY_HOUR, DAILY_MINUTE)
                if due and _daily_state["last_run_date"] != today and not STATE["running"]:
                    from app.core.trading_calendar import is_trading_day

                    if is_trading_day(now.date()):
                        _daily_state["last_run_date"] = today
                        db = SessionLocal()
                        try:
                            rid = start_run(db, trigger="scheduler")
                            _daily_state["last_error"] = None
                            _event(db, rid, "orchestrator",
                                   f"每日定时触发（{DAILY_HOUR:02d}:{DAILY_MINUTE:02d}）",
                                   step="schedule", status="ok")
                            db.commit()
                        finally:
                            db.close()
                    else:
                        _daily_state["last_run_date"] = today  # 非交易日跳过
            except Exception as exc:  # noqa: BLE001
                _daily_state["last_error"] = str(exc)[:300]
            time.sleep(60)

    threading.Thread(target=_loop, daemon=True,
                     name="orchestrator-daily").start()
