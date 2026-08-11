"""行情相关 API：K线、实时快照、标的信息。"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Stock, KlineDaily
from app.schemas import KlinePoint, Quote, StockItem
from app.services import data_source, ingestion

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/stocks", response_model=list[StockItem])
def list_stocks(
    market: str | None = Query(None, description="sh/sz/hk/us 过滤"),
    keyword: str | None = Query(None, description="代码或名称模糊搜索"),
    limit: int = Query(200, le=2000),
    db: Session = Depends(get_db),
):
    stmt = select(Stock)
    if market:
        stmt = stmt.where(Stock.market == market)
    if keyword:
        stmt = stmt.where(
            Stock.symbol.contains(keyword) | Stock.name.contains(keyword)
        )
    stmt = stmt.limit(limit)
    return db.execute(stmt).scalars().all()


@router.get("/kline/{symbol}", response_model=list[KlinePoint])
def get_kline(
    symbol: str,
    start: str | None = Query(None, description="起始日期 YYYY-MM-DD"),
    end: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    limit: int = Query(500, le=5000, description="最近 N 条"),
    adj: str = Query("qfq", description="qfq/hfq/none"),
    db: Session = Depends(get_db),
):
    stmt = select(KlineDaily).where(
        KlineDaily.symbol == symbol, KlineDaily.adj == adj
    )
    if start:
        stmt = stmt.where(KlineDaily.trade_date >= date.fromisoformat(start))
    if end:
        stmt = stmt.where(KlineDaily.trade_date <= date.fromisoformat(end))
    stmt = stmt.order_by(KlineDaily.trade_date.desc()).limit(limit)
    rows = db.execute(stmt).scalars().all()
    # 本地无数据 → 自动从数据源实时回源并落地，保证任意标的都能看行情
    if not rows:
        try:
            from datetime import date as _date
            fetched = data_source.get_daily_kline(
                symbol, start_date=_date(2015, 1, 1),
                end_date=None, adj=adj, limit=limit,
            )
            if fetched:
                ingestion.upsert_kline(db, fetched, symbol, adj)
                db.commit()
                rows = db.execute(stmt).scalars().all()
        except Exception:  # noqa: BLE001
            pass  # 回源失败则保持空，由前端提示
    rows.reverse()  # 时间升序返回，便于画图
    return [
        KlinePoint(
            date=r.trade_date.isoformat(),
            open=r.open, high=r.high, low=r.low, close=r.close,
            volume=r.volume, amount=r.amount,
        )
        for r in rows
    ]


@router.get("/quote/{symbol}", response_model=Quote)
def get_quote(symbol: str):
    """实时快照（盘中有值；非交易时段返回最近收盘价）。"""
    try:
        quotes = data_source.get_spot_quotes([symbol])
        if quotes:
            q = quotes[0]
            return Quote(
                symbol=q["symbol"], name=q["name"],
                price=q["price"], change_pct=q["change_pct"],
                updated_at=datetime.utcnow(),
            )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    raise HTTPException(status_code=404, detail=f"未找到 {symbol} 的实时行情")


@router.get("/quote", response_model=list[Quote])
def get_quotes(symbols: str | None = Query(None, description="逗号分隔代码列表")):
    sym_list = [s.strip() for s in symbols.split(",")] if symbols else None
    try:
        quotes = data_source.get_spot_quotes(sym_list)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return [
        Quote(symbol=q["symbol"], name=q["name"], price=q["price"],
              change_pct=q["change_pct"], updated_at=datetime.utcnow())
        for q in quotes
    ]
