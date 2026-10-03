"""自动研究闭环（v1）：平台自己挖因子、自己复盘，用户只看结果。

两个定时任务（守护线程调度，幂等可补跑）：
- 每日挖矿（交易日 20:47，ETL 19:00 与信号 17:30 之后）：
  GP 遗传规划小规模搜索 → 精英因子全池精评 → 「优秀」自动启用进入每日生产
  （datahub 管道只计算 factor_registry 中 enabled 的因子），「可用」登记为候选。
- 每周复盘（周六 10:15）：信号归因 + 镜像组合 + 因子生产盘点 → HTML 报告
  落盘 data/reports/ 并邮件推送（复用 mailer 通道）。

安全边界：自动动作仅限「因子登记/启用」（可逆，toggle 即退回）；
策略降级/参数调整只进报告建议，不自动执行。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import DATA_DIR

logger = logging.getLogger(__name__)

CONFIG_PATH = DATA_DIR / "auto_research.json"
REPORT_DIR = DATA_DIR / "reports"

DEFAULT_CFG: dict = {
    "mining_enabled": True,        # 每日自动挖因子
    "review_enabled": True,        # 每周自动复盘
    "auto_enable_excellent": True, # 「优秀」因子自动启用进生产
    "email_review": True,          # 复盘报告邮件推送
    "mining_time": "20:47",        # 交易日，ETL(19:00)/信号(17:30) 之后
    "review_weekday": 5,           # 周六（0=周一）
    "review_time": "10:15",
    "mining_pop": 16,              # GP 种群规模（小规模快跑）
    "mining_gens": 5,              # 进化代数
    "mining_top_k": 5,             # 精英全池精评数量
}

_STATE: dict = {"mining_last": "", "review_last": "", "last_error": None}
_RUN_LOCK = threading.Lock()


# ──────────────────────────── 配置 ────────────────────────────

def load_config() -> dict:
    cfg = dict(DEFAULT_CFG)
    try:
        if CONFIG_PATH.exists():
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        pass
    return cfg


def save_config(patch: dict) -> dict:
    cfg = load_config()
    for k in DEFAULT_CFG:
        if k in patch:
            cfg[k] = patch[k]
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return cfg


# ──────────────────────────── 运行记录 ────────────────────────────

def _new_run(db: Session, job: str, trigger: str):
    from app.models import AutoResearchRun

    run = AutoResearchRun(job=job, trigger=trigger, status="RUNNING")
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _finish_run(db: Session, run_id: int, ok: bool, summary: dict,
                report_path: str | None = None, error: str | None = None) -> None:
    from app.models import AutoResearchRun

    run = db.get(AutoResearchRun, run_id)
    if run:
        run.status = "SUCCESS" if ok else "FAILED"
        run.summary_json = json.dumps(summary, ensure_ascii=False, default=str)
        run.report_path = report_path
        run.error = (error or "")[:500] or None
        run.finished_at = datetime.now()
    db.commit()


def latest_runs(db: Session, limit: int = 20) -> list[dict]:
    from app.models import AutoResearchRun

    rows = db.execute(
        select(AutoResearchRun).order_by(AutoResearchRun.id.desc()).limit(limit)
    ).scalars().all()
    return [
        {"id": r.id, "job": r.job, "trigger": r.trigger, "status": r.status,
         "summary": json.loads(r.summary_json or "{}"),
         "report_path": r.report_path, "error": r.error,
         "started_at": r.started_at.isoformat(timespec="seconds") if r.started_at else None,
         "finished_at": r.finished_at.isoformat(timespec="seconds") if r.finished_at else None}
        for r in rows
    ]


# ──────────────────────────── 任务 1：每日自动挖因子 ────────────────────────────

def _mining_body(db: Session, run_id: int, cfg: dict) -> dict:
    """GP 挖掘 → 评级 → 登记/启用。返回摘要。"""
    from app.models import FactorMineResult, FactorRegistry
    from app.services.factor_gp import gp_search

    result = gp_search(
        db, pop_size=int(cfg["mining_pop"]), generations=int(cfg["mining_gens"]),
        top_k=int(cfg["mining_top_k"]),
    )
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "GP 挖掘失败"))

    stamp = date.today().strftime("%m%d")
    enabled, candidates, skipped = [], [], []
    for i, el in enumerate(result.get("elites", [])):
        rating = el.get("rating", "无效")
        # 落库挖掘结果（与手动挖掘同表，可在因子检验页查看）
        row = FactorMineResult(
            name=f"AUTO-{stamp}-{i + 1}·{rating}",
            expr=el["expr"], rating=rating,
            ic_mean=el["ic_mean"], icir=el["icir"],
            result_json=json.dumps(el, ensure_ascii=False, default=str),
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        if rating not in ("优秀", "可用"):
            skipped.append({"name": row.name, "rating": rating})
            continue
        # 登记进注册表（同名幂等）
        exist = db.execute(select(FactorRegistry).where(
            FactorRegistry.name == row.name)).scalar()
        reg = exist or FactorRegistry(
            name=row.name, expr=row.expr,
            direction=1 if (el.get("ic_mean") or 0) >= 0 else -1,
            category="mined", source_id=row.id, ic_mean=el.get("ic_mean"),
            notes=f"自动挖矿入库（GP {cfg['mining_pop']}×{cfg['mining_gens']}）")
        if not exist:
            db.add(reg)
            db.commit()
            db.refresh(reg)
        # 只有「优秀」自动启用进入每日生产；「可用」留候选待人工
        if rating == "优秀" and cfg["auto_enable_excellent"] and reg.status == "candidate":
            reg.status = "enabled"
            db.commit()
        (enabled if reg.status == "enabled" else candidates).append(
            {"name": row.name, "rating": rating, "ic": el.get("ic_mean"),
             "icir": el.get("icir"), "status": reg.status})

    return {
        "evaluated": result.get("n_candidates_evaluated", 0),
        "elites": len(result.get("elites", [])),
        "enabled": enabled, "candidates": candidates, "skipped": skipped,
        "best_ic": max((abs(e.get("ic_mean") or 0) for e in result.get("elites", [])), default=0),
    }


def run_mining(trigger: str = "manual") -> int:
    """启动一次自动挖矿（后台线程）。返回 run_id。"""
    from app.database import SessionLocal

    db0 = SessionLocal()
    try:
        run = _new_run(db0, "mining", trigger)
    finally:
        db0.close()

    def _bg() -> None:
        if not _RUN_LOCK.acquire(blocking=False):
            db = SessionLocal()
            try:
                _finish_run(db, run.id, False, {}, error="已有自动研究任务在运行")
            finally:
                db.close()
            return
        db = SessionLocal()
        try:
            summary = _mining_body(db, run.id, load_config())
            _finish_run(db, run.id, True, summary)
        except Exception as exc:  # noqa: BLE001
            logger.exception("自动挖矿失败")
            _finish_run(db, run.id, False, {}, error=str(exc))
        finally:
            db.close()
            _RUN_LOCK.release()

    threading.Thread(target=_bg, daemon=True, name="auto-mining").start()
    return run.id


# ──────────────────────────── 任务 2：每周自动复盘 ────────────────────────────

def _review_body(db: Session, run_id: int, cfg: dict) -> tuple[dict, str]:
    """归因 + 镜像组合 + 因子生产盘点 → (摘要, 报告路径)。"""
    from app.models import FactorMineResult, FactorRegistry, PortfolioState
    from app.services.xq import stats as xq_stats

    duckdb_path = str(DATA_DIR / "quant.duckdb")
    stats = xq_stats.compute_stats(db, duckdb_path)

    # 1) 信号策略归因：走弱 = T+10 超额 < 0 且样本 ≥ 20
    strat_rows = [r for r in stats.get("by_strategy", []) if r.get("exc_10") is not None]
    weakening = [r for r in strat_rows
                 if (r.get("exc_10") or 0) < 0 and (r.get("n_10") or 0) >= 20]
    best = max(strat_rows, key=lambda r: r["exc_10"], default=None)
    worst = min(strat_rows, key=lambda r: r["exc_10"], default=None)

    # 2) 镜像组合最新状态
    pf_row = db.execute(select(PortfolioState)
                        .order_by(PortfolioState.as_of_date.desc()).limit(1)).scalar()
    pf = json.loads(pf_row.summary_json) if pf_row else {}

    # 3) 因子生产盘点
    reg_rows = db.execute(select(FactorRegistry)).scalars().all()
    n_enabled = sum(1 for r in reg_rows if r.status == "enabled")
    week_ago = datetime.now() - timedelta(days=7)
    new_factors = db.execute(
        select(FactorMineResult).where(FactorMineResult.created_at >= week_ago)
        .order_by(FactorMineResult.id.desc())).scalars().all()

    # 4) 建议（只进报告，不自动执行）
    suggestions = []
    for r in weakening:
        suggestions.append(f"信号「{r['strategy']}」近 90 日 T+10 超额 {r['exc_10']:+.2f}%"
                           f"（{r.get('n_10', 0)} 样本）走弱，建议人工评估是否降级")
    if not suggestions:
        suggestions.append("各信号策略归因均在正值区间，无需干预")

    summary = {
        "date": date.today().isoformat(),
        "strategies": [{"name": r["strategy"], "exc_10": r["exc_10"],
                        "exc_5": r.get("exc_5"), "n": r.get("n_10", 0)} for r in strat_rows],
        "best": best["strategy"] if best else None,
        "worst": worst["strategy"] if worst else None,
        "weakening": [r["strategy"] for r in weakening],
        "portfolio": pf,
        "factors": {"enabled": n_enabled, "total": len(reg_rows),
                    "new_this_week": len(new_factors)},
        "suggestions": suggestions,
    }

    # 5) 渲染 HTML 报告并落盘
    html = _render_review_html(summary)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"auto_review_{date.today().strftime('%Y%m%d')}.html"
    path.write_text(html, encoding="utf-8")

    # 6) 决策留痕：策略档位与上周快照比对，变化自动记入架构文档第 10 章
    try:
        from app.services import decisions_log
        from app.services.xq.strategies import STRATEGY_QUALITY
        ctx = {r["strategy"]: {"exc_10": r.get("exc_10"), "n": r.get("n_10", 0)}
               for r in strat_rows}
        changes = decisions_log.record_tier_changes(STRATEGY_QUALITY, context=ctx)
        if changes:
            summary["decisions_logged"] = changes
    except Exception as e:  # noqa: BLE001
        logger.warning("决策留痕失败（不影响复盘）: %s", e)
    return summary, str(path)


def _render_review_html(s: dict) -> str:
    up, dn = "#c0392b", "#1e8449"

    def _chg(v) -> str:
        if v is None:
            return "—"
        return f"<span style='color:{up if v >= 0 else dn}'>{v:+.2f}%</span>"

    strat_rows = "".join(
        f"<tr><td style='padding:5px 10px'>{r['name']}</td>"
        f"<td style='padding:5px 10px'>{r['n']}</td>"
        f"<td style='padding:5px 10px'>{_chg(r['exc_5'])}</td>"
        f"<td style='padding:5px 10px'>{_chg(r['exc_10'])}</td></tr>"
        for r in s["strategies"])
    sugg = "".join(f"<li style='margin:4px 0'>{x}</li>" for x in s["suggestions"])
    pf = s.get("portfolio") or {}
    return f"""<html><body style="font-family:-apple-system,'PingFang SC',Helvetica;background:#fafafa;margin:0">
<div style="max-width:820px;margin:0 auto;padding:24px">
  <div style="background:#1a1a2e;color:#fff;border-radius:12px 12px 0 0;padding:20px 28px">
    <div style="font-size:22px;font-weight:700">每周自动复盘</div>
    <div style="opacity:.75;font-size:13px;margin-top:4px">{s['date']} · 自动研究闭环生成 · 只读报告，未做任何策略变更</div>
  </div>
  <div style="background:#fff;border:1px solid #eee;border-top:none;padding:8px 28px 24px;border-radius:0 0 12px 12px">
    <h2 style="font-size:16px;border-left:4px solid #2456c8;padding-left:10px">本周概览</h2>
    <p style="font-size:13.5px;line-height:1.8">
      最强信号：<b>{s.get('best') or '—'}</b>；最弱信号：<b>{s.get('worst') or '—'}</b>。<br>
      镜像组合：收益率 {_chg(pf.get('return_pct'))} · 最大回撤 {pf.get('max_drawdown_pct', '—')}% ·
      持仓 {pf.get('n_holdings', '—')} 只。<br>
      因子生产：启用 {s['factors']['enabled']} / 登记 {s['factors']['total']} ·
      本周新挖 {s['factors']['new_this_week']} 个。
    </p>
    <h2 style="font-size:16px;border-left:4px solid #8e44ad;padding-left:10px;margin-top:22px">信号策略归因（T+N 超额 vs 全市场等权）</h2>
    <table style="border-collapse:collapse;width:100%;font-size:13px">
      <tr style="background:#f2f2f2"><th align="left" style="padding:5px 10px">策略</th>
      <th align="left" style="padding:5px 10px">样本</th>
      <th align="left" style="padding:5px 10px">T+5 超额</th>
      <th align="left" style="padding:5px 10px">T+10 超额</th></tr>
      {strat_rows or "<tr><td colspan='4' style='padding:6px 10px;color:#999'>样本不足</td></tr>"}
    </table>
    <h2 style="font-size:16px;border-left:4px solid #c8860d;padding-left:10px;margin-top:22px">复盘建议（需人工确认）</h2>
    <ul style="padding-left:18px;font-size:13.5px">{sugg}</ul>
    <p style="color:#999;font-size:11px;margin-top:24px;border-top:1px solid #eee;padding-top:12px">
      本报告由自动研究闭环生成。因子启停已自动化（可逆）；策略降级与参数调整需人工在平台操作。
      研究结论仅供参考，不构成投资建议。</p>
  </div>
</div></body></html>"""


def run_review(trigger: str = "manual") -> int:
    """启动一次自动复盘（后台线程）。返回 run_id。"""
    from app.database import SessionLocal

    db0 = SessionLocal()
    try:
        run = _new_run(db0, "review", trigger)
    finally:
        db0.close()

    def _bg() -> None:
        if not _RUN_LOCK.acquire(blocking=False):
            db = SessionLocal()
            try:
                _finish_run(db, run.id, False, {}, error="已有自动研究任务在运行")
            finally:
                db.close()
            return
        db = SessionLocal()
        cfg = load_config()
        try:
            summary, path = _review_body(db, run.id, cfg)
            _finish_run(db, run.id, True, summary, report_path=path)
            if cfg["email_review"]:
                from app.services import mailer
                html = Path(path).read_text(encoding="utf-8")
                mailer.send_email_html(db, f"每周自动复盘 · {summary['date']}", html,
                                       dedup_key=f"review|{summary['date']}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("自动复盘失败")
            _finish_run(db, run.id, False, {}, error=str(exc))
        finally:
            db.close()
            _RUN_LOCK.release()

    threading.Thread(target=_bg, daemon=True, name="auto-review").start()
    return run.id


# ──────────────────────────── 调度器 ────────────────────────────

def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def scheduler_state() -> dict:
    cfg = load_config()
    return {
        "config": cfg,
        "mining_next": f"交易日 {cfg['mining_time']}",
        "review_next": f"周{'一二三四五六日'[cfg['review_weekday']]} {cfg['review_time']}",
        "last_error": _STATE["last_error"],
    }


def _already_ran_today(job: str) -> bool:
    """DB 级幂等：今天是否已有该任务的运行记录（防止进程重启后重复补跑）。

    进程级 _STATE 在重启后丢失，而本服务由 launchd KeepAlive 托管，
    崩溃/重启频繁——补跑判据必须以 DB 运行记录为准。
    """
    from app.database import SessionLocal
    from app.models import AutoResearchRun

    db = SessionLocal()
    try:
        today = date.today()
        row = db.execute(
            select(AutoResearchRun.id).where(
                AutoResearchRun.job == job,
                AutoResearchRun.started_at >= datetime.combine(today, datetime.min.time()),
            ).limit(1)).first()
        return row is not None
    except Exception:  # noqa: BLE001
        return True  # 查询失败时宁可不跑（保守），下分钟再试
    finally:
        db.close()


def start_auto_scheduler() -> None:
    """自动研究调度线程（60s 一跳，幂等 + 当日/当周补跑）。"""
    def _loop() -> None:
        while True:
            try:
                cfg = load_config()
                now = datetime.now()
                today = now.date().isoformat()

                # 每日挖矿：交易日，过了设定时点且今天没跑过（DB 级判重）
                if cfg["mining_enabled"] and _STATE["mining_last"] != today:
                    hh, mm = _hm(cfg["mining_time"])
                    if (now.hour, now.minute) >= (hh, mm):
                        from app.core.trading_calendar import is_trading_day

                        if is_trading_day(now.date()):
                            if _already_ran_today("mining"):
                                _STATE["mining_last"] = today  # 今天已跑过（含上次进程）
                            else:
                                _STATE["mining_last"] = today
                                run_mining(trigger="scheduler")
                                logger.info("自动挖矿已触发（%s）", today)

                # 每周复盘：设定 weekday，过了时点且【今天】没跑过（DB 级判重）
                # 2026-10-03 修复：原先按"本周已跑"判重，周三一次手动复盘导致周六
                # 正式复盘被跳过。周级任务的正确判重粒度是"当天"——既防多进程/重启
                # 重复触发，又不会误杀周末的正式运行。
                week_key = f"{now.isocalendar().year}-W{now.isocalendar().week}"
                if (cfg["review_enabled"] and _STATE["review_last"] != week_key
                        and now.weekday() == cfg["review_weekday"]):
                    hh, mm = _hm(cfg["review_time"])
                    if (now.hour, now.minute) >= (hh, mm):
                        if _already_ran_today("review"):
                            _STATE["review_last"] = week_key
                        else:
                            _STATE["review_last"] = week_key
                            run_review(trigger="scheduler")
                            logger.info("自动复盘已触发（%s）", week_key)
            except Exception as exc:  # noqa: BLE001
                _STATE["last_error"] = str(exc)[:300]
            time.sleep(60)

    threading.Thread(target=_loop, daemon=True, name="auto-research").start()
