"""东财全市场新闻流提取器（直连接口，akshare 包装层当前异常）。

产出字段与公众号语料对齐，可共用打分与聚合逻辑：
    article_id / source / title / content_text / publish_time / fetched_at
"""
from __future__ import annotations

import time
from datetime import datetime

import pandas as pd
import requests

from .base import BaseExtractor

API = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


class EastmoneyNewsExtractor(BaseExtractor):
    name = "eastmoney_news"

    def fetch(self, pages: int = 10, page_size: int = 100, sleep: float = 0.3, **kw) -> pd.DataFrame:
        rows = []
        for p in range(1, int(pages) + 1):
            try:
                r = requests.get(API, params={
                    "client": "web", "biz": "web_news_col", "column": "347", "order": "1",
                    "needInteractData": "0", "page_index": p, "page_size": int(page_size),
                    "req_trace": 1,
                    "fields": "code,showTime,title,mediaName,summary,url,uniqueUrl",
                    "types": "1,20",
                }, headers=HEADERS, timeout=15)
                data = r.json().get("data") or {}
                lst = data.get("list") or []
            except Exception:  # noqa: BLE001
                break
            if not lst:
                break
            for it in lst:
                title = (it.get("title") or "").strip()
                summary = (it.get("summary") or "").strip()
                if not title:
                    continue
                st = it.get("showTime") or ""
                try:
                    ts = int(datetime.strptime(st, "%Y-%m-%d %H:%M:%S").timestamp())
                except Exception:  # noqa: BLE001
                    ts = int(time.time())
                rows.append({
                    "article_id": f"em_{it.get('code') or ts}_{p}",
                    "source": it.get("mediaName") or "东财",
                    "title": title,
                    "content_text": summary,
                    "publish_time": ts,
                    "fetched_at": int(time.time()),
                })
            time.sleep(float(sleep))
        return pd.DataFrame(rows)
