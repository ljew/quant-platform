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
import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IndexMembership

logger = logging.getLogger(__name__)


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


def _ts_code(index_code: str) -> str:
    """tushare 指数代码后缀：39 开头为深市（.SZ），其余默认沪市（.SH）。"""
    return f"{index_code}.SZ" if str(index_code).startswith("39") else f"{index_code}.SH"


def _fetch_tushare(index_code: str, ms: date, me: date) -> tuple[str, set] | None:
    """在线拉取某自然月的成分快照（tushare index_weight 最后一个交易日）。

    注意：index_weight 属于高权限接口（需较高积分）。2026-09 实测个人账户已
    无访问权限，因此本函数失败属正常，由后续兜底源接手。
    """
    from app.services import data_source

    try:
        pro = data_source._require_tushare_pro()
    except Exception:  # noqa: BLE001
        return None
    ts_code = _ts_code(index_code)
    try:
        df = pro.index_weight(
            index_code=ts_code,
            start_date=ms.strftime("%Y%m%d"),
            end_date=me.strftime("%Y%m%d"),
        )
    except Exception as _e:  # noqa: BLE001
        logger.debug("index_weight 不可用（%s %s~%s）: %s", index_code, ms, me, _e)
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


def _fetch_csindex(index_code: str, ms: date, me: date) -> tuple[str, set] | None:
    """中证指数官网成分快照（akshare index_stock_cons_weight_csindex，免费无限速）。

    优点：自带精确的快照日期（如 2026-08-31）与权重，且不受 tushare 积分限制。
    局限：只能取「最新一期」快照，无法回溯历史月份 —— 因此仅在请求区间覆盖
    该快照日期时采用（历史月份仍依赖 tushare 或已有落库数据）。
    覆盖范围：中证/上证系列（000906/000300/000905/000852/000016 等），
    深证系列（399006 创业板指）不在其中，会返回异常交由下一级兜底。
    """
    try:
        import akshare as ak
    except Exception:  # noqa: BLE001
        return None
    try:
        df = ak.index_stock_cons_weight_csindex(symbol=index_code)
    except Exception as _e:  # noqa: BLE001
        logger.debug("csindex 成分源不可用（%s）: %s", index_code, _e)
        return None
    if df is None or df.empty or "日期" not in df.columns or "成分券代码" not in df.columns:
        return None
    snap = df["日期"].max()
    snap_d = snap.date() if hasattr(snap, "date") else snap
    if not (ms <= snap_d <= me):
        # 快照日期不在请求区间内（例如请求的是历史月份），不能张冠李戴
        return None
    from app.services.data_source import normalize_symbol

    s: set = set()
    for code in df["成分券代码"]:
        sym, _ = normalize_symbol(str(code))
        s.add(sym)
    return (snap_d.isoformat(), s) if s else None


def _fetch_sina(index_code: str, ms: date, me: date) -> tuple[str, set] | None:
    """新浪当前成分快照（akshare index_stock_cons）——深证系列（399006）兜底。

    只提供「当前」成分，无历史快照日期；因此仅当请求区间包含今天时采用，
    并把快照日期记为 today（月末快照的自然近似）。
    """
    try:
        import akshare as ak
    except Exception:  # noqa: BLE001
        return None
    today = date.today()
    if not (ms <= today <= me):
        return None
    # 新浪的 index_stock_cons 对中证系列只返回**部分**成分（实测 000906 只给 688/800），
    # 拿它当月度快照会让调仓选股池凭空缩水。因此仅用于中证官网覆盖不到的深证系列
    #（399xxx，如 399006 创业板指）—— 那些指数本身也没有别的免费源。
    if not str(index_code).startswith("399"):
        return None
    try:
        df = ak.index_stock_cons(symbol=index_code)
    except Exception as _e:  # noqa: BLE001
        logger.debug("sina 成分源不可用（%s）: %s", index_code, _e)
        return None
    if df is None or df.empty or "品种代码" not in df.columns:
        return None
    from app.services.data_source import normalize_symbol

    s = {normalize_symbol(str(c))[0] for c in df["品种代码"]}
    return (today.isoformat(), s) if s else None


def _fetch_online(index_code: str, ms: date, me: date) -> tuple[str, set] | None:
    """在线拉取某自然月的成分快照，按可用性依次尝试三个源。

    优先级：tushare（可回溯历史）→ 中证官网（最新期，免费无限速）→ 新浪（当前成分）。
    任一源成功即返回；全部失败返回 None（调用方跳过该月，不影响已落库快照）。
    """
    return (
        _fetch_tushare(index_code, ms, me)
        or _fetch_csindex(index_code, ms, me)
        or _fetch_sina(index_code, ms, me)
    )


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
            if not _plausible(latest, syms, snaps):
                logger.warning(
                    "指数 %s 的 %s 快照仅 %d 只，明显少于历史快照，判定为残缺，跳过写入",
                    index_code, latest, len(syms),
                )
                continue
            snaps[latest] = syms
            try:
                _persist(db, index_code, date.fromisoformat(latest), syms)
            except Exception:  # noqa: BLE001
                db.rollback()
    return sorted(snaps.items())


def _plausible(snap_date: str, syms: set, known: dict[str, set]) -> bool:
    """新快照是否可信（防止残缺数据污染 PIT 选股池）。

    有的数据源会静默返回截断的成分列表（实测新浪对中证800 只给 688/800 只）。
    这种残缺快照一旦落库，回测调仓的选股池就会凭空缩水，而且因为「已有快照」
    的判断存在，后续永远不会被补上 —— 属于比缺数据更隐蔽的污染。
    因此：与历史快照中位数相比，若不足 70% 直接否决。
    """
    if not known:
        return len(syms) > 0
    sizes = sorted(len(v) for k, v in known.items() if k < snap_date)
    if not sizes:
        return True
    ref = sizes[len(sizes) // 2]
    return ref <= 0 or len(syms) >= ref * 0.7


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
