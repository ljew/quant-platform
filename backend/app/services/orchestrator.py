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

from app.core.subproc import run_with_timeout
import sys
import threading
import time
from pathlib import Path
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.models import AgentEvent, ResearchRun, SignalAction

# 人工闸门默认倒计时（秒）；驾驶舱确认后可提前放行/否决
# 人工闸门倒计时（秒）。默认 **0 = 不挂起**：这是单人自用的平台，没有第二个人
# 来签字，原来每天 17:30 都要 pending 5 分钟等「人工审查」，页面上就是一条
# 黄条，纯噪音（老刘 2026-11-08：「发布审查啥意思，一直警告」）。
# 想恢复人工确认：QUANT_GATE_SECONDS=300。
GATE_COUNTDOWN_SEC = int(os.getenv("QUANT_GATE_SECONDS", "0"))
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
        # error 只存一行文本，没有堆栈就查不出是哪一步、哪个字段出的问题。
        # 2026-11-09 的 `Invalid isoformat string: '杨逸君'` 连崩 3 次、
        # 复盘时只能靠猜，就是因为缺这一段。这里补「最深一帧」的文件:行号 + 函数名，
        # 500 字上限够放（完整栈只进 stdout 日志）。
        import traceback

        frames = traceback.extract_tb(exc.__traceback__)
        where = ""
        if frames:
            f = frames[-1]
            name = (f.filename or "").rsplit("/", 1)[-1] or "?"
            where = f" @ {name}:{f.lineno} in {f.name}"
        print(traceback.format_exc())
        _finish(db, run_id, "FAILED", (f"{type(exc).__name__}: {exc}{where}")[:500])
    finally:
        db.close()


def _step_data(db: Session, run_id: int) -> None:
    """数据采集与处理：子进程跑完整 datahub 管道（采集→清洗→因子→DuckDB 同步）。"""
    # 后端根目录按**本文件位置**推导，不能用 BASE_DIR/"backend"：
    # 容器内 app 包在 /app/app、后端根是 /app，BASE_DIR 会算成 "/"，
    # 于是 cwd="/backend" 不存在 → [Errno 2]（2026-11-02 踩过，
    # 每日流水线因此从未真正采到数据，也就从未发出过信号邮件）。
    # 宿主机 backend/app/services/orchestrator.py → parents[2]=backend/
    # 容器   /app/app/services/orchestrator.py → parents[2]=/app/
    backend = str(Path(__file__).resolve().parents[2])
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
        # ⚠️ 必须走 run_with_timeout：subprocess.run 的 timeout 只杀**直接子进程**，
        # 孙进程仍持有 stdout 管道时 communicate() 会一直等 EOF —— 2026-11-05
        # 这一处实际跑了 20.2 小时才返回，DATA_STEP_TIMEOUT=1800 形同虚设。
        proc = run_with_timeout([sys.executable, "-c", code], env=env, cwd=backend,
                                timeout=DATA_STEP_TIMEOUT)
    except subprocess.TimeoutExpired:
        _event(db, run_id, "data", f"数据管道超过 {DATA_STEP_TIMEOUT}s 已终止",
               step="data", status="fail", level="error")
        db.commit()
        raise
    rid_line = next((l for l in (proc.stdout or "").splitlines()
                     if l.startswith("PIPELINE_RID")), "")
    # ⚠️ 非零退出必须判失败并中断：原来只看 stdout 里有没有 "SUCCESS"，
    # 子进程炸了也只是 warn、流水线照常往下跑 —— 数据没更新却发布信号，
    # 这是无人值守系统里最坏的失效方式（静默陈旧）。
    if proc.returncode != 0:
        tail = ((proc.stderr or proc.stdout or "").strip().splitlines() or [""])[-1]
        _event(db, run_id, "data",
               f"数据管道失败（rc={proc.returncode}）：{tail[:200]}",
               step="data", status="fail", level="error",
               duration_ms=int((time.time() - t0) * 1000))
        db.commit()
        raise RuntimeError(f"数据管道失败 rc={proc.returncode}: {tail[:200]}")
    _event(db, run_id, "data",
           f"数据管道完成（{time.time()-t0:.0f}s，{rid_line or '无 run 记录'}）",
           step="data", status="ok",
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

    duck = str(DATA_DIR / "quant.duckdb")
    xq_runner.run_signal_scan(db, duck, run_id=run_id)
    _backfill_signal_gaps(db, run_id, duck)


def _backfill_signal_gaps(db: Session, run_id: int, duck_path: str,
                          max_days: int = 5) -> None:
    """补算「行情已有、但 signal_daily 没有」的交易日。

    2026-11-12 事故：11-11 的信号**整天缺失**，今日观点一直显示 11-10。
    `run_signal_scan` 只算「面板最新一天」，而 11-11 那天编排跑的时候分析库
    （DuckDB）还冻结在 11-10（sync_duckdb 因缺 sqlite 扩展在静默跳过），
    于是它算的是 11-10、把 11-10 又写了一遍；11-11 从此没人算 ——
    而且**不会自愈**：第二天只算 11-12，缺口永远留着。

    口径用 SQLite 的 kline 最新日作「应算到哪天」（DuckDB 可能滞后），
    只补 signal_daily 最新日之后的日子，最多 max_days 天，避免一次回填太久。
    任何异常都只发 warning 事件，绝不能拖垮整条编排。
    """
    from app.models import KlineDaily, SignalDaily
    from app.services.xq.data import load_panel
    from app.services.xq.runner import panel_asof, run_from_panel

    try:
        from sqlalchemy import func

        latest_sig = db.execute(select(func.max(SignalDaily.date))).scalar()
        latest_bar = db.execute(select(func.max(KlineDaily.trade_date))).scalar()
        if not latest_sig or not latest_bar or latest_bar <= latest_sig:
            return
        panel = load_panel(duck_path, lookback=320)
        days = sorted({
            (x if isinstance(x, date) else x.date()) for x in panel.df["date"]
        } - {latest_sig})
        days = [d for d in days if latest_sig < d <= latest_bar][-max_days:]
        if not days:
            _event(db, run_id, "research",
                   f"信号缺口检测：行情已到 {latest_bar}、信号停在 {latest_sig}，"
                   "但面板里找不到中间交易日（DuckDB 可能还没同步）",
                   step="signal", status="warn", level="warn")
            db.commit()
            return
        _event(db, run_id, "research",
               f"信号缺口：行情已到 {latest_bar}、信号停在 {latest_sig}，"
               f"补算 {len(days)} 天（{days[0]} ~ {days[-1]}）",
               step="signal", status="running")
        db.commit()
        done = 0
        for d in days:
            r = run_from_panel(db, panel_asof(panel, d, keep_days=260), run_id=run_id)
            if r.get("ok"):
                done += 1
            else:
                _event(db, run_id, "research", f"{d} 补算失败：{r.get('error')}",
                       step="signal", status="fail", level="warn")
                db.commit()
        _event(db, run_id, "research", f"信号补算完成：{done}/{len(days)} 天",
               step="signal", status="ok")
        db.commit()
    except Exception as exc:  # noqa: BLE001
        _event(db, run_id, "research", f"信号缺口补算异常（已跳过）：{exc}"[:200],
               step="signal", status="fail", level="warn")
        db.commit()


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

    # 镜像组合重演（真实成本：佣金/印花税/过户费/滑点；TP+30%/SL-10%）
    from app.services.xq import portfolio as xq_portfolio

    t1 = time.time()
    pf = xq_portfolio.run_portfolio(db, str(DATA_DIR / "quant.duckdb"))
    if pf.get("ok"):
        s = pf["summary"]
        _event(db, run_id, "backtest",
               f"镜像组合重演：净值 {s['total']:,.0f}（{s['return_pct']:+.2f}%）"
               f"· 最大回撤 {s['max_drawdown_pct']:.2f}% · {s['n_trades']} 笔 · "
               f"持有 {s['n_holdings']} 只",
               step="portfolio", status="ok", duration_ms=int((time.time() - t1) * 1000))
    else:
        _event(db, run_id, "backtest", f"镜像组合不可用：{pf.get('error')}",
               step="portfolio", status="warn", level="warn")
    db.commit()

    # 实验组合重演（fresh_ma 规则 + PIT regime 门控，2026-10-14 重新优化；
    # paper sleeve，与经典镜像组合隔离并行，不接生产信号管道）
    from app.services.xq import portfolio_fresh as xq_portfolio_fresh

    t2 = time.time()
    ef = xq_portfolio_fresh.run_portfolio_fresh(db, str(DATA_DIR / "quant.duckdb"))
    if ef.get("ok"):
        s = ef["summary"]
        _event(db, run_id, "backtest",
               f"实验组合重演（fresh_ma+门控）：净值 {s['total']:,.0f}"
               f"（{s['return_pct']:+.2f}%）· 最大回撤 {s['max_drawdown_pct']:.2f}% · "
               f"{s['n_trades']} 笔 · 门控开启 {s['gate_open_days']}/{s['days']} 天",
               step="portfolio_exp", status="ok",
               duration_ms=int((time.time() - t2) * 1000))
    else:
        _event(db, run_id, "backtest", f"实验组合不可用：{ef.get('error')}",
               step="portfolio_exp", status="warn", level="warn")
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
    run = db.get(ResearchRun, run_id)
    if GATE_COUNTDOWN_SEC <= 0:
        # 免审查：直接放行，只在 run 上留一行说明（不再置 pending，避免页面闪黄条）
        run.gate_status = "approved"
        run.gate_note = f"免审查直接放行（动作分布 {dist}）"
        _event(db, run_id, "risk", f"发布审查：自动放行（{dist}）",
               step="risk", status="ok")
        db.commit()
        return True

    deadline = datetime.now() + timedelta(seconds=GATE_COUNTDOWN_SEC)
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

    # 境外资讯（OPS-FN-1b）：读 news_feed fn_*，不把 global_quotes 当新闻
    foreign_items: list[dict] = []
    try:
        from app.services import foreign_news as _fn
        foreign_items = (_fn.query_foreign(db, hours=24, limit=6).get("items") or [])[:6]
    except Exception:  # noqa: BLE001
        foreign_items = []

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
    if foreign_items:
        foreign_rows = "".join(
            f"<li style='margin:3px 0'>"
            + (f"<a href='{f.get('url')}' style='color:#2471a3;text-decoration:none'>{f.get('title') or ''}</a>"
               if f.get("url") else (f.get("title") or ""))
            + f" <span style='color:#aaa;font-size:11px'>{f.get('source_label') or f.get('source') or ''}"
            + (f" · {(f.get('published_at') or '')[:16]}" if f.get("published_at") else "")
            + "</span></li>"
            for f in foreign_items
        )
    else:
        foreign_rows = ("<li style='margin:3px 0;color:#888'>暂无境外入库，见平台「今日观点 → 境外资讯」"
                        "（#foreign-news）</li>")

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

    <h2 style="font-size:15px;border-left:4px solid #16a085;padding-left:10px;margin-top:26px">境外资讯</h2>
    <ul style="padding-left:18px;font-size:13px">{foreign_rows}</ul>

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
    # 飞书通道按 2026-09-30 决策移除（不用飞书）；mailer.send_feishu 保留备用
    mailer.send_email_html(db, title, html, dedup_key=f"signal|{latest}")
    _event(db, run_id, "publish", f"发布完成：邮件（{title}）",
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


def scheduler_state() -> dict:
    """每日调度器的可观测状态（驾驶舱/监控页展示，判断 17:30 是否会跑）。"""
    now = datetime.now()
    nxt = now.replace(hour=DAILY_HOUR, minute=DAILY_MINUTE, second=0, microsecond=0)
    if nxt <= now:
        nxt = nxt + timedelta(days=1)
    return {
        "enabled": True,
        "run_at": f"{DAILY_HOUR:02d}:{DAILY_MINUTE:02d}",
        "last_run_date": _daily_state["last_run_date"] or None,
        "last_error": _daily_state["last_error"],
        "next_run": nxt.isoformat(timespec="seconds"),
        "gate_countdown_sec": GATE_COUNTDOWN_SEC,
    }


def _reap_zombie_runs(older_than_hours: int = 3) -> int:
    """启动时把残留的 RUNNING 编排标 FAILED。

    正常编排全流程 ≤ 30 分钟（data 步骤硬超时 1800s）。超过 3 小时仍 RUNNING
    必然是执行线程已随进程退出、记录没来得及收尾。2026-11-06 实测库里积了
    11 条，最早停在 9-30，监控页「流水线状态」因此长期显示「运行中」。
    """
    from sqlalchemy import select as _select

    from app.database import SessionLocal
    from app.models import ResearchRun

    cutoff = datetime.now() - timedelta(hours=older_than_hours)
    try:
        with SessionLocal() as db:
            rows = db.execute(
                _select(ResearchRun).where(
                    ResearchRun.status == "RUNNING",
                    ResearchRun.started_at < cutoff,
                )
            ).scalars().all()
            for r in rows:
                r.status = "FAILED"
                r.gate_status = "cancelled"
                r.error = f"僵尸运行：超过 {older_than_hours} 小时仍 RUNNING（执行线程已退出）"
                r.finished_at = datetime.now()
            db.commit()
        if rows:
            print(f"[orchestrator] 清理僵尸编排 {len(rows)} 条")
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        print(f"[orchestrator] 清理僵尸编排失败: {exc}")
        return 0


_sched_started = False


def start_daily_scheduler() -> None:
    """启动每日定时线程（守护线程，60s 一跳）。

    幂等：每交易日只触发一次；错过的时点（如机器 17:30 未开机）在当天
    之后检测到「今天还没跑」时立即补跑（catch-up），重启进程亦会补跑。
    与 19:00 的 data_scheduler 不冲突（均幂等，管道步骤对已新数据快速跳过）。
    """
    global _sched_started
    if _sched_started:
        print("[orchestrator] 调度器已在运行，跳过重复启动")
        return
    _reap_zombie_runs()

    def _loop() -> None:
        # ⚠️ 必须在此导入：本模块其它函数都是「函数内局部导入」，
        # 模块作用域并无 SessionLocal —— 直接引用会抛 NameError，
        # 而异常又被外层 try 吞掉只写进 last_error（2026-11-02 踩过：
        # 每日定时自 09-30 上线起连续静默失败一个月，一封信号邮件都没发出去）。
        from app.database import SessionLocal

        def _fire(now: datetime, today: str) -> None:
            """真正启动一次 run。只有成功才记账，失败留待下次重试。"""
            db = SessionLocal()
            try:
                rid = start_run(db, trigger="scheduler")
                _event(db, rid, "orchestrator",
                       f"每日定时触发（{DAILY_HOUR:02d}:{DAILY_MINUTE:02d}）",
                       step="schedule", status="ok")
                db.commit()
            finally:
                db.close()
            # 原实现把「今天已跑」写在 start_run 之前，一旦抛异常当天即被
            # 标记完成、永不再重试 —— 这是这次静默失败的直接原因。
            _daily_state["last_run_date"] = today
            _daily_state["last_success"] = now.isoformat(timespec="seconds")
            _daily_state["last_error"] = None

        while True:
            try:
                now = datetime.now()
                today = now.date().isoformat()
                due = (now.hour, now.minute) >= (DAILY_HOUR, DAILY_MINUTE)
                pending = due and _daily_state["last_run_date"] != today
                # ⚠️ 重启后 _daily_state 是空的，只看内存会重复触发。
                # 2026-11-06 实测：18:36~18:40 容器重启 3 次，连续起了 #307/#308/#309
                # 三条并发编排，每条都会走到 gate 并各发一封邮件。必须回查 DB。
                if pending:
                    try:
                        from sqlalchemy import select as _select

                        from app.database import SessionLocal as _SL
                        from app.models import ResearchRun as _RR

                        with _SL() as _db:
                            # 只认**定时槽之后**跑完的。白天的普通手动运行（信号回填、
                            # 缺口补算等）不算「今天日更做完了」—— 2026-11-12 事故：
                            # 15:34/15:38 两次手动 run 让这里判「已跑」，
                            # 17:30 编排直接不触发，当天行情一条没入库。
                            _slot = datetime.combine(
                                now.date(), datetime.min.time()
                            ).replace(hour=DAILY_HOUR, minute=DAILY_MINUTE)
                            done = _db.execute(
                                _select(_RR.id).where(
                                    _RR.status == "SUCCESS",
                                    _RR.finished_at >= _slot,
                                ).limit(1)
                            ).first()
                        if done:
                            _daily_state["last_run_date"] = today
                            pending = False
                    except Exception:  # noqa: BLE001
                        pass   # 查不到按未跑处理：宁可多跑一次，不要漏跑
                if pending and not STATE["running"]:
                    from app.core.trading_calendar import is_trading_day

                    if not is_trading_day(now.date()):
                        _daily_state["last_run_date"] = today  # 非交易日，当天不再检查
                    else:
                        if _daily_state.get("attempts_date") != today:
                            _daily_state["attempts_date"] = today  # 新的一天，重置重试计数
                            _daily_state["attempts"] = 0
                        if _daily_state.get("attempts", 0) >= 3:
                            pass  # 当天连续失败 3 次后停手，last_error 供看门狗查看
                        else:
                            try:
                                _fire(now, today)
                            except Exception as exc:  # noqa: BLE001
                                _daily_state["attempts"] = _daily_state.get("attempts", 0) + 1
                                _daily_state["last_error"] = str(exc)[:300]
            except Exception as exc:  # noqa: BLE001
                _daily_state["last_error"] = str(exc)[:300]
            time.sleep(60)

    threading.Thread(target=_loop, daemon=True,
                     name="orchestrator-daily").start()
    _sched_started = True
