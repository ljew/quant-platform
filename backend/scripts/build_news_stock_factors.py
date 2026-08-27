#!/usr/bin/env python3
"""构建个股新闻情绪因子（文本数据管道第二层）。

公司简称匹配：语料正文出现 A 股股票简称 → 关联到 symbol，
对提及文章做多空词典计分 → 逐日聚合 (symbol, date) 情绪。
产出 news_stock_daily 表（GP/挖掘的 ns 变量 news_senti 的数据源）。

用法：PYTHONPATH=$(pwd) python scripts/build_news_stock_factors.py [--source PATH] [--min-len 3]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, ".")

from sqlalchemy import select, func

from app.database import init_db, SessionLocal
from app.models import NewsStockDaily, Stock

DEFAULT_SOURCE = "/Users/happyljew/Desktop/kimiwork/Xiaoan_0811_bak/wechat-monitor/knowledge.db"

# 泛词黑名单（简称为通用词汇时禁止匹配，防误关联）
BLACKLIST = {
    "银行", "证券", "保险", "地产", "医药", "能源", "电力", "科技", "传媒",
    "教育", "交通", "建设", "发展", "控股", "集团", "国际", "中国", "国家",
    "东方", "西部", "南方", "北方", "西藏", "新疆", "上海", "北京", "深圳",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=DEFAULT_SOURCE)
    args = ap.parse_args()

    init_db()
    db = SessionLocal()

    # 1) 简称映射（过滤 ST / 黑名单泛词）
    name_map: dict[str, str] = {}
    for sym, nm in db.execute(select(Stock.symbol, Stock.name)).all():
        if not nm or len(nm) < 2:
            continue
        clean = nm.replace(" ", "").replace("*", "")
        if clean.startswith("ST") or clean in BLACKLIST:
            continue
        name_map[clean] = sym
    print(f"简称映射：{len(name_map)} 只")

    # 2) 读语料并匹配
    import sqlite3

    src = sqlite3.connect(f"file:{args.source}?mode=ro", uri=True)
    rows = src.execute(
        "select publish_time, title, substr(content_text, 1, 4000) from articles"
    ).fetchall()
    src.close()
    print(f"语料 {len(rows)} 篇")

    # daily[(sym, date_str)] = [mentions, bull, bear]
    from scripts.build_news_factors import BULL_WORDS, BEAR_WORDS, _count_words

    FIN_HINTS = ["股", "基金", "券商", "上市", "财报", "业绩", "市值", "指数", "板块"]
    daily: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    items = sorted(name_map.items(), key=lambda kv: -len(kv[0]))  # 长名优先防前缀误吞
    n_hit_articles = 0
    for ts, title, content in rows:
        d = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
        body = f"{title}\n{content or ''}"
        if not any(h in body[:500] for h in FIN_HINTS):
            continue
        matched: list[str] = []
        for name, sym in items:
            if name in body:
                matched.append(sym)
                if len(matched) >= 5:
                    break
        if not matched:
            continue
        bull = _count_words(body, BULL_WORDS)
        bear = _count_words(body, BEAR_WORDS)
        if bull + bear == 0:
            continue
        n_hit_articles += 1
        for sym in set(matched):
            rec = daily[(sym, d)]
            rec[0] += 1
            rec[1] += bull
            rec[2] += bear

    print(f"命中文章 {n_hit_articles} 篇 · (股票,日期) 组合 {len(daily)} 条")

    # 3) 落库
    n_new = n_upd = 0
    for (sym, d_str), (mentions, bull, bear) in sorted(daily.items()):
        d = datetime.strptime(d_str, "%Y-%m-%d").date()
        senti = (bull - bear) / max(bull + bear, 1)
        row = db.execute(
            select(NewsStockDaily).where(NewsStockDaily.symbol == sym,
                                         NewsStockDaily.date == d)
        ).scalar()
        if row is None:
            db.add(NewsStockDaily(symbol=sym, date=d, mentions=mentions,
                                  bull_score=bull, bear_score=bear,
                                  net_sentiment=round(senti, 5)))
            n_new += 1
        else:
            row.mentions = mentions
            row.bull_score = bull
            row.bear_score = bear
            row.net_sentiment = round(senti, 5)
            n_upd += 1
    db.commit()

    # 4) 覆盖率统计
    total = db.execute(select(func.count()).select_from(NewsStockDaily)).scalar()
    n_sym = db.execute(select(func.count(func.distinct(NewsStockDaily.symbol)))).scalar()
    recent = db.execute(
        select(func.count()).select_from(NewsStockDaily).where(NewsStockDaily.date >= "2026-01-01")
    ).scalar()
    print("-")
    print(f"入库完成：新增 {n_new} / 更新 {n_upd}；总计 {total} 行，覆盖 {n_sym} 只股票（2026 年以来 {recent} 行）")
    db.close()


if __name__ == "__main__":
    main()
