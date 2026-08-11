"""数据入库服务：将外部数据源拉取的数据落地到本地库。

设计要点：
- 幂等：按 (symbol, trade_date, adj) 唯一约束，重复写入先删后插或 upsert。
- 增量：记录每只股票已拉取到的最新日期，下次只补增量。
- 全量：首次按 history_start_year 拉完整历史。
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy import select, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.database import SessionLocal
from app.models import Stock, KlineDaily
from app.services import data_source

logger = logging.getLogger("quant.ingestion")


def upsert_kline(db, rows: list[dict], symbol: str, adj: str):
    """按 (symbol, trade_date) upsert，兼容 SQLite 与 Postgres。"""
    for r in rows:
        td = r["trade_date"]
        if isinstance(td, str):
            td = date.fromisoformat(td)
        # 先尝试删除旧记录（最简单稳妥的幂等方式）
        db.query(KlineDaily).filter(
            KlineDaily.symbol == symbol,
            KlineDaily.trade_date == td,
            KlineDaily.adj == adj,
        ).delete()
        db.add(KlineDaily(
            symbol=symbol, trade_date=td,
            open=r["open"], high=r["high"], low=r["low"], close=r["close"],
            volume=r["volume"], amount=r["amount"], adj=adj,
        ))


def ingest_stock_universe() -> int:
    """拉取全市场 A股基础信息并写入 stocks 表。返回新增/更新数量。"""
    db = SessionLocal()
    try:
        stocks = data_source.get_stock_list()
        count = 0
        for s in stocks:
            existing = db.query(Stock).filter(Stock.symbol == s["symbol"]).first()
            if existing:
                existing.name = s["name"]
                existing.market = s["market"]
                existing.raw_code = s["raw_code"]
            else:
                db.add(Stock(**s))
                count += 1
        db.commit()
        logger.info(f"stock universe upserted: {count} new, total {len(stocks)}")
        return count
    finally:
        db.close()


def update_stock_attributes() -> int:
    """拉取并写入 stocks 表的行业/市值/估值截面快照。返回更新数量。"""
    db = SessionLocal()
    try:
        stocks = db.query(Stock).all()
        if not stocks:
            return 0
        symbols = [s.symbol for s in stocks]
        attrs = data_source.get_stock_attrs(symbols)
        updated = 0
        for s in stocks:
            a = attrs.get(s.symbol)
            if not a:
                continue
            changed = False
            if a.get("industry") and a["industry"] != s.industry:
                s.industry = a["industry"]
                changed = True
            if a.get("market_cap") is not None and a["market_cap"] != s.market_cap:
                s.market_cap = a["market_cap"]
                changed = True
            if a.get("pe_ttm") is not None:
                s.pe_ttm = a["pe_ttm"]
                changed = True
            if a.get("pb") is not None:
                s.pb = a["pb"]
                changed = True
            if changed:
                updated += 1
        db.commit()
        logger.info(f"stock attributes updated: {updated}/{len(stocks)}")
        return updated
    finally:
        db.close()


def ingest_kline_for_symbol(symbol: str, start_year: int | None = None,
                            adj: str = "qfq") -> int:
    """拉取单标的日K并入库。返回写入条数。"""
    db = SessionLocal()
    try:
        # 查询已有最新日期，做增量
        latest = db.query(func.max(KlineDaily.trade_date)).filter(
            KlineDaily.symbol == symbol, KlineDaily.adj == adj
        ).scalar()
        start = date(start_year or settings.history_start_year, 1, 1)
        if latest:
            start = latest + __import__("datetime").timedelta(days=1)
        rows = data_source.get_daily_kline(symbol, start_date=start, adj=adj)
        if not rows:
            return 0
        upsert_kline(db, rows, symbol, adj)
        db.commit()
        return len(rows)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.error(f"ingest kline failed for {symbol}: {e}")
        raise
    finally:
        db.close()


def latest_kline_date(symbol: str, adj: str = "qfq") -> date | None:
    db = SessionLocal()
    try:
        return db.query(func.max(KlineDaily.trade_date)).filter(
            KlineDaily.symbol == symbol, KlineDaily.adj == adj
        ).scalar()
    finally:
        db.close()
