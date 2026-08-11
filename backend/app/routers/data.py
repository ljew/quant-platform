"""数据接入 API：触发全市场/指定标的的行情入库。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Stock, KlineDaily
from app.schemas import IngestRequest, IngestResponse
from app.services import data_source, ingestion

logger = logging.getLogger("quant.api.data")
router = APIRouter(prefix="/data", tags=["data"])


@router.get("/status")
def data_status(db: Session = Depends(get_db)):
    """当前数据覆盖情况概览。"""
    stock_cnt = db.query(func.count(Stock.id)).scalar()
    kline_cnt = db.query(func.count(KlineDaily.id)).scalar()
    symbols_with_kline = db.query(
        func.count(func.distinct(KlineDaily.symbol))
    ).scalar()
    latest = db.query(func.max(KlineDaily.trade_date)).scalar()
    return {
        "stocks_total": stock_cnt,
        "kline_total": kline_cnt,
        "symbols_with_kline": symbols_with_kline,
        "latest_kline_date": latest.isoformat() if latest else None,
        "data_source_available": data_source.check_akshare(),
    }


@router.post("/ingest/stocks", response_model=IngestResponse)
def ingest_stocks(background: BackgroundTasks):
    """拉取全市场 A股基础信息（异步）。"""
    try:
        data_source.check_akshare()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    background.add_task(_run_ingest_stocks)
    return IngestResponse(
        task="ingest_stocks", status="queued",
        message="全市场股票列表入库任务已提交（后台执行）",
    )


@router.post("/ingest/kline", response_model=IngestResponse)
def ingest_kline(req: IngestRequest, background: BackgroundTasks):
    """拉取日K线。不指定 symbols 则对全市场（较慢）。"""
    try:
        data_source.check_akshare()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    background.add_task(_run_ingest_kline, req)
    return IngestResponse(
        task="ingest_kline", status="queued",
        message=f"日K线入库任务已提交（后台执行），symbols={req.symbols or '全市场'}",
    )


def _run_ingest_stocks():
    try:
        n = ingestion.ingest_stock_universe()
        logger.info(f"ingest_stocks done, new={n}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"ingest_stocks error: {e}")


def _run_ingest_kline(req: IngestRequest):
    db = SessionLocal_for_read = None
    try:
        from app.database import SessionLocal
        if req.symbols:
            targets = req.symbols
        else:
            db = SessionLocal()
            targets = [s.symbol for s in db.query(Stock.symbol).all()]
        total = len(targets)
        done = 0
        for sym in targets:
            try:
                ingestion.ingest_kline_for_symbol(
                    sym, start_year=req.start_year, adj=req.adj
                )
                done += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"skip {sym}: {e}")
            if done % 50 == 0:
                logger.info(f"kline progress {done}/{total}")
        logger.info(f"ingest_kline done: {done}/{total}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"ingest_kline error: {e}")
    finally:
        if db:
            db.close()
