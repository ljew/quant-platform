"""日频估值的点-in-time（PIT）载入。

为什么需要：组合回测的 `attributes` 原来一次性从 `stocks` 表取**最新快照**，
整个回测期（可能 3 年）共用一份 pe_ttm / pb / market_cap。等于用 2026-11 的
估值去选 2023-01 的股票 —— 用估值因子选股时这是实打实的前视偏差。

库里 `stock_valuation_daily` 有 905 万行（2019-01 至今日频，6077 只），
一直没被回测路径用上。

内存取舍：全量载入 3 年约 365 万条（Python 里 500MB+，容器上限 5.5G 扛不住）。
按 `step_days` 每 5 个自然日采一个点，约 78 万条 —— 月度调仓的策略完全够用，
估值的周内波动对 EP/BP 排序几乎无影响（日频调仓的策略才需要全量）。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

# SQLite 单语句绑定变量上限 32766；每批 800 只 + 2 个日期参数，安全
_BATCH = 800


def load_valuation_series(
    db: Session,
    symbols: list[str],
    start,
    end,
    step_days: int = 5,
) -> dict[str, dict]:
    """返回 {symbol: {"dates": [...], "pe_ttm": [...], "pb": [...], "total_mv": [...]}}。

    `dates` 升序；`total_mv` 单位与 `stocks.market_cap` 一致（亿元）。
    某个字段当日为空时保留 None（引擎侧 `valuation_at` 会原样返回 None，
    由 `ctx.attribute` 决定退回快照还是置空）。
    """
    if not symbols:
        return {}
    syms = [str(s) for s in symbols]
    a, b = str(start)[:10], str(end)[:10]
    out: dict[str, dict] = {}

    for i in range(0, len(syms), _BATCH):
        chunk = syms[i:i + _BATCH]
        ph = ",".join(f":s{j}" for j in range(len(chunk)))
        params: dict = {f"s{j}": s for j, s in enumerate(chunk)}
        params.update({"a": a, "b": b})
        # %j = 年积日，每 step_days 取一天。用 % 而不是生成日期白名单，
        # 是为了让 SQL 自己走索引，不用把 730 个日期塞成 IN 列表。
        sql = (
            "select symbol, trade_date, pe_ttm, pb, total_mv "
            "from stock_valuation_daily "
            f"where symbol in ({ph}) and trade_date between :a and :b "
            f"and cast(strftime('%j', trade_date) as integer) % {int(step_days)} = 0 "
            "order by symbol, trade_date"
        )
        try:
            rows = db.execute(text(sql), params).fetchall()
        except Exception:  # noqa: BLE001
            # 表不存在（老库）时静默返回空 —— 调用方会退回快照并把徽章标 fallback
            return {}
        for sym, d, pe, pb, mv in rows:
            rec = out.setdefault(
                str(sym),
                {"dates": [], "pe_ttm": [], "pb": [], "total_mv": []},
            )
            rec["dates"].append(str(d)[:10])
            rec["pe_ttm"].append(pe)
            rec["pb"].append(pb)
            rec["total_mv"].append(mv)

    return out


def coverage(series: dict[str, dict], symbols: list[str]) -> float:
    """有多少比例的标的有至少一个估值点（用于评估徽章的 coverage 字段）。"""
    if not symbols:
        return 0.0
    hit = sum(1 for s in symbols if series.get(str(s), {}).get("dates"))
    return round(hit / len(symbols), 4)
