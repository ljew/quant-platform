"""情绪打分器 v1：多空词典法（自 build_news_factors 迁移，接口化）。"""
from __future__ import annotations

import pandas as pd

from .base import SentimentScorer

FIN_KEYWORDS = [
    "A股", "股市", "沪指", "上证", "深成指", "创业板", "基金", "券商", "银行股",
    "央行", "降准", "降息", "LPR", "美联储", "加息", "债市", "债券", "汇率",
    "人民币", "指数", "板块", "涨停", "跌停", "两市", "成交量", "北向资金",
    "融资融券", "IPO", "注册制", "证监会", "上市公司", "财报", "业绩",
    "牛市", "熊市", "多头", "空头", "K线", "市盈率", "估值", "炒股", "股票",
]

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


class DictScorerV1(SentimentScorer):
    """v1：多空词典命中计数，net=(bull-bear)/(bull+bear)。"""

    version = "dict_v1"

    def __init__(self, fin_keywords: list[str] | None = None):
        self.fin_keywords = fin_keywords or FIN_KEYWORDS

    def score(self, texts: list[str]) -> pd.DataFrame:
        bulls, bears, nets, vers = [], [], [], []
        for t in texts:
            bull = sum(t.count(w) for w in BULL_WORDS)
            bear = sum(t.count(w) for w in BEAR_WORDS)
            net = (bull - bear) / max(bull + bear, 1)
            bulls.append(bull)
            bears.append(bear)
            nets.append(round(net, 5))
            vers.append(self.version)
        return pd.DataFrame({"bull": bulls, "bear": bears, "net": nets,
                             "score_version": vers})
