"""数据资产清单：回答「现在到底有哪些数据、多少行、覆盖多少标的、更新到哪天、滞后多久」。

与 lineage（管道拓扑）互补：lineage 讲「数据怎么来的」，assets 讲「数据现在有多少、新不新鲜」。
统计走 SQL 聚合 + 120 秒内存缓存（大表 count distinct 较慢，页面 30s 刷新不会打爆 DB）。
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from app.core.trading_calendar import is_trading_day
from app.models import (
    FactorDaily,
    FactorMinedDaily,
    FactorRegistry,
    FundamentalsHistory,
    IndexKlineDaily,
    IndexMembership,
    KlineDaily,
    Stock,
)

_CACHE_TTL = 120
_cache: dict = {"ts": 0.0, "data": None}

# 数据集定义：key → (分组, 中文名, 模型, 日期列, 标的列, 允许的滞后交易日, 备注)
_DATASETS = [
    ("kline_daily", "行情", "个股日K（未复权）", KlineDaily, KlineDaily.trade_date, KlineDaily.symbol, 1, "全A 在市股票，未复权原始价"),
    ("index_kline_daily", "行情", "指数日K", IndexKlineDaily, IndexKlineDaily.trade_date, IndexKlineDaily.symbol, 1, "8 个核心指数"),
    ("factor_daily", "因子", "基础因子", FactorDaily, FactorDaily.trade_date, FactorDaily.symbol, 1, "ETL 截面 14 因子"),
    ("factor_mined_daily", "因子", "挖掘因子（GP）", FactorMinedDaily, FactorMinedDaily.date, FactorMinedDaily.symbol, 3, "因子表达式引擎产出"),
    ("stocks", "元数据", "股票列表与属性", Stock, Stock.updated_at, Stock.symbol, 7, "名称/行业/市值/估值"),
    # 财报按季度披露：一个季度约 65 个交易日 + 披露延迟缓冲
    ("fundamentals_history", "元数据", "财报历史", FundamentalsHistory, FundamentalsHistory.report_date, FundamentalsHistory.symbol, 75, "ROE/营收/净利同比"),
    ("index_membership", "元数据", "指数成分（PIT）", IndexMembership, IndexMembership.trade_date, IndexMembership.symbol, 30, "历史成分快照，防前视"),
]

_GROUPS = [
    ("行情", "行情数据", "日K 与指数，策略回测的主粮"),
    ("因子", "因子数据", "选股与归因的输入"),
    ("元数据", "元数据", "标的属性、财报、指数成分"),
    ("基金", "基金数据", "持仓穿透与 FOF 多层的原始披露，独立库按披露日历更新"),
]

# 数据源 → 其产出的落点数据集（用于在源卡片上显示「存量」）
# 说明：文本类数据源（eastmoney_news / eastmoney_announcements / wechat_articles）
# 仍在后台采集并累积，但暂不纳入资产清单与告警（非结构化因子暂时下线）。
_SOURCE_BIND = {
    "core_index_kline": "index_kline_daily",
    "stock_kline_core": "kline_daily",
    "stock_attributes": "stocks",
}


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


# ---------------------------------------------------------------- 基金链路
# 基金数据在独立库 data/fund_research.db。主库 2026-11-08 因「容器 + 本机同时写
# bind mount 上的 SQLite」损坏过（malformed），拆库之后基金重算的风险不再扩散到
# 全平台，代价是这里要多开一个连接做统计。
#
# 新鲜度必须按**披露日历**判定，不能按交易日：季报一年只有 4 次，用「滞后几个
# 交易日」会让页面一年 300 天飘红，形成狼来了。
FUND_DB = Path(os.getenv("QUANT_RESEARCH_DB", "/data/fund_research.db"))
_BIG_TABLE = 3_000_000     # 超过这个行数就不做 count(distinct)，太慢

# key, 标签, 表, 日期列, 标的列, 期次类型, 备注
_FUND_DATASETS = [
    ("fund_nav_daily", "基金净值日线", "fund_nav_daily", "nav_date", "ts_code", "daily",
     "逐基金日净值，回归校验的输入"),
    ("fund_hold_coverage", "基金持仓覆盖", "fund_hold_coverage", "end_date", "base_code", "quarter",
     "逐基金披露持仓（Q1/Q3 前十大、H1/FY 全量）"),
    ("fund_industry_exposure", "基金行业敞口", "fund_industry_exposure", "end_date", "base_code", "quarter",
     "申万一级穿透结果，含 carried 影子轨"),
    ("fund_csrc_industry", "监管行业配置", "fund_csrc_industry", "end_date", "base_code", "quarter",
     "证监会门类季度披露，仓位约束的权威锚"),
    ("fund_fof_holdings", "FOF 基金投资明细", "fund_fof_holdings", "end_date", "base_code", "half",
     "FOF 中报/年报 PDF 解析出的被投基金"),
    ("fund_fof_penetration", "FOF 行业穿透", "fund_fof_penetration", "end_date", "base_code", "half",
     "FOF→底层基金→行业，未穿透部分单列不估算"),
    ("fund_industry_regression", "净值回归校验", "fund_industry_regression", "end_date", "ts_code", "quarter",
     "约束 WLS 回归，独立校验穿透结果"),
]


def _expected_fof_period(today: date) -> date:
    """FOF 只披露中报(0630)与年报(1231)，取最近一个已过法定截止日的期次。"""
    y = today.year
    cands = [(date(y, 8, 31), date(y, 6, 30)), (date(y, 4, 30), date(y - 1, 12, 31))]
    ok = [p for dl, p in cands if dl <= today]
    return max(ok) if ok else date(y - 1, 6, 30)


def _fund_items(base: date, now_dt: datetime) -> list[dict]:
    """基金链路的资产项。库不存在/打不开时返回空列表，不影响主平台。"""
    if not FUND_DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{FUND_DB}?mode=ro", uri=True, timeout=10)
    except Exception:  # noqa: BLE001
        return []

    exp_q = _expected_report_period(base)
    exp_h = _expected_fof_period(base)
    out: list[dict] = []
    try:
        for key, label, table, dcol, scol, kind, note in _FUND_DATASETS:
            try:
                n = int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
                latest_raw = con.execute(f"SELECT MAX({dcol}) FROM {table}").fetchone()[0]
            except Exception:  # noqa: BLE001
                continue
            symbols = None
            if n and n <= _BIG_TABLE:
                try:
                    symbols = int(con.execute(
                        f"SELECT COUNT(DISTINCT {scol}) FROM {table}").fetchone()[0] or 0)
                except Exception:  # noqa: BLE001
                    symbols = None
            latest = _to_date(latest_raw)
            lag_td = _lag_trading_days(latest, base) if (latest and kind == "daily") else None
            lag_cd = (base - latest).days if latest else None

            exp = None if kind == "daily" else (exp_q if kind == "quarter" else exp_h)
            by_period = kind != "daily"
            if not by_period:
                status = _judge(n, lag_td, 1, True)
            elif n <= 0 or latest is None:
                status = "empty"
            elif exp is None or latest >= exp:
                status = "ok"
            elif (exp - latest).days <= 200:
                status = "warn"      # 落后一期，披露还没齐
            else:
                status = "stale"     # 落后两期及以上，说明采集断了
            out.append({
                "key": key, "group": "基金", "label": label,
                "rows": n, "symbols": symbols,
                "start": None,
                "latest": latest.isoformat() if latest else None,
                "lag_trading_days": lag_td,
                "lag_calendar_days": lag_cd,
                "max_lag": 1 if kind == "daily" else 0,
                "note": note,
                "status": status,
                "by_period": by_period,
                "expected": exp.isoformat() if exp else None,
            })
    finally:
        con.close()
    return out


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
    peak = counts[-1]
    # 基准 = 「日常批次的常态覆盖数」，不能直接用峰值：
    #   历史回填批次会一次性写入含退市/换血股的全量成分（如 2821 只），而日常增量
    #   只更新当前核心池（如 1803 只）—— 用峰值当基准会把结构性的正常差异误判成
    #   「6 个交易日数据残缺」，页面天天红、一键修复也永远修不好。
    # 也不能用中位数：断供多日时残缺样本占多数，会把中位数拉低导致漏报。
    # 取「出现频次最高的覆盖数」为常态值，并用 peak*0.5 兜底（防长期断供时众数本身
    # 就是残缺值）：
    #   正常：mode=1803, peak=2821 → 基准 1803（1803 的日子不再误报）
    #   少数几天断供：mode=1803 → 那几天的 80 只判残缺
    #   长期断供：mode=80, peak=1803 → 基准 901，80 只仍判残缺
    from collections import Counter

    mode = Counter(counts).most_common(1)[0][0]
    base_n = max(mode, int(peak * 0.5))
    # 今天盘中/收盘前的数据尚未发布，不算残缺，否则页面天天误报。
    pending_date = date.today().isoformat() if _today_is_pending() else None
    for p in pts:
        p["pending"] = p["date"] == pending_date
        p["partial"] = (not p["pending"]) and base_n > 0 and p["symbols"] < base_n * 0.8
    return {
        "days": pts,
        "median": median,
        "peak": peak,
        "normal": base_n,
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

    # —— 「没有启用中的挖掘因子」不是数据故障 ——
    # factor_mined_daily 的内容来自 factor_registry 里 status=enabled 的表达式。
    # 一条启用因子都没有时，compute_mined_factors 跑出来必然是 0 行，数据集就停在
    # 上次有因子的日期；旧逻辑把它判 stale、把注册表判 empty，于是页面长期挂着
    # 「2 项异常」，一键修复每次都跑一遍必然 0 行的管道，永远修不好 —— 它在修一个
    # 不存在的问题。2026-11-03 实测：注册表只有一条 disabled 的 test 因子。
    # 这里降级为 na（不参与 verdict、不进 _bad_datasets、不作为修复目标）。
    if not items.get("factor_registry", {}).get("rows"):
        reg = items.get("factor_registry")
        if reg:
            reg["status"] = "na"
            reg["note"] = f"共登记 {reg['symbols']} 个，启用 0 个（无启用因子，属正常状态）"
        mined = items.get("factor_mined_daily")
        if mined:
            mined["status"] = "na"
            mined["note"] = "无启用挖掘因子，无需更新"

    # —— 基金链路（独立库 fund_research.db，按披露日历判定新鲜度）——
    for _it in _fund_items(base, now_dt):
        items[_it["key"]] = _it

    # —— 分组输出 ——
    groups = []
    for key, label, desc in _GROUPS:
        gitems = [v for v in items.values() if v["group"] == key]
        # 组内排序：有问题的排前面
        order = {"empty": 0, "stale": 1, "warn": 2, "ok": 3, "na": 4}
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
    # 财报的滞后是按季披露的结构性结果，不应占据「最滞后」位置；
    # status=na（如无启用挖掘因子，属「不适用」）与 status=ok（已达标）同样不占位。
    # 「最滞后」只用于指路真正需要处理的数据集，否则会出现 verdict=ok、
    # 顶部却仍挂「最滞后：挖掘因子（GP）滞后 19 个交易日」的自相矛盾（2026-11-03）。
    dated = [v for v in items.values()
             if v.get("lag_trading_days") is not None
             and not v.get("by_period")
             and v["status"] not in ("na", "ok")]
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
        "lag_trading_days": kline["lag_trading_days"],
        "lag_calendar_days": kline["lag_calendar_days"],
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
