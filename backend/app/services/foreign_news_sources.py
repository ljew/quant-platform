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
        # 2026-11-12 白天一度整条 feed 不可达（容器 20s 超时、宿主代理/直连全断、
        # http 与 rsshub 镜像 502/000），当时置 False 止血。
        # 同日下午代理通道恢复后实测：容器内 HTTP 200 / 41KB / 1.0s，已改回 True。
        "enabled_default": True,
        "tags": ["media"],
    },
    {
        "key": "fn_wsj_mkt",
        "label": "WSJ Markets",
        "kind": "rss",
        "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
        # 2026-11-12 实测：这条 feed 内容冻结在 2025-01-27（返回的头条是
        # "Stocks Sink in Broad AI Rout Sparked by China's DeepSeek"），
        # 官方 wsj.com/xml/rss/3_7031.xml 等新地址一律 401（付费墙）。
        # 灌进来的是**一年多前的旧闻**，比没有更糟 —— 会污染资讯流与日报素材，
        # 故停用。哪天 DJ 恢复更新再打开。
        "enabled_default": False,
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
    # —— 代理通道打通后重新启用（2026-11-12）——
    {
        "key": "fn_ft",
        "label": "FT",
        "kind": "rss",
        "url": "https://www.ft.com/?format=rss",
        # 2026-11-12 代理通道打通后实测 HTTP 200 / 6.5KB / 1.5s，改为默认开。
        "enabled_default": True,
        "tags": ["media"],
    },
    {
        "key": "fn_boe",
        "label": "BoE News",
        "kind": "rss",
        "url": "https://www.bankofengland.co.uk/rss/news",
        # 同上，实测 HTTP 200 / 26KB / 2.3s。
        "enabled_default": True,
        "tags": ["central-bank", "uk"],
    },
    # —— 默认关：Google News 镜像，各约 100 条且与 CNBC/MarketWatch 大量重复，
    #    开着会淹掉一手源。需要时把 enabled_default 改 True 即可（通道已通）。——
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
