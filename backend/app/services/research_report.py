"""券商研报采集（东方财富研报中心 reportapi）。

## 为什么做这个

A 股最强的单因子之一是**盈利预测修正**：分析师把明年 EPS 从 1.0 调到 1.2，
比任何技术指标都更早反映基本面变化。此前平台完全没有这类数据。

## 接口

    GET https://reportapi.eastmoney.com/report/list
        ?qType=0            # 0=个股研报 1=行业研报 2=策略
        &pageSize=100&pageNo=1
        &beginTime=2026-11-01&endTime=2026-11-08
        &code=*&industry=*&rating=&ratingChange=

返回 `{hits, size, data: [...]}`，字段含：
`infoCode`（主键）/ `title` / `stockCode` / `stockName` / `orgSName`（机构简称）/
`publishDate` / `emRatingName`（买入/增持/中性…）/ `indvInduName`（行业）/
`predictThisYearEps` / `predictNextYearEps` / `predictNextTwoYearEps` 及对应 PE。

实测 2026-11-01~11-08 有 744 篇（一周），按此推算全市场每月约 3000 篇。

## 只存元数据，不存正文

正文是 PDF（`pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf`），几 MB 一份、且需要
PDF 解析。先把「谁在什么时候把评级/盈利预测改成了什么」拿下来 —— 这部分
结构化、可直接算因子；正文按需再抓。
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

logger = logging.getLogger("research_report")

API = "https://reportapi.eastmoney.com/report/list"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PAGE_SIZE = 100
MAX_PAGES = 40          # 单次最多 4000 篇，够覆盖一个月的回补


def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_page(begin: str, end: str, page: int, qtype: int = 0) -> dict:
    qs = urllib.parse.urlencode({
        "cb": "", "industryCode": "*", "pageSize": PAGE_SIZE, "industry": "*",
        "rating": "", "ratingChange": "", "beginTime": begin, "endTime": end,
        "pageNo": page, "fields": "", "qType": qtype,
        "orgCode": "", "code": "*", "rcode": "", "p": page, "pageNum": page,
    })
    req = urllib.request.Request(
        f"{API}?{qs}",
        headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/report/"},
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read().decode("utf-8", "ignore").strip()
    # 接口带 cb= 时会包 jsonp，容错处理
    if raw.startswith("(") or (not raw.startswith("{")):
        i, j = raw.find("("), raw.rfind(")")
        if i >= 0 and j > i:
            raw = raw[i + 1:j]
    return json.loads(raw)


def sync(db: Session, days: int = 7, qtype: int = 0) -> int:
    """拉最近 days 天的研报（按 info_code 幂等 upsert），返回写入条数。"""
    from app.models import ResearchReport

    end = date.today()
    begin = end - timedelta(days=max(1, days))
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        try:
            d = _fetch_page(begin.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                            page, qtype)
        except Exception as e:  # noqa: BLE001
            logger.warning("研报接口失败（page=%d）：%s", page, str(e)[:90])
            break
        data = d.get("data") or []
        if not data:
            break
        for r in data:
            code = str(r.get("infoCode") or "").strip()
            if not code:
                continue
            pub = str(r.get("publishDate") or "")[:10]
            try:
                pub_d = datetime.strptime(pub, "%Y-%m-%d").date()
            except ValueError:
                pub_d = end
            rows.append({
                "info_code": code,
                "title": str(r.get("title") or "")[:300],
                "stock_code": str(r.get("stockCode") or "")[:8],
                "stock_name": str(r.get("stockName") or "")[:32],
                "org_code": str(r.get("orgCode") or "")[:16],
                "org_name": str(r.get("orgName") or "")[:64],
                "org_sname": str(r.get("orgSName") or "")[:32],
                "publish_date": pub_d,
                "rating": str(r.get("emRatingName") or "")[:16],
                "industry": str(r.get("indvInduName") or "")[:32],
                "eps_this": _f(r.get("predictThisYearEps")),
                "eps_next": _f(r.get("predictNextYearEps")),
                "eps_next2": _f(r.get("predictNextTwoYearEps")),
                "pe_this": _f(r.get("predictThisYearPe")),
                "pe_next": _f(r.get("predictNextYearPe")),
                "pe_next2": _f(r.get("predictNextTwoYearPe")),
                "url": f"https://data.eastmoney.com/report/info/{code}.html",
                "created_at": datetime.now(),
            })
        if len(data) < PAGE_SIZE:
            break
    if not rows:
        return 0

    # 去重（同一 info_code 只留一条）
    uniq = {x["info_code"]: x for x in rows}
    vals = list(uniq.values())
    skip = {"info_code"}
    done = 0
    for i in range(0, len(vals), 400):        # SQLite 变量上限，分批
        chunk = vals[i:i + 400]
        stmt = insert(ResearchReport).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["info_code"],
            set_={k: getattr(stmt.excluded, k) for k in chunk[0] if k not in skip},
        )
        db.execute(stmt)
        db.commit()
        done += len(chunk)
    logger.info("研报同步 %d 篇（%s ~ %s）", done, begin, end)
    return done
