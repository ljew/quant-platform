"""tushare 资讯类数据：深度新闻（major_news）+ 新闻联播（cctv_news）。

## 为什么单独一个 extractor

平台的文本管道原本只有三个入口：东财新闻流、东财公告、公众号。tushare 这边
`pro.news(src=...)` 各源实测都返回 0 行（权限不够），但下面两个接口能用：

- `major_news`：各媒体深度报道，**只有标题没有正文**（800 条/次）。标题级情绪可信度低，
  但「提到哪些板块/个股」仍有效，所以照常入管道，打分时只跑标题。
- `cctv_news`：新闻联播，每天约 16 条，**有正文**。政策信号最强的一条线。

## 产出字段（与公众号/东财对齐，可共用打分与聚合）

`article_id / source / title / content_text / publish_time / fetched_at`
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import pandas as pd

from .base import BaseExtractor

_TOKEN_ENV = ("TUSHARE_TOKEN", "QUANT_TUSHARE_TOKEN")


def _token() -> str:
    for k in _TOKEN_ENV:
        v = os.getenv(k)
        if v:
            return v.strip()
    # 容器内 .env 挂在 /data/.env；本机在项目根 .env
    for path in ("/data/.env", os.path.join(os.getcwd(), ".env")):
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8"):
            if "=" not in line or not any(k in line for k in _TOKEN_ENV):
                continue
            key, _, val = line.partition("=")
            if key.strip() in _TOKEN_ENV:
                return val.strip().strip('"').strip("'")
    return ""


class TushareNewsExtractor(BaseExtractor):
    """tushare 资讯：major_news（标题）+ cctv_news（正文）。"""

    name = "tushare_news"

    def fetch(self, days: int = 3, major_rows: int = 500, **kw) -> pd.DataFrame:
        tok = _token()
        if not tok:
            return pd.DataFrame()
        try:
            import tushare as ts
        except Exception:  # noqa: BLE001
            return pd.DataFrame()
        pro = ts.pro_api(tok)
        now = datetime.now()
        end = now.strftime("%Y%m%d")
        start = (now - timedelta(days=max(1, int(days)))).strftime("%Y%m%d")
        fetched_at = int(time.time())
        rows: list[dict] = []

        # —— 深度新闻：只有标题，来源媒体在 src 字段 ——
        try:
            df = pro.major_news(start_date=start, end_date=end)
            for _, r in df.head(int(major_rows)).iterrows():
                title = str(r.get("title") or "").strip()
                if not title:
                    continue
                pub = str(r.get("pub_time") or "").strip()
                try:
                    ts = int(datetime.strptime(pub[:19], "%Y-%m-%d %H:%M:%S").timestamp())
                except Exception:  # noqa: BLE001
                    ts = fetched_at
                # article_id 用 url 的哈希尾部：tushare 没给稳定 id，url 是唯一可用的
                url = str(r.get("url") or "")
                aid = "tsmn_" + (url[-24:] if url else f"{ts}_{abs(hash(title)) % 10**10}")
                rows.append({
                    "article_id": aid,
                    "source": str(r.get("src") or "tushare").strip() or "tushare",
                    "title": title,
                    "content_text": "",          # 该接口不返回正文
                    "publish_time": ts,
                    "fetched_at": fetched_at,
                })
        except Exception:  # noqa: BLE001
            pass

        # —— 新闻联播：有正文，政策信号最强 ——
        for i in range(max(1, int(days))):
            d = (now - timedelta(days=i)).strftime("%Y%m%d")
            try:
                df = pro.cctv_news(date=d)
            except Exception:  # noqa: BLE001
                continue
            for _, r in df.iterrows():
                title = str(r.get("title") or "").strip()
                if not title:
                    continue
                body = str(r.get("content") or "").strip()
                rows.append({
                    "article_id": f"cctv_{d}_{abs(hash(title)) % 10**10}",
                    "source": "新闻联播",
                    "title": title,
                    "content_text": body,
                    "publish_time": int(
                        datetime.strptime(d, "%Y%m%d").replace(hour=19).timestamp()),
                    "fetched_at": fetched_at,
                })
            time.sleep(0.3)

        return pd.DataFrame(rows)
