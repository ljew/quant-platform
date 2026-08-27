"""数据健康度引擎：采集/处理/应用三层检查 + 评分 + 告警生成。

每层若干检查项（check），通过比例 ×100 = 层得分；
全平台总分 = 三层平均。
生成 alerts 列表供监控页与后续推送使用。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select, func

from app.database import SessionLocal
from app.models import (
    FactorDaily,
    IndexKlineDaily,
    IndexMembership,
    KlineDaily,
    NewsMarketDaily,
    PipelineRun,
)

_WEIGHTS = {"collect": 0.35, "process": 0.35, "apply": 0.30}


def _mk(name: str, ok: bool | None, value: str, expect: str) -> dict:
    return {"name": name,
            "status": "ok" if ok else ("warn" if ok is None else "error"),
            "value": value, "expect": expect}


def _latest_date(db, col_model_col) -> tuple[str | None, int]:
    d = db.execute(select(func.max(col_model_col))).scalar()
    if not d:
        return None, -1
    return d.isoformat(), (date.today() - d).days


def health_report(db=None) -> dict:
    own = db is None
    from app.database import SessionLocal as _SL

    close_after = own
    db = db or _SL()
    try:
        # ========== 采集层 ==========
        collect_checks: list[dict] = []
        try:
            from app.services import data_source
            ts_ok = bool(data_source.check_tushare())
            ak_ok = bool(data_source.check_akshare())
        except Exception:  # noqa: BLE001
            ts_ok = ak_ok = False
        collect_checks.append(_mk("tushare 连通", ts_ok, "在线" if ts_ok else "离线", "在线"))
        collect_checks.append(_mk("akshare 连通", ak_ok, "在线" if ak_ok else "离线", "在线"))

        kl_latest, kl_days = _latest_date(db, KlineDaily.trade_date)
        collect_checks.append(_mk("个股K线新鲜度",
                                  kl_days is not None and 0 <= kl_days <= 5,
                                  f"{kl_latest} ({kl_days}天前)", "≤5 天"))
        idx_latest, idx_days = _latest_date(db, IndexKlineDaily.trade_date)
        collect_checks.append(_mk("指数K线新鲜度",
                                  idx_days is not None and 0 <= idx_days <= 5,
                                  f"{idx_latest} ({idx_days}天前)", "≤5 天"))

        # 管道最近一次运行
        try:
            run = db.execute(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(1)).scalar()
        except Exception:  # noqa: BLE001
            run = None
        if run:
            ago = ""
            if run.started_at:
                mins = int((datetime.utcnow() - run.started_at).total_seconds() / 60)
                ago = f"({mins} 分钟前)"
            collect_checks.append(_mk(
                "数据管道最近运行",
                run.status in ("SUCCESS", "RUNNING"),
                f"#{run.id} {run.status} {ago}", "SUCCESS/RUNNING"))
        else:
            collect_checks.append(_mk("数据管道最近运行", None, "从未运行", "有记录"))

        bronze_dir = "/Users/happyljew/Desktop/kimiwork/Quant/quant-platform/data/raw"
        n_bronze = 0
        for root, _d, files in __import__("os").walk(bronze_dir):
            n_bronze += len([f for f in files if f.endswith(".parquet")])
        collect_checks.append(_mk("Bronze 分区文件", n_bronze > 0, f"{n_bronze} 个 parquet", ">0"))

        # ========== 处理层 ==========
        process_checks: list[dict] = []
        fa_latest, fa_days = _latest_date(db, FactorDaily.trade_date)
        process_checks.append(_mk("因子截面新鲜度",
                                  fa_days is not None and 0 <= fa_days <= 6,
                                  f"{fa_latest} ({fa_days}天前)", "≤6 天"))
        fa_rows = db.execute(select(func.count()).select_from(FactorDaily)).scalar() or 0
        process_checks.append(_mk("因子覆盖率", fa_rows >= 1000, f"{fa_rows:,} 行", "≥1,000"))

        mem_latest, mem_days = _latest_date(db, IndexMembership.trade_date)
        this_month = date.today().replace(day=1).isoformat()
        process_checks.append(_mk("成分快照月更",
                                  mem_latest is not None and mem_latest >= this_month,
                                  str(mem_latest), f"≥{this_month}"))

        # 新闻情绪近 14 天覆盖
        from_date = date.today() - timedelta(days=14)
        news_days = db.execute(
            select(func.count()).select_from(NewsMarketDaily)
            .where(NewsMarketDaily.date >= from_date, NewsMarketDaily.n_finance > 0)
        ).scalar() or 0
        process_checks.append(_mk("新闻情绪连续性", news_days >= 6,
                                  f"14 天内 {news_days} 天", "≥6 天"))

        # 最近管道失败率
        runs = db.execute(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(10)).scalars().all()
        fails = sum(1 for r in runs if r.status == "FAILED")
        process_checks.append(_mk("管道近期失败率", fails == 0,
                                  f"最近{len(runs)}次 {fails} 次失败", "0 次"))

        # ========== 应用层 ==========
        apply_checks: list[dict] = []
        mismatch = []
        table_cls = {"kline_daily": KlineDaily, "factor_daily": FactorDaily}
        for t, cls in table_cls.items():
            a = db.execute(select(func.count()).select_from(cls)).scalar() or 0
            try:
                from app.services import duckdb_store
                b = duckdb_store.count(t)
            except Exception:  # noqa: BLE001
                b = -1
            if a != b:
                mismatch.append(t)
        apply_checks.append(_mk("DuckDB 同步一致性",
                                not mismatch or all(m not in mismatch for m in ["kline_daily", "factor_daily"]),
                                "一致" if not mismatch else f"差异:{','.join(mismatch)}", "一致"))

        paper_alive = False
        try:
            from app.core.engine import paper_scheduler
            paper_alive = bool(paper_scheduler._thread and paper_scheduler._thread.is_alive())
        except Exception:  # noqa: BLE001
            pass
        apply_checks.append(_mk("模拟盘调度器", paper_alive, "存活" if paper_alive else "未运行", "存活"))

        etl_enabled = False
        try:
            from app.core.data_scheduler import get_status
            st = get_status()
            etl_enabled = bool(st["enabled"])
        except Exception:  # noqa: BLE001
            pass
        apply_checks.append(_mk("ETL 定时调度", etl_enabled,
                                "已启用(17:00)" if etl_enabled else "未启用", "建议启用"))

        # ========== 打分 ==========
        def score_of(checks) -> int:
            known = [c for c in checks if c["status"] != "warn"]
            if not known:
                return 50
            good = sum(1 for c in checks if c["status"] == "ok")
            return round(good / len(checks) * 100)

        layers = {
            "collect": {"label": "数据采集", "checks": collect_checks, "score": score_of(collect_checks)},
            "process": {"label": "数据处理", "checks": process_checks, "score": score_of(process_checks)},
            "apply": {"label": "数据应用", "checks": apply_checks, "score": score_of(apply_checks)},
        }
        for ly in layers.values():
            ly["status"] = ("healthy" if ly["score"] >= 85 else
                            "warn" if ly["score"] >= 60 else "error")
        overall = round(sum(l["score"] * w for l, w in zip(layers.values(), _WEIGHTS.values())))

        # 告警列表
        alerts = []
        for lname, ly in layers.items():
            for c in ly["checks"]:
                if c["status"] != "ok":
                    level = "warn" if ly["score"] >= 60 else "error"
                    alerts.append({"level": level, "layer": ly["label"],
                                   "check": c["name"], "detail": c["value"],
                                   "expect": c["expect"]})
        alerts.sort(key=lambda a: a["level"])

        return {
            "overall_score": overall,
            "overall_status": "healthy" if overall >= 85 else "warn" if overall >= 60 else "error",
            "layers": layers,
            "alerts": alerts,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
    finally:
        if close_after:
            db.close()
