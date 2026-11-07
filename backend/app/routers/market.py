"""行情相关 API：K线、实时快照、标的信息。"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select, func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Stock, KlineDaily, IndexKlineDaily
from app.schemas import KlinePoint, Quote, StockItem
from app.services import data_source, ingestion, quote_snapshot

router = APIRouter(prefix="/market", tags=["market"])

# 常见简称 → 6 位代码。仅收录“简称不是全名连续子串”的词条（如 招商银行→招行），
# 子串能被子句 name LIKE 命中的（茅台/宁德/平安…）无需入表。可继续扩充。
STOCK_ABBREV: dict[str, str] = {
    "招行": "600036", "浦发": "600000", "兴业": "601166", "民生": "600016",
    "光大": "601818", "工行": "601398", "建行": "601939", "农行": "601288",
    "中行": "601988", "交行": "601328", "邮储": "601658", "国君": "601211",
    "招证": "600999", "中石油": "601857", "中石化": "600028", "中海油": "600938",
    "海油": "600938", "长电": "600900", "江铜": "600362", "南航": "600029",
    "东航": "600115", "byd": "002594",
}


@router.get("/stocks", response_model=list[StockItem])
def list_stocks(
    market: str | None = Query(None, description="sh/sz/hk/us 过滤"),
    keyword: str | None = Query(None, description="代码 / 简称 / 名称模糊搜索"),
    limit: int = Query(200, le=2000),
    db: Session = Depends(get_db),
):
    stmt = select(Stock)
    if market:
        stmt = stmt.where(Stock.market == market)
    conds = []
    if keyword:
        kw = keyword.strip()
        if kw:
            # 1) 代码 / 全名子串
            conds.append(Stock.symbol.contains(kw) | Stock.name.contains(kw))
            # 2) 简称映射：如 “招行”→“招商银行”(600036)
            hit = STOCK_ABBREV.get(kw.lower().replace(" ", ""))
            if hit:
                conds.append(Stock.symbol.contains(hit))
    if conds:
        stmt = stmt.where(or_(*conds))
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
    # 指数与个股分表：index_kline_daily 存 sh000001 等 8 个核心指数的完整历史，
    # kline_daily 里没有指数行。原实现查不到就回源 get_daily_kline()，后者把
    # sh000001 剥成 000001 抓回平安银行 —— 指数 K 线画出的是 11.81 的个股价
    # （2026-11-07 修）。指数无复权概念，库内查不到就返回空，绝不回源到个股。
    if data_source.is_index_symbol(symbol):
        stmt = select(IndexKlineDaily).where(IndexKlineDaily.symbol == symbol)
        if start:
            stmt = stmt.where(IndexKlineDaily.trade_date >= date.fromisoformat(start))
        if end:
            stmt = stmt.where(IndexKlineDaily.trade_date <= date.fromisoformat(end))
        stmt = stmt.order_by(IndexKlineDaily.trade_date.desc()).limit(limit)
        idx_rows = db.execute(stmt).scalars().all()
        idx_rows.reverse()  # 时间升序，与个股分支一致
        return [
            KlinePoint(date=r.trade_date.isoformat(), open=r.open, high=r.high,
                       low=r.low, close=r.close, volume=r.volume, amount=r.amount)
            for r in idx_rows
        ]

    stmt = select(KlineDaily).where(
        KlineDaily.symbol == symbol, KlineDaily.adj == "none"
    )
    if start:
        stmt = stmt.where(KlineDaily.trade_date >= date.fromisoformat(start))
    if end:
        stmt = stmt.where(KlineDaily.trade_date <= date.fromisoformat(end))
    stmt = stmt.order_by(KlineDaily.trade_date.desc()).limit(limit)
    rows = db.execute(stmt).scalars().all()
    # 本地无数据 → 自动从数据源实时回源，保证任意标的都能看行情。
    # 注意：在线源返回的是**前复权**价，因此只返回不写库 —— 库内保持
    # 「只存未复权原价」这一单一口径，否则同一 (symbol, date) 会互相覆盖。
    if not rows:
        try:
            from datetime import date as _date
            fetched = data_source.get_daily_kline(
                symbol, start_date=_date(2015, 1, 1),
                end_date=None, adj=adj, limit=limit,
            )
            if fetched:
                return [
                    KlinePoint(
                        date=str(b.get("trade_date") or b.get("date"))[:10],
                        open=b["open"], high=b["high"], low=b["low"], close=b["close"],
                        volume=b.get("volume", 0), amount=b.get("amount", 0.0),
                    )
                    for b in fetched
                ]
        except Exception:  # noqa: BLE001
            pass  # 回源失败则保持空，由前端提示

    if not rows:
        return []  # 库内与在线均无数据：直接返回空，避免下面 rows[0] 越界
    rows.reverse()  # 时间升序返回，便于画图

    # 库内存未复权原价，按请求的 adj 实时折算
    bars = [
        {"date": r.trade_date.isoformat(), "open": r.open, "high": r.high,
         "low": r.low, "close": r.close, "volume": r.volume, "amount": r.amount}
        for r in rows
    ]
    if adj not in (None, "", "none"):
        from app.models import AdjFactorDaily
        from app.services.adjust import apply_adjust_bars

        lo, hi = rows[0].trade_date, rows[-1].trade_date
        facs = {r.trade_date: r.adj_factor for r in db.execute(
            select(AdjFactorDaily).where(
                AdjFactorDaily.symbol == symbol,
                AdjFactorDaily.trade_date >= lo,
                AdjFactorDaily.trade_date <= hi,
            )).scalars().all()}
        for b, r in zip(bars, rows):
            b["_f"] = facs.get(r.trade_date)
        bars = apply_adjust_bars(bars, adj)
    return [
        KlinePoint(date=b["date"], open=b["open"], high=b["high"], low=b["low"],
                   close=b["close"], volume=b["volume"], amount=b["amount"])
        for b in bars
    ]


@router.get("/quote/{symbol}", response_model=Quote)
def get_quote(symbol: str):
    """收盘快照：**只读本地库，零网络请求**。

    数据由 quote_scheduler 每交易日 18:30 后从 tushare 拉回落库（2026-11-07 改）。
    原先每次请求都实时查 akshare 全市场截面，约 11 秒且会拖到看门狗重启。
    """
    rows = quote_snapshot.read([symbol.lower()])
    if not rows:
        raise HTTPException(status_code=404, detail=f"未找到 {symbol} 的行情快照")
    q = rows[0]
    return Quote(symbol=q["symbol"], name=q["name"], price=q["price"],
                 change_pct=q["change_pct"], updated_at=q["updated_at"])


@router.get("/quote", response_model=list[Quote])
def get_quotes(symbols: str | None = Query(None, description="逗号分隔代码列表")):
    """批量收盘快照，只读本地库，按请求顺序返回。"""
    sym_list = [s.strip().lower() for s in symbols.split(",")] if symbols else None
    return [
        Quote(symbol=q["symbol"], name=q["name"], price=q["price"],
              change_pct=q["change_pct"], updated_at=q["updated_at"])
        for q in quote_snapshot.read(sym_list)
    ]


@router.get("/snapshot/status")
def snapshot_status():
    """快照新鲜度 + 定时任务状态（运维排查用，不访问外部数据源）。"""
    from app.core.quote_scheduler import get_status
    return get_status()


@router.post("/snapshot/refresh")
def snapshot_refresh():
    """手动补跑一次 tushare 收盘快照。定时任务失败或临时核对时使用。"""
    r = quote_snapshot.refresh()
    if not r.get("ok"):
        raise HTTPException(status_code=503, detail=str(r.get("error")))
    return r
