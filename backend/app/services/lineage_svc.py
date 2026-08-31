"""数据血缘聚合：源 → 处理步骤 → 层 → 运行记录 的四维视图。"""
from __future__ import annotations

import os

from sqlalchemy import select, func

from app.datahub.registry import RAW_DIR, SILVER_DIR
from app.datahub.source_config import load_sources, project_root
from app.models import (
    FactorDaily,
    FactorMinedDaily,
    FactorRegistry,
    IndexKlineDaily,
    KlineDaily,
    NewsMarketDaily,
    NewsStockDaily,
    PipelineRun,
    PipelineStepLog,
)

REPORT_PATH = os.path.join(SILVER_DIR, "_reports", "latest.json")


def _fmt_ts(ts: float | None):
    from datetime import datetime

    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


def _dir_stat(path: str) -> dict:
    files, size, latest = 0, 0, 0.0
    for root, _d, fs in os.walk(path):
        for f in fs:
            fp = os.path.join(root, f)
            if f.endswith(".parquet"):
                files += 1
                size += os.path.getsize(fp)
                latest = max(latest, os.path.getmtime(fp))
    return {"files": files, "size_mb": round(size / 1048576, 1), "latest": _fmt_ts(latest or None)}


def lineage_report(db) -> dict:
    sources_cfg = load_sources()

    # 最近一次运行中每个步骤的表现（源 ⇄ 步骤 关联依据）
    last_run = db.execute(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(1)).scalar()
    step_stats: dict[str, dict] = {}
    if last_run:
        rows = db.execute(
            select(PipelineStepLog).where(PipelineStepLog.run_id == last_run.id)
        ).scalars().all()
        for st in rows:
            step_stats[st.name] = {
                "status": st.status,
                "rows": st.rows,
                "duration_sec": st.duration_sec,
                "message": (st.message or "")[:160],
            }

    # —— 数据源层（含对应步骤最近表现）——
    sources = []
    for name, cfg in sources_cfg.items():
        step = cfg.get("step") or ""
        st = step_stats.get(step)
        sources.append({
            "name": name,
            "type": cfg.get("type"),
            "description": cfg.get("description", ""),
            "enabled": bool(cfg.get("enabled", True)),
            "params": cfg.get("params") or {},
            "step": step,
            "layer": cfg.get("layer", "bronze"),
            "produces": cfg.get("produces", ""),
            "last_run": ({
                "status": st["status"], "rows": st["rows"],
                "duration_sec": st["duration_sec"], "message": st["message"],
            } if st else None),
        })

    # —— 处理步骤（管道定义顺序即处理流程）——
    from app.datahub.runner import STEPS

    steps = []
    for i, (name, _fn) in enumerate(STEPS):
        st = step_stats.get(name)
        steps.append({
            "order": i + 1,
            "name": name,
            "status": st["status"] if st else "未执行",
            "rows": st["rows"] if st else 0,
            "duration_sec": st["duration_sec"] if st else 0,
        })

    # —— 层（Bronze/Silver/Gold）——
    bronze = {
        "market_bars": _dir_stat(os.path.join(RAW_DIR, "market", "bars_daily")),
        "text_articles": _dir_stat(os.path.join(RAW_DIR, "text", "wechat_articles")),
    }
    silver_files = {}
    for root, _d, fs in os.walk(SILVER_DIR):
        if "_reports" in root:
            continue
        for f in fs:
            if f.endswith(".parquet"):
                fp = os.path.join(root, f)
                silver_files[os.path.relpath(fp, SILVER_DIR)] = {
                    "size_kb": round(os.path.getsize(fp) / 1024, 1),
                    "mtime": _fmt_ts(os.path.getmtime(fp)),
                }
    quality = None
    if os.path.exists(REPORT_PATH):
        try:
            import json

            with open(REPORT_PATH, encoding="utf-8") as fh:
                quality = json.load(fh).get("sections")
        except Exception:  # noqa: BLE001
            pass

    def cnt(model):
        try:
            return int(db.execute(select(func.count()).select_from(model)).scalar() or 0)
        except Exception:  # noqa: BLE001
            return -1

    def latest_of(col):
        d = db.execute(select(col).order_by(col.desc()).limit(1)).scalar()
        return str(d) if d else None

    gold = {
        "tables": {
            "kline_daily": cnt(KlineDaily),
            "index_kline_daily": cnt(IndexKlineDaily),
            "factor_daily": cnt(FactorDaily),
            "news_market_daily": cnt(NewsMarketDaily),
            "news_stock_daily": cnt(NewsStockDaily),
            "factor_mined_daily": cnt(FactorMinedDaily),
        },
        "latest": {
            "kline": latest_of(KlineDaily.trade_date),
            "factor": latest_of(FactorDaily.trade_date),
            "news": latest_of(NewsMarketDaily.date),
        },
        "registry_enabled": int(db.execute(select(func.count())
                                          .select_from(FactorRegistry)
                                          .where(FactorRegistry.status == "enabled")).scalar() or 0),
    }

    # —— 运行时间线（最近 6 次，含步骤耗时）——
    runs = db.execute(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(6)).scalars().all()
    timeline = []
    for r in runs:
        sts = db.execute(select(PipelineStepLog).where(PipelineStepLog.run_id == r.id)
                         .order_by(PipelineStepLog.id)).scalars().all()
        timeline.append({
            "run_id": r.id,
            "trigger": r.trigger,
            "status": r.status,
            "started_at": r.started_at.isoformat(timespec="seconds") if r.started_at else None,
            "finished_at": r.finished_at.isoformat(timespec="seconds") if r.finished_at else None,
            "total_sec": round(sum(s.duration_sec for s in sts), 1),
            "error": (r.error or "")[:200] or None,
            "steps": [{"name": s.name, "status": s.status,
                       "duration_sec": s.duration_sec, "rows": s.rows,
                       "message": (s.message or "")[:120]} for s in sts],
        })

    return {
        "sources": sources,
        "steps": steps,
        "layers": {
            "bronze": bronze,
            "silver": {"files": silver_files, "quality": quality},
            "gold": gold,
        },
        "timeline": timeline,
        "last_run": ({
            "run_id": last_run.id, "status": last_run.status,
            "started_at": last_run.started_at.isoformat(timespec="seconds") if last_run.started_at else None,
        } if last_run else None),
        "raw_dir": RAW_DIR,
        "config_path": os.getenv("QUANT_SOURCES_YAML") or os.path.join(project_root(), "config", "sources.yaml"),
    }
