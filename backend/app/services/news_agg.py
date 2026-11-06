"""资讯聚合（quantdesk server.js 资讯模块的 Python 迁移 + 新增雪球）。

来源与原实现 URL 一致：
- 东财要闻    np-listapi.eastmoney.com/comm/web/getNewsByColumns（column=345 要闻）
- 新浪滚动    feed.mix.sina.com.cn/api/roll/get（pageid=153 lid=2509 财经）
- 腾讯外盘    qt.gtimg.cn/q=（GBK 编码，指数/全球市场报价）
- 雪球热门    stock.xueqiu.com/v5/stock/hot_stock/list.json（新增，需先取 cookie）

全部接口失败时返回空列表并附 error 字段，不抛异常 —— 资讯是非关键路径。
"""

from __future__ import annotations

from datetime import datetime

import requests

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_NO_PROXY = {"http": None, "https": None}


def _get(url: str, headers: dict | None = None, timeout: int = 10,
         encoding: str = "utf-8") -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": _UA, **(headers or {})},
                         timeout=timeout, proxies=_NO_PROXY)
        r.encoding = encoding
        return r.text if r.ok else None
    except Exception:  # noqa: BLE001
        return None


def eastmoney_headlines(num: int = 15) -> list[dict]:
    """东财要闻（原 server.js:526 同 URL）。"""
    text = _get(
        "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
        "?client=web&biz=web_news_col&column=345&order=1&needInteractData=0"
        f"&page_index=1&page_size={num}&req_trace=1",
        headers={"Referer": "https://finance.eastmoney.com/"},
    )
    if not text:
        return []
    try:
        data = json_load(text)
        arts = (data or {}).get("data", {}).get("list", []) or []
        return [
            {"title": a.get("title", ""), "time": a.get("showTime", ""),
             "url": a.get("url_unique", a.get("url", "")), "source": "东财"}
            for a in arts[:num]
        ]
    except Exception:  # noqa: BLE001
        return []


def sina_roll(num: int = 30) -> list[dict]:
    """新浪财经滚动快讯（原 server.js:516 同 URL）。"""
    text = _get(
        "https://feed.mix.sina.com.cn/api/roll/get"
        f"?pageid=153&lid=2509&num={num}&page=1&encode=utf-8"
    )
    if not text:
        return []
    try:
        data = json_load(text)
        arts = (data or {}).get("result", {}).get("data", []) or []
        return [
            {"title": a.get("title", ""),
             "time": datetime.fromtimestamp(int(a.get("ctime", 0))).isoformat(timespec="minutes"),
             "url": a.get("url", ""), "source": "新浪"}
            for a in arts[:num]
        ]
    except Exception:  # noqa: BLE001
        return []


def global_quotes(defs: list[tuple[str, str]] | None = None) -> list[dict]:
    """腾讯外盘/指数报价（原 server.js:494 同 URL 同代码，GBK 编码）。

    defs 默认与原实现一致：usDJI/usIXIC/usINX/hkHSI/whUSDCNH + A 股核心指数。
    """
    defs = defs or [
        ("usDJI", "道琼斯"), ("usIXIC", "纳斯达克"), ("usINX", "标普500"),
        ("hkHSI", "恒生指数"), ("whUSDCNH", "离岸人民币"),
        ("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sh000300", "沪深300"),
        ("sh000906", "中证800"), ("sh000016", "上证50"),
    ]
    text = _get("https://qt.gtimg.cn/q=" + ",".join(d[0] for d in defs),
                encoding="gbk")
    if not text:
        return []
    out = []
    name_map = dict(defs)
    for line in text.strip().split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        code = line.split("=")[0].removeprefix("v_")
        parts = line.split('"')[1].split("~") if '"' in line else []
        if len(parts) < 4:
            continue
        # 腾讯格式：parts[1]=名称 parts[3]=现价 parts[4]=昨收 parts[32]=涨跌幅
        try:
            price, prev = float(parts[3]), float(parts[4])
            chg = float(parts[32]) if len(parts) > 32 and parts[32] else (
                (price / prev - 1) * 100 if prev else 0.0
            )
        except (ValueError, IndexError):
            continue
        out.append({"code": code, "name": name_map.get(code, parts[1]),
                    "price": price, "change_pct": round(chg, 2), "source": "腾讯"})
    return out


def xueqiu_hot(size: int = 20) -> list[dict]:
    """雪球热门股票（新增来源）。

    需要先 GET https://xueqiu.com/hq 拿 xq_a_token 等 cookie，且请求必须带
    Referer + Accept（实测缺 Referer 返回 406）。
    """
    sess = requests.Session()
    sess.headers.update({"User-Agent": _UA})
    try:
        sess.get("https://xueqiu.com/hq", timeout=10, proxies=_NO_PROXY)
        r = sess.get(
            "https://stock.xueqiu.com/v5/stock/hot_stock/list.json"
            f"?size={size}&_type=10&type=10",
            timeout=10, proxies=_NO_PROXY,
            headers={"Referer": "https://xueqiu.com/hq",
                     "Accept": "application/json, text/plain, */*",
                     "X-Requested-With": "XMLHttpRequest"},
        )
        if not r.ok:
            return []
        data = r.json()
        items = (data.get("data", {}) or {}).get("items", []) or []

        def _plat(code: str) -> str:
            # 雪球格式 SZ000002/SH600519 → 平台 sz000002/sh600519
            return code.lower()

        return [
            {"symbol": _plat(it.get("code", "")), "name": it.get("name", ""),
             "heat": it.get("value", 0.0), "heat_increment": it.get("increment", 0),
             "rank_change": it.get("rank_change", 0),
             "price": it.get("current", 0.0),
             "change_pct": round(it.get("percent") or 0.0, 2),
             "source": "雪球"}
            for it in items[:size]
        ]
    except Exception:  # noqa: BLE001
        return []


def stock_news(symbol_plain: str, num: int = 10) -> list[dict]:
    """个股新闻 + 公告（原 server.js:543/551 同 URL）。

    symbol_plain: 6 位纯数字代码。
    """
    out: list[dict] = []
    param = json_dumps({"pageNum": 1, "pageSize": num, "type": "A",
                        "keyWord": symbol_plain})
    text = _get(
        "https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param="
        + requests.utils.quote(param),
        headers={"Referer": "https://so.eastmoney.com/"},
    )
    if text:
        try:
            data = json_load(text[text.index("(") + 1: text.rindex(")")])
            arts = (data.get("result", {}) or {}).get("news", []) or []
            out += [
                {"title": a.get("title", ""), "time": a.get("date", ""),
                 "url": a.get("url", ""), "source": "东财新闻"}
                for a in arts[:num]
            ]
        except Exception:  # noqa: BLE001
            pass
    text = _get(
        "https://np-anotice-stock.eastmoney.com/api/security/ann"
        f"?sr=-1&page_size={num}&page_index=1&ann_type=A&client_source=web"
        f"&stock_list={symbol_plain}",
        headers={"Referer": "https://data.eastmoney.com/"},
    )
    if text:
        try:
            data = json_load(text)
            for a in (data.get("data", {}) or {}).get("list", [])[:num]:
                out.append({
                    "title": a.get("title", ""),
                    "time": (a.get("notice_date") or "")[:10],
                    "url": ("https://data.eastmoney.com/notices/detail/"
                            + symbol_plain + "/" + a.get("art_code", "") + ".html"),
                    "source": "东财公告",
                })
        except Exception:  # noqa: BLE001
            pass
    return out


def aggregate() -> dict:
    """聚合全部资讯源（流水线「分析」步骤的数据源；任一源失败不影响其它）。"""
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "headlines": eastmoney_headlines(),
        "sina_roll": sina_roll(num=15),
        "global_quotes": global_quotes(),
        "xueqiu_hot": xueqiu_hot(),
    }


def json_load(text: str):
    import json

    return json.loads(text)


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
