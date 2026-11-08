"""公募基金 / 基金经理数据入库（tushare fund_basic / fund_manager / fund_nav）。

## 为什么要这个模块

老刘 2026-11-08 在做「基金经理 / 基金的因子发现」，结果发现库里没有这些数据。
查下来不是没权限 —— tushare 三个接口都通，是从来没建表入库。平台此前只有
ETF 的行情/份额/资金流（`etf_*` 系列），没有任何「谁在管、管了多久、什么背景」。

## 三类数据的更新频率（决定调度）

| 数据 | 频率 | 说明 |
|---|---|---|
| fund_basic | 每周 | 基金成立/摘牌才变，全量覆盖 3000+ 行很快 |
| fund_manager | 每周 | 任职公告驱动，增量很少 |
| fund_nav | 每日 | 按 `nav_date` 一次可拉全市场（实测 10500 行/天） |

## ⚠️ 净值口径

`unit_nav` 是单位净值（分红会掉），`accum_nav` 是累计净值，`adj_nav` 是复权净值。
做收益/因子必须用 `adj_nav` 或 accum_nav 差分 —— 直接拿 unit_nav 算收益率，
会把分红当成下跌。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

logger = logging.getLogger("fund_data")

_PAGE = 5000


def _pro():
    """tushare pro 客户端。没配 token 时抛异常，由调用方决定是否降级。"""
    import tushare as ts

    from app.config import settings

    tok = getattr(settings, "tushare_token", "") or ""
    if not tok:
        raise RuntimeError("未配置 tushare token")
    ts.set_token(tok)
    return ts.pro_api()



def _upsert_batched(db: Session, model, rows: list[dict], index_elements: list[str],
                    batch: int = 400) -> int:
    """分批 upsert：SQLite 单条语句的绑定变量有上限（32766），
    整表一次灌会报 `too many SQL variables` —— fund_manager 5000 行 × 12 字段
    = 6 万变量，实测就炸在这里（2026-11-08）。
    """
    if not rows:
        return 0
    skip = set(index_elements)
    done = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        stmt = insert(model).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=index_elements,
            set_={k: getattr(stmt.excluded, k) for k in chunk[0] if k not in skip},
        )
        db.execute(stmt)
        db.commit()
        done += len(chunk)
    return done

def _s(v) -> str:
    """统一转字符串：tushare 会返回 NaN / None / 数字混着来。"""
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    return str(v).strip()


def _f(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)
    except Exception:  # noqa: BLE001
        return None


def _fetch_basic_df(pro, market: str):
    """按 market 拉全量基础信息。fund_basic 单次上限 15000 行，场外必须 offset 分页。"""
    import time

    frames, off = [], 0
    while True:
        df = pro.fund_basic(market=market, offset=off)
        if df is None or df.empty:
            break
        frames.append(df)
        if len(df) < 15000:
            break
        off += 15000
        time.sleep(0.4)
    return pd.concat(frames, ignore_index=True) if frames else None


def sync_basic(db: Session, market: str | None = None) -> int:
    """基金基础信息全量覆盖（按 ts_code upsert）。

    market=None 时**同时拉场内(E)与场外(O)**。此前默认 market="E"，只覆盖 3 千余只
    场内基金（ETF/LOF），而主动权益基金全部是场外(O) —— 结果是 `fund_basic` 里
    查不到任何主动权益产品，按基金类型筛研究必然跑空（2026-11-08 实证）。
    """
    from app.models import FundBasic

    markets = [market] if market else ["E", "O"]
    dfs = []
    for mk in markets:
        df = _fetch_basic_df(_pro(), mk)
        if df is not None and not df.empty:
            dfs.append(df)
            logger.info("fund_basic[%s] 拉到 %d 行", mk, len(df))
    if not dfs:
        return 0
    df = pd.concat(dfs, ignore_index=True).drop_duplicates("ts_code")
    now = datetime.now()
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "ts_code": _s(r.get("ts_code")), "name": _s(r.get("name"))[:128],
            "management": _s(r.get("management"))[:64],
            "custodian": _s(r.get("custodian"))[:64],
            "fund_type": _s(r.get("fund_type"))[:24],
            "found_date": _s(r.get("found_date")) or None,
            "list_date": _s(r.get("list_date")) or None,
            "issue_date": _s(r.get("issue_date")) or None,
            "delist_date": _s(r.get("delist_date")) or None,
            "issue_amount": _f(r.get("issue_amount")),
            "m_fee": _f(r.get("m_fee")), "c_fee": _f(r.get("c_fee")),
            "p_value": _f(r.get("p_value")), "min_amount": _f(r.get("min_amount")),
            "benchmark": _s(r.get("benchmark"))[:200],
            "status": _s(r.get("status"))[:4], "invest_type": _s(r.get("invest_type"))[:24],
            "type": _s(r.get("type"))[:24], "market": _s(r.get("market"))[:4] or market,
            "updated_at": now,
        })
    rows = [x for x in rows if x["ts_code"]]
    if not rows:
        return 0
    n = _upsert_batched(db, FundBasic, rows, ["ts_code"])
    logger.info("fund_basic 同步 %d 只", n)
    return n


def sync_manager(db: Session) -> int:
    """基金经理任职记录（分页拉全量，按 ts_code+name+begin_date upsert）。"""
    from app.models import FundManager

    pro = _pro()
    frames = []
    # 实测总量在 6.5 万~10 万之间（offset=100000 才返回空），留足余量
    for offset in range(0, 200000, _PAGE):
        df = pro.fund_manager(offset=offset, limit=_PAGE)
        if df is None or df.empty:
            break
        frames.append(df)
        if len(df) < _PAGE:
            break
    if not frames:
        return 0
    all_df = pd.concat(frames, ignore_index=True)
    now = datetime.now()
    rows = []
    for _, r in all_df.iterrows():
        ts_code = _s(r.get("ts_code"))
        name = _s(r.get("name"))
        if not ts_code or not name:
            continue
        rows.append({
            "ts_code": ts_code, "ann_date": _s(r.get("ann_date")) or None,
            "name": name[:32], "gender": _s(r.get("gender"))[:2],
            "birth_year": _s(r.get("birth_year")) or None,
            "edu": _s(r.get("edu"))[:16], "nationality": _s(r.get("nationality"))[:24],
            "begin_date": _s(r.get("begin_date")) or None,
            "end_date": _s(r.get("end_date")) or None,
            "resume": _s(r.get("resume"))[:4000], "updated_at": now,
        })
    if not rows:
        return 0
    n = _upsert_batched(db, FundManager, rows, ["ts_code", "name", "begin_date"])
    logger.info("fund_manager 同步 %d 条（%d 只基金）", n, len({x['ts_code'] for x in rows}))
    return n


def sync_nav(db: Session, days: int = 5, end: str | None = None) -> int:
    """按 nav_date 拉最近 days 个自然日的全市场净值。

    ⚠️ `fund_nav(nav_date=)` 一次返回全市场约 1 万行，比分只基金循环调用快
    两个数量级（后者还会撞 tushare 限速）。所以这里按日拉、不做 per-fund。
    """
    from app.models import FundNav

    pro = _pro()
    end_d = datetime.strptime(end, "%Y%m%d").date() if end else datetime.now().date()
    total = 0
    now = datetime.now()
    for i in range(max(1, days)):
        d = (end_d - timedelta(days=i)).strftime("%Y%m%d")
        try:
            df = pro.fund_nav(nav_date=d)
        except Exception as e:  # noqa: BLE001
            logger.info("fund_nav %s 拉取失败：%s", d, str(e)[:80])
            continue
        if df is None or df.empty:
            continue
        rows = []
        for _, r in df.iterrows():
            ts_code = _s(r.get("ts_code"))
            if not ts_code:
                continue
            rows.append({
                "ts_code": ts_code, "ann_date": _s(r.get("ann_date")) or None,
                "nav_date": _s(r.get("nav_date")) or d,
                "unit_nav": _f(r.get("unit_nav")), "accum_nav": _f(r.get("accum_nav")),
                "accum_div": _f(r.get("accum_div")), "net_asset": _f(r.get("net_asset")),
                "adj_nav": _f(r.get("adj_nav")), "updated_at": now,
            })
        if not rows:
            continue
        total += _upsert_batched(db, FundNav, rows, ["ts_code", "nav_date"])
        logger.info("fund_nav %s 写入 %d 行", d, len(rows))
    return total


def sync_all(db: Session, nav_days: int = 5) -> dict:
    """一次跑全套（basic + manager + nav），返回各自行数。"""
    out = {"basic": 0, "manager": 0, "nav": 0, "errors": []}
    for key, fn in (("basic", sync_basic), ("manager", sync_manager)):
        try:
            out[key] = fn(db)
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{key}: {type(e).__name__}: {str(e)[:100]}")
    try:
        out["nav"] = sync_nav(db, days=nav_days)
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"nav: {type(e).__name__}: {str(e)[:100]}")
    return out
