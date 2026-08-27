"""提取器基类：每个数据源一个 extractor，统一签名 fetch(**params) -> DataFrame。"""
from __future__ import annotations

import pandas as pd


class BaseExtractor:
    name: str = "base"

    def fetch(self, **params) -> pd.DataFrame:
        raise NotImplementedError
