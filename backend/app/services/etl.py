"""ETL 数据管道（设计 v1.0：tushare 采集 → 增量入库 → 因子计算落库）。

调度：每晚 17:00（data_scheduler 触发 scripts/etl_daily.py）。

阶段：
  Extract   tushare：个股日K（前复权，增量）/ 指数日K（增量）/ 股票属性（daily_basic）
            / 指数成分快照（index_weight）
  Transform 核心池全市场最新交易日截面：14 因子（factor_library 表达式引擎，
            与回测引擎同一套计算口径，ns 构造对齐 multi_factor）
  Load      SQLite（kline_daily / index_kline_daily / stocks / factor_daily）
            → DuckDB 同步（duckdb_sync.sync_after_seed）

每日增量池 = 核心指数成分并集（中证800/沪深300/中证500/中证1000/上证50/创业板指），
tushare 每日调用量 ~2000 次以内，免费 token 额度安全。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.database import init_db, SessionLocal
from app.models import (
    FactorDaily,
    KlineDaily,
    IndexKlineDaily,
    Stock,
    FundamentalsHistory,
    FACTOR_COLUMNS,
)
from app.services.data_source import _require_tushare_pro, to_ts_code, normalize_symbol
from app.core.engine.factor_library import FACTORS
from app.core.engine.factor_expr import eval_factor

logger = logging.getLogger("etl")

# 每日增量核心池指数（成分并集）
CORE_INDEXES = ["000906", "000300", "000905", "000852", "000016", "399006"]
# 因子时序 lookback（与 multi_factor 对齐）：动量120/低波60/beta120/尾部120
LOOKBACK = 125
# tushare 每次批量调用的股票数上限（避免单次响应过大）
_BATCH = 200


# ============ E: Extract ============
def _tushare_qfq(symbol: str, sd: date, ed: date) -> list[dict]:
    """tushare 前复权日K：daily × adj_factor / 最新 adj_factor。"""
    pro = _require_tushare_pro()
    ts = to_ts_code(symbol)
    df = pro.daily(ts_code=ts, start_date=sd.strftime("%Y%m%d"), end_date=ed.strftime("%Y%m%d"))
    if df is None or df.empty:
        return []
    af = pro.adj_factor(ts_code=ts, start_date=sd.strftime("%Y%m%d"), end_date=ed.strftime("%Y%m%d"))
    if af is None or af.empty:
        return []
    factor_map = {str(r["trade_date"]): float(r["adj_factor"]) for _, r in af.iterrows()}
    if not factor_map:
        return []
    latest_f = max(factor_map.values())
    out = []
    for _, r in df.iterrows():
        f = factor_map.get(str(r["trade_date"]))
        if not f:
            continue
        k = f / latest_f
        out.append({
            "trade_date": date.fromisoformat(str(r["trade_date"])),
            "open": round(float(r["open"]) * k, 3),
            "high": round(float(r["high"]) * k, 3),
            "low": round(float(r["low"]) * k, 3),
            "close": round(float(r["close"]) * k, 3),
            "volume": int(float(r["vol"]) / k),
            "amount": float(r["amount"]),
        })
    out.sort(key=lambda x: x["trade_date"])
    return out


def _get_universe(db: Session) -> list[str]:
    """核心池：核心指数成分并集（已归一化 symbol），缺失回退 stocks 全表。"""
    from app.services.membership_store import get_membership

    sd = date.today() - timedelta(days=45)
    union: set = set()
    for code in CORE_INDEXES:
        try:
            snaps = get_membership(db, code, sd, date.today())
            for _, sset in snaps[-2:]:  # 最近两个月快照（成分可能调整）
                union |= sset
        except Exception:  # noqa: BLE001
            logger.warning("core index %s membership 获取失败", code)
    if len(union) < 1000:
        # 兜底：全市场股票表
        rows = db.execute(select(Stock.symbol).limit(6000)).all()
        union = {r[0] for r in rows}
    return sorted(union)


def _last_kline_date(db: Session, symbol: str) -> date | None:
    r = db.execute(
        select(KlineDaily.trade_date).where(KlineDaily.symbol == symbol)
        .order_by(KlineDaily.trade_date.desc()).limit(1)
    ).scalar()
    return r


def extract_kline_incremental(db: Session, symbols: list[str], sd: date, ed: date,
                              progress=None) -> int:
    """增量拉个股日K（tushare 前复权），upsert kline_daily。返回新增行数。"""
    from app.services import ingestion

    total = 0
    n = len(symbols)
    for i, sym in enumerate(symbols):
        last = _last_kline_date(db, sym)
        s = (last + timedelta(days=1)) if last else (sd - timedelta(days=500))
        if s > ed:
            continue
        try:
            bars = _tushare_qfq(sym, s, ed)
            if bars:
                ingestion.upsert_kline(db, bars, sym, "qfq")
                total += len(bars)
        except Exception as e:  # noqa: BLE001
            logger.warning("kline %s 增量失败: %s", sym, e)
        if progress and (i % 200 == 0 or i == n - 1):
            progress(i, n, total)
    db.commit()
    return total


def extract_index_incremental(db: Session, ed: date) -> int:
    """指数日K增量（tushare index_daily）。返回新增行数。"""
    from scripts.seed_index_kline import DEFAULT_INDICES

    pro = _require_tushare_pro()
    total = 0
    for symbol in DEFAULT_INDICES:
        last = db.execute(
            select(IndexKlineDaily.trade_date).where(IndexKlineDaily.symbol == symbol)
            .order_by(IndexKlineDaily.trade_date.desc()).limit(1)
        ).scalar()
        s = (last + timedelta(days=1)) if last else date(2015, 1, 1)
        if s > ed:
            continue
        try:
            df = pro.index_daily(ts_code=to_ts_code(symbol),
                                 start_date=s.strftime("%Y%m%d"), end_date=ed.strftime("%Y%m%d"))
            if df is None or df.empty:
                continue
            objs = []
            for _, r in df.iterrows():
                objs.append(IndexKlineDaily(
                    symbol=symbol, trade_date=date.fromisoformat(str(r["trade_date"])),
                    open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
                    close=float(r["close"]),
                    volume=int(float(r.get("vol", 0) or 0)), amount=float(r.get("amount", 0) or 0),
                ))
            if objs:
                db.bulk_save_objects(objs)
                total += len(objs)
        except Exception as e:  # noqa: BLE001
            logger.warning("index %s 增量失败: %s", symbol, e)
    db.commit()
    return total


def extract_attributes(db: Session) -> int:
    """更新 stocks 截面属性（tushare daily_basic 最新交易日：市值/PE/PB）。"""
    try:
        from app.services import ingestion

        return ingestion.update_stock_attributes()
    except Exception as e:  # noqa: BLE001
        logger.warning("股票属性更新失败: %s", e)
        return 0


# ============ T: Transform（因子计算） ============
def _attrs_for(db: Session, syms: list[str]) -> dict:
    rows = db.execute(
        select(Stock.symbol, Stock.industry, Stock.market_cap, Stock.pe_ttm, Stock.pb,
               Stock.roe, Stock.revenue_yoy, Stock.profit_yoy)
        .where(Stock.symbol.in_(syms))
    ).all()
    return {
        r.symbol: {"industry": r.industry, "market_cap": r.market_cap, "pe_ttm": r.pe_ttm,
                   "pb": r.pb, "roe": r.roe, "revenue_yoy": r.revenue_yoy, "profit_yoy": r.profit_yoy}
        for r in rows
    }


def _earnings_surprise_latest(db: Session, sym: str) -> float | None:
    """PEAD 盈余惊喜（与回测引擎 _pit_earnings_surprise 同口径）：
    最新报告期利润增速 − 历史利润增速均值，需 ≥2 期。"""
    rows = db.execute(
        select(FundamentalsHistory.report_date, FundamentalsHistory.profit_yoy)
        .where(FundamentalsHistory.symbol == sym)
        .order_by(FundamentalsHistory.report_date)
    ).all()
    pts = [float(r[1]) for r in rows if r[1] is not None]
    if len(pts) < 2:
        return None
    cur = pts[-1]
    hist_mean = sum(pts[:-1]) / len(pts[:-1])
    return (cur - hist_mean) / 100.0  # 归一化（与回测 report_factor 取向一致）


def _benchmark_closes(db: Session, sd: date, ed: date) -> list[float]:
    rows = db.execute(
        select(IndexKlineDaily.close).where(
            IndexKlineDaily.symbol == "sh000906",
            IndexKlineDaily.trade_date >= sd, IndexKlineDaily.trade_date <= ed,
        ).order_by(IndexKlineDaily.trade_date)
    ).all()
    return [float(r[0]) for r in rows]


def compute_factor_cross_section(db: Session, syms: list[str], trade_date: date) -> int:
    """对核心池计算 trade_date 截面的 14 因子，写入 factor_daily（upsert）。"""
    from app.core.engine import indicators as ind
    from app.models import NewsStockDaily
    from app.datahub.ns_vars import news_lookup_factory

    # 个股新闻情绪 lookup（当日或近 3 日均值；无数据返回 None）
    _nhist: dict[str, dict] = {}
    for _s, _d, _v in db.execute(
        select(NewsStockDaily.symbol, NewsStockDaily.date, NewsStockDaily.net_sentiment)
    ).all():
        if _v is not None:
            _nhist.setdefault(_s, {})[_d] = float(_v)
    news_lookup = news_lookup_factory(_nhist)

    attrs_map = _attrs_for(db, syms)
    # 基准（中证800）对齐序列：与股票区间对齐
    sd = trade_date - timedelta(days=400)
    bench = _benchmark_closes(db, sd, trade_date)
    if len(bench) < LOOKBACK:
        logger.warning("基准序列不足，跳过因子计算")
        return 0

    rows_written = 0
    existing = {
        r[0] for r in db.execute(
            select(FactorDaily.symbol).where(FactorDaily.trade_date == trade_date)
        ).all()
    }
    objs = []
    for i, sym in enumerate(syms):
        if sym in existing:
            continue
        bars = db.execute(
            select(KlineDaily.trade_date, KlineDaily.close).where(
                KlineDaily.symbol == sym, KlineDaily.adj == "qfq",
                KlineDaily.trade_date >= sd, KlineDaily.trade_date <= trade_date,
            ).order_by(KlineDaily.trade_date)
        ).all()
        closes = [float(b[1]) for b in bars]
        if len(closes) < LOOKBACK:
            continue
        mkt_b = bench[-len(closes):]
        if len(mkt_b) != len(closes):
            continue
        attrs = attrs_map.get(sym, {}) or {}
        from app.datahub.ns_vars import make_ns
        esv = _earnings_surprise_latest(db, sym)
        ns = make_ns(closes, mkt_b, attrs,
                     news=news_lookup(sym, trade_date), esv=esv)
        vals = {}
        for f in FACTORS:
            vals[f.name] = eval_factor(f.expr, ns)
        row = FactorDaily(symbol=sym, trade_date=trade_date)
        for col in FACTOR_COLUMNS:
            setattr(row, col, vals.get(col))
        objs.append(row)
        if len(objs) >= _BATCH:
            db.bulk_save_objects(objs)
            objs = []
        if i % 500 == 0:
            logger.info("因子计算进度 %d/%d", i, len(syms))
    if objs:
        db.bulk_save_objects(objs)
    db.commit()
    rows_written = db.execute(
        select(func.count()).select_from(FactorDaily).where(FactorDaily.trade_date == trade_date)
    ).scalar() or 0
    return rows_written


# ============ L: Load ============
def load_sync_duckdb() -> None:
    from app.services import duckdb_sync

    duckdb_sync.sync_after_seed()


# ============ 主流程 ============
def run_etl(db: Session | None = None, universe: list[str] | None = None,
            progress=None, no_duckdb: bool = False) -> dict:
    """执行一次完整 ETL。返回统计 dict。"""
    if db is None:
        init_db()
        db = SessionLocal()
    ed = date.today()
    stats: dict = {"index_kline": 0, "kline": 0, "attrs": 0, "factors": 0}

    # E: 指数日K + 股票属性
    stats["index_kline"] = extract_index_incremental(db, ed)
    stats["attrs"] = extract_attributes(db)

    # E: 核心池个股日K 增量（tushare 前复权）
    syms = universe or _get_universe(db)
    logger.info("核心池 %d 只，开始增量拉取…", len(syms))
    stats["universe"] = len(syms)

    def _kline_prog(i, n, total):
        if progress:
            progress(0.15 + 0.45 * i / max(n, 1), f"拉取K线 {i}/{n}（已新增 {total} 行）")

    stats["kline"] = extract_kline_incremental(db, syms, date.today() - timedelta(days=30), ed,
                                               progress=_kline_prog)

    # T: 因子计算（最新交易日截面）
    if progress:
        progress(0.65, "计算因子截面…")
    trade_date = ed
    # 若今天非交易日，用最近有数据的日期
    last_k = db.execute(
        select(KlineDaily.trade_date).where(KlineDaily.symbol == syms[0])
        .order_by(KlineDaily.trade_date.desc()).limit(1)
    ).scalar()
    if last_k:
        trade_date = last_k
    stats["factor_date"] = trade_date.isoformat()
    stats["factors"] = compute_factor_cross_section(db, syms, trade_date)
    if progress:
        progress(0.9, f"因子截面落库 {stats['factors']} 条")

    # L: DuckDB 同步
    if not no_duckdb:
        load_sync_duckdb()
    if progress:
        progress(1.0, "ETL 完成")
    db.close()
    return stats
