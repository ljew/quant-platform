"""规则化数据健康度引擎。

检查项 = health_rules 表中的规则（可配置/启停/改阈值），每个规则指定
metric 检查器 + 比较符 + 阈值。系统级检查（连通性/调度器等不可参数化的）
保留为内置检查，与规则结果合并输出。

层分 = Σ(weight×通过) / Σ(weight) × 100。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

from sqlalchemy import select, func

from app.database import SessionLocal, init_db
from app.datahub.registry import RAW_DIR
from app.models import (
    FactorDaily,
    HealthRule,
    IndexKlineDaily,
    IndexMembership,
    KlineDaily,
    NewsMarketDaily,
    PipelineRun,
)

# ============ 指标检查器：metric -> fn(db, params) -> (num_value, display) ============

def _latest_date_days(db, table: str, column: str = "trade_date") -> tuple[float, str]:
    model = {"kline_daily": KlineDaily, "index_kline_daily": IndexKlineDaily,
             "factor_daily": FactorDaily, "news_market_daily": NewsMarketDaily,
             "index_membership": IndexMembership}[table]
    d = db.execute(select(getattr(model, column)).order_by(getattr(model, column).desc()).limit(1)).scalar()
    if not d:
        return -1, "无数据"
    days = (date.today() - d).days
    return float(days), f"{d} ({days} 天前)"


def _table_rows(db, table: str) -> tuple[float, str]:
    model = {"kline_daily": KlineDaily, "factor_daily": FactorDaily,
             "news_market_daily": NewsMarketDaily,
             "news_stock_daily": __import__("app.models", fromlist=["NewsStockDaily"]).NewsStockDaily}[table]
    n = db.execute(select(func.count()).select_from(model)).scalar() or 0
    return float(n), f"{n:,} 行"


def _news_coverage(db, params) -> tuple[float, str]:
    days = int(params.get("window_days", 14))
    min_fin = int(params.get("min_articles", 1))
    from_date = date.today() - timedelta(days=days)
    n = db.execute(
        select(func.count()).select_from(NewsMarketDaily)
        .where(NewsMarketDaily.date >= from_date, NewsMarketDaily.n_finance >= min_fin)
    ).scalar() or 0
    return float(n), f"{days} 天内 {n} 天有数据"


def _pipeline_fail_count(db, params) -> tuple[float, str]:
    n_runs = int(params.get("recent_runs", 10))
    runs = db.execute(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(n_runs)).scalars().all()
    fails = sum(1 for r in runs if r.status == "FAILED")
    return float(fails), f"最近 {len(runs)} 次 {fails} 次失败"


def _pipeline_last_status(db, params) -> tuple[float, str]:
    r = db.execute(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(1)).scalar()
    if not r:
        return 0.0, "从未运行"
    mins = int((datetime.utcnow() - r.started_at).total_seconds() / 60) if r.started_at else 9999
    ok = 1.0 if (r.status in ("SUCCESS", "RUNNING") and mins <= 2880) else 0.0
    return ok, f"#{r.id} {r.status} ({mins} 分钟前)"


def _bronze_files(db, params) -> tuple[float, str]:
    dataset = params.get("dataset", "")
    path = os.path.join(RAW_DIR, dataset) if dataset else RAW_DIR
    n = 0
    for root, _d, fs in os.walk(path):
        n += len([f for f in fs if f.endswith(".parquet")])
    return float(n), f"{n} 个 parquet"


def _quality_report_age(db, params) -> tuple[float, str]:
    import os

    from app.datahub.cleaners.core import read_latest_report
    rep = read_latest_report()
    gen = (rep or {}).get("generated_at", "")
    if not gen:
        return -1.0, "无质检报告"
    days = (datetime.now() - datetime.fromisoformat(gen)).days
    return float(days), f"{gen} ({days} 天前)"


def _component_snapshot(db, params) -> tuple[float, str]:
    d = db.execute(select(IndexMembership.trade_date)
                   .order_by(IndexMembership.trade_date.desc()).limit(1)).scalar()
    if not d:
        return -1.0, "无数据"
    this_month = date.today().replace(day=1).isoformat()
    ok = 1.0 if d.isoformat() >= this_month else 0.0
    return ok, f"{d}（当月 {'已更新' if ok else '未更新'}）"


METRICS = {
    "freshness": lambda db, p: _latest_date_days(db, p.get("table"), p.get("column", "trade_date")),
    "table_rows": lambda db, p: _table_rows(db, p.get("table")),
    "news_coverage": _news_coverage,
    "pipeline_fail_count": _pipeline_fail_count,
    "pipeline_last_status": _pipeline_last_status,
    "bronze_files": _bronze_files,
    "quality_report_age": _quality_report_age,
    "component_snapshot": _component_snapshot,
}

METRIC_DOCS = {
    "freshness": "数据新鲜度：某表最新日期距今天数（params: table, max_days）",
    "table_rows": "表行数下限（params: table）",
    "news_coverage": "新闻情绪覆盖：近 N 天有数据天数（params: window_days, min_articles）",
    "pipeline_fail_count": "管道失败次数上限（params: recent_runs）",
    "pipeline_last_status": "管道最近一次运行须成功",
    "bronze_files": "Bronze 分区 Parquet 文件数（params: dataset）",
    "quality_report_age": "质检报告距今天数",
    "component_snapshot": "成分快照当月更新",
}


def _compare(value: float, comp: str, threshold: float | None) -> bool:
    if threshold is None:
        return bool(value)
    return {">=": value >= threshold, "<=": value <= threshold,
            "==": value == threshold, "!=": value != threshold,
            ">": value > threshold, "<": value < threshold}.get(comp, bool(value))


DEFAULT_RULES = [
    # layer, name, metric, params, comp, threshold, level
    ("collect", "个股K线新鲜度", "freshness", {"table": "kline_daily"}, "<=", 5, "error"),
    ("collect", "指数K线新鲜度", "freshness", {"table": "index_kline_daily"}, "<=", 5, "error"),
    ("collect", "因子截面新鲜度", "freshness", {"table": "factor_daily"}, "<=", 6, "error"),
    ("collect", "K线数据量", "table_rows", {"table": "kline_daily"}, ">=", 1000000, "warn"),
    ("collect", "Bronze 分区文件", "bronze_files", {}, ">=", 1, "warn"),
    ("collect", "数据管道最近运行", "pipeline_last_status", {}, "==", 1, "error"),
    ("process", "因子覆盖率", "table_rows", {"table": "factor_daily"}, ">=", 1000, "error"),
    ("process", "成分快照月更", "component_snapshot", {}, "==", 1, "warn"),
    ("process", "新闻情绪覆盖", "news_coverage", {"window_days": 14, "min_articles": 1}, ">=", 6, "warn"),
    ("process", "管道近期失败数", "pipeline_fail_count", {"recent_runs": 10}, "==", 0, "warn"),
    ("process", "Silver 质检报告", "quality_report_age", {}, "<=", 7, "warn"),
]


def ensure_default_rules() -> None:
    init_db()
    db = SessionLocal()
    try:
        n = db.execute(select(func.count()).select_from(HealthRule)).scalar() or 0
        if n > 0:
            return
        for layer, name, metric, params, comp, th, level in DEFAULT_RULES:
            db.add(HealthRule(name=name, layer=layer, metric=metric,
                              params=json.dumps(params, ensure_ascii=False),
                              comparator=comp, threshold=th, level=level,
                              weight=1.0, enabled=1))
        db.commit()
    finally:
        db.close()


def run_rules(db=None) -> dict:
    """执行全部启用规则，返回与旧 health_report 兼容的结构。"""
    ensure_default_rules()
    own = db is None
    db = db or SessionLocal()
    try:
        rules = db.execute(select(HealthRule).where(HealthRule.enabled == 1)
                           .order_by(HealthRule.layer, HealthRule.id)).scalars().all()
        layers: dict[str, dict] = {
            "collect": {"label": "数据采集", "checks": [], "_w": 0.0, "_s": 0.0},
            "process": {"label": "数据处理", "checks": [], "_w": 0.0, "_s": 0.0},
            "apply": {"label": "数据应用", "checks": [], "_w": 0.0, "_s": 0.0},
        }
        alerts = []
        now = datetime.utcnow()
        for r in rules:
            fn = METRICS.get(r.metric)
            check = {"name": r.name, "metric": r.metric, "rule_id": r.id,
                     "params": r.params, "comparator": r.comparator,
                     "threshold": r.threshold, "level": r.level,
                     "status": "ok", "value": "-", "expect": f"{r.comparator} {r.threshold}"}
            if not fn:
                check.update(status="warn", value="未知检查器")
            else:
                try:
                    p = json.loads(r.params or "{}")
                    value, display = fn(db, p)
                    check["value"] = display
                    passed = _compare(value, r.comparator, r.threshold)
                    check["status"] = "ok" if passed else r.level
                except Exception as e:  # noqa: BLE001
                    check.update(status="warn", value=f"执行异常: {str(e)[:80]}")
            r.last_value = check["value"]
            r.last_status = check["status"]
            r.last_checked = now
            layers[r.layer]["checks"].append(check)
            w = max(r.weight, 0.1)
            layers[r.layer]["_w"] += w
            if check["status"] == "ok":
                layers[r.layer]["_s"] += w
            else:
                alerts.append({"level": check["status"], "layer": layers[r.layer]["label"],
                               "check": r.name, "detail": check["value"],
                               "expect": check["expect"], "rule_id": r.id})
        db.commit()

        # —— apply 层内置系统检查（不可参数化）——
        sys_checks = []
        try:
            from app.services import duckdb_store
            a = db.execute(select(func.count()).select_from(KlineDaily)).scalar() or 0
            b = duckdb_store.count("kline_daily")
            sys_checks.append({"name": "DuckDB 同步一致性",
                               "status": "ok" if a == b else "error",
                               "value": "一致" if a == b else f"差异 sqlite={a} duckdb={b}",
                               "expect": "一致", "metric": "builtin", "level": "error"})
        except Exception as e:  # noqa: BLE001
            sys_checks.append({"name": "DuckDB 同步一致性", "status": "warn",
                               "value": f"检查异常: {str(e)[:60]}", "expect": "一致",
                               "metric": "builtin", "level": "warn"})
        try:
            from app.core.engine import paper_scheduler
            alive = bool(paper_scheduler._thread and paper_scheduler._thread.is_alive())
        except Exception:  # noqa: BLE001
            alive = False
        sys_checks.append({"name": "模拟盘调度器", "status": "ok" if alive else "warn",
                           "value": "存活" if alive else "未运行", "expect": "存活",
                           "metric": "builtin", "level": "warn"})
        layers["apply"]["checks"].extend(sys_checks)
        layers["apply"]["_w"] += len(sys_checks)
        layers["apply"]["_s"] += sum(1 for c in sys_checks if c["status"] == "ok")

        result_layers = {}
        for k, ly in layers.items():
            score = round(ly["_s"] / ly["_w"] * 100) if ly["_w"] else 50
            result_layers[k] = {"label": ly["label"], "checks": ly["checks"],
                                "score": score,
                                "status": "healthy" if score >= 85 else "warn" if score >= 60 else "error"}
        weights = {"collect": 0.35, "process": 0.35, "apply": 0.30}
        overall = round(sum(result_layers[k]["score"] * w for k, w in weights.items()))
        return {
            "overall_score": overall,
            "overall_status": "healthy" if overall >= 85 else "warn" if overall >= 60 else "error",
            "layers": result_layers,
            "alerts": alerts,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "rule_based": True,
        }
    finally:
        if own:
            db.close()
