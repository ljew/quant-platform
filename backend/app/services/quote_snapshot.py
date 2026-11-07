"""收盘行情快照：定时从 tushare 拉回，API 只读本地。

分工（2026-11-07）：
  refresh() —— 定时任务唯一联网入口，每交易日拉一次全市场收盘截面。
  read()    —— /market/quote 调用，**纯本地读，绝不联网**。

为什么这么拆：原先 /market/quote 每次请求都实时查 akshare 的全市场截面
（stock_zh_a_spot_em 要分页拉 59 页，约 11 秒），首页四个指数 + 行情看板标题
同时打开会把服务拖到看门狗重启。行情展示是「一天一更就够」的数据，
不该出现在请求链路里。

数值口径（A 股展示标准，统一在此 + schemas.py 出口双保险）：
  价格 / 指数点位：2 位小数（交易所最小报价单位 0.01）
  涨跌幅：2 位小数（百分数），取 tushare pct_chg —— 它按 pre_close 计算，
          已处理除权，比「相邻两根 close 相除」准确（除权日不会误报腰斩）
  成交量：手，整数        成交额：千元，2 位小数（tushare 原单位，不改）
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select

logger = logging.getLogger("quote_snapshot")

DEFAULT_SYMBOLS = ["sh600519", "sz300750", "sh601318", "sz000858", "sh600036"]

# 最近一次成功刷新（含手动补跑）。进程内记录，供 /snapshot/status 排查用。
_last_refresh: dict = {}

# 核心指数名称（tushare 返回的是代码，没有中文名）。与 index_kline_daily 覆盖范围对齐。
INDEX_NAMES: dict[str, str] = {
    "sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指",
    "sh000300": "沪深300", "sh000906": "中证800", "sh000016": "上证50",
    "sh000905": "中证500", "sh000852": "中证1000", "sz399317": "国证A指",
}


def from_ts_code(code: str) -> str | None:
    """tushare 代码 → 平台代码：688166.SH → sh688166 / 399001.SZ → sz399001。"""
    if "." not in code:
        return None
    num, mkt = code.split(".", 1)
    mkt = mkt.lower()
    if mkt == "sh":
        return f"sh{num}"
    if mkt == "sz":
        return f"sz{num}"
    return None


def _r2(x) -> float:
    """统一到 2 位小数；None / 非数值不抛异常。"""
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return 0.0


def _stock_names(db) -> dict[str, str]:
    from app.models import Stock
    return {str(sym): str(name) for sym, name in
            db.execute(select(Stock.symbol, Stock.name)).all()}


def _from_kline(db, symbols: list[str]) -> dict[str, dict]:
    """本地日线兜底：快照表缺的标的，用库里已有 K 线生成（零网络）。

    涨跌幅用相邻两根 close 计算，除权日会有偏差，因此 source 标为
    kline_fallback，便于事后识别；正常路径一律用 tushare pct_chg。
    """
    from app.models import IndexKlineDaily, KlineDaily, QuoteSnapshot, Stock

    out: dict[str, dict] = {}
    names = _stock_names(db)
    for sym in symbols:
        model = IndexKlineDaily if sym in INDEX_NAMES else KlineDaily
        stmt = select(model).where(model.symbol == sym).order_by(
            model.trade_date.desc()).limit(2)
        if model is KlineDaily:
            stmt = stmt.where(KlineDaily.adj == "none")
        rows = db.execute(stmt).scalars().all()
        if not rows:
            continue
        cur = rows[0]
        pct = None
        if len(rows) > 1 and rows[1].close:
            pct = round((cur.close - rows[1].close) / rows[1].close * 100, 2)
        name = INDEX_NAMES.get(sym) or names.get(sym) or sym
        out[sym] = {
            "symbol": sym, "name": name,
            "price": _r2(cur.close), "change_pct": pct,
            "updated_at": datetime.combine(cur.trade_date, datetime.min.time()),
            "trade_date": cur.trade_date,
            "source": "kline_fallback",
        }
    return out


def refresh(trade_date: date | None = None) -> dict:
    """从 tushare 拉最近交易日全市场收盘截面，整表替换写入 quote_snapshot。

    只在定时任务 / 手动补跑时调用。返回 {ok, trade_date, n_rows, source, error}。
    """
    from app.database import SessionLocal
    from app.models import QuoteSnapshot
    from app.services import data_source

    pro = data_source._require_tushare_pro()
    fields = "ts_code,trade_date,close,pre_close,pct_chg"

    # 今天往前最多回溯 8 天找最近交易日：周末/节假日不会卡住，也不会拉到旧数据。
    df = None
    used = trade_date
    days = range(8) if trade_date is None else [0]
    for i in days:
        d = trade_date or (date.today() - timedelta(days=i))
        try:
            cand = pro.daily(trade_date=d.strftime("%Y%m%d"), fields=fields)
        except Exception as e:  # noqa: BLE001
            logger.warning("tushare daily(%s) 失败：%s", d, e)
            continue
        if cand is not None and not cand.empty:
            df, used = cand, d
            break
        if trade_date is not None:
            break
    if df is None or used is None:
        return {"ok": False, "error": "tushare 最近 8 天无日线数据", "n_rows": 0}

    dsdate = used.strftime("%Y%m%d")
    try:
        idx_df = pro.index_daily(trade_date=dsdate, fields=fields)
    except Exception as e:  # noqa: BLE001
        logger.warning("tushare index_daily(%s) 失败：%s", dsdate, e)
        idx_df = None

    db = SessionLocal()
    try:
        names = _stock_names(db)
        now = datetime.now()
        rows: list[dict] = []

        def _append(code, close, pct, name):
            sym = from_ts_code(str(code))
            if not sym or close is None:
                return
            try:
                price = _r2(close)
            except (TypeError, ValueError):
                return
            if price <= 0:
                return
            rows.append({
                "symbol": sym, "name": name or sym, "price": price,
                "change_pct": None if pct is None else _r2(pct),
                "trade_date": used, "source": "tushare", "updated_at": now,
            })

        n_stock = 0
        n_index = 0
        for r in df.itertuples(index=False):
            sym = from_ts_code(str(r.ts_code))
            before = len(rows)
            _append(r.ts_code, r.close, r.pct_chg,
                    names.get(sym) if sym else None)
            n_stock += len(rows) - before
        if idx_df is not None:
            for r in idx_df.itertuples(index=False):
                sym = from_ts_code(str(r.ts_code))
                if sym in INDEX_NAMES:
                    before = len(rows)
                    _append(r.ts_code, r.close, r.pct_chg, INDEX_NAMES[sym])
                    n_index += len(rows) - before

        if not rows:
            return {"ok": False, "error": "tushare 返回数据为空", "n_rows": 0}

        # 只要最新截面，整表替换比 5000 次 upsert 快得多；事务内完成，读者不会看到空表。
        db.execute(delete(QuoteSnapshot))
        db.bulk_insert_mappings(QuoteSnapshot, rows)
        db.commit()
        logger.info("行情快照已更新：%s，%d 条（个股 %d + 指数 %d）",
                    used, len(rows), n_stock, n_index)
        result = {"ok": True, "trade_date": used.isoformat(), "n_rows": len(rows),
                  "source": "tushare", "updated_at": now.isoformat(timespec="seconds")}
        _last_refresh.clear()
        _last_refresh.update(result)
        return result
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.exception("行情快照写入失败")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "n_rows": 0}
    finally:
        db.close()


def read(symbols: list[str] | None = None) -> list[dict]:
    """只读本地快照，按请求顺序返回；表缺项时用库内 K 线兜底。**零网络请求**。"""
    from app.database import SessionLocal
    from app.models import QuoteSnapshot

    targets = [s.strip().lower() for s in (symbols or DEFAULT_SYMBOLS) if s.strip()]
    db = SessionLocal()
    try:
        stmt = select(QuoteSnapshot).where(QuoteSnapshot.symbol.in_(targets))
        got = {r.symbol: {
            "symbol": r.symbol, "name": r.name,
            "price": _r2(r.price),
            "change_pct": None if r.change_pct is None else _r2(r.change_pct),
            "updated_at": r.updated_at, "trade_date": r.trade_date,
            "source": r.source,
        } for r in db.execute(stmt).scalars().all()}

        missing = [s for s in targets if s not in got]
        if missing:
            got.update(_from_kline(db, missing))
        return [got[s] for s in targets if s in got]
    finally:
        db.close()


def get_status() -> dict:
    """供运维查看快照新鲜度（不与外部数据源交互）。"""
    from app.database import SessionLocal
    from app.models import QuoteSnapshot

    db = SessionLocal()
    try:
        n = db.execute(select(func.count()).select_from(QuoteSnapshot)).scalar() or 0
        latest = db.execute(select(func.max(QuoteSnapshot.trade_date))).scalar()
        src = db.execute(select(QuoteSnapshot.source).limit(1)).scalar()
        return {"rows": n, "trade_date": latest.isoformat() if latest else None,
                "source": src, "last_refresh": dict(_last_refresh) or None}
    finally:
        db.close()
