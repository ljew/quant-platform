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
from app.models import KlineDaily, NewsMarketDaily, PipelineRun, PipelineStepLog

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


def step_compute_factors(db) -> int:
    """Gold：核心池最新截面因子计算（复用 ETL）。"""
    from app.services.etl import compute_factor_cross_section, _get_universe

    syms = _get_universe(db)
    last = db.execute(select(KlineDaily.trade_date)
                      .order_by(KlineDaily.trade_date.desc()).limit(1)).scalar()
    return compute_factor_cross_section(db, syms, last or date.today())


# ============ 步骤注册与执行器 ============
STEPS = [
    ("extract_index_kline", step_extract_index_kline),
    ("extract_stock_kline", step_extract_stock_kline),
    ("extract_attributes", step_extract_attributes),
    ("extract_wechat_articles", step_extract_wechat_articles),
    ("score_sentiment", step_score_sentiment),
    ("compute_factors", step_compute_factors),
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
