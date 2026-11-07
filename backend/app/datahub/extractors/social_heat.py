"""社交热度采集：雪球 A 股热股 + 微博热搜。

## 为什么这两个源不能当「情绪」用

雪球热股和微博热搜**没有正文**，只有「热度 / 搜索量」。拿多空词典去打分没有输入，
硬凑就是编数据。所以这里只取**关注度**这一层：

- 雪球：A 股热股榜（type=12）前 N 名的热度值与排名变化 → 散户资金关注谁
- 微博：热搜榜里命中财经关键词的条数与热度 → 全民层面的市场注意力

产出落到 `social_heat_daily`（date + source 唯一），与 `news_market_daily`（多空情绪）
互补：一个是「大家在聊什么」，一个是「聊的是偏多还是偏空」。

## 实测备注（2026-11-07）

- 雪球必须先用同一 Session 访问 `xueqiu.com/hq` 拿 `xq_a_token` cookie，
  且请求要带 Referer + X-Requested-With，缺 Referer 返回 406。
- 雪球 `type=10/14` 是全球热股（NVDA/AMD），**A 股是 `type=12`**，港股 `type=13`。
- 微博 `m.weibo.cn` 那套容器接口返回 432（反爬），但 `weibo.com/ajax/side/hotSearch`
  免登录可用，返回 realtime 约 52 条，带 num（热度）。
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime

import pandas as pd
import requests

from .base import BaseExtractor

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 微博热搜里判「跟市场有关」的关键词。宁可少判，也别把娱乐热搜当财经。
FIN_KW = (
    "A股", "股市", "股票", "上证", "深证", "创业板", "科创板", "北交所", "港股", "美股",
    "央行", "降准", "降息", "加息", "LPR", "MLF", "逆回购", "流动性",
    "证监会", "IPO", "注册制", "退市", "减持", "回购", "分红", "并购", "重组",
    "基金", "ETF", "债券", "国债", "城投", "汇率", "人民币", "美元", "美联储",
    "关税", "出口", "进口", "外贸", "GDP", "CPI", "PPI", "PMI", "通胀", "通缩",
    "财政", "国债", "专项债", "国常会", "政治局", "经济工作会议",
    "涨停", "跌停", "市值", "股价", "业绩", "财报", "预告", "净利", "营收",
    "黄金", "原油", "铜价", "锂价", "光伏", "储能", "芯片", "半导体", "算力",
    "房地产", "楼市", "房贷", "汽车", "新能源", "医药", "集采",
    # 龙头公司名：热搜里提到它们，往往就是资金在盯的方向
    "小米", "华为", "比亚迪", "宁德时代", "特斯拉", "苹果", "英伟达", "阿里",
    "腾讯", "京东", "美团", "拼多多", "字节", "寒武纪", "中芯", "茅台",
    # 市场行为词
    "上市", "退市", "罚款", "监管", "调查", "做空", "爆雷", "违约", "减持",
    "涨", "跌", "涨价", "降价", "销量", "裁员", "破产", "关税",
)


def _xueqiu_a_hot(size: int = 50) -> list[dict]:
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    sess.get("https://xueqiu.com/hq", timeout=10)
    r = sess.get(
        "https://stock.xueqiu.com/v5/stock/hot_stock/list.json"
        f"?size={int(size)}&_type=12&type=12",
        timeout=10,
        headers={"Referer": "https://xueqiu.com/hq",
                 "Accept": "application/json, text/plain, */*",
                 "X-Requested-With": "XMLHttpRequest"},
    )
    items = (r.json().get("data") or {}).get("items") or []
    out = []
    for i, it in enumerate(items, 1):
        code = str(it.get("code") or "").lower()      # SZ002384 -> sz002384
        out.append({
            "rank": i,
            "symbol": code,
            "name": it.get("name") or "",
            "heat": float(it.get("value") or 0),
        })
    return out


def _weibo_hot() -> list[dict]:
    req = urllib.request.Request(
        "https://weibo.com/ajax/side/hotSearch",
        headers={"User-Agent": UA, "Referer": "https://weibo.com/",
                 "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        d = json.loads(r.read().decode("utf-8", "ignore"))
    rt = ((d.get("data") or {}).get("realtime")) or []
    out = []
    for i, it in enumerate(rt, 1):
        word = str(it.get("word") or "").strip()
        if not word:
            continue
        out.append({
            "rank": i,
            "word": word,
            "heat": float(it.get("num") or 0),
            "is_fin": int(any(k in word for k in FIN_KW)),
        })
    return out


class SocialHeatExtractor(BaseExtractor):
    """雪球 A 股热股 + 微博热搜 → 关注度日频。

    fetch() 返回明细行（source/rank/key/heat/is_fin），聚合在 runner 里做：
    明细留档便于回溯「到底哪条热搜算进了财经」，只存聚合值会查不清。
    """

    name = "social_heat"

    def fetch(self, xq_size: int = 50, weibo_size: int = 60, **kw) -> pd.DataFrame:
        now = int(time.time())
        day = datetime.now().strftime("%Y-%m-%d")
        rows: list[dict] = []

        try:
            for it in _xueqiu_a_hot(xq_size)[:xq_size]:
                rows.append({
                    "date": day, "source": "雪球", "rank": it["rank"],
                    "key": it["name"], "code": it["symbol"],
                    "heat": it["heat"], "is_fin": 1, "fetched_at": now,
                })
        except Exception:  # noqa: BLE001
            pass

        try:
            for it in _weibo_hot()[:weibo_size]:
                rows.append({
                    "date": day, "source": "微博", "rank": it["rank"],
                    "key": it["word"], "code": "",
                    "heat": it["heat"], "is_fin": it["is_fin"], "fetched_at": now,
                })
        except Exception:  # noqa: BLE001
            pass

        return pd.DataFrame(rows)
