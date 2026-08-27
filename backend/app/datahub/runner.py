"""每日数据管道：配置化源抽取 → Bronze → 清洗/打分 → Gold 因子。

步骤由 STEPS 注册表声明（可 only 指定子集）；每次运行记录到
pipeline_runs / pipeline_run_steps 供监控页展示。
"""
from __future__ import annotations

import os
import time
import traceback
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import select

from app.datahub.registry import write_bronze
from app.datahub.source_config import get_source
from app.database import SessionLocal, init_db
from app.models import (KlineDaily, NewsMarketDaily, NewsStockDaily, PipelineRun,
                        PipelineStepLog, Stock, IndexKlineDaily, FactorRegistry,
                        FactorMinedDaily)

# ============ 步骤实现 ============


def step_extract_index_kline(db) -> int:
    src = get_source("core_index_kline")
    if not src:
        return 0
    from app.services.etl import extract_index_incremental

    return extract_index_incremental(db, date.today())


def _bronze_dir(dataset: str) -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "..", "data", "raw", dataset)


def _latest_close_snapshot(db) -> pd.DataFrame | None:
    last = db.execute(select(KlineDaily.trade_date)
                      .order_by(KlineDaily.trade_date.desc()).limit(1)).scalar()
    if not last:
        return None
    rows = db.execute(
        select(KlineDaily.symbol, KlineDaily.trade_date, KlineDaily.open,
               KlineDaily.high, KlineDaily.low, KlineDaily.close,
               KlineDaily.volume, KlineDaily.amount)
        .where(KlineDaily.trade_date == last)
    ).all()
    if not rows:
        return None
    return pd.DataFrame(rows, columns=["symbol", "trade_date", "open", "high",
                                       "low", "close", "volume", "amount"])


def step_extract_stock_kline(db) -> int:
    src = get_source("stock_kline_core")
    if not src:
        return 0
    from app.services.etl import extract_kline_incremental, _get_universe

    params = src.get("params", {})
    syms = _get_universe(db)
    lookback = int(params.get("lookback_days", 30))
    sd = date.today() - timedelta(days=lookback)
    n = extract_kline_incremental(db, syms, sd, date.today())
    try:  # Bronze 快照落盘（失败不影响主流程）
        df = _latest_close_snapshot(db)
        if df is not None and len(df):
            write_bronze("market/bars_daily", df)
    except Exception:  # noqa: BLE001
        pass
    return n


def step_extract_attributes(db) -> int:
    if not get_source("stock_attributes"):
        return 0
    from app.services.etl import extract_attributes

    return extract_attributes(db)


def _bronze_articles_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "..", "data", "raw", "text", "wechat_articles")


def _last_article_fetched() -> int:
    base = _bronze_articles_dir()
    best = 0
    if os.path.isdir(base):
        for f in sorted(os.listdir(base))[-3:]:
            if f.endswith(".parquet"):
                try:
                    df = pd.read_parquet(os.path.join(base, f), columns=["fetched_at"])
                    if len(df):
                        best = max(best, int(df["fetched_at"].max()))
                except Exception:  # noqa: BLE001
                    pass
    return best


def step_extract_wechat_articles(db) -> int:
    """公众号语料增量：fetched_at 高于上次批次 → bronze parquet。"""
    src = get_source("wechat_articles")
    if not src:
        return 0
    db_path = src.get("params", {}).get("db_path", "")
    body_chars = int(src.get("params", {}).get("body_chars", 4000))
    if not db_path or not os.path.exists(db_path):
        return 0
    import sqlite3

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        since = _last_article_fetched()
        rows = con.execute(
            f"select id, mp_name, title, publish_time, substr(content_text,1,{body_chars}), fetched_at "
            "from articles where fetched_at > ? order by fetched_at", (since,)
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=["article_id", "mp_name", "title", "publish_time",
                                     "content_text", "fetched_at"])
    write_bronze("text/wechat_articles", df,
                 batch=datetime.now().strftime("%Y%m%d%H%M%S"))
    return len(df)


def step_score_sentiment(db) -> int:
    """Silver/Gold：合并最近 bronze 批次文章 → 词典 v1 打分 → news_market_daily 重算。"""
    from app.datahub.scorers.dict_v1 import DictScorerV1

    base = _bronze_articles_dir()
    if not os.path.isdir(base):
        return 0
    frames = []
    files = sorted(f for f in os.listdir(base) if f.endswith(".parquet"))[-5:]
    for f in files:
        try:
            frames.append(pd.read_parquet(os.path.join(base, f)))
        except Exception:  # noqa: BLE001
            pass
    if not frames:
        return 0
    df = pd.concat(frames).drop_duplicates(subset=["article_id"])
    scorer = DictScorerV1()
    texts = list((df["title"].fillna("") + "\n"
                  + df["content_text"].fillna("").str.slice(0, 3000)))
    scored = scorer.score(texts)
    scored["date_str"] = pd.to_datetime(df["publish_time"], unit="s").dt.strftime("%Y-%m-%d").values
    fin_mask = [int(any(k in t[:600] for k in scorer.fin_keywords))
                for t in (df["title"].fillna("") + "\n"
                          + df["content_text"].fillna("").str.slice(0, 600))]
    scored["fin"] = fin_mask
    day = (scored[scored["fin"] > 0]
           .groupby("date_str").agg(n_finance=("fin", "sum"),
                                    bull=("bull", "sum"), bear=("bear", "sum")))
    n = 0
    for d_str, r in day.iterrows():
        d2 = datetime.strptime(d_str, "%Y-%m-%d").date()
        senti = (r.bull - r.bear) / max(r.bull + r.bear, 1)
        row = db.execute(select(NewsMarketDaily).where(NewsMarketDaily.date == d2)).scalar()
        if row is None:
            db.add(NewsMarketDaily(date=d2, n_articles=int(r.n_finance),
                                   n_finance=int(r.n_finance), bull_score=int(r.bull),
                                   bear_score=int(r.bear), net_sentiment=round(senti, 5)))
        else:
            row.n_finance = max(int(row.n_finance or 0), int(r.n_finance))
            row.bull_score = int(r.bull)
            row.bear_score = int(r.bear)
            row.net_sentiment = round(senti, 5)
        n += 1
    db.commit()
    return n


def step_sync_duckdb(db) -> int:
    """分析库同步（Gold 层写入后）。"""
    from app.services.duckdb_sync import sync_after_seed

    sync_after_seed()
    return 0


def step_compute_factors(db) -> int:
    """Gold：核心池最新截面因子计算（复用 ETL）。"""
    from app.services.etl import compute_factor_cross_section, _get_universe

    syms = _get_universe(db)
    last = db.execute(select(KlineDaily.trade_date)
                      .order_by(KlineDaily.trade_date.desc()).limit(1)).scalar()
    return compute_factor_cross_section(db, syms, last or date.today())


# ============ 步骤注册与执行器 ============
def step_clean_bars(db) -> int:
    """Silver：K线清洗 + 质检报告。"""
    from app.datahub.cleaners.core import clean_bars, write_report

    df = _latest_close_snapshot(db)
    if df is None:
        return 0
    cleaned, metrics = clean_bars(df)
    report = {"bars": {k: v for k, v in metrics.items() if k != "silver_file"}}
    write_report(None, report)
    return len(cleaned)


def step_clean_text(db) -> int:
    """Silver：文章去重清洗 → text_cleaned。"""
    import os as _os
    from app.datahub.cleaners.core import clean_articles, latest_run_dir, write_report

    base = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__)))), "..", "data", "raw", "text", "wechat_articles")
    frames = []
    if _os.path.isdir(base):
        for f in sorted(_os.listdir(base)):
            if f.endswith(".parquet"):
                try:
                    frames.append(pd.read_parquet(_os.path.join(base, f)))
                except Exception:  # noqa: BLE001
                    pass
    _, metrics = clean_articles(frames)
    rep = read_report = None
    del rep
    prev = None
    try:
        from app.datahub.cleaners.core import read_latest_report
        prev = read_latest_report()
    except Exception:  # noqa: BLE001
        pass
    sections = (prev or {}).get("sections", {})
    sections["text"] = metrics
    if "bars" in sections or metrics:
        write_report(None, sections)
    return metrics.get("rows_out", 0)


def step_compute_mined_factors(db) -> int:
    """Gold：启用状态注册因子的最新截面 → factor_mined_daily（长表，幂等覆盖）。"""
    from app.models import FactorRegistry, FactorMinedDaily
    from app.core.engine.factor_expr import eval_factor

    enabled = db.execute(select(FactorRegistry).where(
        FactorRegistry.status == "enabled")).scalars().all()
    if not enabled:
        return 0
    syms = db.execute(select(Stock.symbol)).scalars().all()
    last = db.execute(select(KlineDaily.trade_date)
                      .order_by(KlineDaily.trade_date.desc()).limit(1)).scalar()
    if not last:
        return 0

    n = 0
    axis = db.execute(select(KlineDaily.trade_date).distinct()
                      .order_by(KlineDaily.trade_date)).all()
    axis = [a[0] for a in axis]
    try:
        idx0 = max(0, len(axis) - 131)
        slice_dates = axis[idx0:]
    except Exception:  # noqa: BLE001
        return 0
    aligned: dict[str, dict] = {}
    for sym2, td2, c2 in db.execute(select(KlineDaily.symbol, KlineDaily.trade_date,
                                           KlineDaily.close).where(
            KlineDaily.adj == "qfq", KlineDaily.trade_date >= slice_dates[0])):
        aligned.setdefault(sym2, {})[td2] = float(c2)
    bench_map = {}
    for td2, c2 in db.execute(select(IndexKlineDaily.trade_date, IndexKlineDaily.close).where(
            IndexKlineDaily.symbol == "sh000906",
            IndexKlineDaily.trade_date >= slice_dates[0]).order_by(IndexKlineDaily.trade_date)):
        bench_map[td2] = float(c2)

    # 个股新闻情绪 lookup（当日或近3日均值）
    from app.models import NewsStockDaily

    news_hist: dict[str, dict] = {}
    for ns_sym, ns_d, ns_v in db.execute(
        select(NewsStockDaily.symbol, NewsStockDaily.date, NewsStockDaily.net_sentiment)
    ).all():
        if ns_v is not None:
            news_hist.setdefault(ns_sym, {})[ns_d] = float(ns_v)

    def _news_lookup(sym2: str) -> float | None:
        hist = news_hist.get(sym2)
        if not hist or not last:
            return None
        vals = [hist[d2] for d2 in hist if 0 <= (last - d2).days <= 3]
        return (sum(vals) / len(vals)) if vals else None

    snap_slice = [d for d in slice_dates]
    rows_by_fac: dict[str, list] = {}
    for fac in enabled:
        vals: list[tuple[str, float]] = []
        for sym2 in syms:
            amap = aligned.get(sym2)
            if not amap or len(amap) < 55:
                continue
            seg = [amap.get(d2) for d2 in snap_slice[-126:] if d2 in amap]
            mkt_b = [bench_map.get(d2) for d2 in snap_slice[-len(seg):]]
            try:
                v = eval_factor(fac.expr, {"c_m": seg, "c_r": seg, "c_v": seg,
                                           "c_b": seg, "c_t": seg, "mkt_b": mkt_b,
                                           "pe_ttm": None, "pb": None,
                                           "market_cap": None, "roe": None,
                                           "revenue_yoy": None, "profit_yoy": None,
                                           "earnings_surprise": None,
                                           "news_senti": _news_lookup(sym2)})
            except Exception:  # noqa: BLE001
                v = None
            if v is not None:
                import math as _m
                if _m.isfinite(v):
                    vals.append((sym2, v))
        for sym2, v in vals:
            rows_by_fac.setdefault(fac.name, []).append(
                FactorMinedDaily(factor_name=fac.name, date=last, symbol=sym2, value=v))
        n += len(vals)
    # 幂等写入：Core API 删除当日同名因子行（立即生效），再批量插入
    from sqlalchemy import delete as sa_delete

    for fname, objs in rows_by_fac.items():
        db.execute(sa_delete(FactorMinedDaily).where(
            FactorMinedDaily.factor_name == fname,
            FactorMinedDaily.date == last))
        db.add_all(objs)
    db.commit()
    return n


STEPS = [
    ("extract_index_kline", step_extract_index_kline),
    ("extract_stock_kline", step_extract_stock_kline),
    ("extract_attributes", step_extract_attributes),
    ("clean_bars", step_clean_bars),
    ("extract_wechat_articles", step_extract_wechat_articles),
    ("clean_text", step_clean_text),
    ("score_sentiment", step_score_sentiment),
    ("compute_mined_factors", step_compute_mined_factors),
    ("compute_factors", step_compute_factors),
    ("sync_duckdb", step_sync_duckdb),
]


def run_pipeline(trigger: str = "manual", only: list[str] | None = None,
                 stop_on_fail: bool = True) -> int:
    """执行管道；每步落 pipeline_run_steps，run 状态含耗时/行数/错误。返回 run_id。"""
    from app.database import Base, engine

    init_db()
    Base.metadata.create_all(engine, tables=[
        PipelineRun.__table__, PipelineStepLog.__table__])
    run_row = PipelineRun(trigger=trigger, status="RUNNING")
    with SessionLocal() as db:
        db.add(run_row)
        db.commit()
        db.refresh(run_row)
        run_id = run_row.id
        steps_ok = steps_fail = 0
        first_error = None
        for name, fn in STEPS:
            if only and name not in only:
                continue
            log = PipelineStepLog(run_id=run_id, name=name, status="OK")
            t0 = time.time()
            try:
                rows = fn(db)
                log.rows = int(rows or 0)
                log.message = f"{name} 完成"
                steps_ok += 1
            except Exception as e:  # noqa: BLE001
                log.status = "FAIL"
                log.message = f"{type(e).__name__}: {e} {traceback.format_exc()[-300:]}"
                steps_fail += 1
                first_error = first_error or str(e)
                db.add(log)
                db.commit()
                if stop_on_fail:
                    break
            finally:
                log.duration_sec = round(time.time() - t0, 2)
                db.add(log)
                db.commit()
        run_row.status = "FAILED" if steps_fail else ("SUCCESS" if steps_ok else "SKIPPED")
        run_row.finished_at = datetime.utcnow()
        run_row.error = first_error
        db.commit()
        return run_id


if __name__ == "__main__":
    print(f"run_id={run_pipeline('manual')}")
