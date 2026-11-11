"""境外资讯源注册表（写死；OPS-FN-1a）。

对齐契约 docs-design-OPS-FN-1-foreign-news.md §3.2：
- 默认开 8 个 RSS + fn_x
- 默认关 4 个（FT/BoE/GNews 镜像）
- 启用态以本表 enabled_default 为准；本切片不做完整管理后台
"""
from __future__ import annotations

from typing import Any

# Google News RSS（非官方镜像；仅标题级，产品给定 query）
_GNEWS_REU = (
    "https://news.google.com/rss/search?"
    "q=site:reuters.com+(China+OR+Fed+OR+markets)&hl=en-US&gl=US&ceid=US:en"
)
_GNEWS_BBG = (
    "https://news.google.com/rss/search?"
    "q=site:bloomberg.com+(China+OR+Fed)&hl=en-US&gl=US&ceid=US:en"
)

FOREIGN_SOURCES: list[dict[str, Any]] = [
    # —— 默认开（8 RSS）——
    {
        "key": "fn_fed",
        "label": "Fed 新闻稿",
        "kind": "rss",
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
        "enabled_default": True,
        "tags": ["central-bank", "us"],
    },
    {
        "key": "fn_fed_mon",
        "label": "Fed 货币",
        "kind": "rss",
        "url": "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "enabled_default": True,
        "tags": ["central-bank", "us"],
    },
    {
        "key": "fn_sec",
        "label": "SEC 新闻稿",
        "kind": "rss",
        "url": "https://www.sec.gov/news/pressreleases.rss",
        "enabled_default": True,
        "tags": ["regulator", "us"],
    },
    {
        "key": "fn_bbc_biz",
        "label": "BBC Business",
        "kind": "rss",
        "url": "https://feeds.bbci.co.uk/news/business/rss.xml",
        # 2026-11-12 实测：这条 feed 从本网络彻底不可达 —— 容器内 20s 超时、
        # 宿主走代理 10s 断、直连 25s 断，http/rsshub 镜像也全 502/000。
        # 留着只会每槽（07:00/12:30/21:00）刷一条「异常」，且拖慢整批。
        # 网络恢复后把它改回 True 即可，采集逻辑不用动。
        "enabled_default": False,
        "tags": ["media"],
    },
    {
        "key": "fn_wsj_mkt",
        "label": "WSJ Markets",
        "kind": "rss",
        "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
        "enabled_default": True,
        "tags": ["media"],
    },
    {
        "key": "fn_cnbc",
        "label": "CNBC Top",
        "kind": "rss",
        "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "enabled_default": True,
        "tags": ["media"],
    },
    {
        "key": "fn_mw",
        "label": "MarketWatch",
        "kind": "rss",
        "url": "https://feeds.marketwatch.com/marketwatch/topstories/",
        "enabled_default": True,
        "tags": ["media"],
    },
    {
        "key": "fn_ecb",
        "label": "ECB Press",
        "kind": "rss",
        "url": "https://www.ecb.europa.eu/rss/press.html",
        "enabled_default": True,
        "tags": ["central-bank", "eu"],
    },
    # —— 默认关 ——
    {
        "key": "fn_ft",
        "label": "FT",
        "kind": "rss",
        "url": "https://www.ft.com/?format=rss",
        "enabled_default": False,
        "tags": ["media"],
    },
    {
        "key": "fn_boe",
        "label": "BoE News",
        "kind": "rss",
        "url": "https://www.bankofengland.co.uk/rss/news",
        "enabled_default": False,
        "tags": ["central-bank", "uk"],
    },
    {
        "key": "fn_gnews_reu",
        "label": "GNews·Reuters 镜像",
        "kind": "rss",
        "url": _GNEWS_REU,
        "enabled_default": False,
        "tags": ["media", "mirror"],
    },
    {
        "key": "fn_gnews_bbg",
        "label": "GNews·Bloomberg 镜像",
        "kind": "rss",
        "url": _GNEWS_BBG,
        "enabled_default": False,
        "tags": ["media", "mirror"],
    },
    # —— X（无 Bearer 时运行态 → unconfigured）——
    {
        "key": "fn_x",
        "label": "X · 宏观账号",
        "kind": "x",
        "url": None,
        "enabled_default": True,
        "tags": ["x", "macro"],
    },
]

# 默认 handles（12）；可选关的账号本切片不拉
DEFAULT_X_HANDLES: list[str] = [
    "federalreserve",
    "USTreasury",
    "SecGov",
    "IMFNews",
    "ecb",
    "bankofengland",
    "WSJ",
    "FT",
    "Reuters",
    "CNBC",
    "business",
    "DeItaone",
]

# source key → 展示名（含默认关，避免入库后标签空白）
FOREIGN_SOURCE_LABELS: dict[str, str] = {s["key"]: s["label"] for s in FOREIGN_SOURCES}

# 前缀：查询时筛全部境外源
FOREIGN_SOURCE_PREFIX = "fn_"


def all_foreign_keys() -> list[str]:
    return [s["key"] for s in FOREIGN_SOURCES]


def enabled_sources() -> list[dict[str, Any]]:
    return [s for s in FOREIGN_SOURCES if s.get("enabled_default")]


def get_source(key: str) -> dict[str, Any] | None:
    for s in FOREIGN_SOURCES:
        if s["key"] == key:
            return s
    return None
