"""信号中心 API（Sequoia-X 迁移）。

- POST /signal/scan          跑一次选股 + 信号判定（同步；向量化实现，秒级）
- GET  /signal/dates         有信号的日期列表（新→旧）
- GET  /signal/actions       某日买卖动作（雷达视图，按动作等级排序）
- GET  /signal/picks         某日各策略命中明细
- GET  /signal/timeline      近 N 日上榜时间线（streak 可视化用）
- POST /signal/backfill      历史回填：按交易日从最早缺失日起逐日重演
"""

from __future__ import annotations

import json
import threading
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import DATA_DIR
from app.database import get_db
from app.models import AgentEvent, ResearchRun, SignalAction, SignalDaily
from app.services.xq import runner as xq_runner

router = APIRouter(prefix="/signal", tags=["signal"])

# 动作展示顺序（雷达分组顺序，与 Sequoia 一致）
_ACTION_ORDER = {"BUY_STRONG": 0, "WATCH": 1, "NEW": 2, "REDUCE": 3, "SELL": 4, "EXIT": 5}

DUCKDB_PATH = str(DATA_DIR / "quant.duckdb")


class ScanPayload(BaseModel):
    as_of: str = Field("", description="数据日期 YYYY-MM-DD；空=最新已收盘日")
    days: int = Field(60, ge=1, le=250, description="backfill：回填交易日数")


@router.post("/scan")
def scan(payload: ScanPayload, db: Session = Depends(get_db)):
    """跑一次完整选股 + 信号判定（幂等，同日重跑=整批重写）。"""
    as_of = date.fromisoformat(payload.as_of) if payload.as_of else None

    run = ResearchRun(trigger="manual", status="RUNNING",
                      data_date=as_of, gate_status="none")
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        result = xq_runner.run_signal_scan(db, DUCKDB_PATH, as_of=as_of, run_id=run.id)
    except Exception as exc:  # noqa: BLE001
        run.status = "FAILED"
        run.error = str(exc)[:500]
        run.finished_at = run.finished_at or __import__("datetime").datetime.now()
        db.commit()
        raise HTTPException(status_code=500, detail=f"信号扫描失败: {exc}") from exc

    run.status = "SUCCESS"
    from datetime import datetime

    run.finished_at = datetime.now()
    db.commit()
    result["run_id"] = run.id
    return result


@router.post("/backfill")
def backfill(payload: ScanPayload, db: Session = Depends(get_db)):
    """历史回填（后台线程）：近 N 个交易日逐日重演选股。

    time_travel 语义：每个日期用「截至该日的数据」跑同一套策略，天然防前视。
    面板只载入一次、按日切片（run_from_panel），60 日回填约 3~6 分钟。
    进度轮询：GET /signal/events?run_id=... 与 GET /signal/backfill/status。
    """
    if _backfill_state["running"]:
        return {"ok": False, "error": "已有回填在进行中", "state": _backfill_state}
    as_of = date.fromisoformat(payload.as_of) if payload.as_of else None

    run = ResearchRun(trigger="manual", status="RUNNING", data_date=as_of)
    db.add(run)
    db.commit()
    db.refresh(run)

    threading.Thread(
        target=_bg_backfill, args=(run.id, payload.days or 60, as_of), daemon=True
    ).start()
    return {"ok": True, "run_id": run.id, "note": "后台执行中，用 /signal/backfill/status 查进度"}


_backfill_state: dict = {"running": False, "run_id": None, "done": 0, "total": 0,
                         "current": None, "finished_at": None, "error": None}


def _bg_backfill(run_id: int, days: int, as_of: date | None) -> None:
    """回填工作线程：面板一次载入，逐日切片重演（每日独立 session，崩溃安全）。"""
    from datetime import datetime

    from app.database import SessionLocal
    from app.services.xq.data import load_panel
    from app.services.xq.runner import panel_asof, run_from_panel

    _backfill_state.update(running=True, run_id=run_id, done=0, total=days,
                           current=None, finished_at=None, error=None)
    db = SessionLocal()
    try:
        panel = load_panel(DUCKDB_PATH, lookback=260)
        latest = panel.as_of or panel.latest
        trade_dates = sorted(
            d for d in {
                (x if isinstance(x, date) else x.date()) for x in panel.df["date"]
            } if d <= latest
        )[-days:]
        if as_of is not None:
            trade_dates = [d for d in trade_dates if d <= as_of]
        _backfill_state["total"] = len(trade_dates)
        db.add(AgentEvent(run_id=run_id, agent="research", step="backfill",
                          status="running",
                          message=f"回填开始：{len(trade_dates)} 个交易日"))
        db.commit()
        for i, d in enumerate(trade_dates, 1):
            _backfill_state["current"] = d.isoformat()
            r = run_from_panel(db, panel_asof(panel, d), run_id=run_id)
            _backfill_state["done"] = i
            if not r.get("ok"):
                db.add(AgentEvent(run_id=run_id, agent="research", step="backfill",
                                  level="warn", status="fail",
                                  message=f"{d} 回填失败: {r.get('error')}"))
                db.commit()
        run = db.get(ResearchRun, run_id)
        if run:
            run.status = "SUCCESS"
            run.finished_at = datetime.now()
        db.add(AgentEvent(run_id=run_id, agent="research", step="backfill",
                          status="ok", message=f"回填完成：{len(trade_dates)} 天"))
        db.commit()
        _backfill_state.update(running=False, finished_at=datetime.now().isoformat(timespec="seconds"))
    except Exception as exc:  # noqa: BLE001
        _backfill_state.update(running=False, error=str(exc)[:300],
                               finished_at=datetime.now().isoformat(timespec="seconds"))
        try:
            run = db.get(ResearchRun, run_id)
            if run:
                run.status = "FAILED"
                run.error = str(exc)[:500]
                run.finished_at = datetime.now()
            db.add(AgentEvent(run_id=run_id, agent="research", step="backfill",
                              level="error", status="fail", message=str(exc)[:300]))
            db.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


@router.get("/backfill/status")
def backfill_status():
    return _backfill_state


@router.get("/dates")
def dates(db: Session = Depends(get_db)):
    rows = db.execute(
        select(SignalDaily.date, func.count(func.distinct(SignalDaily.symbol)))
        .group_by(SignalDaily.date)
        .order_by(SignalDaily.date.desc())
        .limit(60)
    ).all()
    return [{"date": d.isoformat(), "symbols": n} for d, n in rows]


@router.get("/actions")
def actions(date_str: str = "", db: Session = Depends(get_db)):
    """某日买卖动作雷达。缺省取最新有动作的一天。"""
    q = select(SignalAction)
    if date_str:
        q = q.where(SignalAction.date == date.fromisoformat(date_str))
    rows = db.scalars(q.order_by(SignalAction.date.desc(), SignalAction.streak.desc())).all()
    if not rows:
        return {"date": None, "items": []}
    d = rows[0].date
    items = [r for r in rows if r.date == d]
    items.sort(key=lambda r: (_ACTION_ORDER.get(r.action, 9), -r.streak))
    return {
        "date": d.isoformat(),
        "items": [
            {
                "symbol": r.symbol, "name": r.name, "action": r.action,
                "streak": r.streak, "resonance": r.resonance,
                "strategies": r.strategies, "quality_tier": r.quality_tier,
                "close": r.close, "ma10": r.ma10, "ma20": r.ma20, "reason": r.reason,
            } for r in items
        ],
    }


@router.get("/picks")
def picks(date_str: str = "", db: Session = Depends(get_db)):
    """某日各策略命中明细。"""
    q = select(SignalDaily)
    if date_str:
        q = q.where(SignalDaily.date == date.fromisoformat(date_str))
    rows = db.scalars(q.order_by(SignalDaily.date.desc())).all()
    if not rows:
        return {"date": None, "groups": []}
    d = rows[0].date
    groups: dict[str, list] = {}
    for r in (x for x in rows if x.date == d):
        groups.setdefault(r.strategy, []).append(
            {"symbol": r.symbol, "name": r.name}
        )
    return {
        "date": d.isoformat(),
        "groups": [
            {"strategy": k, "items": v} for k, v in sorted(groups.items())
        ],
    }


@router.get("/timeline")
def timeline(days: int = 30, db: Session = Depends(get_db)):
    """近 N 个自然日的上榜时间线（驾驶舱 streak 曲线用）。"""
    since = date.today() - timedelta(days=days)
    rows = db.execute(
        select(SignalDaily.date, SignalDaily.symbol, SignalDaily.strategy)
        .where(SignalDaily.date >= since)
        .order_by(SignalDaily.date)
    ).all()
    out: dict[str, dict] = {}
    for d, sym, strat in rows:
        key = d.isoformat()
        slot = out.setdefault(key, {"date": key, "symbols": 0, "by_symbol": {}})
        slot["symbols"] += 1
        slot["by_symbol"].setdefault(sym, []).append(strat)
    for slot in out.values():
        slot["by_symbol"] = {s: v for s, v in sorted(
            slot["by_symbol"].items(), key=lambda kv: -len(kv[1]))[:50]}
    return sorted(out.values(), key=lambda x: x["date"])


@router.get("/events")
def events(run_id: int = 0, limit: int = 200, db: Session = Depends(get_db)):
    """Agent 事件流（驾驶舱用；run_id=0 取最近一次 run）。"""
    q = select(AgentEvent)
    if run_id:
        q = q.where(AgentEvent.run_id == run_id)
    else:
        last = db.scalars(
            select(AgentEvent.run_id).order_by(AgentEvent.id.desc()).limit(1)
        ).first()
        if last:
            q = q.where(AgentEvent.run_id == last)
    rows = db.scalars(q.order_by(AgentEvent.id.desc()).limit(limit)).all()
    return [
        {
            "id": r.id, "run_id": r.run_id, "agent": r.agent, "step": r.step,
            "level": r.level, "status": r.status, "message": r.message,
            "rows": r.rows, "duration_ms": r.duration_ms,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in reversed(rows)
    ]


@router.get("/stats")
def stats(days: int = 90, db: Session = Depends(get_db)):
    """信号有效性六维统计（归因分析页数据源；10 分钟缓存）。"""
    from app.services.xq import stats as xq_stats

    return xq_stats.compute_stats(db, DUCKDB_PATH, days=days)


@router.get("/report", response_class=PlainTextResponse)
def report_preview(db: Session = Depends(get_db)):
    """预览每日信号报告（与邮件正文同一份 HTML）。"""
    from app.services.orchestrator import build_report

    _title, html = build_report(db)
    return PlainTextResponse(html, media_type="text/html")


@router.get("/portfolio")
def portfolio(db: Session = Depends(get_db)):
    """镜像组合最新快照（真实成本模拟：T+1 建仓/整手/佣金印花税/止盈止损）。"""
    from sqlalchemy import select as _select

    from app.models import PortfolioState

    row = db.scalars(
        _select(PortfolioState).order_by(PortfolioState.as_of_date.desc()).limit(1)
    ).first()
    if not row:
        return {"ok": False, "error": "尚无组合快照（流水线运行后生成）"}
    return {
        "ok": True, "as_of": row.as_of_date.isoformat(),
        "summary": json.loads(row.summary_json),
        "config": json.loads(row.config_json),
        "equity": json.loads(row.equity_json),
        "holdings": json.loads(row.holdings_json),
        "trades": json.loads(row.trades_json),
    }
