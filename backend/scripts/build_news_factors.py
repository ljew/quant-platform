#!/usr/bin/env python3
"""构建市场新闻情绪因子（文本数据管道第一层）。

数据源：XiaoAn wechat-monitor 语料库 knowledge.db（94 公众号全文，2020-2026）。
方法：逐日聚合 → 财经文章识别 + 多空词典计分 → 量化平台 news_market_daily 表。
产出：市场级情绪择时因子（net_sentiment / 文章热度），供事件检验与择时研究。

用法（backend/ 下）：PYTHONPATH=$(pwd) python scripts/build_news_factors.py [--source PATH]
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, ".")

from sqlalchemy import select, func

from app.database import init_db, SessionLocal
from app.models import NewsMarketDaily

DEFAULT_SOURCE = "/Users/happyljew/Desktop/kimiwork/Xiaoan_0811_bak/wechat-monitor/knowledge.db"

# 财经相关性关键词（标题或正文命中即计入财经文章池）
FIN_KEYWORDS = [
    "A股", "股市", "沪指", "上证", "深成指", "创业板", "基金", "券商", "银行股",
    "央行", "降准", "降息", "LPR", "美联储", "加息", "债市", "债券", "汇率",
    "人民币", "指数", "板块", "涨停", "跌停", "两市", "成交量", "北向资金",
    "融资融券", "IPO", "注册制", "证监会", "上市公司", "财报", "业绩",
    "牛市", "熊市", "多头", "空头", "K线", "市盈率", "估值", "炒股", "股票",
]

# 多空词典（命中次数加总；保守起步，后续可换 LLM 打分）
BULL_WORDS = [
    "利好", "上涨", "大涨", "反弹", "回升", "突破", "新高", "超预期", "净流入",
    "加仓", "增持", "看多", "做多", "走强", "拉升", "放量上行", "企稳", "修复",
    "景气", "盈利改善", "牛市", "飘红", "高开", "领涨",
]
BEAR_WORDS = [
    "利空", "下跌", "大跌", "回调", "回落", "跌破", "新低", "不及预期", "净流出",
    "减仓", "减持", "看空", "做空", "走弱", "杀跌", "缩量阴跌", "探底", "恶化",
    "衰退", "业绩爆雷", "熊市", "翻绿", "低开", "领跌", "退市", "闪崩", "暴跌",
]

_WORD_RE = None


def _count_words(text: str, words: list[str]) -> int:
    total = 0
    for w in words:
        total += text.count(w)
    return total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=DEFAULT_SOURCE, help="wechat-monitor knowledge.db 路径")
    args = ap.parse_args()

    import sqlite3

    src = sqlite3.connect(f"file:{args.source}?mode=ro", uri=True)
    print("读取语料…")
    rows = src.execute(
        "select publish_time, title, substr(content_text, 1, 3000) from articles"
    ).fetchall()
    src.close()
    print(f"共 {len(rows)} 篇文章")

    # 逐日聚合（北京时间）
    day_stat: dict[str, dict] = defaultdict(lambda: {"total": 0, "fin": 0, "bull": 0, "bear": 0})
    for ts, title, content in rows:
        d = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
        st = day_stat[d]
        st["total"] += 1
        body = f"{title}\n{content or ''}"
        if any(k in body[:600] for k in FIN_KEYWORDS):
            st["fin"] += 1
            bull = _count_words(body, BULL_WORDS)
            bear = _count_words(body, BEAR_WORDS)
            st["bull"] += bull
            st["bear"] += bear

    # 落库
    init_db()
    db = SessionLocal()
    n_new = n_upd = 0
    for d_str in sorted(day_stat.keys()):
        st = day_stat[d_str]
        d = datetime.strptime(d_str, "%Y-%m-%d").date()
        senti = (st["bull"] - st["bear"]) / max(st["bull"] + st["bear"], 1)
        row = db.execute(select(NewsMarketDaily).where(NewsMarketDaily.date == d)).scalar()
        if row is None:
            db.add(NewsMarketDaily(
                date=d, n_articles=st["total"], n_finance=st["fin"],
                bull_score=st["bull"], bear_score=st["bear"], net_sentiment=round(senti, 5),
            ))
            n_new += 1
        else:
            row.n_articles = st["total"]
            row.n_finance = st["fin"]
            row.bull_score = st["bull"]
            row.bear_score = st["bear"]
            row.net_sentiment = round(senti, 5)
            n_upd += 1
    db.commit()
    total = db.execute(select(func.count()).select_from(NewsMarketDaily)).scalar()
    fin_days = db.execute(select(func.count()).select_from(NewsMarketDaily).where(NewsMarketDaily.n_finance > 0)).scalar()
    print(f"-") ; print(f"入库完成：新增 {n_new} 行 / 更新 {n_upd} 行，总计 {total} 天（其中含财经文章 {fin_days} 天）")
    db.close()


if __name__ == "__main__":
    main()
