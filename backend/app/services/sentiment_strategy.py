"""舆情策略：把「谁在被讨论、讨论得多空」落成可执行的行业 / 个股 / 仓位判断。

## 为什么单独成模块

平台里已经有三套情绪数据，但都停在「指标」层面：
- `news_market_daily` 市场级多空（每天几百篇财经稿打分）
- `news_stock_daily`  个股级多空 + 提及次数
- `mp_sentiment_daily` 公众号语料情绪

老刘 2026-11-08：「直接做一个舆情信息的策略，包括重点行业和股票，情绪择时也可以有」。
所以这里做的是**把它们组合成三层决策**，而不是再产出一个指标：

1. **情绪择时（市场级）** —— 今天该用多少仓位
2. **重点行业（行业级）** —— 钱往哪个方向放
3. **重点个股（个股级）** —— 具体买什么

## 口径说明（别踩）

- 情绪值范围 -1（全空）~ +1（全多），由 `(bull-bear)/(bull+bear)` 得来
- 个股情绪按 **mentions 加权** 聚到行业 —— 被讨论 1 次的票和 20 次的票，
  对行业情绪的贡献不该一样
- 择时用**分位数**而不是绝对值：市场情绪中枢会漂移（牛市里 +0.2 算悲观，
  熊市里 +0.2 算亢奋），拿历史分布定位才可比
- 只用「样本足够」的交易日：周末/节假日新闻极少（实测 11-07 只有 27 只、
  11-08 只有 7 只），拿它当基准会把噪音当信号
"""
from __future__ import annotations

import math
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

# 择时用的历史窗口（交易日）。太大则中枢漂移跟不动，太小则分位不稳。
LOOKBACK_DAYS = 60
# 一天至少这么多只有情绪打分的股票，才算「样本足够」的交易日
MIN_COVERAGE = 30


def _pick_date(db: Session) -> str | None:
    """最近一个样本足够的日子（跳过周末那种只有十几条的残缺日）。"""
    row = db.execute(text(
        "SELECT date, COUNT(*) c FROM news_stock_daily "
        "GROUP BY date HAVING c >= :n ORDER BY date DESC LIMIT 1"
    ), {"n": MIN_COVERAGE}).first()
    return str(row[0]) if row else None


def _timing(db: Session) -> dict:
    """情绪择时：当前市场情绪在过去 N 个交易日里的分位 → 建议仓位。

    分位低（悲观极致）→ 逆向加仓；分位高（亢奋）→ 减仓。
    这是**反向**逻辑：舆情是散户情绪的映射，极端处往往是最好的反向指标
    （平台早前实测：极端悲观日后 20 日正收益概率显著高于均值）。
    """
    # ⚠️ 必须过样本门槛：周日只有十几篇稿（实测 11-08 = 14 篇），
    # 全正面就得到 net=+1.0 的极值，分位直接顶到 100% → 报「亢奋」。
    # 那不是市场亢奋，是样本太少。
    rows = db.execute(text(
        "SELECT date, net_sentiment FROM news_market_daily "
        "WHERE net_sentiment IS NOT NULL AND n_articles >= 50 "
        "ORDER BY date DESC LIMIT :n"
    ), {"n": LOOKBACK_DAYS}).fetchall()
    if not rows:
        return {"ok": False, "comment": "还没有市场级情绪数据"}
    vals = [float(r[1]) for r in rows]
    cur = vals[0]
    pct = sum(1 for v in vals if v <= cur) / len(vals) * 100
    ma5 = sum(vals[:5]) / min(5, len(vals))
    ma20 = sum(vals[:20]) / min(20, len(vals))

    if pct <= 20:
        stance, position = "悲观极致", 80
        comment = "情绪处在历史低位，逆向加仓区。舆情最悲观时往往已反映在价格里。"
    elif pct <= 40:
        stance, position = "偏冷", 65
        comment = "情绪低于中枢，可以逐步建仓，但还没到极致逆向的位置。"
    elif pct <= 60:
        stance, position = "中性", 50
        comment = "情绪在中枢附近，仓位维持跟随，不做方向性加码。"
    elif pct <= 80:
        stance, position = "偏热", 35
        comment = "情绪偏乐观，注意兑现；舆情转热通常领先于价格见顶。"
    else:
        stance, position = "亢奋", 20
        comment = "情绪处在历史高位，减仓区。人人在讨论时风险收益比最差。"

    return {
        "ok": True, "date": str(rows[0][0]), "value": round(cur, 3),
        "pct": round(pct, 1), "ma5": round(ma5, 3), "ma20": round(ma20, 3),
        "n_days": len(vals), "stance": stance, "position": position,
        "comment": comment,
    }


def _industries(db: Session, day: str, top: int) -> list[dict]:
    """行业舆情排名：提及数按情绪加权聚合。"""
    rows = db.execute(text("""
        SELECT s.industry AS ind,
               COUNT(*) AS n_stock,
               SUM(d.mentions) AS m,
               SUM(d.net_sentiment * d.mentions) * 1.0 / SUM(d.mentions) AS net
        FROM news_stock_daily d
        JOIN stocks s ON s.symbol = d.symbol
        WHERE d.date = :d AND s.industry IS NOT NULL AND s.industry != ''
        GROUP BY s.industry
        HAVING SUM(d.mentions) >= 2
        ORDER BY net DESC, m DESC
        LIMIT :n
    """), {"d": day, "n": top}).fetchall()

    out = []
    for r in rows:
        # 该行业当日情绪最好的两只票，作为「代表股」
        reps = db.execute(text("""
            SELECT d.symbol, s.name, d.net_sentiment, d.mentions
            FROM news_stock_daily d JOIN stocks s ON s.symbol = d.symbol
            WHERE d.date = :d AND s.industry = :ind AND d.mentions >= 1
            ORDER BY d.net_sentiment DESC, d.mentions DESC LIMIT 2
        """), {"d": day, "ind": r[0]}).fetchall()
        out.append({
            "industry": r[0], "n_stock": int(r[1]), "mentions": int(r[2] or 0),
            "net": round(float(r[3] or 0), 3),
            "leaders": [{"symbol": x[0], "name": x[1],
                         "net": round(float(x[2] or 0), 3), "mentions": int(x[3] or 0)}
                        for x in reps],
        })
    return out


def _stocks(db: Session, day: str, top: int) -> list[dict]:
    """个股舆情榜：情绪 × 讨论量的强度排序，附 5 日趋势。"""
    rows = db.execute(text("""
        SELECT d.symbol, s.name, s.industry, d.mentions, d.net_sentiment
        FROM news_stock_daily d LEFT JOIN stocks s ON s.symbol = d.symbol
        WHERE d.date = :d AND d.mentions >= 2
        ORDER BY d.net_sentiment * (1 + LOG(1 + d.mentions)) DESC
        LIMIT :n
    """), {"d": day, "n": top}).fetchall()

    out = []
    for r in rows:
        sym, name, ind, m, net = r[0], r[1], r[2], int(r[3] or 0), float(r[4] or 0)
        prev = db.execute(text(
            "SELECT net_sentiment FROM news_stock_daily "
            "WHERE symbol = :s AND date < :d ORDER BY date DESC LIMIT 1"
        ), {"s": sym, "d": day}).scalar()
        trend = None
        if prev is not None:
            trend = round(net - float(prev), 3)
        out.append({
            "symbol": sym, "name": name or sym, "industry": ind or "未分类",
            "mentions": m, "net": round(net, 3),
            "heat": round(net * (1 + math.log(1 + m)), 3),
            "trend": trend,
        })
    return out


def snapshot(db: Session, top_industry: int = 8, top_stock: int = 15) -> dict:
    """一次取全：数据日 + 择时 + 行业 + 个股。"""
    day = _pick_date(db)
    if not day:
        return {"ok": False, "error": "个股情绪数据不足（需单日 ≥30 只）"}
    timing = _timing(db)
    return {
        "ok": True,
        "asof": day,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "timing": timing,
        "industries": _industries(db, day, top_industry),
        "stocks": _stocks(db, day, top_stock),
        "note": "情绪值域 -1~+1；行业情绪按个股提及数加权；择时用 60 日分位反向",
    }
