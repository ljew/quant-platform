"""A股交易日历（调度器用：区分周末与法定节假日休市）。

数据优先级：
① 本地缓存 data/trading_calendar.json（在线拉取成功后写入）
② 在线拉取 akshare tool_trade_date_hist_sina（完整交易日序列，1990 至今）
③ 内置静态节假日表（兜底，覆盖 2020-2026 主要休市日）

注意：内置表为人工整理，若无法在线刷新，节假日判断以交易所公告为准。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_CACHE_PATH = os.path.join(_PROJECT_ROOT, "data", "trading_calendar.json")

# 内置兜底：A股法定节假日休市日（不含周末；周末已单独判断）
_HOLIDAYS: set[str] = {
    # 2020
    "2020-01-01", "2020-01-24", "2020-01-27", "2020-01-28", "2020-01-29", "2020-01-30",
    "2020-04-06", "2020-05-01", "2020-05-04", "2020-05-05", "2020-06-25", "2020-06-26",
    "2020-10-01", "2020-10-02", "2020-10-05", "2020-10-06", "2020-10-07", "2020-10-08",
    # 2021
    "2021-01-01", "2021-02-11", "2021-02-12", "2021-02-15", "2021-02-16", "2021-02-17",
    "2021-04-05", "2021-05-03", "2021-05-04", "2021-05-05", "2021-06-14",
    "2021-09-20", "2021-09-21", "2021-10-01", "2021-10-04", "2021-10-05", "2021-10-06", "2021-10-07",
    # 2022
    "2022-01-03", "2022-01-31", "2022-02-01", "2022-02-02", "2022-02-03", "2022-02-04",
    "2022-04-04", "2022-04-05", "2022-05-02", "2022-05-03", "2022-05-04", "2022-06-03",
    "2022-09-12", "2022-10-03", "2022-10-04", "2022-10-05", "2022-10-06", "2022-10-07",
    # 2023
    "2023-01-02", "2023-01-23", "2023-01-24", "2023-01-25", "2023-01-26", "2023-01-27",
    "2023-04-05", "2023-05-01", "2023-05-02", "2023-05-03", "2023-06-22", "2023-06-23",
    "2023-09-29", "2023-10-02", "2023-10-03", "2023-10-04", "2023-10-05", "2023-10-06",
    # 2024
    "2024-01-01", "2024-02-09", "2024-02-12", "2024-02-13", "2024-02-14", "2024-02-15", "2024-02-16",
    "2024-04-04", "2024-04-05", "2024-05-01", "2024-05-02", "2024-05-03",
    "2024-06-10", "2024-09-16", "2024-09-17", "2024-10-01", "2024-10-02", "2024-10-03",
    "2024-10-04", "2024-10-07",
    # 2025
    "2025-01-01", "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
    "2025-02-03", "2025-02-04", "2025-04-04", "2025-05-01", "2025-05-02", "2025-05-05",
    "2025-06-02", "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06", "2025-10-07", "2025-10-08",
    # 2026
    "2026-01-01", "2026-01-02", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-23", "2026-02-24", "2026-04-06", "2026-04-07",
    "2026-05-01", "2026-05-04", "2026-05-05", "2026-06-19",
    "2026-09-25", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
}

_trade_dates: set[date] | None = None
_holiday_set: set[date] | None = None
_loaded = False


def _load() -> None:
    """加载日历：缓存 JSON → 在线拉取 → 内置兜底。"""
    global _loaded, _trade_dates, _holiday_set
    if _loaded:
        return
    _loaded = True
    # ① 本地缓存
    if os.path.exists(_CACHE_PATH):
        try:
            with open(_CACHE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("trade_dates"):
                _trade_dates = {date.fromisoformat(x) for x in data["trade_dates"]}
                return
        except Exception:  # noqa: BLE001
            pass
    # ② 在线拉取 akshare 完整交易日序列
    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        dates = [str(x).split(" ")[0] for x in df["trade_date"].tolist()]
        _trade_dates = {date.fromisoformat(x) for x in dates}
        try:
            os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
            with open(_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump({"trade_dates": sorted(x.isoformat() for x in _trade_dates), "source": "akshare"}, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
        return
    except Exception:  # noqa: BLE001
        pass
    # ③ 内置兜底
    _holiday_set = {date.fromisoformat(x) for x in _HOLIDAYS}


def is_trading_day(d: date | datetime) -> bool:
    """是否为 A 股交易日（非周末 且 非法定节假日休市）。"""
    if isinstance(d, datetime):
        d = d.date()
    _load()
    if _trade_dates is not None:
        return d in _trade_dates
    if d.weekday() >= 5:
        return False
    return d not in (_holiday_set or set())


def trading_days(sd: date, ed: date) -> list[date]:
    """区间内交易日列表（含边界）。"""
    out = []
    cur = sd
    while cur <= ed:
        if is_trading_day(cur):
            out.append(cur)
        cur += __import__("datetime").timedelta(days=1)
    return out
