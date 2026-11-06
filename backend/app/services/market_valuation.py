"""全市场估值序列（供「估值 × 资金流」条件判断）。

## 这个模块解决什么

`stock_valuation_daily` 是逐股 PIT 表（2019 起、900 万行），适合算因子，不适合
直接喂看板 —— 每次按日算中位数要扫全表。本模块把它压成一张 1900 行的日频聚合表
`market_valuation_daily`，并给出**滚动 5 年分位**。

分位这个词很容易被滥用，所以口径写死在这里，改动即破坏历史可比性：

| 字段 | 口径 |
|---|---|
| `pe_median` | 全 A 股 PE(TTM) 中位数，剔除 `pe≤0`（亏损）与 `>300`（极端值） |
| `pe_weighted` | 总市值加权：Σ总市值 ÷ Σ(总市值÷PE)，同样只含 `pe>0` |
| `pb_median` | PB 中位数，剔除 `pb≤0` 与 `>30` |
| `pct_pe` | 当日 `pe_median` 在**过去 1250 个交易日**（约 5 年）中的分位，% |
| `pct_pb` | 同上，对 `pb_median` |

为什么用中位数而不是加权：A 股 PE 分布极右偏（少数百倍股能把均值拉高一个量级），
中位数更稳。加权值同时存下来做交叉校验 —— 两者走势长期应该一致，背离说明
市值结构在剧烈变化。

## 与脚本的分工

`scripts/backfill_valuation.py` 负责**深回填**（带 `--resume` / `--dry-run` /
`--limit`，适合一次性补齐几年）。本模块的 `update()` 只补**尾部缺口**（通常
1–20 个交易日），供调度器每日调用。两者都写同一张源表，幂等。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import MarketValuationDaily, StockValuationDaily

logger = logging.getLogger(__name__)

# 口径常量：改动会让历史分位不可比，别随手调
PE_MAX = 300.0          # 剔除 >300 倍的极端值
PB_MAX = 30.0
PCT_WINDOW = 1250       # 滚动窗口（交易日），约 5 年
PCT_MIN = 250           # 窗口内样本不足 250 天则不给分位
HIST_START = date(2019, 1, 1)

_last_call = 0.0


def _api():
    import tushare as ts

    from app.config import settings

    ts.set_token(settings.tushare_token)
    return ts.pro_api()


def _call(fn, retries: int = 3, **kw):
    """带限流与重试的单次调用（tushare 按日全市场约 0.6s）。"""
    global _last_call
    for i in range(retries):
        gap = time.time() - _last_call
        if gap < 0.12:
            time.sleep(0.12 - gap)
        _last_call = time.time()
        try:
            return fn(**kw)
        except Exception as e:  # noqa: BLE001
            if i == retries - 1:
                logger.warning("daily_basic 调用失败：%s", str(e)[:160])
                raise
            time.sleep(1.5 * (i + 1))
    return pd.DataFrame()


def _to_symbol(ts_code: str) -> str:
    code, _, ex = ts_code.partition(".")
    ex = ex.upper()
    if ex == "SH":
        return f"sh{code}"
    if ex == "SZ":
        return f"sz{code}"
    return ts_code


# ══════════════════════════════════════════════════════════════════════
# 一、补源表缺口
# ══════════════════════════════════════════════════════════════════════
def missing_days(db: Session) -> list[date]:
    """源表落后于「今天」的交易日列表（通常 0–20 天）。"""
    from app.core.trading_calendar import trading_days

    vmax = db.scalar(select(func.max(StockValuationDaily.trade_date)))
    if vmax is None:
        return []
    start = vmax + timedelta(days=1)
    today = date.today()
    if start > today:
        return []
    return trading_days(start, today)


def update(db: Session, max_days: int = 40) -> int:
    """补 `stock_valuation_daily` 的尾部缺口，返回写入行数。

    只处理最近 `max_days` 个交易日 —— 再往前属于深回填，用
    `scripts/backfill_valuation.py --resume`，不要在这里一次拉几年。
    """
    days = missing_days(db)
    if not days:
        return 0
    if len(days) > max_days:
        logger.warning("估值缺口 %d 天，仅补最近 %d 天（更早的用脚本回填）",
                       len(days), max_days)
        days = days[-max_days:]

    pro = _api()
    fields = "ts_code,trade_date,pe_ttm,pb,total_mv,circ_mv,turnover_rate"
    total, batch = 0, []
    for d in days:
        ymd = d.strftime("%Y%m%d")
        try:
            df = _call(pro.daily_basic, trade_date=ymd, fields=fields)
        except Exception:  # noqa: BLE001
            continue
        if df is None or not len(df):
            continue                      # 非交易日 / 数据未落地，跳过不报错
        for r in df.to_dict("records"):
            batch.append({
                "symbol": _to_symbol(r["ts_code"]),
                "trade_date": d,
                "pe_ttm": r.get("pe_ttm"),
                "pb": r.get("pb"),
                # tushare total_mv 单位万元 → 亿元
                "total_mv": (r["total_mv"] / 10000.0) if r.get("total_mv") is not None else None,
                "circ_mv": (r["circ_mv"] / 10000.0) if r.get("circ_mv") is not None else None,
                "turnover_rate": r.get("turnover_rate"),
            })
        if len(batch) >= 20000:
            db.bulk_insert_mappings(StockValuationDaily, batch)
            db.commit()
            total += len(batch)
            batch.clear()
    if batch:
        db.bulk_insert_mappings(StockValuationDaily, batch)
        db.commit()
        total += len(batch)
    logger.info("估值源表补齐：%d 个交易日 / %d 行", len(days), total)
    return total


# ══════════════════════════════════════════════════════════════════════
# 二、聚合 + 分位
# ══════════════════════════════════════════════════════════════════════
def _aggregate(db: Session, lo: date) -> pd.DataFrame:
    """按日聚合出 pe/pb 的中位数与市值加权 PE。

    分年扫描：900 万行一次读进来约 300MB，容器里不划算；按年切每片
    不到 100 万行，峰值内存可控。
    """
    frames_pe, frames_pb = [], []
    year = lo.year
    while year <= date.today().year:
        y0 = max(date(year, 1, 1), lo)
        y1 = date(year + 1, 1, 1)
        cond = (StockValuationDaily.trade_date >= y0,
                StockValuationDaily.trade_date < y1)

        pe_rows = db.execute(select(
            StockValuationDaily.trade_date, StockValuationDaily.pe_ttm,
            StockValuationDaily.total_mv).where(
            *cond,
            StockValuationDaily.pe_ttm > 0,
            StockValuationDaily.pe_ttm <= PE_MAX,
            StockValuationDaily.total_mv.isnot(None),
            StockValuationDaily.total_mv > 0)).all()
        if pe_rows:
            df = pd.DataFrame(pe_rows, columns=["d", "pe", "mv"])
            df["earn"] = df["mv"] / df["pe"]
            g = df.groupby("d").agg(pe_median=("pe", "median"),
                                    mv_sum=("mv", "sum"),
                                    earn_sum=("earn", "sum"),
                                    n=("pe", "size"))
            g["pe_weighted"] = g["mv_sum"] / g["earn_sum"]
            frames_pe.append(g[["pe_median", "pe_weighted", "n"]])

        pb_rows = db.execute(select(
            StockValuationDaily.trade_date, StockValuationDaily.pb).where(
            *cond,
            StockValuationDaily.pb > 0,
            StockValuationDaily.pb <= PB_MAX)).all()
        if pb_rows:
            p = pd.DataFrame(pb_rows, columns=["d", "pb"])
            frames_pb.append(p.groupby("d").agg(pb_median=("pb", "median")))

        year += 1

    if not frames_pe:
        return pd.DataFrame()
    # 按年份是**纵向**拼（各年日期不重叠）；若按 axis=1 拼，列名会重复成
    # 8 个 pe_median，后面取 agg["pe_median"] 拿到的就不是 Series 了
    out = pd.concat(frames_pe, axis=0)
    if frames_pb:
        out = out.join(pd.concat(frames_pb, axis=0), how="outer")
    return out.sort_index()


def _rolling_pct(s: pd.Series, window: int = PCT_WINDOW,
                 min_n: int = PCT_MIN) -> pd.Series:
    """滚动分位：当日值在过去 window 个交易日（含当日）中的百分位。"""
    vals = s.values
    out: list[float | None] = []
    for i in range(len(vals)):
        lo = max(0, i - window + 1)
        w = vals[lo:i + 1]
        w = w[~pd.isna(w)]
        if len(w) < min_n or pd.isna(vals[i]):
            out.append(None)
        else:
            out.append(round(float((w <= vals[i]).mean() * 100), 1))
    return pd.Series(out, index=s.index)


def rebuild(db: Session, full: bool = False) -> int:
    """重建 `market_valuation_daily`。默认增量：只算新增日期 + 重算尾部窗口。"""
    vmax = db.scalar(select(func.max(MarketValuationDaily.trade_date)))
    lo = HIST_START if (full or vmax is None) else vmax + timedelta(days=1)
    if lo > date.today():
        return 0

    agg = _aggregate(db, lo)
    if agg.empty:
        return 0

    # 分位要用历史序列，把已有部分读进来拼上新增
    hist = db.execute(select(MarketValuationDaily.trade_date,
                             MarketValuationDaily.pe_median,
                             MarketValuationDaily.pb_median)
                      .order_by(MarketValuationDaily.trade_date)).all()
    pe_old = pd.Series({r[0]: r[1] for r in hist}, dtype="float64")
    pb_old = pd.Series({r[0]: r[2] for r in hist}, dtype="float64")
    pe_all = pd.concat([pe_old, agg["pe_median"].astype("float64")]).sort_index()
    pe_all = pe_all[~pe_all.index.duplicated(keep="last")]
    pb_all = pd.concat([pb_old, agg["pb_median"].astype("float64")]).sort_index()
    pb_all = pb_all[~pb_all.index.duplicated(keep="last")]

    pct_pe = _rolling_pct(pe_all)
    pct_pb = _rolling_pct(pb_all)

    new_dates = list(agg.index)
    db.execute(delete(MarketValuationDaily).where(
        MarketValuationDaily.trade_date.in_(new_dates)))
    db.bulk_insert_mappings(MarketValuationDaily, [{
        "trade_date": d,
        "pe_median": round(float(agg.at[d, "pe_median"]), 2)
        if pd.notna(agg.at[d, "pe_median"]) else None,
        "pe_weighted": round(float(agg.at[d, "pe_weighted"]), 2)
        if pd.notna(agg.at[d, "pe_weighted"]) else None,
        "pb_median": round(float(agg.at[d, "pb_median"]), 2)
        if "pb_median" in agg.columns and pd.notna(agg.at[d, "pb_median"]) else None,
        "sample_count": int(agg.at[d, "n"]),
        "pct_pe": None if pd.isna(pct_pe.get(d)) else float(pct_pe.get(d)),
        "pct_pb": None if pd.isna(pct_pb.get(d)) else float(pct_pb.get(d)),
        "updated_at": datetime.now(),
    } for d in new_dates])
    db.commit()

    # 增量时尾部老行的分位也要跟着更新（窗口右移会改变历史百分位）
    if not full and vmax is not None and len(new_dates):
        tail = db.execute(select(MarketValuationDaily).where(
            MarketValuationDaily.trade_date < new_dates[0]).order_by(
            MarketValuationDaily.trade_date.desc()).limit(PCT_WINDOW)).scalars().all()
        changed = 0
        for row in tail:
            np = pct_pe.get(row.trade_date)
            nb = pct_pb.get(row.trade_date)
            if np is not None and not pd.isna(np) and row.pct_pe != float(np):
                row.pct_pe = float(np)
                changed += 1
            if nb is not None and not pd.isna(nb) and row.pct_pb != float(nb):
                row.pct_pb = float(nb)
                changed += 1
        if changed:
            db.commit()
        logger.info("估值聚合：新增 %d 天，回填尾部 %d 个分位", len(new_dates), changed)
    else:
        logger.info("估值聚合：写入 %d 天", len(new_dates))
    return len(new_dates)


# ══════════════════════════════════════════════════════════════════════
# 三、读给看板
# ══════════════════════════════════════════════════════════════════════
def series(db: Session) -> dict:
    """返回看板需要的估值序列与当前读数。"""
    rows = db.execute(select(
        MarketValuationDaily.trade_date, MarketValuationDaily.pe_median,
        MarketValuationDaily.pct_pe, MarketValuationDaily.pb_median,
        MarketValuationDaily.pct_pb)
        .order_by(MarketValuationDaily.trade_date)).all()
    if not rows:
        return {"available": False}

    dates = [f"{r[0]:%Y-%m-%d}" for r in rows]
    pe = [r[1] for r in rows]
    pct = [r[2] for r in rows]
    pb = [r[3] for r in rows]
    pct_pb = [r[4] for r in rows]

    last = rows[-1]
    return {
        "available": True,
        "as_of": f"{last[0]:%Y-%m-%d}",
        "pe": round(float(last[1]), 2) if last[1] is not None else None,
        "pct": round(float(last[2]), 1) if last[2] is not None else None,
        "pb": round(float(last[3]), 2) if last[3] is not None else None,
        "pct_pb": round(float(last[4]), 1) if last[4] is not None else None,
        "start": dates[0],
        "count": len(dates),
        "dates": dates,
        "pe_series": [None if v is None else round(float(v), 2) for v in pe],
        "pct_series": pct,
    }
