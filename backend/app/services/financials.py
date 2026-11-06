"""财务原始三表的增量更新（按报告期补）。

## 为什么需要这个模块

`financials_raw` 原本是**一次性导入**：`scripts/import_alla_gold.py` 从旧研究
缓存（`cache_alla/*.parquet`）搬进来，之后没有任何增量更新路径。结果是表里
最新只到 2026-06-30 期，2026Q3 一行没有 —— 而三季报 10-31 前就该披露完，
到 11-06 已经空了快一周，**且没有任何报错**。

这跟估值表上次静默滞后 21 天是同一类问题：只有人肉翻日期才会发现。

## 取数口径

tushare 的 `*_vip(period=YYYYMMDD)` 能按报告期一次拉全市场，一个季度
3 次调用（cashflow / balancesheet / fina_indicator），成本可以忽略。
非 vip 版必须逐股传 ts_code，5000 多只 × 3 = 不可能。

字段映射（与 `import_alla_gold.py` 写入的口径保持一致，不能改）：

| 本表字段 | 来源 | 含义 |
|---|---|---|
| `roe` | fina_indicator_vip.roe | 净资产收益率 |
| `n_cashflow_act` | cashflow_vip | 经营活动现金流净额 |
| `capex` | cashflow_vip.c_pay_acq_const_fiolta 取绝对值 | 购建长期资产支付的现金 |
| `total_assets` | balancesheet_vip | 总资产 |

同一 (ts_code, end_date) 可能出现多条（更正、快报），统一取 `ann_date` 最新的一条。

## 披露节奏

| 报告期 | 法定截止 |
|---|---|
| 3-31 | 当年 4-30 |
| 6-30 | 当年 8-31 |
| 9-30 | 当年 10-31 |
| 12-31 | 次年 4-30 |

`expected_period()` 按这个表算「此刻应该已经有的最新一期」，调度器拿它和库内
最新一期比，落后就补。截止日之后 45 天内允许重复拉，用来捞晚披露与更正。
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import FinancialsRaw

logger = logging.getLogger(__name__)

# 报告期 → 法定披露截止日（月, 日）；年报是次年
DEADLINES = {
    (3, 31): (0, 4, 30),    # 0 表示当年
    (6, 30): (0, 8, 31),
    (9, 30): (0, 10, 31),
    (12, 31): (1, 4, 30),   # 1 表示次年
}
REPULL_DAYS = 45           # 截止日后多少天内允许重复拉（捞晚披露 / 更正）

_last_call = 0.0


def _api():
    import tushare as ts

    from app.config import settings

    ts.set_token(settings.tushare_token)
    return ts.pro_api()


def _call(fn, retries: int = 3, **kw):
    global _last_call
    for i in range(retries):
        gap = time.time() - _last_call
        if gap < 0.15:
            time.sleep(0.15 - gap)
        _last_call = time.time()
        try:
            return fn(**kw)
        except Exception as e:  # noqa: BLE001
            if i == retries - 1:
                logger.warning("财报接口调用失败：%s", str(e)[:160])
                raise
            time.sleep(1.5 * (i + 1))
    return pd.DataFrame()


def _fetch_all(fn, period: str, fields: str, page: int = 5000,
               max_pages: int = 6) -> pd.DataFrame:
    """分页拉全。

    ⚠️ vip 接口单次最多返回 7000 行（服务端硬上限）。资产负债表一期有 1 万行左右，
    不分页会静默丢掉尾部 —— 实测 2026Q3 只拿到 4836 只（茅台都不在里面），
    而现金流因为没到上限就是全的。这种"部分缺失"比报错更难发现。
    """
    frames, off = [], 0
    for _ in range(max_pages):
        df = _call(fn, period=period, offset=off, limit=page, fields=fields)
        if df is None or not len(df):
            break
        frames.append(df)
        if len(df) < page:
            break
        off += page
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _to_symbol(ts_code: str) -> str:
    """600519.SH → sh600519（与 financials_raw 既有数据的写法保持一致）。"""
    code, _, ex = ts_code.partition(".")
    ex = ex.upper()
    if ex == "SH":
        return f"sh{code}"
    if ex == "SZ":
        return f"sz{code}"
    return ts_code


def expected_period(today: date) -> date | None:
    """按法定披露截止日，算出此刻「本应已有」的最新报告期。"""
    best: date | None = None
    for y in (today.year - 1, today.year):
        for (m, d), (off, dm, dd) in DEADLINES.items():
            p = date(y, m, d)
            dl = date(y + off, dm, dd)
            if dl <= today and (best is None or p > best):
                best = p
    return best


def periods_to_fetch(db: Session, today: date | None = None) -> list[date]:
    """需要（重新）拉取的报告期。

    · 先进先出：比库内最新一期更新的所有报告期都要补（正常情况只有 1 期）
    · 截止日后 REPULL_DAYS 天内，最新一期允许重复拉（晚披露 / 更正）
    """
    today = today or date.today()
    exp = expected_period(today)
    if exp is None:
        return []
    vmax = db.scalar(select(func.max(FinancialsRaw.end_date)))
    todo: list[date] = []
    if vmax is None:
        return [exp]
    if exp > vmax:
        # 把中间漏掉的期次一并补上（最多补 4 期，再多说明是深回填，用脚本）
        cur = vmax
        while cur < exp and len(todo) < 4:
            for (m, d) in DEADLINES:
                nxt = date(cur.year, m, d)
                if nxt > cur:
                    cur = nxt
                    break
            else:
                cur = date(cur.year + 1, 3, 31)
            todo.append(cur)
    else:
        off, dm, dd = DEADLINES[(exp.month, exp.day)]
        deadline = date(exp.year + off, dm, dd)
        if today <= deadline + timedelta(days=REPULL_DAYS):
            todo.append(exp)      # 窗口内重拉，捞晚披露
    return todo


def update_period(db: Session, period: date) -> int:
    """拉取并覆盖写入某一报告期，返回写入行数。"""
    pro = _api()
    ymd = period.strftime("%Y%m%d")
    cf = _fetch_all(pro.cashflow_vip, ymd,
                    "ts_code,ann_date,f_ann_date,end_date,report_type,"
                    "n_cashflow_act,c_pay_acq_const_fiolta")
    bs = _fetch_all(pro.balancesheet_vip, ymd,
                    "ts_code,ann_date,f_ann_date,end_date,report_type,total_assets")
    fi = _fetch_all(pro.fina_indicator_vip, ymd,
                    "ts_code,ann_date,end_date,roe")
    if cf is None or not len(cf):
        logger.info("报告期 %s 尚未发布（cashflow 返回 0 行）", period)
        return 0

    def clean(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
        if df is None or not len(df):
            return pd.DataFrame(columns=["ts_code", "ann_date"] + cols)
        d = df.copy()
        if "report_type" in d.columns:
            d = d[d["report_type"].astype(str) == "1"]   # 只要合并报表
        d["ts_code"] = d["ts_code"].astype(str)
        d["ann_date"] = d["ann_date"].astype(str)
        d = d[d["ann_date"].str.len() == 8]
        # 同期多条（更正/快报）取最新公告
        return d.sort_values("ann_date").drop_duplicates(
            subset=["ts_code"], keep="last")[["ts_code", "ann_date"] + cols]

    cf = clean(cf, ["n_cashflow_act", "c_pay_acq_const_fiolta"])
    bs = clean(bs, ["total_assets"])
    fi = clean(fi, ["roe"])
    if not len(cf):
        return 0

    # ⚠️ 必须按 ts_code 合并，不能按 (ts_code, ann_date)：
    # 现金流与资产负债表的披露日经常差一两天，按 ann_date 合并会把同一只股票
    # 拆成两行（一行有现金流、一行有总资产），两个字段都变成缺口。
    m = cf.merge(bs, on="ts_code", how="outer", suffixes=("", "_bs"))
    m = m.merge(fi, on="ts_code", how="outer", suffixes=("", "_fi"))
    # 三侧的公告日取最晚 —— 那是三期信息齐备的时点（PIT 取值用 ann_date）。
    # ⚠️ 不能直接 max(axis=1)：outer join 后某侧是 NaN(float)，与 str 比较会抛
    # TypeError（' >=' not supported between 'str' and 'float'）。先统一成字符串。
    for c in ("ann_date_bs", "ann_date_fi"):
        if c in m.columns:
            a = m["ann_date"].fillna("").astype(str)
            b = m[c].fillna("").astype(str)
            m["ann_date"] = a.where(a >= b, b)
            m = m.drop(columns=[c])
    m = m[(m["ts_code"].notna()) & (m["ann_date"] != "")]
    m["capex"] = m["c_pay_acq_const_fiolta"].abs()

    def num(v):
        return None if v is None or pd.isna(v) else float(v)

    rows = [{
        "symbol": _to_symbol(r.ts_code),
        "end_date": period,
        "ann_date": date(int(r.ann_date[:4]), int(r.ann_date[4:6]), int(r.ann_date[6:8])),
        "roe": num(getattr(r, "roe", None)),
        "n_cashflow_act": num(r.n_cashflow_act),
        "capex": num(r.capex),
        "total_assets": num(r.total_assets),
    } for r in m.itertuples(index=False)]

    db.execute(delete(FinancialsRaw).where(FinancialsRaw.end_date == period))
    if rows:
        db.bulk_insert_mappings(FinancialsRaw, rows)
    db.commit()
    logger.info("财报 %s 已写入 %d 行", period, len(rows))
    return len(rows)


def update(db: Session, today: date | None = None) -> int:
    """把 `periods_to_fetch` 里的期次全部补上，返回总写入行数。"""
    todo = periods_to_fetch(db, today)
    if not todo:
        return 0
    total = 0
    for p in todo:
        try:
            total += update_period(db, p)
        except Exception as e:  # noqa: BLE001
            logger.warning("财报 %s 更新失败：%s", p, str(e)[:160])
    return total
