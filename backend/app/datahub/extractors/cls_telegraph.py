"""财联社电报：A 股时效性最强的快讯源。

## 为什么值得接

对比其它文本源，财联社电报有三个别处没有的字段：

- `stock_list`  —— **接口直接标注了相关个股**（含代码），比「正文里匹配公司简称」
  准得多（公众号/东财只能靠简称匹配，容易漏也容易误判）
- `plate_list`  —— 所属板块
- `level`       —— 重要等级（A/B/C），可用来只取重点电报

有标题、有正文、有阅读量，是这批数据源里信噪比最高的一个。

## 接口要点（2026-11-07 实测）

- 路径 `https://www.cls.cn/v1/roll/get_roll_list`，老的 `nodeapi/telegraphList` 已 404
- **必须带 sign**：参数按 key 排序拼成 `k=v&k=v`，先 sha1 再 md5
- 分页用 `last_time`（传上一页最后一条的 `ctime`），不是页码
- 要带 Referer `https://www.cls.cn/telegraph`，否则容易被拦

产出字段与其它文本源对齐：
`article_id / source / title / content_text / publish_time / fetched_at`
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from datetime import datetime

import pandas as pd

from .base import BaseExtractor

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
API = "https://www.cls.cn/v1/roll/get_roll_list"


def _sign(params: dict) -> str:
    """财联社签名：参数按 key 排序拼串 → sha1 → md5。"""
    raw = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.md5(hashlib.sha1(raw.encode()).hexdigest().encode()).hexdigest()


class ClsTelegraphExtractor(BaseExtractor):
    name = "cls_telegraph"

    def fetch(self, rn: int = 50, pages: int = 3, min_level: str = "", **kw) -> pd.DataFrame:
        """拉最近若干页电报。min_level 可只保留 A/B 级（'' 表示全要）。

        ⚠️ `rn` 上限实测为 50：传 100 时接口直接返回空 `roll_data`
        （2026-11-07 踩过，表现为「采集成功但 0 条」）。要更多就多翻页。
        """
        rn = min(int(rn), 50)
        rows: list[dict] = []
        seen: set[str] = set()
        last_time = ""
        fetched_at = int(time.time())
        for _ in range(max(1, int(pages))):
            p = {
                "app": "CailianpressWeb", "category": "",
                "last_time": str(last_time), "os": "web",
                "refresh_type": "1", "rn": str(int(rn)), "sv": "7.7.5",
            }
            p["sign"] = _sign(p)
            url = API + "?" + "&".join(f"{k}={p[k]}" for k in p)
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json",
                "Referer": "https://www.cls.cn/telegraph",
            })
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    d = json.loads(r.read().decode("utf-8", "ignore"))
            except Exception:  # noqa: BLE001
                break
            items = ((d.get("data") or {}).get("roll_data")) or []
            if not items:
                break
            for it in items:
                title = str(it.get("title") or "").strip()
                body = str(it.get("content") or it.get("brief") or "").strip()
                # 正文常以「【标题】」开头，去掉避免标题重复计分
                if title and body.startswith("【"):
                    cut = body.find("】")
                    if cut > 0:
                        body = body[cut + 1:].lstrip("，, ")
                level = str(it.get("level") or "").strip()
                if min_level and level and level > min_level:
                    continue
                # 接口自带的相关个股 / 板块：拼到正文尾部，供个股匹配直接命中，
                # 比靠简称在正文里找可靠得多
                extra = []
                for s in (it.get("stock_list") or []):
                    nm = s.get("name") or s.get("stock_name") or ""
                    if nm:
                        extra.append(nm)
                for pl in (it.get("plate_list") or []):
                    nm = pl.get("name") or ""
                    if nm:
                        extra.append(nm)
                if extra:
                    body = (body + " " + " ".join(f"${x}" for x in extra)).strip()
                if not title and not body:
                    continue
                ctime = int(it.get("ctime") or fetched_at)
                aid = f"cls_{it.get('id') or ctime}"
                if aid in seen:      # 翻页边界会重复返回同一条
                    continue
                seen.add(aid)
                rows.append({
                    "article_id": aid,
                    "source": "财联社",
                    "title": title or body[:40],
                    "content_text": body,
                    "publish_time": ctime,
                    "fetched_at": fetched_at,
                })
            last_time = max((int(x.get("ctime") or 0) for x in items), default=0)
            if not last_time:
                break
            time.sleep(0.4)
        return pd.DataFrame(rows)
