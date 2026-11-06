"""ETF 资金流数据服务（国家队 ETF 资金看板 · 观察口径）。

## 定位：观察，不是信号

平台里已有经过严格回测的国家队信号 —— NSI（`docs/NSI-SPEC.md`），用 **5 只核心
宽基 ETF 的份额**算 0–100 分，T+1 开盘执行，由 `nsi_scheduler` 每交易日 08:30 刷新。
本模块**不复刻、不修改**它，只做三件 NSI 不做的事：

1. 把覆盖面从 5 只扩到 20 只、并归成 12 类宽基 —— 回答「护盘重心在哪一类」
2. 把时间粒度压到 1 分钟 —— 补 NSI 明确承认的盲区（「盘中痕迹看不见」）
3. 把结果可视化

**⚠️ 三条纪律，改动前必读**

- 盘中量能只是观察。NSI 规格书 §9.3 反面清单实测过「用 ETF 成交量替代份额，
  IC 训练 −0.19 / 验证 +0.13，方向整个翻转」。本模块的分钟序列**不得**喂给任何
  策略或信号，页面上也必须标明这一点。
- 20 只样本含主题 ETF 的自发申购噪声，比 NSI 的 5 只「不干净」。规格书 §2.2
  明确说过加进来 IC 反而下降，所以这里只用于展示分类资金流。
- 份额折算是硬伤。`Δ份额 × 净值` 在份额合并/拆分日会算出几百亿的假流入，
  必须走 `rebuild_flow()` 里的 split 检测（510310 在 2024-09-20 做过 1:2 合并）。

## 调用量

- 全量首次：约 84 次（指数 1 + 池子筛选 2 + 20 只 × 日线/份额/净值 + 21 只 × 分钟）
- 增量启动：通常 0–5 次（按库内最新日期判断）
- 盘中采样：**每分钟 1 次** —— `realtime_quote` 支持批量，20 只 ETF 加上证指数
  打包在一个请求里，不是 21 次
- 分类矩阵：每天 2 次 × 23 个交易日 = 46 次，只在日级更新时跑
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, time as dtime, timedelta

import pandas as pd
import tushare as ts
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import DATA_DIR, settings
from app.services import market_valuation
from app.models import (
    EtfCategoryDaily,
    EtfDailyBar,
    EtfFlowDaily,
    EtfMinuteBar,
    EtfQuoteIntraday,
    EtfShareDaily,
    EtfUniverse,
)

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
# 口径常量
# ══════════════════════════════════════════════════════════════════════
INDEX_CODE = "000001.SH"
HIST_START = "20230101"          # 日度历史起点（净流入需要足够回看窗口筛托底日）
MIN_LOOKBACK = 25                # 分钟线回看交易日数
SUPPORT_THRESHOLD = 100.0        # 托底日门槛：单日净流入 > 100 亿
CAT_MONTH_DAYS = 22              # 分类矩阵回看交易日数
# 盘中采样满 2 个点就用当日实时，**不再要求 30 个点**。
# 门槛定 30 时的实际行为是「服务刚启动的半小时里，你看到的是昨天的数据」——
# 与「看板要看实时」直接相悖。2026-11-06 实测：服务 14:50 才起来，采到 13 个点，
# 被判定不足 30 而整页回退到 11-05。采样起点已在卡片 tag 与 meta 里标出，
# 用户看得见「这是从几点开始采的」，就不需要一个替用户做决定的门槛。
LIVE_MIN_POINTS = 2
PROXY_CATEGORY = "沪深300"       # 15 分钟形态里的走势代理

# 事件驱动的集中申赎窗口，量级失真，从托底日样本里剔除
SUPPORT_EXCLUDED = [
    ("20240206", "20240208", "救市资金集中入场，份额单日跳增"),
    ("20240924", "20241008", "九二四行情，被动资金集中申购"),
    ("20250407", "20250411", "关税冲击后集中申购，量级失真"),
]

# 宽基归类：tushare 的 benchmark 原文极不统一，必须用别名**前缀**精确匹配。
# 用关键词 contains 会漏掉 510050（「上海证券交易所50成份指数」）、
# 510500（「中证小盘500指数」）、588000（「上证科创板50成份指数」）这些主力，
# 还会把「中证800制药与生物科技」误判进沪深300。
CATEGORIES: list[tuple[str, list[str]]] = [
    ("上证50", ["上海证券交易所50成份指数", "上证50指数"]),
    ("沪深300", ["沪深300指数"]),
    ("中证500", ["中证小盘500指数", "中证500指数"]),
    ("中证1000", ["中证1000指数"]),
    ("中证2000", ["中证2000指数"]),
    ("中证A500", ["中证A500指数"]),
    ("中证A50", ["中证A50指数"]),
    ("科创50", ["上证科创板50成份指数", "科创50指数"]),
    ("科创100", ["上证科创板100指数"]),
    ("创业板指", ["创业板指数"]),
    ("创业板50", ["创业板50指数"]),
    ("上证180", ["上海证券交易所180成份指数", "上证180指数"]),
]

# 20 只样本池的类别配额（每类按「规模排名 + 成交额排名」加权取前 N）
UNIVERSE_QUOTA = {
    "沪深300": 4, "中证A500": 3, "中证1000": 3, "上证50": 2,
    "中证500": 2, "科创50": 2, "创业板指": 2, "中证2000": 1, "创业板50": 1,
}

SLOT_LABELS = [
    "09:30-09:45", "09:45-10:00", "10:00-10:15", "10:15-10:30",
    "10:30-10:45", "10:45-11:00", "11:00-11:15", "11:15-11:30",
    "13:00-13:15", "13:15-13:30", "13:30-13:45", "13:45-14:00",
    "14:00-14:15", "14:15-14:30", "14:30-14:45", "14:45-15:00",
]


def minute_labels() -> list[str]:
    """240 个分钟刻度：09:31–11:30 + 13:01–15:00（09:30 集合竞价并入 09:31）。"""
    out = []
    for m in range(120):
        t = 9 * 60 + 31 + m
        out.append(f"{t // 60:02d}:{t % 60:02d}")
    for m in range(120):
        t = 13 * 60 + 1 + m
        out.append(f"{t // 60:02d}:{t % 60:02d}")
    return out


LABELS = minute_labels()


# ══════════════════════════════════════════════════════════════════════
# tushare 接入
# ══════════════════════════════════════════════════════════════════════
_pro = None
_last_call = 0.0


def _api():
    global _pro
    if _pro is None:
        ts.set_token(settings.tushare_token)
        _pro = ts.pro_api()
    return _pro


def _call(fn_name: str, retries: int = 3, **kw) -> pd.DataFrame:
    """调 tushare 接口：限速 + 限流退避重试。"""
    global _last_call
    fn = getattr(_api(), fn_name, None)
    if fn is None:
        raise RuntimeError(f"tushare 无此接口：{fn_name}")
    for i in range(retries):
        gap = time.time() - _last_call
        if gap < 0.12:
            time.sleep(0.12 - gap)
        _last_call = time.time()
        try:
            df = fn(**kw)
            return df if df is not None else pd.DataFrame()
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if i == retries - 1:
                logger.warning("tushare %s 失败：%s", fn_name, msg[:160])
                raise
            wait = 3 * (i + 1) if ("每分钟" in msg or "频率" in msg or "limit" in msg.lower()) else 1
            time.sleep(wait)
    return pd.DataFrame()


def _realtime(codes: list[str], retries: int = 3) -> pd.DataFrame:
    """批量实时快照。

    ⚠️ `realtime_quote` 是 tushare 模块的**顶层函数**，不在 pro 对象上走
    `getattr(pro, ...)` 会报「请指定正确的接口名」。它走新浪/腾讯行情，
    支持一次传多只（20 只 ETF + 1 个指数只要 0.5s），所以盘中每分钟 1 次调用就够。
    """
    global _last_call
    for i in range(retries):
        gap = time.time() - _last_call
        if gap < 0.12:
            time.sleep(0.12 - gap)
        _last_call = time.time()
        try:
            df = ts.realtime_quote(ts_code=",".join(codes))
            return df if df is not None else pd.DataFrame()
        except Exception as e:  # noqa: BLE001
            if i == retries - 1:
                logger.warning("realtime_quote 失败：%s", str(e)[:160])
                raise
            time.sleep(1.5 * (i + 1))
    return pd.DataFrame()


def _ymd(d) -> str:
    return d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d)


def _to_date(s) -> date:
    if isinstance(s, date):
        return s
    return datetime.strptime(str(s)[:8], "%Y%m%d").date()


def _classify(benchmark: str) -> str | None:
    bm = str(benchmark or "")
    for cat, aliases in CATEGORIES:
        for a in aliases:
            if bm.startswith(a):
                return cat
    return None


# ══════════════════════════════════════════════════════════════════════
# 指数日线（存 index_kline_daily 会与既有回测口径混用，这里放 etf_flow_daily）
# ══════════════════════════════════════════════════════════════════════
def _index_bars(db: Session, start: str, end: str) -> pd.DataFrame:
    """上证指数日线。单独走一个小函数，便于日度重算时复用。"""
    df = _call("index_daily", ts_code=INDEX_CODE, start_date=start, end_date=end)
    if df is None or not len(df):
        return pd.DataFrame(columns=["trade_date", "close", "pct_chg"])
    out = df[["trade_date", "close", "pct_chg"]].copy()
    out["trade_date"] = out["trade_date"].astype(str)
    return out.sort_values("trade_date")


# ══════════════════════════════════════════════════════════════════════
# 样本池
# ══════════════════════════════════════════════════════════════════════
def ensure_universe(db: Session, force: bool = False) -> list[str]:
    """筛出 20 只重点宽基 ETF（全市场快照 2 次调用）。

    评分口径：类别内「规模排名 × 0.6 + 成交额排名 × 0.4」。
    只看规模会选出长期不动的僵尸 ETF，只看成交额会混进短期炒作的小盘品种。
    """
    have = db.scalars(select(EtfUniverse)).all()
    if len(have) >= 15 and not force:
        return [u.symbol for u in have]

    # 用最近一个交易日的全市场快照
    idx = _call("index_daily", ts_code=INDEX_CODE,
                start_date=(date.today() - timedelta(days=20)).strftime("%Y%m%d"),
                end_date=date.today().strftime("%Y%m%d"))
    if idx is None or not len(idx):
        raise RuntimeError("无法获取指数日线，样本池筛选中止")
    td = str(idx["trade_date"].max())

    basic = _call("fund_basic", market="E")
    daily = _call("fund_daily", trade_date=td)
    share = _call("fund_share", trade_date=td)
    if not len(basic) or not len(daily):
        raise RuntimeError("无法获取全市场快照，请检查 token 与网络")

    m = basic.merge(daily[["ts_code", "close", "amount"]], on="ts_code", how="inner")
    if len(share):
        m = m.merge(share[["ts_code", "fd_share"]], on="ts_code", how="left")
    else:
        m["fd_share"] = None
    m["scale"] = m["fd_share"] * 1e4 * m["close"] / 1e8      # 亿元
    m["amount"] = pd.to_numeric(m["amount"], errors="coerce") / 1e5   # 千元 → 亿元
    m["bm"] = m["benchmark"].fillna("").astype(str)

    m = m[m["invest_type"].astype(str).str.contains("被动指数", na=False)]
    m = m[m["fund_type"].astype(str).str.contains("股票", na=False)]
    m["category"] = m["bm"].map(_classify)
    m = m[m["category"].notna()].copy()
    if not len(m):
        raise RuntimeError("宽基分类结果为空，请检查 benchmark 别名表")

    m["scale"] = m["scale"].fillna(0)
    m["r_scale"] = m.groupby("category")["scale"].rank(ascending=False, method="min")
    m["r_amount"] = m.groupby("category")["amount"].rank(ascending=False, method="min")
    m["score"] = m["r_scale"] * 0.6 + m["r_amount"] * 0.4

    picked = [m[m["category"] == cat].nsmallest(q, "score").assign(want=cat)
              for cat, q in UNIVERSE_QUOTA.items()]
    u = pd.concat(picked).drop_duplicates("ts_code")

    db.execute(delete(EtfUniverse))
    db.bulk_insert_mappings(EtfUniverse, [{
        "symbol": r.ts_code, "name": r.name, "category": r.want,
        "scale": round(float(r.scale), 1), "updated_at": datetime.now(),
    } for r in u.itertuples()])
    db.commit()
    logger.info("ETF 样本池已更新：%d 只", len(u))
    return [r.ts_code for r in u.itertuples()]


def universe_map(db: Session) -> dict[str, dict]:
    return {u.symbol: {"name": u.name, "category": u.category, "scale": u.scale}
            for u in db.scalars(select(EtfUniverse)).all()}


# ══════════════════════════════════════════════════════════════════════
# 日度：日线 + 份额 + 净值
# ══════════════════════════════════════════════════════════════════════
def _latest_share_date(db: Session, symbol: str) -> str | None:
    v = db.scalar(select(func.max(EtfShareDaily.trade_date)).where(EtfShareDaily.symbol == symbol))
    return v.strftime("%Y%m%d") if v else None


def update_daily(db: Session, codes: list[str], force: bool = False) -> int:
    """拉日线与份额净值。已有完整历史则跳过（增量只补最近 10 天）。"""
    end = date.today().strftime("%Y%m%d")
    n = 0
    for code in codes:
        latest = _latest_share_date(db, code)
        if latest and not force:
            start = (datetime.strptime(latest, "%Y%m%d") - timedelta(days=7)).strftime("%Y%m%d")
        else:
            start = HIST_START
        # 份额
        sh = _call("fund_share", ts_code=code, start_date=start, end_date=end)
        # 净值（份额折算检测要用，不能用收盘价近似）
        nv = _call("fund_nav", ts_code=code, start_date=start, end_date=end)
        if not len(sh):
            continue
        s = sh[["trade_date", "fd_share"]].copy()
        s["trade_date"] = s["trade_date"].astype(str)
        s["fd_share"] = pd.to_numeric(s["fd_share"], errors="coerce")
        if len(nv):
            v = nv.rename(columns={"nav_date": "trade_date"})[["trade_date", "unit_nav"]].copy()
            v["trade_date"] = v["trade_date"].astype(str)
            v["unit_nav"] = pd.to_numeric(v["unit_nav"], errors="coerce")
            s = s.merge(v, on="trade_date", how="left")
        else:
            s["unit_nav"] = None
        s = s[(s["fd_share"] > 0)]

        d0, d1 = _to_date(s["trade_date"].min()), _to_date(s["trade_date"].max())
        db.execute(delete(EtfShareDaily).where(
            EtfShareDaily.symbol == code, EtfShareDaily.trade_date >= d0,
            EtfShareDaily.trade_date <= d1))
        db.bulk_insert_mappings(EtfShareDaily, [{
            "symbol": code, "trade_date": _to_date(r.trade_date),
            "fd_share": float(r.fd_share),
            "unit_nav": float(r.unit_nav) if pd.notna(r.unit_nav) else None,
        } for r in s.itertuples()])
        db.commit()
        n += 1
    return n


def update_index(db: Session, force: bool = False) -> int:
    """上证指数日线写进 etf_flow_daily 的伴生列（不重复建表）。"""
    row = db.scalar(select(func.max(EtfFlowDaily.trade_date)))
    start = HIST_START if (force or row is None) else (
        row - timedelta(days=10)).strftime("%Y%m%d")
    df = _index_bars(db, start, date.today().strftime("%Y%m%d"))
    if not len(df):
        return 0
    for r in df.itertuples():
        d = _to_date(r.trade_date)
        obj = db.scalar(select(EtfFlowDaily).where(EtfFlowDaily.trade_date == d))
        if obj is None:
            obj = EtfFlowDaily(trade_date=d)
            db.add(obj)
        obj.index_close = float(r.close)
        obj.index_chg = float(r.pct_chg) if pd.notna(r.pct_chg) else None
    db.commit()
    return len(df)


# ══════════════════════════════════════════════════════════════════════
# 分钟线
# ══════════════════════════════════════════════════════════════════════
def update_minutes(db: Session, codes: list[str], force: bool = False,
                   day: str | None = None) -> int:
    """一次调用拉一只标的的连续多日 1 分钟线。

    `stk_mins` 对 `000001.SH` 同样有效，指数分钟线一并拉，用于分时对照。
    ⚠️ 盘中该接口对当日返回空（T 日盘后才可得），所以盘中只能靠实时采样。
    """
    end = datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)
    if day:
        # 只补指定一天（收盘后覆盖盘中实时采样）
        s_str, e_str = f"{day} 09:30:00", f"{day} 15:00:00"
    else:
        start = end - timedelta(days=int(MIN_LOOKBACK * 1.75) + 5)
        s_str, e_str = start.strftime("%Y-%m-%d 09:30:00"), end.strftime("%Y-%m-%d 15:00:00")
    n = 0
    for code in list(codes) + [INDEX_CODE]:
        latest = db.scalar(select(func.max(EtfMinuteBar.trade_time))
                           .where(EtfMinuteBar.symbol == code))
        if not force and not day and latest and (end - latest).days <= 1:
            continue
        df = _call("stk_mins", ts_code=code, freq="1min", start_date=s_str, end_date=e_str)
        if not len(df):
            continue
        d = df[["trade_time", "amount", "close"]].copy()
        d["trade_time"] = pd.to_datetime(d["trade_time"])
        d["amount"] = pd.to_numeric(d["amount"], errors="coerce") / 1e8   # 元 → 亿元
        d["close"] = pd.to_numeric(d["close"], errors="coerce")
        d = d.dropna(subset=["trade_time"])

        t0, t1 = d["trade_time"].min().to_pydatetime(), d["trade_time"].max().to_pydatetime()
        db.execute(delete(EtfMinuteBar).where(
            EtfMinuteBar.symbol == code, EtfMinuteBar.trade_time >= t0,
            EtfMinuteBar.trade_time <= t1))
        db.bulk_insert_mappings(EtfMinuteBar, [{
            "symbol": code, "trade_time": r.trade_time.to_pydatetime(),
            "amount": float(r.amount) if pd.notna(r.amount) else 0.0,
            "close": float(r.close) if pd.notna(r.close) else 0.0,
        } for r in d.itertuples()])
        db.commit()
        n += 1
    return n


# ══════════════════════════════════════════════════════════════════════
# 12 类宽基分类矩阵
# ══════════════════════════════════════════════════════════════════════
def update_category(db: Session, force: bool = False) -> int:
    """按日拉全市场快照，聚合成 12 类宽基的每日净申赎与成交额。

    每天 2 次调用（share + daily），22 个交易日约 46 次，只在日级更新时跑。
    用收盘价近似净值 —— 对分类矩阵而言，误差不影响方向与量级。
    """
    idx = _call("index_daily", ts_code=INDEX_CODE,
                start_date=(date.today() - timedelta(days=60)).strftime("%Y%m%d"),
                end_date=date.today().strftime("%Y%m%d"))
    if not len(idx):
        return 0
    tds = sorted(idx["trade_date"].astype(str).tolist())[-(CAT_MONTH_DAYS + 1):]
    if len(tds) < 2:
        return 0
    have = db.scalar(select(func.count(func.distinct(EtfCategoryDaily.trade_date))))
    if not force and have and have >= len(tds) - 1:
        return 0

    basic = _call("fund_basic", market="E")
    basic = basic[basic["invest_type"].astype(str).str.contains("被动指数", na=False)]
    basic = basic[basic["fund_type"].astype(str).str.contains("股票", na=False)]
    basic = basic.assign(category=basic["benchmark"].fillna("").astype(str).map(_classify))
    basic = basic[basic["category"].notna()][["ts_code", "category"]]

    prev: pd.DataFrame | None = None
    written = 0
    for td in tds:
        sh = _call("fund_share", trade_date=td)
        dl = _call("fund_daily", trade_date=td)
        if not len(sh) or not len(dl):
            continue
        m = basic.merge(sh[["ts_code", "fd_share"]], on="ts_code") \
                  .merge(dl[["ts_code", "close", "amount"]], on="ts_code")
        m["fd_share"] = pd.to_numeric(m["fd_share"], errors="coerce")
        m["close"] = pd.to_numeric(m["close"], errors="coerce")
        m["amount"] = pd.to_numeric(m["amount"], errors="coerce") / 1e5
        m["scale"] = m["fd_share"] * 1e4 * m["close"] / 1e8

        if prev is not None:
            j = m.merge(prev, on="ts_code", how="left", suffixes=("", "_prev"))
            j["ni"] = (j["fd_share"] - j["fd_share_prev"]) * 1e4 * j["close"] / 1e8
            g = j.groupby("category").agg(
                net_inflow=("ni", "sum"), turnover=("amount", "sum"),
                scale=("scale", "sum")).reset_index()
            d = _to_date(td)
            db.execute(delete(EtfCategoryDaily).where(EtfCategoryDaily.trade_date == d))
            db.bulk_insert_mappings(EtfCategoryDaily, [{
                "trade_date": d, "category": r.category,
                "net_inflow": round(float(r.net_inflow), 2),
                "turnover": round(float(r.turnover), 2),
                "scale": round(float(r.scale), 1),
            } for r in g.itertuples()])
            db.commit()
            written += len(g)
        prev = m[["ts_code", "fd_share"]].rename(columns={"fd_share": "fd_share_prev"})
    return written


# ══════════════════════════════════════════════════════════════════════
# 净流入重算（含份额折算检测）
# ══════════════════════════════════════════════════════════════════════
def rebuild_flow(db: Session) -> int:
    """重算 20 只 ETF 的日度成交额与净流入汇总，写 etf_flow_daily。

    净流入 = Σ(当日份额 − 前日份额) × 单位净值。
    份额折算日必须先剔除，否则一次 1:2 合并会被算成几百亿的假赎回。
    """
    rows = db.execute(select(
        EtfShareDaily.symbol, EtfShareDaily.trade_date,
        EtfShareDaily.fd_share, EtfShareDaily.unit_nav)).all()
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=["symbol", "trade_date", "fd_share", "unit_nav"])
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")

    px = df.pivot_table(index="trade_date", columns="symbol", values="fd_share").sort_index()
    nv = df.pivot_table(index="trade_date", columns="symbol", values="unit_nav").sort_index()

    # 份额折算：份额与净值同时大幅反向变动、且两者乘积 ≈ 1
    ratio_s = px / px.shift(1)
    ratio_n = nv / nv.shift(1)
    split = ((ratio_s - 1).abs() > 0.25) & ((ratio_n - 1).abs() > 0.25) & \
            ((ratio_s * ratio_n - 1).abs() < 0.12)
    d_share = px.diff().mask(split, 0.0)
    inflow = (d_share * 1e4 * nv).sum(axis=1, min_count=1) / 1e8

    # 成交额取最近 30 个交易日的 fund_daily 汇总（只在需要时补）
    turnover = _turnover_series(db)

    # 成交额和净流入的**可得时间不同**：成交额 T 日盘后就到，份额要 T+1 才到。
    # 只遍历 inflow.index（= 有份额的日期）的话，当天那一行的 turnover 永远不会被写
    # ——它由 update_index 预建、默认 0，结果看板 KPI 显示「当日成交额 0 亿」，
    # 而结论生成器据此判成「量能无异常」。必须取两边的并集。
    all_dates = sorted(set(inflow.index) | set(turnover.index))

    n = 0
    for td in all_dates:
        d = _to_date(td)
        obj = db.scalar(select(EtfFlowDaily).where(EtfFlowDaily.trade_date == d))
        if obj is None:
            obj = EtfFlowDaily(trade_date=d)
            db.add(obj)
        v = inflow.get(td)
        if v is not None and pd.notna(v):
            obj.net_inflow = round(float(v), 2)
        if td in turnover.index:
            tv = turnover.loc[td]
            if pd.notna(tv):
                obj.turnover = round(float(tv), 2)
        n += 1
    db.commit()
    logger.info("ETF 资金流已重算：%d 个交易日", n)
    return n


def update_bars(db: Session, codes: list[str], force: bool = False) -> int:
    """单只 ETF 日线（成交额 / 涨跌幅），供「单只 ETF 当日表现」拆解与汇总用。"""
    end = date.today().strftime("%Y%m%d")
    n = 0
    for code in codes:
        latest = db.scalar(select(func.max(EtfDailyBar.trade_date))
                           .where(EtfDailyBar.symbol == code))
        start = HIST_START if (force or latest is None) else (
            latest - timedelta(days=10)).strftime("%Y%m%d")
        df = _call("fund_daily", ts_code=code, start_date=start, end_date=end)
        if not len(df):
            continue
        d = df[["trade_date", "close", "amount", "pct_chg"]].copy()
        d["trade_date"] = d["trade_date"].astype(str)
        d["amount"] = pd.to_numeric(d["amount"], errors="coerce") / 1e5   # 千元 → 亿元
        d["close"] = pd.to_numeric(d["close"], errors="coerce")
        d["pct_chg"] = pd.to_numeric(d["pct_chg"], errors="coerce")
        d = d.dropna(subset=["trade_date"])
        if not len(d):
            continue
        t0, t1 = _to_date(d["trade_date"].min()), _to_date(d["trade_date"].max())
        db.execute(delete(EtfDailyBar).where(
            EtfDailyBar.symbol == code, EtfDailyBar.trade_date >= t0,
            EtfDailyBar.trade_date <= t1))
        db.bulk_insert_mappings(EtfDailyBar, [{
            "symbol": code, "trade_date": _to_date(r.trade_date),
            "close": float(r.close) if pd.notna(r.close) else 0.0,
            "amount": float(r.amount) if pd.notna(r.amount) else 0.0,
            "pct_chg": float(r.pct_chg) if pd.notna(r.pct_chg) else None,
        } for r in d.itertuples()])
        db.commit()
        n += 1
    return n


def _turnover_series(db: Session) -> pd.Series:
    """20 只 ETF 的日成交额合计（直接从 etf_daily_bar 汇总，不再单独拉接口）。"""
    rows = db.execute(select(EtfDailyBar.trade_date, EtfDailyBar.amount)).all()
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows, columns=["trade_date", "amount"])
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
    return df.groupby("trade_date")["amount"].sum(min_count=1)


# ══════════════════════════════════════════════════════════════════════
# 盘中实时采样
# ══════════════════════════════════════════════════════════════════════
def sample_intraday(db: Session) -> int:
    """采一次实时快照。20 只 ETF + 上证指数打包在**同一个请求**里。

    用行情自带的 TIME 作为 sample_time，而不是本地时间 —— 非交易时段行情不更新，
    同一时刻重复采样会命中唯一约束而幂等覆盖，不会写出假数据点。
    """
    codes = [u.symbol for u in db.scalars(select(EtfUniverse)).all()]
    if not codes:
        return 0
    df = _realtime(codes + [INDEX_CODE])
    if df is None or not len(df):
        return 0
    df = df.rename(columns={"TS_CODE": "symbol", "TIME": "tm", "PRICE": "price",
                            "PRE_CLOSE": "pre_close", "AMOUNT": "cum_amount"})
    t = str(df["tm"].iloc[0])[:8]
    try:
        stamp = datetime.combine(date.today(), datetime.strptime(t, "%H:%M:%S").time())
    except ValueError:
        return 0

    db.execute(delete(EtfQuoteIntraday).where(EtfQuoteIntraday.sample_time == stamp))
    rows = []
    for r in df.itertuples():
        amt = pd.to_numeric(pd.Series([r.cum_amount]), errors="coerce").iloc[0]
        pr = pd.to_numeric(pd.Series([r.price]), errors="coerce").iloc[0]
        pc = pd.to_numeric(pd.Series([r.pre_close]), errors="coerce").iloc[0]
        rows.append({
            "symbol": r.symbol, "sample_time": stamp,
            "price": float(pr) if pd.notna(pr) else None,
            "cum_amount": float(amt) / 1e8 if pd.notna(amt) else None,
            "pre_close": float(pc) if pd.notna(pc) else None,
        })
    db.bulk_insert_mappings(EtfQuoteIntraday, rows)
    db.commit()
    return len(rows)


def in_session(t: dtime) -> bool:
    """连续竞价时段（含少量缓冲，避免开盘瞬间拿不到数据）。"""
    return (dtime(9, 25) <= t <= dtime(11, 35)) or (dtime(12, 55) <= t <= dtime(15, 5))


# ══════════════════════════════════════════════════════════════════════
# 看板数据组装
# ══════════════════════════════════════════════════════════════════════
def _series_240(sub: pd.DataFrame, col: str = "amount") -> list[float] | None:
    """某一天的分钟明细 → 240 点序列（09:30 集合竞价并入 09:31）。"""
    if sub is None or not len(sub):
        return None
    s = sub.copy()
    s["hm"] = s["trade_time"].dt.strftime("%H:%M")
    g = s.groupby("hm")[col].sum()
    v = [float(g.get(x, 0.0)) for x in LABELS]
    if "09:30" in g.index:
        v[0] += float(g["09:30"])
    return v


def _close_240(sub: pd.DataFrame) -> list[float] | None:
    """某一天某只标的的分钟收盘价 → 240 点（前值填充）。"""
    if sub is None or not len(sub):
        return None
    s = sub.sort_values("trade_time").copy()
    s["hm"] = s["trade_time"].dt.strftime("%H:%M")
    mp = dict(zip(s["hm"], s["close"]))
    out, last = [], None
    for x in ["09:30"] + LABELS:
        v = mp.get(x)
        if v is not None and pd.notna(v):
            last = float(v)
        if x != "09:30":
            out.append(last)
    return out


def _to_slots(vals: list[float]) -> list[float]:
    return [round(float(sum(vals[i * 15:(i + 1) * 15])), 1) for i in range(16)]


def _slot_chg(prices: list[float] | None, base: float | None) -> list[float]:
    if not prices or not base:
        return [0.0] * 16
    out = []
    for i in range(16):
        seg = [v for v in prices[i * 15:(i + 1) * 15] if v is not None]
        out.append(round((seg[-1] / base - 1) * 100, 2) if seg else 0.0)
    return out


def _cum(arr: list[float | None]) -> list[float | None]:
    out, s = [], 0.0
    for v in arr:
        if v is None:
            out.append(None)
        else:
            s += v
            out.append(round(s, 2))
    return out


def _read_nsi() -> dict:
    """读 NSI 当日读数与最近 250 日历史（复用既有信号，不重算）。"""
    base = DATA_DIR / "research"
    latest_p, hist_p = base / "nsi_latest.json", base / "nsi_signal.csv"
    out: dict = {"available": False}
    try:
        if latest_p.exists():
            out.update(json.loads(latest_p.read_text(encoding="utf-8")))
            out["available"] = True
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 nsi_latest.json 失败：%s", e)
    try:
        if hist_p.exists():
            h = pd.read_csv(hist_p)
            h = h.dropna(subset=["score"]).tail(250)
            out["history"] = [{
                "date": str(r.date)[:10],
                "score": round(float(r.score), 2),
                "nsi": round(float(r.NSI), 2) if pd.notna(r.NSI) else None,
                "position": float(r.position) if pd.notna(r.position) else None,
            } for r in h.itertuples()]
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 nsi_signal.csv 失败：%s", e)
    return out



def _build_summary(turnover, avg_turn, quantile, net_in, chg, nsi,
                   sup_n, as_of, is_live, tail_mult=None, tail_today=None) -> dict:
    """把四个维度（量能 / 资金方向 / 指数 / NSI 信号）压成一段人话结论。

    为什么规则驱动而不是接 LLM：这个判断每天只依赖五个数字，规则完全可解释、
    可回溯、零延迟零成本；换成大模型反而引入不确定性，还多一个失败点。

    判断的骨架是「量 × 方向 × 价格」的二维交叉：
      - 放量 + 大额净申购 + 下跌  → 托底（最典型）
      - 放量 + 大额净申购 + 上涨  → 主动加仓
      - 放量 + 净流出             → 换手热闹但钱在撤
      - 缩量 + 大额净申购         → 低吸（比放量更值得留意）
    再叠加 NSI 是否触发做交叉验证，两者同向才算「互相印证」。
    """
    mult = (turnover / avg_turn) if avg_turn else 0.0
    heavy, light = mult >= 1.25, (0 < mult <= 0.80)
    share_pending = net_in is None
    big_in = (net_in is not None) and net_in >= SUPPORT_THRESHOLD
    outflow = (net_in is not None) and net_in < 0
    chg = chg or 0.0

    # —— 量能 ——
    if heavy:
        vol = f"20 只 ETF 今日成交 {turnover:.0f} 亿，是 20 日均值的 {mult:.2f} 倍，明显放量"
    elif light:
        vol = f"20 只 ETF 今日成交 {turnover:.0f} 亿，仅为 20 日均值的 {mult:.2f} 倍，明显缩量"
    else:
        vol = f"20 只 ETF 今日成交 {turnover:.0f} 亿，与 20 日均值基本持平（{mult:.2f} 倍）"
    if quantile:
        vol += f"；放在 {sup_n} 个历史托底日里排 {quantile:.0f} 分位"
    # 尾盘量能是「当日净流入」公布前唯一可用的前置指标，必须进结论而不是只挂个图
    if tail_mult is not None:
        tail_word = ("收盘前明显集中放量" if tail_mult >= 1.3
                     else "尾盘明显缩量" if tail_mult <= 0.8 else "尾盘量能正常")
        vol += (f"；尾盘（14:31–15:00）占全天 {tail_today:.1f}%，"
                f"为 20 日均值的 {tail_mult:.2f} 倍，{tail_word}" if tail_today is not None
                else f"；尾盘量能为 20 日均值的 {tail_mult:.2f} 倍，{tail_word}")

    # —— 资金方向 ——
    if share_pending:
        dirn = "净流入待公布（份额由交易所 T 日盘后清算，通常次日盘前可见），当前只能用成交额做前置判断"
        if tail_mult is not None:
            if tail_mult >= 1.3:
                dirn += "。不过尾盘放量集中在收盘前，形态上更像有资金介入"
            elif tail_mult <= 0.8:
                dirn += "。尾盘反而在缩量，护盘痕迹不明显"
    elif big_in:
        dirn = f"申赎口径净流入 {net_in:+.0f} 亿，已越过 {SUPPORT_THRESHOLD:.0f} 亿的托底门槛"
    elif net_in >= 0:
        dirn = f"申赎口径净流入 {net_in:+.0f} 亿，方向为正但未达托底门槛"
    else:
        dirn = f"申赎口径净流出 {net_in:.0f} 亿，资金在往外走"

    # —— 结论 ——
    if share_pending:
        if heavy and chg < -0.5:
            head, tone = "放量下跌，资金方向待份额确认", "warn"
        elif heavy and chg > 0.5:
            head, tone = "放量上涨，资金方向待份额确认", "warn"
        elif heavy:
            head, tone = "量能明显放大，资金方向待份额确认", "warn"
        elif light:
            head, tone = "缩量整理，等待盘后份额数据", "calm"
        else:
            head, tone = "量能平稳，等待盘后份额数据", "calm"
    elif big_in and chg < -0.3:
        head, tone = "放量下跌 + 大额净申购：教科书式的托底形态", "up"
    elif big_in and chg > 0.3:
        head, tone = "上涨伴随大额净申购：资金主动加仓而非被动承接", "up"
    elif big_in:
        head, tone = "横盘中出现大额净申购：低吸式承接", "up"
    elif outflow and heavy:
        head, tone = "放量却净流出：换手热闹，但钱在往外走", "down"
    elif outflow:
        head, tone = "缩量净流出：资金观望为主", "down"
    elif heavy:
        head, tone = "放量但净流入温和：有量无跟，尚未构成托底确认", "warn"
    elif light:
        head, tone = "缩量且资金小幅流入：细水长流式低吸", "calm"
    else:
        head, tone = "量能与资金均无异常，维持常态", "calm"

    # —— NSI 交叉验证 ——
    pts = [{"tag": "量能", "text": vol}, {"tag": "方向", "text": dirn}]
    if nsi.get("available"):
        score = nsi.get("score") or 0
        hi = nsi.get("hi_threshold") or 71.34
        pos = nsi.get("position") or 0.7
        base = nsi.get("base_position") or 0.7
        days = nsi.get("holding_days") or 0
        boost = pos > base + 1e-6
        if boost and big_in:
            sig = (f"NSI {score:.1f} 高于阈值 {hi:.1f}，仓位 {pos:.0%}，已加仓 {days} 个交易日。"
                   f"当日资金流与信号同向，两者互相印证。")
        elif boost and outflow:
            sig = (f"NSI {score:.1f} 仍在阈值 {hi:.1f} 之上，仓位 {pos:.0%}，已加仓 {days} 个交易日；"
                   f"但当日转为净流出——信号与观察出现分歧，需留意是否只是单日扰动。")
        elif boost and share_pending:
            sig = (f"NSI {score:.1f} 高于阈值 {hi:.1f}，仓位 {pos:.0%}，已加仓 {days} 个交易日；"
                   f"当日资金方向待份额公布后才能与信号对照。")
        elif boost:
            sig = (f"NSI {score:.1f} 高于阈值 {hi:.1f}，仓位 {pos:.0%}，已加仓 {days} 个交易日；"
                   f"当日净流入未达托底门槛，属信号期内的常态日子。")
        else:
            sig = (f"NSI {score:.1f} 低于阈值 {hi:.1f}，仓位维持基础 {base:.0%}，"
                   f"本期没有新的托底触发。")
        pts.append({"tag": "信号", "text": sig})
    else:
        pts.append({"tag": "信号", "text": "NSI 信号文件暂不可读，本段跳过交叉验证。"})

    metrics = [
        {"label": "20ETF 成交额", "value": f"{turnover:,.0f}", "unit": "亿",
         "sub": f"20 日均值的 {mult:.2f} 倍" if avg_turn else "—",
         "tone": "up" if heavy else ("down" if light else "plain")},
        {"label": "当日净流入", "value": "待公布" if share_pending else f"{net_in:+.0f}",
         "unit": "" if share_pending else "亿",
         "sub": "份额盘后才可得" if share_pending else f"托底门槛 {SUPPORT_THRESHOLD:.0f} 亿",
         "tone": "plain" if share_pending else ("up" if (net_in or 0) > 0 else "down")},
        {"label": "上证综指", "value": f"{(chg or 0):+.2f}", "unit": "%",
         "sub": as_of, "tone": "up" if chg > 0 else ("down" if chg < 0 else "plain")},
        {"label": "NSI 信号分", "value": f"{nsi.get('score', 0):.1f}" if nsi.get("available") else "—",
         "unit": "",
         "sub": (f"阈值 {nsi.get('hi_threshold', 0):.1f} · 仓位 {nsi.get('position', 0):.0%}"
                 if nsi.get("available") else "信号不可读"),
         "tone": "up" if (nsi.get("available") and (nsi.get("position") or 0) > (nsi.get("base_position") or 0.7)) else "plain"},
    ]

    return {
        "as_of": as_of,
        "live": is_live,
        "headline": head,
        "tone": tone,
        "metrics": metrics,
        "points": pts,
        "caveat": ("观察口径，不构成交易信号。仓位依据以顶部 NSI 为准；"
                   "盘中量能已被实测证伪为信号（IC 方向翻转），这里只用于判断资金活跃度。"),
    }


def build_dashboard(db: Session) -> dict:
    """组装前端所需的全部数据（结构与前端 EtfFlowPage 约定）。"""
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    u = universe_map(db)
    codes = list(u.keys())

    # ── 日度基础 ──
    flow_rows = db.execute(select(
        EtfFlowDaily.trade_date, EtfFlowDaily.turnover,
        EtfFlowDaily.net_inflow, EtfFlowDaily.index_close,
        EtfFlowDaily.index_chg).order_by(EtfFlowDaily.trade_date)).all()
    flow = pd.DataFrame(flow_rows, columns=[
        "trade_date", "turnover", "net_inflow", "index_close", "index_chg"])
    if len(flow):
        flow["trade_date"] = pd.to_datetime(flow["trade_date"]).dt.strftime("%Y%m%d")
    dates = flow["trade_date"].tolist() if len(flow) else []
    before = [d for d in dates if d < today]
    prev_trade = before[-1] if before else (dates[-1] if dates else None)
    has_today = today in dates

    # ── 分钟 ──
    mrows = db.execute(select(
        EtfMinuteBar.symbol, EtfMinuteBar.trade_time,
        EtfMinuteBar.amount, EtfMinuteBar.close)).all()
    mins = pd.DataFrame(mrows, columns=["symbol", "trade_time", "amount", "close"])
    if len(mins):
        mins["trade_time"] = pd.to_datetime(mins["trade_time"])
        mins["d"] = mins["trade_time"].dt.strftime("%Y%m%d")
        mins["amount"] = pd.to_numeric(mins["amount"], errors="coerce")
        mins["close"] = pd.to_numeric(mins["close"], errors="coerce")
    md = set(mins["d"].unique()) if len(mins) else set()
    etf_mins = mins[mins["symbol"] != INDEX_CODE] if len(mins) else mins
    idx_mins = mins[mins["symbol"] == INDEX_CODE] if len(mins) else mins

    def day_amt(d: str):
        sub = etf_mins[etf_mins["d"] == d]
        if not len(sub):
            return None
        return _series_240(sub.groupby("trade_time", as_index=False)["amount"].sum())

    def day_idx(d: str):
        sub = idx_mins[idx_mins["d"] == d]
        return _close_240(sub) if len(sub) else None

    # ── 盘中实时（当日分钟线缺失时才用）──
    live_pts, live_px, live_pc = None, None, None
    live_base = None      # 采样起点处的真实累计成交额（亿元），给累计曲线定标
    if today not in md:
        q = db.execute(select(
            EtfQuoteIntraday.symbol, EtfQuoteIntraday.sample_time,
            EtfQuoteIntraday.price, EtfQuoteIntraday.cum_amount,
            EtfQuoteIntraday.pre_close).where(
            EtfQuoteIntraday.sample_time >= datetime.combine(date.today(), dtime(0, 0)))
            .order_by(EtfQuoteIntraday.sample_time)).all()
        if q:
            rt = pd.DataFrame(q, columns=["symbol", "sample_time", "price",
                                          "cum_amount", "pre_close"])
            rt["hm"] = pd.to_datetime(rt["sample_time"]).dt.strftime("%H:%M")
            per: dict[str, float] = {}
            base_sum, base_ok = 0.0, False
            for _, g in rt[rt["symbol"] != INDEX_CODE].groupby("symbol"):
                g = g.sort_values("sample_time").groupby("hm", as_index=True).last()
                # 采样起点那一格只有「累计值」、没有前值可减，不能当分钟成交额；
                # 但这个累计值本身是真实数据 —— 用它给累计曲线定标。否则累计曲线
                # 会从采样起点重新从 0 起算，收盘时比日线口径少一大截，和 KPI 自相矛盾。
                first_cum = g["cum_amount"].iloc[0] if len(g) else None
                if pd.notna(first_cum):
                    base_sum += float(first_cum)
                    base_ok = True
                diff = g["cum_amount"].diff()      # 首项天然 NaN，不能当分钟成交额
                for hm, v in diff.items():
                    if pd.notna(v) and v >= 0:
                        per[hm] = per.get(hm, 0.0) + float(v)
            live_base = round(base_sum, 2) if base_ok else None
            valid = [k for k in per if k in LABELS]
            if len(valid) >= 2:
                a = min(LABELS.index(k) for k in valid)
                b = max(LABELS.index(k) for k in valid) + 1
                live_pts = [None] * a + [round(per.get(x, 0.0), 3) for x in LABELS[a:b]]
            ix = rt[rt["symbol"] == INDEX_CODE].sort_values("sample_time")
            if len(ix):
                live_px = float(ix["price"].iloc[-1])
                pc = ix["pre_close"].dropna()
                live_pc = float(pc.iloc[-1]) if len(pc) else None

    # ── 选展示日 ──
    # 优先级：当日官方分钟线 > 当日实时采样 > 最近一个完整交易日。
    # 「官方 > 采样」是因为官方线精度更高（实测与 fund_daily.amount 误差 0.0%），
    # 收盘补数到了就自动切过去，前端无需感知。
    valid_live = sum(1 for v in (live_pts or []) if v is not None)
    if today in md:
        active, series, is_live = today, day_amt(today), False
    elif live_pts and valid_live >= LIVE_MIN_POINTS:
        active, series, is_live = today, live_pts, True
    else:
        active, series = prev_trade, (day_amt(prev_trade) if prev_trade else None)
        is_live, live_px, live_pc = False, None, None

    # 兜底锚定：今天已有日度数据（盘后日线入库）但一分钱分钟数据都没采到 —— 例如
    # 服务今天整天没起来。此时若继续展示上一个交易日，KPI / 结论 / 单只表全停在昨天，
    # 用户看到的就是「隔日数据」。宁可把今天锚住、分钟图留空并说明，也不要静默退回昨天。
    # 注意判断的是「今天没有分钟数据」，不能写成 `not series` —— 兜底分支里
    # series 装的是**昨天**那 240 个点，非空。
    if active != today and has_today and today not in md and not live_pts:
        active, series, is_live, live_px, live_pc = today, [], False, None, None

    seq = [d for d in before if d in md]
    if active in seq and seq.index(active) > 0:
        cmp_prev = seq[seq.index(active) - 1]
    else:
        cmp_prev = prev_trade
    hist_before = [d for d in seq if d < (active or "")][-20:]

    prev_series = day_amt(cmp_prev) if cmp_prev else None
    pool = [s for s in (day_amt(d) for d in hist_before) if s]
    avg20 = ([round(float(pd.Series([p[i] for p in pool]).mean()), 3) for i in range(240)]
             if pool else [0.0] * 240)

    series = series or []
    cnt = len(series)
    today_cum = _cum(series)

    # 实时采样模式下，累计曲线在采样起点直接接上**真实累计值**，之后逐分钟累加；
    # 起点之前那段（服务还没起来、没采到）留 null，不伪造走势。
    # 不做这一步的话，累计曲线会从采样起点重新从 0 起算，收盘时显示的累计额
    # 只有日线口径的一小段，与顶部 KPI 的「当日成交额」自相矛盾。
    if is_live and live_base is not None:
        fv = next((i for i, v in enumerate(series) if v is not None), None)
        if fv is not None:
            acc = float(live_base)
            cum: list[float | None] = [None] * fv
            cum.append(round(acc, 2))
            for v in series[fv + 1:]:
                if v is None:
                    cum.append(None)        # 采样断档：留空好过画成 0 成交
                else:
                    acc += float(v)
                    cum.append(round(acc, 2))
            today_cum = cum

    prev_cum = _cum((prev_series or [0.0] * 240)[:max(cnt, 1)])
    avg_cum = _cum(avg20[:max(cnt, 1)])

    # ── 指数序列 ──
    if is_live and live_px:
        pm = {}
        q2 = db.execute(select(EtfQuoteIntraday.sample_time, EtfQuoteIntraday.price).where(
            EtfQuoteIntraday.symbol == INDEX_CODE,
            EtfQuoteIntraday.sample_time >= datetime.combine(date.today(), dtime(0, 0)))
            .order_by(EtfQuoteIntraday.sample_time)).all()
        for st, pr in q2:
            if pr is not None:
                pm[pd.Timestamp(st).strftime("%H:%M")] = float(pr)
        s = pd.Series([pm.get(x) for x in LABELS[:cnt]], dtype="float64").interpolate()
        idx_series = [round(float(v), 2) if pd.notna(v) else None for v in s.tolist()]
        idx_last, idx_prev = live_px, (live_pc or live_px)
    else:
        ix = day_idx(active) if active else None
        if ix:
            idx_series = [round(v, 2) if v is not None else None for v in ix[:max(cnt, 1)]]
            if len(idx_series) < cnt:
                idx_series += [idx_series[-1]] * (cnt - len(idx_series))
            s = pd.Series(idx_series, dtype="float64").interpolate().ffill().bfill()
            idx_series = [round(float(v), 2) if pd.notna(v) else None for v in s.tolist()]
        else:
            idx_series = [None] * max(cnt, 1)
        _row = flow[flow["trade_date"] == active]
        idx_last = float(_row["index_close"].iloc[0]) if len(_row) and pd.notna(_row["index_close"].iloc[0]) else None
        _pos = dates.index(active) if active in dates else -1
        idx_prev = float(flow["index_close"].iloc[_pos - 1]) if _pos > 0 and pd.notna(
            flow["index_close"].iloc[_pos - 1]) else idx_last

    # ── 15 分钟形态 ──
    proxy = next((k for k, v in u.items() if v["category"] == PROXY_CATEGORY), codes[0] if codes else None)
    sample_pool = []
    for d in hist_before:
        s = day_amt(d)
        if not s:
            continue
        cl = _close_240(idx_mins[idx_mins["d"] == d]) if len(idx_mins) else None
        if not cl and proxy:
            sub = mins[(mins["d"] == d) & (mins["symbol"] == proxy)]
            cl = _close_240(sub)
        # 基准价要取当日第一个非空值：某些日 09:30 那条缺数据，取 cl[0] 会得到 None，
        # 整日涨跌幅退化成全 0，多日平均后曲线被压平
        base = next((v for v in (cl or []) if v), None)
        sample_pool.append({"amt": s, "tot": sum(s), "chg": _slot_chg(cl, base)})
    sample_pool.sort(key=lambda x: -x["tot"])
    if len(sample_pool) >= 6:
        med = pd.Series([p["tot"] for p in sample_pool]).median()
        surge = [p for p in sample_pool if p["tot"] > med]
        normal = [p for p in sample_pool if p["tot"] <= med]
    else:
        surge, normal = sample_pool[:1], sample_pool[1:] or sample_pool[:1]

    def mean_slots(group, key):
        if not group:
            return [0.0] * 16
        if key == "amt":
            return [round(float(pd.Series([_to_slots(p["amt"])[i] for p in group]).mean()), 1)
                    for i in range(16)]
        return [round(float(pd.Series([p["chg"][i] for p in group]).mean()), 2)
                for i in range(16)]

    # ── 尾盘集中度：国家队进场最典型的形态是尾盘一根放量柱 ──
    # 份额要等 T+1，盘中唯一能当天就用的就是量能结构。把「最后 30 分钟占全天成交额
    # 的比例」和过去 20 日均值比，倍数明显抬升 = 资金在收盘前集中进场。
    def _tail_of(arr) -> float | None:
        if not arr or len(arr) < 240:
            return None
        seg = arr[210:240]                      # 14:31–15:00
        if any(v is None for v in seg):
            return None                          # 采样没覆盖到尾盘就不算，别用残缺数据编比例
        tot = sum(v for v in arr if v is not None)
        if tot <= 0:
            return None
        return round(sum(seg) / tot * 100, 2)

    tail_today = _tail_of(series)
    tail_hist = [(d, _tail_of(day_amt(d))) for d in hist_before]
    tail_hist = [(d, v) for d, v in tail_hist if v is not None]
    tail_avg = (round(float(pd.Series([v for _, v in tail_hist]).mean()), 2)
                if tail_hist else None)
    tail = {
        "window": "14:31–15:00",
        "today": tail_today,
        "avg20": tail_avg,
        "mult": (round(tail_today / tail_avg, 2)
                 if tail_today and tail_avg else None),
        "series": [{"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "ratio": v}
                   for d, v in tail_hist[-20:]],
    }

    # ── 托底日 ──
    def excluded(d: str) -> bool:
        return any(a <= d <= b for a, b, _ in SUPPORT_EXCLUDED)

    sup_rows = flow[flow["net_inflow"] > SUPPORT_THRESHOLD] if len(flow) else flow
    raw_n = len(sup_rows)
    sup_rows = sup_rows[~sup_rows["trade_date"].map(excluded)] if len(sup_rows) else sup_rows
    support_days = [{
        "date": f"{r.trade_date[:4]}-{r.trade_date[4:6]}-{r.trade_date[6:]}",
        "net_inflow": round(float(r.net_inflow), 1),
        "turnover": round(float(r.turnover), 1) if pd.notna(r.turnover) else 0.0,
        "index": round(float(r.index_close), 2) if pd.notna(r.index_close) else None,
        "index_chg": round(float(r.index_chg), 2) if pd.notna(r.index_chg) else 0.0,
    } for r in sup_rows.itertuples()]

    # ── 托底之后：历史上托底日的后市表现 ──
    # 看板原来只能回答「今天像不像托底」，回答不了「托底之后会怎样」。
    # 用全部交易日的上证收盘算 T+n 前瞻收益，把「观察」升级成有胜率、有尾部的参照。
    #
    # ⚠️ 这是**同一指数口径的事后统计**，不是可交易策略回测：没扣成本、没算滑点、
    # 没做样本外，样本还有重叠（连续托底日的持有期互相覆盖）。只用来校准预期，
    # 不能当收益率承诺，更不能据此定仓位 —— 那件事归 NSI。
    _pos = {d: i for i, d in enumerate(dates)}
    _close = flow["index_close"].tolist() if len(flow) else []

    def _fwd(d: str, n: int) -> float | None:
        i = _pos.get(d)
        if i is None or not _close or i + n >= len(_close):
            return None
        a, b = _close[i], _close[i + n]
        if pd.isna(a) or pd.isna(b) or not a:
            return None
        return round((float(b) / float(a) - 1) * 100, 2)

    FWD_HORIZONS = (5, 20, 60)
    fwd_pts = []
    for d in support_days:
        row = {"date": d["date"], "net_inflow": d["net_inflow"],
               "index_chg": d["index_chg"]}
        key = d["date"].replace("-", "")
        for n in FWD_HORIZONS:
            row[f"t{n}"] = _fwd(key, n)
        fwd_pts.append(row)

    fwd_stats: dict[str, dict] = {}
    fwd_corr: dict[str, float | None] = {}
    forward_block: dict = {}
    for n in FWD_HORIZONS:
        vals = [p[f"t{n}"] for p in fwd_pts if p[f"t{n}"] is not None]
        if vals:
            sr = pd.Series(vals)
            fwd_stats[str(n)] = {
                "n": len(vals),
                "up": int((sr > 0).sum()),
                "win": round(float((sr > 0).mean() * 100), 1),
                "mean": round(float(sr.mean()), 2),
                "median": round(float(sr.median()), 2),
                "worst": round(float(sr.min()), 2),
                "best": round(float(sr.max()), 2),
            }
            pairs = [(p["net_inflow"], p[f"t{n}"]) for p in fwd_pts
                     if p[f"t{n}"] is not None]
            fwd_corr[str(n)] = (round(float(pd.Series([a for a, _ in pairs])
                                           .corr(pd.Series([b for _, b in pairs]))), 3)
                                if len(pairs) >= 5 else None)
        else:
            fwd_stats[str(n)] = {"n": 0, "up": 0, "win": 0.0, "mean": 0.0,
                                 "median": 0.0, "worst": 0.0, "best": 0.0}
            fwd_corr[str(n)] = None

    forward_block = {
        "horizons": list(FWD_HORIZONS),
        "stats": fwd_stats,
        "corr": fwd_corr,
        "points": fwd_pts,
        # 按托底时的估值分位分组 —— 估值决定不了「会不会托底」，
        # 但强烈决定「托底之后赚不赚」，这里把这件事量化出来
        "by_valuation": [],
    }

    # ── 分类矩阵 ──
    crows = db.execute(select(
        EtfCategoryDaily.trade_date, EtfCategoryDaily.category,
        EtfCategoryDaily.net_inflow, EtfCategoryDaily.turnover,
        EtfCategoryDaily.scale).order_by(EtfCategoryDaily.trade_date)).all()
    c_names, c_scale, c_dates, c_mat, c_total, c_turn = [], [], [], [], [], []
    if crows:
        cdf = pd.DataFrame(crows, columns=["trade_date", "category", "net_inflow",
                                           "turnover", "scale"])
        cdf["trade_date"] = pd.to_datetime(cdf["trade_date"]).dt.strftime("%Y%m%d")
        dls = sorted(cdf["trade_date"].unique())[-CAT_MONTH_DAYS:]
        c_dates = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dls]
        for cat, _aliases in CATEGORIES:
            sub = cdf[cdf["category"] == cat]
            if not len(sub):
                continue
            mp = dict(zip(sub["trade_date"], sub["net_inflow"]))
            row = [round(float(mp.get(d, 0.0)), 1) for d in dls]
            if not any(abs(x) > 0.01 for x in row):
                continue
            last = sub[sub["trade_date"] == dls[-1]]
            c_names.append(cat)
            c_mat.append(row)
            c_total.append(round(sum(row), 1))
            c_scale.append(int(round(float(last["scale"].iloc[0]))) if len(last) else 0)
            c_turn.append(round(float(sub["turnover"].tail(CAT_MONTH_DAYS).sum()), 1))

    # ── 全天预估 ──
    tot_avg = sum(avg20)
    est = 0.0
    if cnt and tot_avg > 0 and today_cum[-1] is not None:
        elp = sum(avg20[:cnt]) / tot_avg
        if elp > 0.02:
            est = round(today_cum[-1] / elp, 1)

    first_valid = next((i for i, v in enumerate(series) if v is not None), -1)

    # ── 单只 ETF 当日表现（拆开看是谁在放量）──
    etf_detail: list[dict] = []
    if active:
        brows = db.execute(select(EtfDailyBar.symbol, EtfDailyBar.amount,
                                  EtfDailyBar.pct_chg).where(
            EtfDailyBar.trade_date == _to_date(active))).all()
        tot_amt = sum(float(r.amount or 0) for r in brows)
        mx = max((float(r.amount or 0) for r in brows), default=0.0)
        for r in brows:
            info = u.get(r.symbol, {})
            amt = float(r.amount or 0)
            etf_detail.append({
                "symbol": r.symbol,
                "name": info.get("name", ""),
                "category": info.get("category", ""),
                "amount": round(amt, 2),
                "share": round(amt / tot_amt * 100, 1) if tot_amt else 0.0,
                "pct_chg": round(float(r.pct_chg), 2) if r.pct_chg is not None else None,
                "rel": round(amt / mx, 3) if mx else 0.0,
            })
        etf_detail.sort(key=lambda x: -x["amount"])

    # ── 焦点切换：国家队「每天挑不同的 ETF 买」的可见痕迹 ──
    # 单看总量看不出这件事，但把当日成交额 top3 与前一日 top3 一比就出来了：
    # 重合度低 = 资金在换标的，重合度高 = 连续几天压在同一类上。
    def _top3(d: str) -> list[dict]:
        if not d:
            return []
        rows = db.execute(select(EtfDailyBar.symbol, EtfDailyBar.amount)
                          .where(EtfDailyBar.trade_date == _to_date(d))).all()
        if not rows:
            return []
        tot = sum(float(r.amount or 0) for r in rows) or 1.0
        ranked = sorted(rows, key=lambda r: -(r.amount or 0))[:3]
        return [{"symbol": r.symbol,
                 "name": u.get(r.symbol, {}).get("name", r.symbol),
                 "amount": round(float(r.amount or 0), 1),
                 "share": round(float(r.amount or 0) / tot * 100, 1)}
                for r in ranked]

    def _hhi(d: str) -> float | None:
        """成交额集中度（0–1，越大越集中在少数几只）。"""
        if not d:
            return None
        rows = db.execute(select(EtfDailyBar.amount)
                          .where(EtfDailyBar.trade_date == _to_date(d))).all()
        vals = [float(r.amount or 0) for r in rows]
        tot = sum(vals)
        if tot <= 0:
            return None
        return round(sum((v / tot) ** 2 for v in vals), 4)

    # ── 估值分位（全 A 股 PE/PB 中位数的滚动 5 年分位）──
    # 螺丝钉那套方法的核心前提是「国家队主要在低估值区动手」。这里用可算的口径
    # 去验证它：把 66 个托底日发生时的估值分位拉出来，看是否真的集中在低位。
    valuation = market_valuation.series(db)
    if valuation.get("available"):
        pct_map = dict(zip(valuation["dates"], valuation["pct_series"]))
        marks = [{"date": d["date"], "pct": pct_map[d["date"]]}
                 for d in support_days
                 if d["date"] in pct_map and pct_map[d["date"]] is not None]
        if marks:
            _v = sorted(m["pct"] for m in marks)
            _se = pd.Series(_v)
            valuation["support_marks"] = marks
            valuation["support_pct"] = {
                "n": len(_v),
                "median": round(float(_se.median()), 1),
                "p25": round(float(_se.quantile(0.25)), 1),
                "p75": round(float(_se.quantile(0.75)), 1),
                # 分布是双峰的：既有 2024 年那种极端低估救市，也有 2025-26 年
                # 在中高位持续买入。只给中位数会把这件事抹平，所以两头都给。
                "low_share": round(float((_se <= 20).mean() * 100), 1),
                "high_share": round(float((_se >= 80).mean() * 100), 1),
            }
            # 按年看模式变化：这是最有说服力的一张表
            _by_year: dict[str, list[float]] = {}
            for m in marks:
                _by_year.setdefault(m["date"][:4], []).append(m["pct"])
            valuation["support_yearly"] = [
                {"year": y, "n": len(v), "median": round(float(pd.Series(v).median()), 1)}
                for y, v in sorted(_by_year.items())]

            # ── 按估值分位分组看托底后的表现 ──
            # 这是整张看板里最能用的一条：估值不决定托底「会不会发生」（前面已证中位 62%），
            # 但决定托底「值不值得跟」——低估值那组 T+60 胜率 86%，高估值那组只有 33%。
            _pct_of = {m["date"]: m["pct"] for m in marks}

            def _grp(vals: list[float]) -> dict | None:
                if not vals:
                    return None
                _s = pd.Series(vals)
                return {"n": len(vals), "up": int((_s > 0).sum()),
                        "win": round(float((_s > 0).mean() * 100), 1),
                        "median": round(float(_s.median()), 2),
                        "mean": round(float(_s.mean()), 2)}

            _by_val = []
            for _label, _lo, _hi in (("低（≤30%）", -0.1, 30.0),
                                     ("中（30–70%）", 30.0, 70.0),
                                     ("高（>70%）", 70.0, 100.1)):
                _pts = [x for x in fwd_pts
                        if _pct_of.get(x["date"]) is not None
                        and _lo <= _pct_of[x["date"]] < _hi]
                _item: dict = {"label": _label, "n": len(_pts)}
                for _n in FWD_HORIZONS:
                    _item[f"t{_n}"] = _grp([x[f"t{_n}"] for x in _pts
                                            if x[f"t{_n}"] is not None])
                _by_val.append(_item)
            forward_block["by_valuation"] = _by_val

            # ── 稳健性检验：按「托底段」去重 ──
            # 连续多日托底（2024-01~02、2024-09 那种）会让 T+60 窗口大段重叠，
            # 同一段行情被数好几遍，胜率会被放大。这里只取每段首日重算一遍。
            _pos_map = {d: _i for _i, d in enumerate(dates)}
            _sup_idx = [_pos_map[x["date"].replace("-", "")] for x in support_days
                        if x["date"].replace("-", "") in _pos_map]
            _seg_idx, _prev = [], -99
            for _i in _sup_idx:
                if _i - _prev > 1:
                    _seg_idx.append(_i)
                _prev = _i
            _seg_dates = {dates[_i] for _i in _seg_idx}
            _seg_pts = [x for x in fwd_pts
                        if x["date"].replace("-", "") in _seg_dates]

            def _win(group: list[dict], n: int) -> dict | None:
                vals = [x[f"t{n}"] for x in group if x[f"t{n}"] is not None]
                return _grp(vals)

            forward_block["dedup"] = {
                "segments": len(_seg_idx),
                "days": len(_seg_pts),
                "low": _win([x for x in _seg_pts if _pct_of.get(x["date"]) is not None
                             and _pct_of[x["date"]] <= 30], 60),
                "mid": _win([x for x in _seg_pts if _pct_of.get(x["date"]) is not None
                             and 30 < _pct_of[x["date"]] <= 70], 60),
                "high": _win([x for x in _seg_pts if _pct_of.get(x["date"]) is not None
                              and _pct_of[x["date"]] > 70], 60),
            }
        else:
            valuation["support_marks"] = []
            valuation["support_pct"] = None
            valuation["support_yearly"] = []

    f_today, f_prev = _top3(active), _top3(cmp_prev)
    focus = {
        "today": f_today,
        "prev": f_prev,
        "prev_date": (f"{cmp_prev[:4]}-{cmp_prev[4:6]}-{cmp_prev[6:]}"
                      if cmp_prev else ""),
        "overlap": len({x["symbol"] for x in f_today} &
                       {x["symbol"] for x in f_prev}),
        "top1_changed": bool(f_today and f_prev and
                             f_today[0]["symbol"] != f_prev[0]["symbol"]),
        "hhi": _hhi(active),
        "hhi_prev": _hhi(cmp_prev),
    }

    # ── 顶部总结 ──
    nsi_data = _read_nsi()
    # 份额 T+1 才可见。当日若还没有份额，flow 里的 net_inflow 是表默认值 0，
    # 必须当成「待公布」而不是「流入 0 亿」——两者对结论的指向完全相反
    # （前者是「还不知道」，后者是「钱没进来」）。
    share_keys = {d.strftime("%Y%m%d") for d in
                  db.scalars(select(EtfShareDaily.trade_date).distinct()).all()}
    _cur = flow[flow["trade_date"] == active] if active else flow.iloc[0:0]
    t_turn = (float(_cur["turnover"].iloc[0])
              if len(_cur) and pd.notna(_cur["turnover"].iloc[0]) else 0.0)
    # 日线成交额缺失时（盘中 / 收盘后日线还没入库）用分钟累计兜底。
    # 两者同源（实测分钟合计与 fund_daily.amount 误差 0.0%），但日线只有收盘后才有，
    # 盘中若直接用 t_turn=0，结论会被判成「量能无异常」，等于把当天全抹平。
    if t_turn <= 0 and today_cum and today_cum[-1] is not None:
        t_turn = float(today_cum[-1])
    t_net = (float(_cur["net_inflow"].iloc[0])
             if len(_cur) and pd.notna(_cur["net_inflow"].iloc[0])
             and active in share_keys else None)
    t_chg = (float(_cur["index_chg"].iloc[0])
             if len(_cur) and pd.notna(_cur["index_chg"].iloc[0]) else 0.0)
    # 均值用「展示日之前」的 20 个交易日，别把当天算进自己的基准
    _p = dates.index(active) if active in dates else len(dates)
    _prev20 = flow.iloc[max(0, _p - 20):_p]["turnover"]
    avg_turn20 = (float(_prev20.mean())
                  if len(_prev20) and _prev20.notna().any() else 0.0)
    _turns = sorted(d["turnover"] for d in support_days)
    q_t = (sum(1 for v in _turns if v < t_turn) / len(_turns) * 100) if _turns else 0.0
    # 「盘中」必须同时满足：用的是采样序列 + 当前确实在交易时段。
    # 只看 is_live 会在收盘后仍显示「盘中 15:00」，属于误导。
    live_now = bool(is_live) and in_session(now.time())
    sampled_from = LABELS[first_valid] if (is_live and first_valid >= 0) else None
    if is_live:
        bars_source = "sampled"
    elif active == today and not series:
        bars_source = "none"       # 今天完全没有分钟数据，前端要给出「去拉一次」的入口
    else:
        bars_source = "official"

    # 已收盘（或本来就用官方线）且成交额有值 → 全天量就是它本身，不要再「按节奏外推」。
    # 否则同一屏会出现「KPI 761 亿 / 分位图今日预估 757 亿」两个数打架。
    if not live_now and t_turn > 0:
        est = round(t_turn, 1)

    summary = _build_summary(
        t_turn, avg_turn20, q_t, t_net, t_chg, nsi_data,
        len(support_days),
        f"{active[:4]}-{active[4:6]}-{active[6:]}" if active else "",
        live_now, tail.get("mult"), tail.get("today"))

    return {
        "meta": {
            "as_of": f"{active[:4]}-{active[4:6]}-{active[6:]}" if active else "",
            "prev_trade_date": f"{cmp_prev[:4]}-{cmp_prev[4:6]}-{cmp_prev[6:]}" if cmp_prev else "",
            "last_minute": LABELS[cnt - 1] if cnt else "--:--",
            "count": cnt,
            "live": live_now,
            "is_today": active == today,
            # bars_source 告诉前端这一屏分钟线是官方 1min 线还是实时采样拼的，
            # 两者的「完整度」完全不同，UI 上要能区分（收盘后看到只有尾巴时尤其需要）
            "bars_source": bars_source,
            "sampled_from": sampled_from,
            "sampled_count": valid_live if is_live else None,
            "partial_from": first_valid if first_valid > 0 else None,
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "universe": [{"symbol": k, "name": v["name"], "category": v["category"],
                          "scale": v["scale"]} for k, v in u.items()],
        },
        "nsi": nsi_data,
        "summary": summary,
        "etf_detail": etf_detail,
        "focus": focus,
        "valuation": valuation,
        "minute": {
            "labels": LABELS,
            "count": cnt,
            "today_amount": series,
            "prev_amount": (prev_series or [0.0] * 240)[:max(cnt, 1)],
            "avg20_amount": avg20[:max(cnt, 1)],
            "today_index": idx_series,
            "today_cum": today_cum,
            "prev_cum": prev_cum,
            "avg20_cum": avg_cum,
            "last": idx_last,
            "prev_close": idx_prev,
            "tail": tail,
        },
        "intraday_15m": {
            "labels": SLOT_LABELS,
            "surge_amount": mean_slots(surge, "amt"),
            "surge_chg": mean_slots(surge, "chg"),
            "normal_amount": mean_slots(normal, "amt"),
            "normal_chg": mean_slots(normal, "chg"),
            "surge_samples": len(surge),
            "normal_samples": len(normal),
        },
        "support": {
            "excluded": [{"start": f"{a[:4]}-{a[4:6]}-{a[6:]}",
                          "end": f"{b[:4]}-{b[4:6]}-{b[6:]}", "reason": r}
                         for a, b, r in SUPPORT_EXCLUDED],
            "days": support_days,
            # 与顶部 KPI 用同一个数（日线优先、分钟累计兜底），
            # 否则同一天会出现「KPI 761 亿 / 分位图 757 亿」两个值
            "today_turnover": round(t_turn, 1),
            "today_estimate": est,
            "threshold": SUPPORT_THRESHOLD,
            "sample_count": len(support_days),
            "raw_count": raw_n,
            # 托底日之后的表现：每个 horizon 的胜率／中位／尾部，流入规模的相关性，
            # 以及按估值分位分组的对比
            "forward": forward_block,
        },
        "daily": {
            "dates": [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates],
            "index": [round(float(v), 2) if pd.notna(v) else None
                      for v in flow["index_close"]] if len(flow) else [],
            "chg": [round(float(v), 2) if pd.notna(v) else 0.0
                    for v in flow["index_chg"]] if len(flow) else [],
            "turnover": [round(float(v), 1) if pd.notna(v) else None
                         for v in flow["turnover"]] if len(flow) else [],
            # 份额未到的日期给 None（图上是断点），不要画成 0 —— 0 看起来像
            # 「当日既没申购也没赎回」，而真实情况是「还没公布」
            "net_inflow": [round(float(v), 1) if pd.notna(v) and d in share_keys else None
                           for v, d in zip(flow["net_inflow"], dates)] if len(flow) else [],
        },
        "category": {
            "labels": c_names,
            "scale": c_scale,
            "dates": c_dates,
            "matrix": c_mat,
            "month_total": c_total,
            "turnover": c_turn,
        },
    }


# ══════════════════════════════════════════════════════════════════════
# 对外入口
# ══════════════════════════════════════════════════════════════════════
def bootstrap(db: Session, force: bool = False) -> dict:
    """首次全量 / 强制重建。约 84 次接口调用、40 秒。"""
    t0 = time.time()
    codes = ensure_universe(db, force=True)
    update_daily(db, codes, force=force)
    update_bars(db, codes, force=force)
    update_index(db, force=True)
    update_minutes(db, codes, force=True)
    update_category(db, force=True)
    n = rebuild_flow(db)
    msg = (f"样本池 {len(codes)} 只，日度 {n} 个交易日，"
           f"耗时 {time.time() - t0:.1f}s")
    logger.info("ETF 资金流初始化完成：%s", msg)
    return {"ok": True, "message": msg, "codes": codes}


def update(db: Session) -> dict:
    """日级增量更新（每交易日盘后跑）。"""
    codes = ensure_universe(db)
    a = update_daily(db, codes)
    update_bars(db, codes)
    b = update_minutes(db, codes)
    c = update_category(db)
    update_index(db)
    n = rebuild_flow(db)
    return {"ok": True, "daily": a, "minutes": b, "category": c, "days": n}


def status(db: Session) -> dict:
    shares = db.scalar(select(func.count()).select_from(EtfShareDaily)) or 0
    mins = db.scalar(select(func.count()).select_from(EtfMinuteBar)) or 0
    flow_n = db.scalar(select(func.count()).select_from(EtfFlowDaily)) or 0
    last_share = db.scalar(select(func.max(EtfShareDaily.trade_date)))
    last_flow = db.scalar(select(func.max(EtfFlowDaily.trade_date)))
    last_quote = db.scalar(select(func.max(EtfQuoteIntraday.sample_time)))
    uni = db.scalar(select(func.count()).select_from(EtfUniverse)) or 0
    return {
        "universe": uni,
        "share_rows": shares,
        "minute_rows": mins,
        "flow_days": flow_n,
        "last_share_date": last_share.isoformat() if last_share else None,
        "last_flow_date": last_flow.isoformat() if last_flow else None,
        "last_sample": last_quote.strftime("%Y-%m-%d %H:%M:%S") if last_quote else None,
    }
