"""指数成分股时点快照存储（落库缓存）。

背景：`data_source.get_index_membership` 每次都在线拉 tushare，在线数据源
波动会导致同参数回测在不同时间跑出不同结果（不可复现）。本模块将月度快照
落库到 `index_membership` 表：回测时**优先读库**，缺失月份才在线拉取并回填，
之后完全离线可复现。

返回格式与 data_source.get_index_membership 一致：
    [(trade_date_str, {symbol,...}), ...]  升序
"""
from __future__ import annotations

import datetime as _dt
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IndexMembership


def _iter_months(sd: date, ed: date):
    """生成 (month_start, month_end) 序列（闭区间，覆盖 [sd, ed]）。"""
    y, m = sd.year, sd.month
    end_y, end_m = ed.year, ed.month
    while True:
        if m == 12:
            ny, nm = y + 1, 1
        else:
            ny, nm = y, m + 1
        month_end = date(ny, nm, 1) - timedelta(days=1)
        ms = date(y, m, 1)
        me = min(month_end, ed)
        yield ms, me
        if (y, m) == (end_y, end_m):
            break
        y, m = ny, nm


def _load_from_db(db: Session, index_code: str, sd: date, ed: date) -> dict[str, set]:
    rows = db.execute(
        select(IndexMembership.trade_date, IndexMembership.symbol)
        .where(
            IndexMembership.index_code == index_code,
            IndexMembership.trade_date >= sd - timedelta(days=40),
            IndexMembership.trade_date <= ed,
        )
        .order_by(IndexMembership.trade_date)
    ).all()
    snaps: dict[str, set] = {}
    for td, sym in rows:
        snaps.setdefault(td.isoformat(), set()).add(sym)
    return snaps


def _fetch_online(index_code: str, ms: date, me: date) -> tuple[str, set] | None:
    """在线拉取某自然月的成分快照（tushare index_weight 最后一个交易日）。"""
    from app.services import data_source

    try:
        pro = data_source._require_tushare_pro()
    except Exception:  # noqa: BLE001
        return None
    ts_code = f"{index_code}.SH"
    try:
        df = pro.index_weight(
            index_code=ts_code,
            start_date=ms.strftime("%Y%m%d"),
            end_date=me.strftime("%Y%m%d"),
        )
    except Exception:  # noqa: BLE001
        return None
    if df is None or df.empty or "trade_date" not in df.columns:
        return None
    latest = str(df["trade_date"].max())
    sub = df[df["trade_date"] == latest]
    s: set = set()
    for _, row in sub.iterrows():
        code = str(row["con_code"]).split(".")[0]
        try:
            sym, _ = data_source.normalize_symbol(code)
        except Exception:  # noqa: BLE001
            continue
        s.add(sym)
    if not s:
        return None
    return latest, s


def _persist(db: Session, index_code: str, trade_date: date, symbols: set[str]) -> None:
    """upsert 单月快照（按唯一约束去重）。"""
    existing = {
        r[0] for r in db.execute(
            select(IndexMembership.symbol).where(
                IndexMembership.index_code == index_code,
                IndexMembership.trade_date == trade_date,
            )
        ).all()
    }
    objs = [
        IndexMembership(index_code=index_code, trade_date=trade_date, symbol=sym)
        for sym in symbols
        if sym not in existing
    ]
    if objs:
        db.bulk_save_objects(objs)
        db.commit()


def get_membership(db: Session, index_code: str, sd: date, ed: date) -> list[tuple[str, set]]:
    """成分股时点快照：库优先，缺失月份在线拉取并回填。"""
    snaps = _load_from_db(db, index_code, sd, ed)
    for ms, me in _iter_months(sd, ed):
        # 若该月已有快照（或月末临近），跳过
        if any(s >= ms.isoformat() and s <= me.isoformat() for s in snaps):
            continue
        got = _fetch_online(index_code, ms, me)
        if got:
            latest, syms = got
            snaps[latest] = syms
            try:
                _persist(db, index_code, date.fromisoformat(latest), syms)
            except Exception:  # noqa: BLE001
                db.rollback()
    return sorted(snaps.items())


def has_local(index_code: str, sd: date, ed: date, db: Session) -> bool:
    """该指数在区间内是否已有本地快照（供 seed 判断）。"""
    n = db.execute(
        select(IndexMembership.id).where(
            IndexMembership.index_code == index_code,
            IndexMembership.trade_date >= sd - timedelta(days=40),
            IndexMembership.trade_date <= ed,
        ).limit(1)
    ).scalar()
    return n is not None
