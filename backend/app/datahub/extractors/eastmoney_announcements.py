"""东财上市公司公告提取器（全市场，带精确股票代码关联）。

字段对齐公众号/新闻语料：
    article_id / source / title / content_text / publish_time / fetched_at / symbols
公告标题含强事件词（预增/预亏/减持/回购/中标/立案…），词典打分有效性高。
"""
from __future__ import annotations

import time
from datetime import datetime

import pandas as pd
import requests

from .base import BaseExtractor

API = "https://np-anotice-stock.eastmoney.com/api/security/ann"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


class EastmoneyAnnouncementExtractor(BaseExtractor):
    name = "eastmoney_announcements"

    def fetch(self, pages: int = 5, page_size: int = 100, sleep: float = 0.25, **kw) -> pd.DataFrame:
        rows = []
        for p in range(1, int(pages) + 1):
            try:
                r = requests.get(API, params={
                    "sr": -1, "page_size": int(page_size), "page_index": p,
                    "ann_type": "A", "client_source": "web", "f_node": 0, "s_node": 0,
                }, headers=HEADERS, timeout=15)
                lst = (r.json().get("data") or {}).get("list") or []
            except Exception:  # noqa: BLE001
                break
            if not lst:
                break
            for it in lst:
                title = (it.get("title") or "").strip()
                if not title:
                    continue
                nd = (it.get("notice_date") or "")[:10]
                try:
                    ts = int(datetime.strptime(nd, "%Y-%m-%d").timestamp())
                except Exception:  # noqa: BLE001
                    ts = int(time.time())
                codes = it.get("codes") or []
                symbols = "|".join(c.get("stock_code") or "" for c in codes if c.get("stock_code"))
                col_names = ",".join(
                    c.get("column_name") or "" for c in (it.get("columns") or []) if c.get("column_name"))
                rows.append({
                    "article_id": f"ann_{it.get('art_code')}",
                    "source": "公司公告",
                    "title": title,
                    "content_text": f"{col_names} {symbols} {title}".strip(),
                    "publish_time": ts,
                    "fetched_at": int(time.time()),
                    "symbols": symbols,
                })
            time.sleep(float(sleep))
        return pd.DataFrame(rows)
