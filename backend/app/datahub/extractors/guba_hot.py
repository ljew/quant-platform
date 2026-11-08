"""东财股吧讨论热度：抓热门股吧的帖子标题 + 阅读/评论数。

## 定位

股吧是散户情绪最集中的地方，但**内容质量参差**：有正经分析，也有大量标题党、
喊单、情绪宣泄。所以这里**不做多空打分**（用词典给「明天必涨」打分没有意义），
只取「讨论热度」：帖子数、阅读数、评论数 —— 反映散户注意力集中在哪里。

## 取哪些股票的吧

不抓全市场（5000 多只，成本不可接受），只取**雪球 A 股热股 Top N** 的股吧。
这样两个源关注的是同一批票，可以对照「雪球关注度 vs 股吧讨论量」。

## 接口要点（2026-11-07 实测）

- 官方 `gbapi.eastmoney.com/webarticlelist/...` 返回 `{"re":[],"count":0}`（已废弃）
- 有效方式：GET `https://guba.eastmoney.com/list,<6位代码>.html`，
  页面尾部内嵌 `var article_list={...}` 的完整 JSON（82 条/页），正则取出后 json.loads
- 字段：`post_title` / `post_click_count` / `post_comment_count` / `post_publish_time` / `stockbar_name`

产出格式与 `social_heat.py` 对齐，方便共用聚合与写表逻辑。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime

import pandas as pd
import requests

from .base import BaseExtractor

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_LIST_RE = re.compile(r"var\s+article_list\s*=\s*(\{.*?\});", re.S)


class GubaHotExtractor(BaseExtractor):
    name = "guba_hot"

    def fetch(self, top_n: int = 10, max_posts: int = 40,
              symbols: list[str] | None = None, **kw) -> pd.DataFrame:
        """抓股吧帖。

        `symbols` 给了就只抓这些票（模拟盘持仓 / 今日信号），否则回退到
        「雪球 A 股热股 Top N」—— 那是全市场视角，跟自己的仓位没关系。
        老刘 2026-11-08 要求：股吧只看自选股和模拟盘里的票。
        """
        from .social_heat import _xueqiu_a_hot

        day = datetime.now().strftime("%Y-%m-%d")
        now = int(time.time())
        if symbols:
            hot = [{"symbol": s} for s in symbols[:max(1, int(top_n))]]
        else:
            try:
                hot = _xueqiu_a_hot(max(1, int(top_n)))
            except Exception:  # noqa: BLE001
                hot = []

        rows: list[dict] = []
        sess = requests.Session()
        sess.headers.update({"User-Agent": UA, "Referer": "https://guba.eastmoney.com/"})
        for item in hot:
            sym = str(item.get("symbol") or "").lower()
            plain = re.sub(r"^(sh|sz|bj)", "", sym)
            if not re.fullmatch(r"\d{6}", plain):
                continue
            try:
                h = sess.get(f"https://guba.eastmoney.com/list,{plain}.html", timeout=12).text
            except Exception:  # noqa: BLE001
                continue
            m = _LIST_RE.search(h)
            if not m:
                continue
            try:
                posts = (json.loads(m.group(1)) or {}).get("re") or []
            except Exception:  # noqa: BLE001
                continue
            for p in posts[:max_posts]:
                title = str(p.get("post_title") or "").strip()
                if not title:
                    continue
                rows.append({
                    "date": day,
                    "source": "股吧",
                    "rank": int(item.get("rank") or 0),
                    "key": title[:60],
                    "code": plain,
                    "heat": float(p.get("post_click_count") or 0)
                            + 5 * float(p.get("post_comment_count") or 0),
                    "is_fin": 1,          # 股吧内容天然是股票相关
                    "fetched_at": now,
                })
            time.sleep(0.35)
        return pd.DataFrame(rows)
