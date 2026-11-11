"""表级新鲜度：88 张表逐张看「最新数据是哪天、落后几天」。

为什么要有这个：`/monitor/status` 里的 `_freshness` 只看 4 张关键表
（K线/指数/因子/成分）。但出问题时往往是别的表 —— 比如 `articles` 停更、
`signal_daily` 没写、`stock_valuation_daily` 断档。只有逐张看才发现。

三条口径（对应设计文档第 11 节「数据健康」）：

  ① **日期列自动识别**：优先 trade_date → date → as_of → ann_date →
     created_at / updated_at / publish_time。认不出的表标 `n/a`，
     不硬猜（猜错会误报，比不报更糟）。
  ② **时刻感知**：交易日 19:00 前，当天 ETL 还没跑，允许 1 天滞后。
     否则每天上午打开页面都会看到一片红。
  ③ **空表单列**：0 行的表不算陈旧，但要让人看见 —— 建了表没数据，
     通常是采集步骤静默失败。
"""

from __future__ import annotations

import time
from datetime import date, datetime

from sqlalchemy import text

# 优先级顺序：越靠前越可信
_DATE_COLS = ("trade_date", "date", "as_of", "ann_date",
              "created_at", "updated_at", "publish_time")

# 缓存：86 张表逐张查要 1~2 秒，页面轮询别每次都扫
_CACHE: dict = {"at": 0.0, "data": None}
_TTL = 60.0


def _grace_days(now: datetime) -> int:
    """交易日 19:00 前，允许 1 天滞后（当天数据还没生成）。"""
    try:
        from app.core.trading_calendar import is_trading_day
        if is_trading_day(now.date()) and now.hour < 19:
            return 1
    except Exception:  # noqa: BLE001
        pass
    return 0


def _pick_col(cols: set[str]) -> str | None:
    for c in _DATE_COLS:
        if c in cols:
            return c
    return None


def _latest_value(db, table: str, col: str):
    """取某列最大值。publish_time 是秒级 unix，要转日期。"""
    row = db.execute(text(f'select max("{col}") from "{table}"')).scalar()
    if row is None:
        return None
    if col == "publish_time":
        try:
            return datetime.fromtimestamp(int(row)).date().isoformat()
        except (TypeError, ValueError, OSError):
            return None
    return str(row)[:10]


def _classify(table: str, col: str, rows: int) -> tuple[str, int, int]:
    """按列语义定阈值，返回 (类型, WARN 阈值, FAIL 阈值)。

    一刀切会误报：`llm_providers` 54 天没动是**正常**（配置表），
    `fund_hold_coverage` 292 天也是正常（基金半年报）。
    误报多了就没人看这个页面了 —— 所以宁可分档，不要一片红。
    """
    if col == "ann_date":
        return "财报类（半年/年度）", 400, 800
    if col in ("created_at", "updated_at"):
        if rows < 50:
            return "config", 0, 0          # 配置/登记表：不参与判断
        return "事件/运行记录", 30, 90
    if "fund" in table or "financial" in table:
        return "基金/财务低频", 200, 400
    return "日频事实表", 1, 4


def table_freshness(db, use_cache: bool = True) -> dict:
    now = datetime.now()
    if use_cache and _CACHE["data"] and time.time() - _CACHE["at"] < _TTL:
        return _CACHE["data"]

    grace = _grace_days(now)
    names = db.execute(text(
        "select name from sqlite_master where type='table' "
        "and name not like 'sqlite_%' order by name"
    )).scalars().all()

    rows: list[dict] = []
    for name in names:
        info = db.execute(text(f'pragma table_info("{name}")')).fetchall()
        cols = {r[1] for r in info}
        col = _pick_col(cols)
        try:
            cnt = db.execute(text(f'select count(*) from "{name}"')).scalar() or 0
        except Exception:  # noqa: BLE001
            cnt = -1

        if cnt == 0:
            rows.append({"table": name, "date_col": col, "rows": 0,
                         "latest": None, "days_ago": None, "status": "EMPTY"})
            continue
        if not col:
            rows.append({"table": name, "date_col": None, "rows": cnt,
                         "latest": None, "days_ago": None, "status": "n/a"})
            continue

        latest = _latest_value(db, name, col)
        if not latest:
            rows.append({"table": name, "date_col": col, "rows": cnt,
                         "latest": None, "days_ago": None, "status": "n/a"})
            continue
        try:
            days = (now.date() - date.fromisoformat(latest)).days
        except ValueError:
            # 日期列里混进了非日期值（2026-11-09 的 '杨逸君' 事件）。原来这里
            # 降级成 n/a —— 页面上一片灰，看不出「这张表已经不可信」。
            # 脏值是硬故障：要么数据被写坏了，要么 _pick_col 把非日期列当成了
            # 日期列（fund 表的 manager 就是典型），两种都要红出来。
            rows.append({"table": name, "date_col": col, "rows": cnt,
                         "latest": None, "days_ago": None, "status": "FAIL",
                         "note": f"日期列 {col} 含非日期值（{str(latest)[:24]}）"})
            continue

        kind, warn_d, fail_d = _classify(name, col, cnt)
        if kind == "config":
            rows.append({"table": name, "date_col": col, "rows": cnt,
                         "latest": latest, "days_ago": days, "status": "n/a",
                         "note": "配置/登记表，不按数据新鲜度判断"})
            continue
        eff = days - grace
        if eff <= warn_d:
            status = "OK"
        elif eff <= fail_d:
            status = "WARN"
        else:
            status = "FAIL"
        rows.append({"table": name, "date_col": col, "rows": cnt,
                     "latest": latest, "days_ago": days, "status": status,
                     "note": f"阈值 {warn_d}/{fail_d} 天（{kind}）"})

    order = {"FAIL": 0, "WARN": 1, "EMPTY": 2, "OK": 3, "n/a": 4}
    rows.sort(key=lambda r: (order.get(r["status"], 9), -(r["days_ago"] or 0), r["table"]))

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    out = {
        "as_of": now.isoformat(timespec="seconds"),
        "grace_days": grace,
        "total": len(rows),
        "counts": counts,
        "worst": next((s for s in ("FAIL", "WARN", "EMPTY") if counts.get(s)), "OK"),
        "tables": rows,
    }
    _CACHE.update({"at": time.time(), "data": out})
    return out
