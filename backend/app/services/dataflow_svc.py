"""数据流全景聚合服务（源头配置 / Bronze / Silver / Gold）。"""
from __future__ import annotations

import json
import os
from datetime import datetime

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.datahub.source_config import load_sources
from app.datahub.registry import RAW_DIR, SILVER_DIR
from app.models import (
    FactorDaily,
    FactorMinedDaily,
    FactorRegistry,
    IndexKlineDaily,
    KlineDaily,
    NewsMarketDaily,
    NewsStockDaily,
)

_TEXT_DIR = os.path.join(RAW_DIR, "text", "wechat_articles")
_MARKET_DIR = os.path.join(RAW_DIR, "market", "bars_daily")
_REPORT_PATH = os.path.join(SILVER_DIR, "_reports", "latest.json")


def _fmt_ts(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


def _dir_stat(path: str) -> dict:
    n_files, n_bytes, latest = 0, 0, 0.0
    if os.path.isdir(path):
        for f in os.listdir(path):
            fp = os.path.join(path, f)
            if f.endswith(".parquet") and os.path.isfile(fp):
                n_files += 1
                n_bytes += os.path.getsize(fp)
                latest = max(latest, os.path.getmtime(fp))
    return {"files": n_files, "size_mb": round(n_bytes / 1048576, 1),
            "latest": _fmt_ts(latest or None)}


def dataflow_report(db: Session) -> dict:
    # —— ① 数据源配置层 ——
    sources = []
    for name, cfg in load_sources().items():
        sources.append({
            "name": name,
            "type": cfg.get("type"),
            "enabled": bool(cfg.get("enabled", True)),
            "description": cfg.get("description", ""),
            "params_brief": ", ".join(
                f"{k}={str(v)[:44]}" for k, v in (cfg.get("params") or {}).items()) or "-",
        })

    # —— ② Bronze 层 ——
    bronze = {
        "market_bars": _dir_stat(_MARKET_DIR),
        "text_articles": _dir_stat(_TEXT_DIR),
    }

    # —— ③ Silver 层 ——
    silver_files: dict[str, dict] = {}
    for root, _dirs, files in os.walk(SILVER_DIR):
        if "_reports" in root:
            continue
        for f in sorted(files):
            fp = os.path.join(root, f)
            if f.endswith(".parquet"):
                rel = os.path.relpath(fp, SILVER_DIR)
                silver_files[rel] = {
                    "size_kb": round(os.path.getsize(fp) / 1024, 1),
                    "mtime": _fmt_ts(os.path.getmtime(fp)),
                }
    quality = None
    if os.path.exists(_REPORT_PATH):
        try:
            with open(_REPORT_PATH, encoding="utf-8") as fh:
                quality = json.load(fh).get("sections")
        except Exception:  # noqa: BLE001
            pass

    # —— ④ Gold 层 ——
    gold: dict = {}
    for key, model in [
        ("kline_daily", KlineDaily), ("index_kline_daily", IndexKlineDaily),
        ("factor_daily", FactorDaily), ("news_market_daily", NewsMarketDaily),
        ("news_stock_daily", NewsStockDaily), ("factor_mined_daily", FactorMinedDaily),
    ]:
        try:
            gold[key] = int(db.execute(select(func.count()).select_from(model)).scalar() or 0)
        except Exception:  # noqa: BLE001
            gold[key] = -1

    def latest_of(col):
        d = db.execute(select(col).order_by(col.desc()).limit(1)).scalar()
        return str(d) if d else None

    gold.update({
        "kline_latest": latest_of(KlineDaily.trade_date),
        "factor_latest": latest_of(FactorDaily.trade_date),
        "news_latest": latest_of(NewsMarketDaily.date),
        "registry_enabled": int(db.execute(
            select(func.count()).select_from(FactorRegistry)
            .where(FactorRegistry.status == "enabled")).scalar() or 0),
        "mined_rows": int(db.execute(select(func.count())
                                     .select_from(FactorMinedDaily)).scalar() or 0),
    })

    scorers = [
        {"version": "dict_v1", "desc": "多空词典计分（24 多词 / 26 空词）", "active": True},
        {"version": "llm_v2", "desc": "DeepSeek LLM 打分（需配置 QUANT_LLM_API_KEY）",
         "active": bool(os.getenv("QUANT_LLM_API_KEY"))},
    ]

    return {"sources": sources, "bronze": bronze, "silver": {"files": silver_files,
            "quality": quality}, "gold": gold, "scorers": scorers}
