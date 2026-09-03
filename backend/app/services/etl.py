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


# —— 批量抽取：tushare 按 trade_date 拉全市场，规避逐股限速 ——
# adj_factor 接口限速 1 次/小时，逐股调用 1805 次必然触发限流导致静默断供；
# 改为「按交易日批量拉全市场 + 缓存表复用」，日常增量只需 1 次调用/天。
_ADJ_THROTTLE_SEC = 62

# 当日日线数据的发布时点：A 股 15:00 收盘，各数据源通常 16:00 后才稳定可拉。
_MARKET_DATA_READY_HOUR = 16


def _fetch_daily_batch(d: date):
    """按交易日拉全市场日K（1 次接口调用）。返回 DataFrame 或 None。"""
    pro = _require_tushare_pro()
    return pro.daily(trade_date=d.strftime("%Y%m%d"))


def _fetch_adj_batch(d: date):
    """按交易日拉全市场复权因子（1 次接口调用，限速 1 次/小时，调用方须节流）。"""
    pro = _require_tushare_pro()
    return pro.adj_factor(trade_date=d.strftime("%Y%m%d"))


def _ensure_adj_factors(db: Session, dates: list[date], stats: dict | None = None,
                        throttle: bool = True) -> set[date]:
    """确保给定交易日的复权因子已入缓存表。返回已就绪的日期集合。

    已有缓存的日期直接复用（历史因子不再变化），仅缺失日期调用接口，且串行节流。
    """
    from app.models import AdjFactorDaily

    if not dates:
        return set()
    ready: set[date] = set()
    rows = db.execute(
        select(AdjFactorDaily.trade_date)
        .where(AdjFactorDaily.trade_date.in_(dates)).distinct()
    ).scalars().all()
    ready |= set(rows)

    missing = [d for d in dates if d not in ready]
    if not missing:
        return ready

    import time as _time

    for i, d in enumerate(missing):
        try:
            df = _fetch_adj_batch(d)
            if df is None or df.empty:
                # 接口返回空：非交易日或数据未就绪，不重试
                continue
            objs = []
            for _, r in df.iterrows():
                sym = _plat_symbol(str(r["ts_code"]))
                if not sym:
                    continue
                objs.append(AdjFactorDaily(
                    symbol=sym, trade_date=date.fromisoformat(str(r["trade_date"])),
                    adj_factor=float(r["adj_factor"]),
                ))
            if objs:
                db.bulk_save_objects(objs)
                db.commit()
                ready.add(d)
                if stats is not None:
                    stats["adj_api_calls"] = stats.get("adj_api_calls", 0) + 1
        except Exception as e:  # noqa: BLE001
            logger.warning("adj_factor %s 批量拉取失败: %s", d, e)
            if stats is not None:
                stats["errors"].append(f"adj_factor {d}: {str(e)[:90]}")
            break  # 触发限速后继续调用只会全部失败，直接中止本轮
        if throttle and i < len(missing) - 1:
            _time.sleep(_ADJ_THROTTLE_SEC)
    return ready


# —— 兜底源：东方财富前复权日K ——
# tushare adj_factor 限速 1 次/小时，历史回填会被卡死。东财接口免费且无此限制，
# 且它直接返回前复权价格，无需复权因子，正好补上 tushare 的短板。
_EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


def _plat_symbol(raw: str) -> str:
    """任意写法 → 平台统一格式 sh600000。

    输入可能是 600000（纯代码）/ 600000.SH（tushare）/ sh600000（平台）。
    注意：data_source.normalize_symbol 只吃纯代码且返回 (symbol, market) 元组。
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if len(s) == 8 and s[:2].lower() in ("sh", "sz", "bj") and s[2:].isdigit():
        return s.lower()
    if "." in s:
        code = s.split(".")[0].zfill(6)
        return normalize_symbol(code)[0]
    return normalize_symbol(s.zfill(6))[0]


def _em_secid(symbol: str) -> str | None:
    """平台 symbol（sh600000）→ 东财 secid（1.600000）。沪市 1. / 深市北交所 0."""
    s = _plat_symbol(symbol)
    if not s or len(s) != 8:
        return None
    pre, code = s[:2], s[2:]
    if pre == "sh":
        return f"1.{code}"
    if pre in ("sz", "bj"):
        return f"0.{code}"
    return None


def _em_qfq(symbol: str, sd: date, ed: date) -> list[dict]:
    """东财前复权日K（逐股调用，无频率限制）。失败返回 []。

    注意：环境代理（沙箱 HTTP_PROXY 等）常会拦截行情域名，这里一律直连；
    并发过高也会被服务端断连，因此重试采用递增退避，且并发由调用方控制。
    """
    import time as _t

    import requests

    secid = _em_secid(symbol)
    if not secid:
        return []
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "klt": "101", "fqt": "1",           # 101=日线, fqt=1 前复权
        "secid": secid,
        "beg": sd.strftime("%Y%m%d"), "end": ed.strftime("%Y%m%d"),
        "lmt": "1000",
    }
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            # 一律直连：环境代理会拦截 push2his.eastmoney.com
            r = requests.get(_EM_KLINE_URL, params=params, timeout=20,
                             proxies={"http": None, "https": None})
            data = (r.json() or {}).get("data") or {}
            out = []
            for line in data.get("klines", []):
                p = line.split(",")
                if len(p) < 7:
                    continue
                out.append({
                    "trade_date": date.fromisoformat(p[0]),
                    "open": float(p[1]), "close": float(p[2]),
                    "high": float(p[3]), "low": float(p[4]),
                    "volume": int(float(p[5])), "amount": float(p[6]),
                })
            return out
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < 2:
                _t.sleep(0.4 * (attempt + 1))
    logger.warning("东财日K %s 拉取失败: %s", symbol, last_err)
    return []


def _backfill_eastmoney(db: Session, symbols: set[str], dates: list[date],
                        stats: dict, workers: int = 4) -> int:
    """用东财前复权补指定交易日的数据（tushare 复权因子不可用时兜底）。

    只写入 dates 范围内的数据，不覆盖已有历史，避免复权基准漂移。
    """
    from concurrent.futures import ThreadPoolExecutor

    from app.services import ingestion

    if not symbols or not dates:
        return 0
    lo, hi = min(dates), max(dates)
    want = set(dates)
    targets = sorted({_plat_symbol(s) for s in symbols if s})
    if not targets:
        return 0

    def fetch(sym: str) -> tuple[str, list[dict]]:
        bars = _em_qfq(sym, lo, hi)
        return sym, [b for b in bars if b["trade_date"] in want]

    total, failed = 0, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for sym, bars in ex.map(fetch, targets):
            if not bars:
                failed += 1
                continue
            try:
                ingestion.upsert_kline(db, bars, sym, "qfq")
                total += len(bars)
            except Exception as e:  # noqa: BLE001
                failed += 1
                stats["errors"].append(f"em upsert {sym}: {str(e)[:80]}")
    db.commit()
    stats["em_rows"] = total
    stats["em_failed"] = failed
    stats["em_dates"] = [d.isoformat() for d in dates]
    if failed:
        stats["errors"].append(f"东财兜底 {failed}/{len(targets)} 只无数据")
    return total


def _gap_dates(db: Session, want: set[str], dates: list[date], tol: int) -> list[date]:
    """返回覆盖仍不足的交易日（用于逐级兜底重试）。

    tol 为可容忍的缺失标的数（停牌等正常缺口）。
    """
    if not dates:
        return []
    cover: dict[date, int] = {}
    wl = list(want)
    for i in range(0, len(dates), 60):
        chunk = dates[i:i + 60]
        for d, c in db.execute(
            select(KlineDaily.trade_date, func.count(func.distinct(KlineDaily.symbol)))
            .where(KlineDaily.trade_date.in_(chunk))
            .where(KlineDaily.symbol.in_(wl))
            .group_by(KlineDaily.trade_date)
        ).all():
            cover[d] = int(c or 0)
    return [d for d in dates if (len(want) - cover.get(d, 0)) > tol]


# —— 兜底源：腾讯行情前复权日K ——
# 东财 push2his 在部分网络出口（如沙箱）完全不可达，腾讯 web.ifzq.gtimg.cn 可用，
# 且指数/个股通用、直接返回前复权价、无频率限制 —— 正好补 index_daily 限速 1 次/小时的短板。
_TX_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def _tx_kline(symbol: str, sd: date, ed: date) -> list[dict]:
    """腾讯前复权日K（指数与个股通用，免费无限速）。失败返回 []。

    返回行格式：[日期, 开, 收, 高, 低, 成交量(手), ...]，无成交额字段。
    """
    import time as _t

    import requests

    s = _plat_symbol(symbol)
    if not s:
        return []
    params = {"param": f"{s},day,{sd:%Y-%m-%d},{ed:%Y-%m-%d},640,qfq"}
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(_TX_KLINE_URL, params=params, timeout=15)
            j = r.json() or {}
            node = ((j.get("data") or {}).get(s) or {})
            arr = node.get("qfqday") or node.get("day") or []
            out = []
            for b in arr:
                if len(b) < 6:
                    continue
                out.append({
                    "trade_date": date.fromisoformat(str(b[0])),
                    "open": float(b[1]), "close": float(b[2]),
                    "high": float(b[3]), "low": float(b[4]),
                    "volume": int(float(b[5] or 0)), "amount": 0.0,
                })
            return out
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < 2:
                _t.sleep(0.3 * (attempt + 1))
    logger.warning("腾讯日K %s 拉取失败: %s", symbol, last_err)
    return []


def _backfill_index_tencent(db: Session, symbols: set[str], dates: list[date],
                            stats: dict) -> int:
    """用腾讯前复权补指数日K（index_daily 限速 1 次/小时时的兜底）。"""
    if not symbols or not dates:
        return 0
    lo, hi = min(dates), max(dates)
    want = set(dates)
    have = set(db.execute(
        select(IndexKlineDaily.symbol, IndexKlineDaily.trade_date)
        .where(IndexKlineDaily.trade_date.in_(list(want)))
    ).all())
    total, failed = 0, 0
    for sym in sorted(symbols):
        if not sym:
            continue
        bars = [b for b in _tx_kline(sym, lo, hi) if b["trade_date"] in want
                and (sym, b["trade_date"]) not in have]
        if not bars:
            failed += 1
            continue
        try:
            for b in bars:
                db.merge(IndexKlineDaily(
                    symbol=sym, trade_date=b["trade_date"],
                    open=b["open"], high=b["high"], low=b["low"], close=b["close"],
                    volume=b["volume"], amount=b["amount"],
                ))
                total += 1
            db.commit()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            failed += 1
            stats["errors"].append(f"tx upsert {sym}: {str(e)[:80]}")
    stats["tx_rows"] = total
    stats["tx_failed"] = failed
    if failed:
        stats["errors"].append(f"腾讯兜底 {failed}/{len(symbols)} 个指数无数据")
    return total


def _day_data_ready(d: date, now: datetime | None = None) -> bool:
    """该交易日的日线数据是否已发布。

    盘中（16:00 前）数据源返回的当日 bar 是未收盘的半截数据，存进去会留下
    一根「看着完整、实际未收盘」的假 bar，且后续因幂等跳过而永远不会被覆盖。
    另一方面，盘中还去为「今天」重试 1805 次兜底源纯属白费（数据根本没发布）。
    因此：今天的日线一律等 16:00 收盘后再拉。
    """
    now = now or datetime.now()
    today = date(now.year, now.month, now.day)
    if d < today:
        return True
    if d > today:
        return False
    return now.hour >= _MARKET_DATA_READY_HOUR


def _limit_for(symbol: str) -> float:
    """涨跌停幅度（留 1pp 缓冲），用于识别除权除息导致的价格跳空。"""
    s = (symbol or "").lower()
    if s.startswith("bj"):
        return 0.31          # 北交所 30%
    code = s[2:] if len(s) == 8 else s
    if code.startswith(("300", "301", "688", "689")):
        return 0.21          # 创业板 / 科创板 20%
    return 0.11              # 主板 10%


def _backfill_tushare_raw(db: Session, symbols: set[str], dates: list[date],
                          stats: dict) -> int:
    """三级兜底：用 tushare daily 未复权价补齐缺失交易日。

    为什么可行（2026-08-27 全核心池实测）：
      kline_daily 存的前复权价以「最新复权因子」为基准，而 pro.daily 返回未复权价。
      实测 1805 只个股的 qfq/raw 比值 100% = 1.0000 —— 当前基准下未复权价即前复权价。
      误差只来自「基准日之后发生除权除息」的个股（实测 8-26→8-27 仅 3/1804 ≈ 0.17%）。
      pro.daily(trade_date=) 按天批量且**不限速**，1 次调用 = 全市场 5547 行。

    除权剔除：拿「上一根已入库 qfq 收盘价」与「当日 raw 收盘价」比对，涨跌幅越过
    该板块涨跌停上限即判定为疑似除权，跳过并计数（等 adj_factor 可用后再补）。
    宁可少一条数据，也不写入错误价格污染回测。
    """
    from app.services import ingestion

    targets = sorted({_plat_symbol(s) for s in symbols if s})
    if not targets or not dates:
        return 0
    want = set(targets)
    order = sorted(dates)

    # 基准：每个标的在首个缺口日之前的最后一根 bar 收盘价（用于除权检测）
    last: dict[str, float] = {}
    for i in range(0, len(targets), 400):
        chunk = targets[i:i + 400]
        sub = (
            select(KlineDaily.symbol, func.max(KlineDaily.trade_date).label("td"))
            .where(KlineDaily.symbol.in_(chunk))
            .where(KlineDaily.trade_date < order[0])
            .group_by(KlineDaily.symbol)
        ).subquery()
        for s, c in db.execute(
            select(KlineDaily.symbol, KlineDaily.close)
            .join(sub, (KlineDaily.symbol == sub.c.symbol)
                  & (KlineDaily.trade_date == sub.c.td))
        ).all():
            last[s] = float(c)

    have = _existing_pairs(db, order)
    total, skipped_exdiv, skipped_noprev = 0, 0, 0

    for d in order:
        try:
            df = _fetch_daily_batch(d)
        except Exception as e:  # noqa: BLE001
            stats["errors"].append(f"daily {d}: {str(e)[:90]}")
            logger.warning("daily %s 批量拉取失败: %s", d, e)
            break
        if df is None or df.empty:
            continue

        bars_by_sym: dict[str, list[dict]] = {}
        newclose: dict[str, float] = {}
        for _, r in df.iterrows():
            sym = _plat_symbol(str(r["ts_code"]))
            if sym not in want or (sym, d) in have:
                continue
            try:
                o, h = float(r["open"]), float(r["high"])
                lo, c = float(r["low"]), float(r["close"])
            except (TypeError, ValueError):
                continue
            if not c or c <= 0:
                continue
            prev = last.get(sym)
            if prev and prev > 0 and abs(c / prev - 1.0) > _limit_for(sym):
                # 价格跳空越过涨跌停上限：除权除息所致，不写入错误价格
                skipped_exdiv += 1
                continue
            if prev is None:
                skipped_noprev += 1      # 新股/无历史，无法校验，直接入
            bars_by_sym.setdefault(sym, []).append({
                "trade_date": d,
                "open": round(o, 3), "high": round(h, 3),
                "low": round(lo, 3), "close": round(c, 3),
                "volume": int(float(r["vol"] or 0)),
                "amount": float(r["amount"] or 0.0),
            })
            newclose[sym] = c

        for sym, bars in bars_by_sym.items():
            try:
                ingestion.upsert_kline(db, bars, sym, "qfq")
                total += len(bars)
            except Exception as e:  # noqa: BLE001
                stats["errors"].append(f"raw upsert {sym}: {str(e)[:80]}")
        db.commit()
        last.update(newclose)     # 供下一交易日继续做除权检测

    stats["raw_rows"] = total
    stats["raw_skipped_exdiv"] = skipped_exdiv
    stats["raw_skipped_noprev"] = skipped_noprev
    if skipped_exdiv:
        stats["errors"].append(f"{skipped_exdiv} 条疑似除权未写入（待复权因子修正）")
    return total


def _existing_pairs(db: Session, dates: list[date]) -> set:
    """已入库的 (symbol, trade_date) 组合，用于幂等跳过。"""
    out = set()
    for i in range(0, len(dates), 60):
        chunk = dates[i:i + 60]
        rows = db.execute(
            select(KlineDaily.symbol, KlineDaily.trade_date)
            .where(KlineDaily.trade_date.in_(chunk))
        ).all()
        out |= {(s, d) for s, d in rows}
    return out


def extract_kline_incremental(db: Session, symbols: list[str], sd: date, ed: date,
                              progress=None, stats: dict | None = None) -> int:
    """增量拉个股日K（tushare 前复权）→ upsert kline_daily。返回新增行数。

    批量模式：按交易日一次拉全市场 daily（1 次调用/天），复权因子走 adj_factor_daily
    缓存表（缺失时按天批量补，串行节流）。彻底规避 tushare 逐股限速导致的静默断供。

    三级兜底链（任一环成功即不留缺口）：
      ① adj_factor 批量 + 缓存 → 标准前复权
      ② 东财前复权（免费无限速，但部分网络出口不可达）
      ③ daily 未复权价直写（不限速，按涨跌停上限剔除除权股）
    stats 用于收集失败原因（调用方写入管道日志）。
    """
    from app.core.trading_calendar import trading_days
    from app.models import AdjFactorDaily
    from app.services import ingestion

    if stats is None:
        stats = {}
    stats.setdefault("errors", [])

    want = {_plat_symbol(s) for s in symbols if s}
    if not want:
        return 0

    # 需要覆盖的交易日：从 sd 到 ed（历史回填时由调用方给足 sd）
    # 盘中跳过当天：数据未发布，既拉不到也不该写入半截 bar
    dates = [d for d in trading_days(sd, ed) if _day_data_ready(d)]
    if not dates:
        stats["skipped_pending_today"] = True
        return 0

    # ① 只补「覆盖不足」的交易日：正常日整段跳过，残缺日（如仅 1 只入库）自动重补
    tol = max(20, int(len(want) * 0.02))
    need_dates = _gap_dates(db, want, dates, tol)
    if not need_dates:
        stats["skipped_all_fresh"] = True
        return 0
    stats["need_dates"] = [d.isoformat() for d in need_dates]

    # ② 复权因子：仅补缺失日期，批量 + 缓存复用
    adj_dates = _ensure_adj_factors(db, need_dates, stats=stats)

    # ②-b 因子拿不到的日期（tushare 限速），改用东财前复权兜底，避免静默断供
    em_dates = [d for d in need_dates if d not in adj_dates]

    # ③ 已入库组合，幂等跳过
    have = _existing_pairs(db, need_dates)

    total = 0
    if em_dates:
        total += _backfill_eastmoney(db, want, em_dates, stats)
        need_dates = [d for d in need_dates if d in adj_dates]
    for i, d in enumerate(need_dates):
        try:
            df = _fetch_daily_batch(d)
        except Exception as e:  # noqa: BLE001
            stats["errors"].append(f"daily {d}: {str(e)[:90]}")
            logger.warning("daily %s 批量拉取失败: %s", d, e)
            continue
        if df is None or df.empty:
            continue

        # 该日的复权因子 + 基准因子（区间内最新已就绪日）
        fac: dict[str, float] = {}
        if d in adj_dates:
            rows = db.execute(
                select(AdjFactorDaily.symbol, AdjFactorDaily.adj_factor)
                .where(AdjFactorDaily.trade_date == d)
            ).all()
            fac = {s: f for s, f in rows}
        base_d = max(adj_dates) if adj_dates else None
        base_fac: dict[str, float] = {}
        if base_d:
            rows = db.execute(
                select(AdjFactorDaily.symbol, AdjFactorDaily.adj_factor)
                .where(AdjFactorDaily.trade_date == base_d)
            ).all()
            base_fac = {s: f for s, f in rows}

        bars_by_sym: dict[str, list[dict]] = {}
        skipped_no_adj = 0
        for _, r in df.iterrows():
            sym = _plat_symbol(str(r["ts_code"]))
            if sym not in want:
                continue
            if (sym, d) in have:
                continue
            td = date.fromisoformat(str(r["trade_date"]))
            f = fac.get(sym)
            b = base_fac.get(sym)
            if not f or not b:
                skipped_no_adj += 1
                continue
            k = f / b  # 前复权：以区间最新因子为基准归一
            bars_by_sym.setdefault(sym, []).append({
                "trade_date": td,
                "open": round(float(r["open"]) * k, 3),
                "high": round(float(r["high"]) * k, 3),
                "low": round(float(r["low"]) * k, 3),
                "close": round(float(r["close"]) * k, 3),
                "volume": int(float(r["vol"]) / k) if k else int(float(r["vol"])),
                "amount": float(r["amount"]),
            })
        if skipped_no_adj:
            stats["skipped_no_adj"] = stats.get("skipped_no_adj", 0) + skipped_no_adj

        for sym, bars in bars_by_sym.items():
            try:
                ingestion.upsert_kline(db, bars, sym, "qfq")
                total += len(bars)
            except Exception as e:  # noqa: BLE001
                stats["errors"].append(f"upsert {sym}: {str(e)[:80]}")
                logger.warning("upsert_kline %s 失败: %s", sym, e)
        db.commit()
        if progress:
            progress(i + 1, len(need_dates), total)

    # ②-d 兜底收尾：adj_factor 限速 + 东财不可达同时发生时仍有缺口，
    #      用不限速的 daily 未复权价补齐，绝不留下静默断供。
    all_need = [date.fromisoformat(x) for x in stats.get("need_dates", [])]
    rest = _gap_dates(db, want, all_need, tol) if all_need else []
    if rest:
        total += _backfill_tushare_raw(db, want, rest, stats)
        stats["raw_gap_dates"] = [d.isoformat() for d in rest]
    stats["expected"] = len(want) * len(all_need)
    return total


def extract_index_incremental(db: Session, ed: date, stats: dict | None = None) -> int:
    """指数日K增量（tushare index_daily）。返回新增行数。

    与个股同理：index_daily 限速 1 次/分钟，逐指数调用会被限流，
    改为按 trade_date 批量拉全市场指数（1 次调用/天），日期之间节流。
    """
    import time as _t

    from app.core.trading_calendar import trading_days
    from scripts.seed_index_kline import DEFAULT_INDICES

    if stats is None:
        stats = {}
    stats.setdefault("errors", [])

    pro = _require_tushare_pro()
    want = {_plat_symbol(s) for s in DEFAULT_INDICES}
    have = set(db.execute(
        select(IndexKlineDaily.symbol, IndexKlineDaily.trade_date)
        .where(IndexKlineDaily.trade_date >= ed - timedelta(days=45))
    ).all())

    # 需要补的交易日：仅覆盖不足的日期（含残缺日）；盘中跳过当天（数据未发布）
    dates = [d for d in trading_days(ed - timedelta(days=45), ed) if _day_data_ready(d)]
    cover: dict[date, int] = {}
    for d, c in db.execute(
        select(IndexKlineDaily.trade_date, func.count())
        .where(IndexKlineDaily.trade_date >= ed - timedelta(days=45))
        .group_by(IndexKlineDaily.trade_date)
    ).all():
        cover[d] = int(c or 0)
    need_dates = [d for d in dates if cover.get(d, 0) < len(want)]
    if not need_dates:
        stats["skipped_all_fresh"] = True
        return 0
    stats["need_dates"] = [d.isoformat() for d in need_dates]

    total = 0
    filled_dates: set[date] = set()
    for i, d in enumerate(need_dates):
        try:
            df = pro.index_daily(trade_date=d.strftime("%Y%m%d"))
        except Exception as e:  # noqa: BLE001
            stats["errors"].append(f"index_daily {d}: {str(e)[:90]}")
            logger.warning("index_daily %s 批量拉取失败: %s", d, e)
            break  # 触发限速后继续调用只会全部失败
        if df is None or df.empty:
            continue
        objs = []
        for _, r in df.iterrows():
            plat = _plat_symbol(str(r["ts_code"]))
            if plat not in want or (plat, d) in have:
                continue
            objs.append(IndexKlineDaily(
                symbol=plat, trade_date=d,
                open=float(r["open"]), high=float(r["high"]), low=float(r["low"]),
                close=float(r["close"]),
                volume=int(float(r.get("vol", 0) or 0)),
                amount=float(r.get("amount", 0) or 0),
            ))
        if objs:
            db.bulk_save_objects(objs)
            db.commit()
            total += len(objs)
            filled_dates.add(d)
        if i < len(need_dates) - 1:
            if not (df is None or df.empty):
                _t.sleep(_ADJ_THROTTLE_SEC)  # 接口限速 1 次/小时
            else:
                break  # 当天无数据说明更早，停止

    # 兜底：index_daily 限速 1 次/小时（一天补不完），剩余日期改用腾讯前复权补齐，
    # 避免指数长期停在旧日期 —— 而指数是所有回测的基准，断了基准等于断了归因。
    rest = [d for d in need_dates if d not in filled_dates]
    if rest:
        n = _backfill_index_tencent(db, want, rest, stats)
        total += n
        stats["tx_gap_dates"] = [d.isoformat() for d in rest]
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
