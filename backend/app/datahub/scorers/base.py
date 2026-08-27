"""情绪打分器接口：v1 词典 / v2 LLM 可切换可并存（结果带 score_version）。"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class SentimentScorer(ABC):
    version: str = "base"

    @abstractmethod
    def score(self, texts: list[str]) -> pd.DataFrame:
        """输入文本列表 -> DataFrame[bull, bear, net]（按索引对齐）。"""
