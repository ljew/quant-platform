"""PIT（Point-in-Time）时间定级 —— 知识库最容易出错、也最不能出错的一层。

规则（A 股，无夏令时，固定 UTC+8）：

    交易日 15:00:00 及之前发布  → trade_date_applicable = 当日
    交易日 15:00:00 之后发布    → trade_date_applicable = 次一交易日
    非交易日发布                → trade_date_applicable = 次一交易日

`available_at` 取**发布时刻本身**，即「信息对市场公开的时刻」。
刻意不用「我方抓取时刻」：抓晚了只说明采集链路的工程延迟，不代表当时拿不到；
若用抓取时刻做 PIT 基准，历史回填会把大量文档错误地推到抓取日，反而制造人为的数据缺失。
抓取延迟单列 `fetch_delay_sec`，只作为可行性指标供审计，不参与 PIT 判定。

时间戳口径：源数据 `publish_time` / `fetched_at` 是 epoch 秒，本地时间 = UTC + 8h
（已用 parquet 文件名的抓取时刻交叉验证）。
"""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd

from app.core import trading_calendar as tc

LOCAL_OFFSET = pd.Timedelta(hours=8)   # A 股：UTC+8，无夏令时
CUTOFF_SECONDS = 15 * 3600             # 15:00:00 收盘截面分界（精确到秒）
TABLE_CLOSE_HOUR = 15


def _trading_ordinals() -> np.ndarray:
    """交易日序列的 date.toordinal 数组（升序）。"""
    tc._load()
    if tc._trade_dates:
        return np.array(sorted(d.toordinal() for d in tc._trade_dates), dtype=np.int64)
    # 兜底：内置节假日表模式下，生成 2000–2035 的工作日序列
    out, d = [], date(2000, 1, 1)
    while d <= date(2035, 12, 31):
        if d.weekday() < 5 and not tc.is_trading_day(d):
            pass
        if tc.is_trading_day(d):
            out.append(d.toordinal())
        d += timedelta(days=1)
    return np.array(out, dtype=np.int64)


@lru_cache(maxsize=4)
def _day_map(start_ord: int, end_ord: int) -> dict[int, tuple[int, int]]:
    """{自然日序号: (盘前盘中发布→适用交易日, 盘后发布→适用交易日)}。

    覆盖 [start-1, end+40] 天，足够跨长假（最长春节休市 9 天）。
    """
    ords = _trading_ordinals()
    out: dict[int, tuple[int, int]] = {}
    for o in range(start_ord - 1, end_ord + 41):
        i = int(np.searchsorted(ords, o, side="left"))
        if i < len(ords):
            before = int(ords[i])
        else:  # 超出日历覆盖范围：按工作日兜底
            d = date.fromordinal(o)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            before = d.toordinal()
        j = int(np.searchsorted(ords, o + 1, side="left"))
        if j < len(ords):
            after = int(ords[j])
        else:
            d = date.fromordinal(o + 1)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            after = d.toordinal()
        out[o] = (before, after)
    return out


def to_local(ts: pd.Series) -> pd.Series:
    """epoch 秒 → 本地 naive datetime（UTC+8）。"""
    return pd.to_datetime(ts, unit="s") + LOCAL_OFFSET


def compute_pit(ts: pd.Series) -> pd.DataFrame:
    """给定发布时刻序列，返回 (available_at, trade_date_applicable)。

    available_at           : 'YYYY-MM-DD HH:MM:SS'（本地时间）
    trade_date_applicable  : 'YYYY-MM-DD'（应归入哪个交易日的截面）
    """
    local = to_local(pd.Series(ts).reset_index(drop=True))
    d_ser = local.dt.date
    ords = np.array([d.toordinal() for d in d_ser], dtype=np.int64)
    # 必须精确到秒：只用 hour*60+minute 会把 15:00:26 判成「15:00 前」，
    # 使盘后 26 秒发布的收盘播报泄漏进当日截面（实测 812 条，占 1.3%）。
    secs = (local.dt.hour * 3600 + local.dt.minute * 60 + local.dt.second).to_numpy()

    trading_set = set(np.unique(_trading_ordinals()).tolist())
    is_td = np.array([o in trading_set for o in ords], dtype=bool)

    lo, hi = int(ords.min()), int(ords.max())
    dmap = _day_map(lo, hi)

    before = np.array([dmap[o][0] for o in ords], dtype=np.int64)
    after = np.array([dmap[o][1] for o in ords], dtype=np.int64)

    # 交易日 15:00:00 及之前 → 当日（即 before）；其余（盘后 1 秒起/非交易日）→ after
    use_before = is_td & (secs <= CUTOFF_SECONDS)
    applicable_ord = np.where(use_before, before, after)

    return pd.DataFrame({
        "available_at": local.dt.strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date_applicable": [
            date.fromordinal(int(o)).isoformat() for o in applicable_ord
        ],
    })


def next_trading_day(d: date) -> date:
    """严格大于 d 的最近交易日。"""
    ords = _trading_ordinals()
    i = int(np.searchsorted(ords, d.toordinal(), side="right"))
    if i < len(ords):
        return date.fromordinal(int(ords[i]))
    nd = d + timedelta(days=1)
    while nd.weekday() >= 5:
        nd += timedelta(days=1)
    return nd


def check_no_leak(available_at: str, trade_date: str) -> bool:
    """PIT 泄漏检查：信号可用时刻必须严格早于该截面的决策时点。

    决策时点取 trade_date 收盘（15:00）。available_at 是本地时间字符串。
    """
    av = pd.to_datetime(available_at)
    td = pd.to_datetime(trade_date) + pd.Timedelta(hours=TABLE_CLOSE_HOUR)
    return bool(av <= td)


def now_local_str() -> str:
    """当前本地时间（'YYYY-MM-DD HH:MM:SS'）。"""
    return (pd.Timestamp.utcnow() + LOCAL_OFFSET).strftime("%Y-%m-%d %H:%M:%S")
