"""数据资产清单：回答「现在到底有哪些数据、多少行、覆盖多少标的、更新到哪天、滞后多久」。

与 lineage（管道拓扑）互补：lineage 讲「数据怎么来的」，assets 讲「数据现在有多少、新不新鲜」。
统计走 SQL 聚合 + 120 秒内存缓存（大表 count distinct 较慢，页面 30s 刷新不会打爆 DB）。
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from app.core.trading_calendar import is_trading_day
from app.datahub.registry import RAW_DIR, SILVER_DIR
from app.models import (
    FactorDaily,
    FactorMinedDaily,
    FactorRegistry,
    FundamentalsHistory,
    IndexKlineDaily,
    IndexMembership,
    KlineDaily,
    NewsMarketDaily,
    NewsStockDaily,
    Stock,
)

_CACHE_TTL = 120
_cache: dict = {"ts": 0.0, "data": None}

# 数据集定义：key → (分组, 中文名, 模型, 日期列, 标的列, 允许的滞后交易日, 备注)
_DATASETS = [
    ("kline_daily", "行情", "个股日K（前复权）", KlineDaily, KlineDaily.trade_date, KlineDaily.symbol, 1, "核心池成分并集"),
    ("index_kline_daily", "行情", "指数日K", IndexKlineDaily, IndexKlineDaily.trade_date, IndexKlineDaily.symbol, 1, "8 个核心指数"),
    ("factor_daily", "因子", "基础因子", FactorDaily, FactorDaily.trade_date, FactorDaily.symbol, 1, "ETL 截面 14 因子"),
    ("factor_mined_daily", "因子", "挖掘因子（GP）", FactorMinedDaily, FactorMinedDaily.date, FactorMinedDaily.symbol, 3, "因子表达式引擎产出"),
    ("news_market_daily", "文本", "市场情绪（日）", NewsMarketDaily, NewsMarketDaily.date, None, 1, "全市场新闻/公告打分"),
    ("news_stock_daily", "文本", "个股情绪（日）", NewsStockDaily, NewsStockDaily.date, NewsStockDaily.symbol, 3, "个股提及与情绪"),
    ("stocks", "元数据", "股票列表与属性", Stock, Stock.updated_at, Stock.symbol, 7, "名称/行业/市值/估值"),
    # 财报按季度披露：一个季度约 65 个交易日 + 披露延迟缓冲
    ("fundamentals_history", "元数据", "财报历史", FundamentalsHistory, FundamentalsHistory.report_date, FundamentalsHistory.symbol, 75, "ROE/营收/净利同比"),
    ("index_membership", "元数据", "指数成分（PIT）", IndexMembership, IndexMembership.trade_date, IndexMembership.symbol, 30, "历史成分快照，防前视"),
]

_GROUPS = [
    ("行情", "行情数据", "日K 与指数，策略回测的主粮"),
    ("因子", "因子数据", "选股与归因的输入"),
    ("文本", "文本情绪", "新闻/公告/公众号情绪打分"),
    ("元数据", "元数据", "标的属性、财报、指数成分"),
]

# 数据源 → 其产出的落点数据集（用于在源卡片上显示「存量」）
_SOURCE_BIND = {
    "core_index_kline": "index_kline_daily",
    "stock_kline_core": "kline_daily",
    "stock_attributes": "stocks",
    "eastmoney_announcements": "news_stock_daily",
    "eastmoney_news": "news_market_daily",
    "wechat_articles": "__bronze_text__",
}


def _dir_stat(path: str) -> dict:
    files, size, latest = 0, 0, 0.0
    for root, _d, fs in os.walk(path):
        for f in fs:
            if f.endswith(".parquet"):
                fp = os.path.join(root, f)
                files += 1
                size += os.path.getsize(fp)
                latest = max(latest, os.path.getmtime(fp))
    return {"files": files, "size_mb": round(size / 1048576, 1),
            "latest": datetime.fromtimestamp(latest).isoformat(timespec="seconds") if latest else None}


def _to_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        s = str(v)[:10]
        return date.fromisoformat(s)
    except Exception:  # noqa: BLE001
        return None


# 当日行情数据的发布时点：A 股 15:00 收盘，tushare/akshare 的日线通常 16:00 后才稳定可拉。
_MARKET_DATA_READY_HOUR = 16


_PERIOD_DEADLINE = {"0331": (4, 30), "0630": (8, 31), "0930": (10, 31), "1231": (4, 30)}
_PERIOD_OF_QUARTER = {1: "0331", 2: "0630", 3: "0930", 4: "1231"}


def _expected_report_period(today: date | None = None) -> date | None:
    """按当前日期推算「理应已披露完毕」的最新报告期。

    财报按季披露且有法定截止日（一季报 4/30、中报 8/31、三季报 10/31、年报次年 4/30），
    所以「最新报告期」落后今天最多可达 一个季度 + 披露延迟 ≈ 110 个交易日。
    用固定天数阈值判定必然每季度误报一次 —— 改为直接比对理应披露的期次。
    """
    today = today or date.today()
    y, q = today.year, (today.month - 1) // 3 + 1
    for _ in range(8):
        period = _PERIOD_OF_QUARTER[q]
        mm, dd = _PERIOD_DEADLINE[period]
        if date(y + 1 if period == "1231" else y, mm, dd) <= today:
            return date(y, int(period[:2]), int(period[2:]))
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    return None


def _today_is_pending(now: datetime | None = None) -> bool:
    """当日是否「尚未到数据发布时点」。

    盘中（或收盘后不久）去拉 daily 只会拿到空/半截数据 —— 这是正常的，不算断供。
    不剔除的话，页面每天早上都会把「今天」误报为数据残缺，形成天天报警的狼来了。
    """
    now = now or datetime.now()
    if not is_trading_day(date(now.year, now.month, now.day)):
        return False
    return now.hour < _MARKET_DATA_READY_HOUR


def _expected_latest(today: date, now: datetime | None = None) -> date:
    """数据「理应更新到」哪一天。盘中今天的数据还没发布，基准回退一个交易日。"""
    if not _today_is_pending(now):
        return today
    cur = today - timedelta(days=1)
    for _ in range(10):
        if is_trading_day(cur):
            return cur
        cur -= timedelta(days=1)
    return today


def _lag_trading_days(start: date | None, end: date) -> int | None:
    """start 之后（不含）到 end 之间还有几个交易日 —— 即数据滞后了几个交易日。"""
    if start is None:
        return None
    if start >= end:
        return 0
    n, cur = 0, start + timedelta(days=1)
    while cur <= end:
        if is_trading_day(cur):
            n += 1
        cur += timedelta(days=1)
    return n


def _stat_table(db, model, date_col, sym_col):
    """一次 SQL 聚合出 行数 / 标的数 / 起止日期。"""
    cols = [func.count()]
    if sym_col is not None:
        cols.append(func.count(func.distinct(sym_col)))
    if date_col is not None:
        cols += [func.min(date_col), func.max(date_col)]
    try:
        row = db.execute(select(*cols).select_from(model)).one()
    except Exception:  # noqa: BLE001
        return {"rows": -1, "symbols": None, "start": None, "latest": None}
    rows = int(row[0] or 0)
    idx = 1
    symbols = None
    if sym_col is not None:
        symbols = int(row[idx] or 0)
        idx += 1
    start = latest = None
    if date_col is not None:
        start = _to_date(row[idx])
        latest = _to_date(row[idx + 1])
    return {"rows": rows, "symbols": symbols, "start": start, "latest": latest}


def _judge(status_rows: int, lag: int | None, max_lag: int, has_date: bool) -> str:
    if status_rows <= 0:
        return "empty"
    if not has_date or lag is None:
        return "ok"
    if lag <= max_lag:
        return "ok"
    if lag <= max_lag * 3:
        return "warn"
    return "stale"


def _recent_coverage(db, days: int = 12) -> dict:
    """最近 N 个自然日内，个股日K 每天覆盖了多少只股票。

    用途：识别「当天只有一小部分股票入库」的半截数据（抓取中途失败/限流），
    这类问题只看「最新日期」是看不出来的。
    """
    since = date.today() - timedelta(days=days)
    try:
        rows = db.execute(
            select(KlineDaily.trade_date, func.count(func.distinct(KlineDaily.symbol)))
            .where(KlineDaily.trade_date >= since)
            .group_by(KlineDaily.trade_date)
            .order_by(KlineDaily.trade_date)
        ).all()
    except Exception:  # noqa: BLE001
        return {"days": [], "median": 0, "partial_count": 0}

    pts = [{"date": d.isoformat(), "symbols": int(c or 0)} for d, c in rows]
    if not pts:
        return {"days": pts, "median": 0, "peak": 0, "partial_count": 0}
    counts = sorted(p["symbols"] for p in pts)
    median = counts[len(counts) // 2]
    # 基准用「近期最高覆盖」而非中位数：若断供多日，残缺样本会占多数并拉低中位数，
    # 导致残缺日被误判为正常（例如连续 5 天只抓到 80 只时中位数也是 80）。
    peak = counts[-1]
    # 今天盘中/收盘前的数据尚未发布，不算残缺，否则页面天天误报。
    pending_date = date.today().isoformat() if _today_is_pending() else None
    for p in pts:
        p["pending"] = p["date"] == pending_date
        p["partial"] = (not p["pending"]) and peak > 0 and p["symbols"] < peak * 0.8
    return {
        "days": pts,
        "median": median,
        "peak": peak,
        "partial_count": sum(1 for p in pts if p["partial"]),
        "pending_date": pending_date,
    }


def assets_report(db, force: bool = False) -> dict:
    now = time.time()
    if not force and _cache["data"] and now - _cache["ts"] < _CACHE_TTL:
        return _cache["data"]

    today = date.today()
    now_dt = datetime.now()
    # 盘中时当日数据尚未发布，滞后基准回退到上一个交易日，避免「今天没数据」被判为过期
    base = _expected_latest(today, now_dt)
    pending_today = base != today
    items: dict[str, dict] = {}

    for key, group, label, model, date_col, sym_col, max_lag, note in _DATASETS:
        st = _stat_table(db, model, date_col, sym_col)
        latest = st["latest"]
        lag_td = _lag_trading_days(latest, base) if latest else None
        lag_cd = (base - latest).days if latest else None
        status = _judge(st["rows"], lag_td, max_lag, date_col is not None)
        # 财报按季披露 + 法定截止日，用「理应披露的最新期次」判定，不用固定天数
        by_period = key == "fundamentals_history"
        if by_period:
            exp = _expected_report_period(today)
            status = "ok" if (exp and latest and latest >= exp) else (
                "empty" if st["rows"] <= 0 else "warn")
        items[key] = {
            "key": key,
            "group": group,
            "label": label,
            "rows": st["rows"],
            "symbols": st["symbols"],
            "start": st["start"].isoformat() if st["start"] else None,
            "latest": latest.isoformat() if latest else None,
            "lag_trading_days": lag_td,
            "lag_calendar_days": lag_cd,
            "max_lag": max_lag,
            "note": note,
            "status": status,
            # 按披露期次判定（而非滞后天数）——结论条里的「最滞后」应排除这类，
            # 否则财报永远是最滞后项，把真正的问题顶下去。
            "by_period": by_period,
        }

    # —— 因子注册表（非时序，单列统计）——
    try:
        total_reg = int(db.execute(select(func.count()).select_from(FactorRegistry)).scalar() or 0)
        enabled_reg = int(db.execute(select(func.count()).select_from(FactorRegistry)
                                     .where(FactorRegistry.status == "enabled")).scalar() or 0)
    except Exception:  # noqa: BLE001
        total_reg, enabled_reg = 0, 0
    items["factor_registry"] = {
        "key": "factor_registry", "group": "因子", "label": "因子注册表",
        "rows": enabled_reg, "symbols": total_reg,
        "start": None, "latest": None, "lag_trading_days": None,
        "lag_calendar_days": None, "max_lag": 0,
        "note": f"共登记 {total_reg} 个，启用 {enabled_reg} 个",
        "status": "ok" if enabled_reg else "empty",
    }

    # —— Bronze 原始语料（目录统计）——
    bronze_text = _dir_stat(os.path.join(RAW_DIR, "text"))
    bronze_market = _dir_stat(os.path.join(RAW_DIR, "market"))
    bronze_latest = max([x for x in [bronze_text["latest"], bronze_market["latest"]] if x] or [None])
    bt_dt = _to_date(bronze_latest)
    bronze_lag = _lag_trading_days(bt_dt, base) if bt_dt else None
    items["__bronze_text__"] = {
        "key": "__bronze_text__", "group": "文本", "label": "原始语料快照（Bronze）",
        "rows": bronze_text["files"] + bronze_market["files"],
        "symbols": None, "start": None,
        "latest": (bt_dt.isoformat() if bt_dt else None),
        "lag_trading_days": bronze_lag,
        "lag_calendar_days": ((base - bt_dt).days if bt_dt else None),
        "max_lag": 3,
        "note": f"Parquet 文件 · 文本 {bronze_text['size_mb']}MB / 行情 {bronze_market['size_mb']}MB",
        "status": _judge(bronze_text["files"] + bronze_market["files"], bronze_lag, 3, True),
    }
    silver = _dir_stat(SILVER_DIR)
    items["__silver__"] = {
        "key": "__silver__", "group": "文本", "label": "清洗后文本（Silver）",
        "rows": silver["files"], "symbols": None, "start": None,
        "latest": (_to_date(silver["latest"]).isoformat() if silver["latest"] else None),
        "lag_trading_days": (_lag_trading_days(_to_date(silver["latest"]), base)
                             if silver["latest"] else None),
        "lag_calendar_days": ((base - _to_date(silver["latest"])).days if silver["latest"] else None),
        "max_lag": 3, "note": f"清洗并打分后的 Parquet，共 {silver['size_mb']}MB",
        "status": _judge(silver["files"], _lag_trading_days(_to_date(silver["latest"]), base)
                         if silver["latest"] else None, 3, True),
    }

    # —— 分组输出 ——
    groups = []
    for key, label, desc in _GROUPS:
        gitems = [v for v in items.values() if v["group"] == key]
        # 组内排序：有问题的排前面
        order = {"empty": 0, "stale": 1, "warn": 2, "ok": 3}
        gitems.sort(key=lambda x: order.get(x["status"], 9))
        n_bad = sum(1 for x in gitems if x["status"] in ("stale", "empty"))
        groups.append({
            "key": key, "label": label, "desc": desc, "items": gitems,
            "rows": sum(x["rows"] for x in gitems if x["rows"] > 0),
            "bad": n_bad,
        })

    # —— 近期每日覆盖度（在结论之前算，供 verdict 引用）——
    coverage = _recent_coverage(db, days=12)

    # —— 顶层结论 ——
    kline = items["kline_daily"]
    news = items["news_market_daily"]
    # 财报的滞后是按季披露的结构性结果，不应占据「最滞后」位置
    dated = [v for v in items.values()
             if v.get("lag_trading_days") is not None and not v.get("by_period")]
    worst = max(dated, key=lambda x: x["lag_trading_days"]) if dated else None
    n_stale = sum(1 for v in items.values() if v["status"] == "stale")
    n_empty = sum(1 for v in items.values() if v["status"] == "empty")
    n_warn = sum(1 for v in items.values() if v["status"] == "warn")

    n_partial = coverage.get("partial_count", 0)
    if n_empty or n_stale:
        parts = []
        if n_stale:
            parts.append(f"{n_stale} 项已过期")
        if n_empty:
            parts.append(f"{n_empty} 项为空")
        if n_partial:
            parts.append(f"{n_partial} 个交易日数据残缺")
        verdict = "、".join(parts)
        verdict_level = "stale"
    elif n_warn:
        verdict = f"{n_warn} 项数据滞后（未更新到最新交易日）"
        verdict_level = "warn"
    else:
        verdict = "全部数据已更新至最新交易日"
        verdict_level = "ok"
    if n_partial and verdict_level == "ok":
        verdict = f"{n_partial} 个交易日数据残缺（抓取不完整）"
        verdict_level = "warn"

    summary = {
        "latest_trade_date": kline["latest"],
        "latest_news_date": news["latest"],
        "lag_trading_days": kline["lag_trading_days"],
        "lag_calendar_days": kline["lag_calendar_days"],
        "news_lag_trading_days": news["lag_trading_days"],
        "total_rows": sum(v["rows"] for v in items.values() if v["rows"] > 0),
        "symbols": kline["symbols"],
        "n_stale": n_stale, "n_warn": n_warn, "n_empty": n_empty,
        "n_total": len(items),
        "verdict": verdict,
        "verdict_level": verdict_level,
        "worst": ({"label": worst["label"], "latest": worst["latest"],
                   "lag": worst["lag_trading_days"]} if worst and worst["lag_trading_days"] else None),
        "n_partial_days": n_partial,
        # 盘中/收盘前：当日数据尚未发布，结论里显式说明，避免被误读成断供
        "pending_today": pending_today,
        "expected_latest": base.isoformat(),
    }

    # —— 近期每日覆盖度：暴露「某天只抓到一部分股票」的半截数据 ——
    by_source = {s: items[k] for s, k in _SOURCE_BIND.items() if k in items}

    data = {
        "generated_at": now_dt.isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "summary": summary,
        "groups": groups,
        "by_source": by_source,
        "coverage": coverage,
    }
    _cache["ts"] = now
    _cache["data"] = data
    return data
