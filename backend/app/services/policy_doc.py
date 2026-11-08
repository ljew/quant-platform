"""政策原文采集：用财联社快讯做「触发器」，反查部委官网原文。

## 为什么不直接爬 gov.cn

`gov.cn/zhengce/zuixin/` 是 JS 动态渲染（HTML 只有骨架），政策文件库路径直接
403。维护一个能抗改版的政府网站爬虫，成本远高于收益。

## 改用「快讯触发 + 搜索引擎反查」

财联社电报是政策发布的第一落点（通常比官网列表页还快），但它只给摘要。
所以：

1. 从财联社快讯里筛出**政策类**（国务院/国常会/发改委/工信部/证监会/央行/财政部…）
2. 用 DeepSeek 的 `/v1/tools/search` 带 `site:ndrc.gov.cn` 这类限定反查原文
3. 校验命中的标题相似度 + 日期接近（否则会抓到三年前的无关旧文）
4. 用 `/v1/tools/fetch` 抓正文，写进 `doc_document`（doc_type='policy'）

成本：每条 1 次 search + 1 次 fetch ≈ 0.01 元。每天政策类快讯 5~15 条，
一个月不到 5 元。

## 校验为什么必要

实测搜「六网协同 新场景 site:ndrc.gov.cn」返回的是 2026-07 的两篇**旧文** ——
因为这条政策当时还没正式发文（只是会议表态）。不校验标题和日期，就会把
无关旧文当政策入库。
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("policy_doc")

# 部门 → 官网域名（site: 限定用）。顺序即优先级。
DEPTS = {
    "国务院": ["gov.cn/zhengce", "gov.cn"],
    "国常会": ["gov.cn/zhengce", "gov.cn"],
    "发改委": ["ndrc.gov.cn"],
    "工信部": ["miit.gov.cn"],
    "证监会": ["csrc.gov.cn"],
    "央行": ["pbc.gov.cn"],
    "人民银行": ["pbc.gov.cn"],
    "财政部": ["mof.gov.cn"],
    "商务部": ["mofcom.gov.cn"],
    "能源局": ["nea.gov.cn"],
    "卫健委": ["nhc.gov.cn"],
}

# 出现这些词说明是「解读/点评」，不是政策本身
_NOISE = ("电报解读", "机构称", "点评", "解读：", "快评", "研报", "早知道")
# 外国机构：它们的名字里含「央行」「财政部」，不排掉会白跑搜索
_FOREIGN = ("欧洲央行", "日本央行", "英国央行", "美联储", "美国财政部",
            "美国商务部", "韩国央行", "澳洲联储")
# 政策体裁词 —— 标题里至少要有一个。
# 除了正式文件名，也收「会议/部署/答记者问」：国常会「部署」某件事同样是
# 可交易的政策信号，只是没有正式文号。实测最近 4 天 42 条含部门词的快讯里
# 全是讲话/数据/外媒，一条正式文件都没有，放宽后才有东西可抓。
_DOC_KW = ("意见", "办法", "条例", "规划", "细则", "通知", "公告", "决定",
           "方案", "指引", "规定", "措施", "试点", "实施意见", "管理办法",
           "会议", "部署", "答记者问", "印发", "出台")
_RE_TAG = re.compile(r"<[^>]+>")
_RE_BRACKET = re.compile(r"^【[^】]*】\s*")
_RE_WS = re.compile(r"[ \t\u3000]+")


def _bigrams(s: str) -> set[str]:
    t = "".join(ch for ch in (s or "") if ch.strip())
    return {t[i:i + 2] for i in range(len(t) - 1)} or ({t} if t else set())


def _similar(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def _clean(s: str) -> str:
    s = _RE_TAG.sub("", s or "")
    s = s.replace("\r\n", "\n").replace("\xa0", " ")
    s = _RE_WS.sub(" ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def is_policy(title: str, body: str = "") -> tuple[bool, str]:
    """判断是不是政策快讯，返回 (是否, 命中的部门)。"""
    t = _RE_BRACKET.sub("", (title or "").strip())
    if not t or any(k in t for k in _NOISE):
        return False, ""
    if any(k in t for k in _FOREIGN):
        return False, ""
    dept = next((d for d in DEPTS if d in t), "")
    if not dept:
        return False, ""
    # 标题里没有体裁词 → 多半是「某领导讲话」「某数据发布」，不是文件
    if not any(k in t for k in _DOC_KW) and "会议" not in t and "部署" not in t:
        return False, ""
    return True, dept


def _candidates(db: Session, days: int, limit: int) -> list[tuple[str, str, str]]:
    """从财联社快讯里挑出政策类条目，返回 (标题, 正文, 日期)。"""
    since = (datetime.now() - timedelta(days=max(1, days))).strftime("%Y-%m-%d")
    rows = db.execute(text(
        "SELECT title, summary, DATE(published_at) FROM news_feed "
        "WHERE source = 'cls' AND published_at >= :since "
        "ORDER BY published_at DESC LIMIT :lim"
    # ⚠️ 候选池要够大：政策快讯在全部快讯里只占百分之几，
    # `limit*6` 等于只看最近 48 条，几乎永远筛不出东西（2026-11-08 实测 0 条）。
    ), {"since": since, "lim": max(600, limit * 40)}).fetchall()

    out, seen = [], set()
    for title, summary, d in rows:
        ok, dept = is_policy(title, summary or "")
        if not ok:
            continue
        key = _RE_BRACKET.sub("", title)[:40]
        if key in seen:
            continue
        seen.add(key)
        out.append((title, dept, str(d)))
        if len(out) >= limit:
            break
    return out


def _insert(db: Session, *, url: str, title: str, body: str,
            published: date, source: str) -> bool:
    """写 doc_document（幂等：doc_id 由 url 决定）。"""
    doc_id = hashlib.sha1(f"policy|{url}".encode()).hexdigest()[:20]
    content_hash = hashlib.sha1(f"{title}|{body[:500]}".encode()).hexdigest()
    now = datetime.now()
    # T+1 可用：政策发布当天收盘后才可能被读到，次日才允许用于交易
    avail = datetime.combine(published, datetime.min.time()) + timedelta(days=1)
    while avail.weekday() >= 5:
        avail += timedelta(days=1)
    try:
        db.execute(text("""
            INSERT INTO doc_document
              (doc_id, content_hash, source, source_doc_id, doc_type, title, body,
               body_len, published_at, first_seen_at, available_at,
               trade_date_applicable, market_scope, link_status, extract_status,
               quality_flag, raw_json)
            VALUES
              (:doc_id, :ch, :src, :sid, 'policy', :title, :body,
               :blen, :pub, :now, :avail,
               :tda, 'market', 'linked', 'done', 'ok', '{}')
            ON CONFLICT(doc_id) DO UPDATE SET
              body = excluded.body, body_len = excluded.body_len
        """), {"doc_id": doc_id, "ch": content_hash, "src": source, "sid": url[:200],
               "title": title[:300], "body": body, "blen": len(body),
               "pub": published.strftime("%Y-%m-%d %H:%M:%S"),
               "now": now.strftime("%Y-%m-%d %H:%M:%S"),
               "avail": avail.strftime("%Y-%m-%d %H:%M:%S"),
               "tda": avail.strftime("%Y-%m-%d")})
        db.commit()
        return True
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.warning("写入政策文档失败：%s", str(e)[:100])
        return False


def collect(db: Session, days: int = 3, limit: int = 12) -> dict:
    """一轮：筛快讯 → 反查 → 校验 → 抓正文 → 入库。"""
    from app.services import ds_search

    stat = {"candidates": 0, "hit": 0, "skip_similar": 0, "empty": 0, "no_url": 0}
    if not ds_search.available():
        return {**stat, "error": "搜索通道不可用"}
    cands = _candidates(db, days, limit)
    stat["candidates"] = len(cands)
    if not cands:
        return stat

    today = date.today()
    for title, dept, _day in cands:
        core = _RE_BRACKET.sub("", title).strip()[:60]
        got = None
        for site in DEPTS.get(dept, ["gov.cn"]):
            hits = ds_search.search(f"{core} site:{site}", count=5,
                                    ori_question="查找政策文件原文")
            for h in hits:
                # 校验 1：标题相似度（同一份文件的标题会被站点改写，0.45 起步）
                if _similar(core, h["title"]) < 0.45:
                    stat["skip_similar"] += 1
                    continue
                # 校验 2：日期不能太旧（政策原文通常当天或前一天发布）
                try:
                    hd = datetime.strptime(h["date"], "%Y-%m-%d").date()
                except Exception:  # noqa: BLE001
                    hd = today
                if (today - hd).days > 7:
                    stat["skip_similar"] += 1
                    continue
                got = (h, hd)
                break
            if got:
                break
        if not got:
            stat["no_url"] += 1
            continue

        h, hd = got
        page = ds_search.fetch(h["url"])
        if page.get("error"):
            stat["empty"] += 1
            continue
        body = _clean(page.get("content") or "")
        if len(body) < 400:          # 太短的多半是列表页/摘要页
            stat["empty"] += 1
            continue
        if _insert(db, url=h["url"], title=h["title"] or core, body=body,
                   published=hd, source=f"gov_{dept}"):
            stat["hit"] += 1
    logger.info("政策原文：候选 %d → 入库 %d", stat["candidates"], stat["hit"])
    return stat
