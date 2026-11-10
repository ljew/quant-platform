"""热点简报（按交易时段）：双来源资讯 → LLM 筛选 → 结构化「对 A 股有没有实质影响」。

触发时刻表见 `core/hot_brief_scheduler.py`：交易日 07/09/10/11/13/14/16/21 点，
非交易日 09/21 点。每轮的取数窗口 = 距上一个时段的实际分钟数（隔夜那轮约 10 小时）。

## 两条腿取数

1. **一手快讯**：财联社电报（时效最强，覆盖政策/公告/盘中异动）
2. **网页搜索**：DeepSeek `/v1/tools/search`（覆盖快讯发不到的地方：
   部委原文、行业数据、券商观点、隔夜外盘）

只有快讯时，会漏掉「财联社没发但重要」的东西；只有搜索时，会慢半拍且
拿到的多是二手转载。两者合起来才既有速度又有广度。

## 一段需要记住的误判（2026-11-07）

当天我先断言「DeepSeek API 没有联网能力」，依据是 `tools:[{type:"web_search"}]`
返回 422、`/v1/search` 等路径 404。**这两条都不是证据**：联网能力做成了独立
端点 `/v1/tools/search` + `/v1/tools/fetch`，我试的路径都不是它。老刘给了
文档链接才纠正。教训：**「试了几条路径都 404」≠「功能不存在」，先查文档**。

## 提示词取向（按老刘的偏好）

- 不做摘要，只做影响判断：每条必须能落到板块/个股/资金面
- 没有具体数字支撑的「看好/警惕」一律丢弃 —— 这类话在研报里最廉价
- 拿不准就少说，宁可 3 条也不凑 8 条
- 明确给方向和强度，避免「可能影响、需关注」这种免责表述
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, time as dtime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HotBrief

logger = logging.getLogger("hot_brief")

SYSTEM_PROMPT = """你是一名 A 股量化投研分析师，服务于一个只看「能不能落地」的研究流程。
你的输出会直接用于判断当日交易方向，所以：

1. 不做新闻摘要。读者已经看过原文，他要的是判断。
2. 没有具体数字支撑的「看好 / 警惕 / 值得关注」一律丢弃 —— 这类话最廉价。
3. 拿不准就少说。宁可给 3 条真的，也不要凑 8 条凑数。
4. 不要写「可能影响」「需关注」这种免责表述，要给出明确方向。
5. 只关心 A 股：海外消息只有在能传导到 A 股板块/个股时才保留。
6. 同一件事在不同来源重复出现时，合并成一条，不要输出两遍。
"""

USER_TEMPLATE = """下面是多个来源的资讯（顺序：快讯 → 境外日爬 → 搜索 → 其他媒体）。

【一】一手快讯（财联社电报，过去 {minutes} 分钟，共 {n_flash} 条）
{flash_corpus}
{foreign_block}{search_block}{recent_block}
请筛出**对 A 股有实质影响**的，按下述结构输出。

筛选标准（严格执行）：
- 能落到「行业 / 板块 / 个股 / 资金面」的才留
- 剔除：纯人物动态、无数据的产品发布、重复报道、无具体数字的预测性喊话
- 多个来源说的是同一件事，合并成一条
- 搜索腿里明显是旧闻（事件发生时间早于最近 3 天）直接丢弃
- 优先：货币政策、财政、监管、产业政策；有金额/增速/产能/价格数字的
- 最多 10 条

每条字段：
- theme: 主题，不超过 20 字
- direction: bullish / bearish / neutral（对 A 股整体或对应板块）
- sectors: 受影响板块，申万一级或二级，最多 3 个，数组
- tickers: 明确提到的 A 股公司名或代码，没有就空数组
- horizon: 1-3日 / 1-4周 / 长期
- why: 影响路径，一句话说清「因为什么 → 所以什么」，不超过 40 字
- strength: 1-5 整数，1=可忽略，5=可能改变趋势
- src: 这条来自哪些输入条目，用输入的编号，例如 "F3"、"S2"、"O1"、"M4"、"F1,O2"。
  合并了多个来源就把编号都写上，用英文逗号分隔。**必须填真实编号，不要编造。**

另外给两个整体判断：

- mainline：一句话主线，**60 字以内**，说清「现在市场在交易什么」。

⚠️ 不再生成 summary（今日综述）。原因：它和「当日全景」（brief_digest）
是同一件事的两份稿子 —— 这份每轮重写 937 字，全景每天 21:45 写 1680 字，
用户看到的是重复内容。2026-11-10 合并成一份：简报只管「本轮新增 + 一句话主线」，
整体判断交给每轮刷新的全景。

只输出 JSON，不要任何解释、不要 markdown 代码块：
{{"items": [...], "mainline": "..."}}
"""

SEARCH_BLOCK = """
【三】网页搜索补充（共 {n_search} 条；可能含二手转载，注意核对时效）
{corpus}
"""

MEDIA_BLOCK = """
【四】其他媒体（东方财富 / 新浪 / tushare / 公众号，过去 {hours} 小时，共 {n_media} 条）
{corpus}
"""

FOREIGN_BLOCK = """
【二】境外日爬（平台自持 RSS/X，过去 {hours} 小时，共 {n_foreign} 条）
{corpus}
"""

FOREIGN_BLOCK_EMPTY = """
【二】境外日爬（平台自持 RSS/X，过去 {hours} 小时，共 0 条）
（境外源本轮无新增或未配置 —— 禁止编造外盘新闻）
"""

RECENT_BLOCK = """
【四】今日已经报过的主题（前面各轮累计，共 {n_prev} 条）
{themes}

除非出现**实质新增信息**（新数字、新进展、方向反转），否则不要重复输出上面的主题。
items 只报本轮真正的新增变化。

🔴 如果本轮没有任何需要新增的内容，items 必须返回空数组 []。
不要为了填满条目而重复上面的主题、或把无关内容降级硬塞进来 —— 空结果是有价值的结论。
但 summary 仍要正常写（它覆盖今日整体，不受 items 是否为空影响）。
"""


MERGE_PROMPT = """下面是今日已收录的 A 股主题（编号 + 主题 + 影响路径）。
其中若干条是同一件事的不同说法（不同来源标题不一样）。请挑出应当保留的编号。

判断标准：
- 同一政策 / 同一事件 / 同一批经济数据 → 只留信息最全的一条
  例：「暴雪黄色预警覆盖东北华北」「京津冀暴雪与强冷空气」「强冷空气来袭气温创新低」
      都是同一次冷空气过程，合并
  例：「央行连续24个月增持黄金」「10月外储回升、黄金连增24月」是同一批数据，合并
  例：「美制裁中企与中方严正关切」「美方制裁两家中国企业」是同一事件，合并
  例：「Fed 维持利率不变」「美联储按兵不动」与境外日爬/搜索同一声明，合并
- 不同对象、不同数据、不同政策措施 → 都保留
  例：「国常会定调稳市机制」与「券商互换便利落地」相关但抓手不同，保留
  例：「中央企业外资利用超1200亿美元」与「央企前三季进口同比增5.1%」是不同数据，保留
- **宁可少合并，也不要把不同事件并掉**

只输出 JSON：{{"keep": [编号...]}}（按原顺序，至少保留 1 个），不要解释。

{items}
"""


def _noise_filter(text: str) -> bool:
    """粗筛掉明显无信息量的条目，减少 token 浪费。"""
    t = (text or "").strip()
    if len(t) < 8:
        return True
    junk = ("点击查看", "扫码", "广告", "招聘", "转载", "免责声明")
    return any(k in t for k in junk)


def collect_flash(minutes: int = 60, limit: int | None = None,
                  pages: int | None = None) -> list[dict]:
    """拉最近 minutes 分钟的财联社电报（一手快讯，时效最强）。

    ⚠️ 翻页数必须跟窗口走：调度改成时段制后，21:00 → 次日 07:00 隔了 10 小时，
    固定 `pages=2`（100 条）只够两三个小时的量，隔夜消息会漏掉一大半。
    `rn` 上限仍是 50 —— 实测 `rn=100` 会返回空 `roll_data`。
    """
    from app.datahub.extractors.cls_telegraph import ClsTelegraphExtractor

    pages = pages or (2 if minutes <= 180 else 4)
    limit = limit or min(120, max(60, minutes // 2))
    try:
        df = ClsTelegraphExtractor().fetch(rn=50, pages=pages)
    except Exception:  # noqa: BLE001
        return []
    if df is None or df.empty:
        return []
    cutoff = (datetime.now() - timedelta(minutes=minutes)).timestamp()
    rows = []
    for _, r in df.iterrows():
        ts = int(r.get("publish_time") or 0)
        if ts < cutoff:
            continue
        title = str(r.get("title") or "").strip()
        body = str(r.get("content_text") or "").strip()
        if _noise_filter(title + body):
            continue
        rows.append({"src": "快讯", "ts": ts, "title": title,
                     # 存全文（截 1500 防脏数据）：简报正文和「原始输入」面板
                     # 都要用它，喂模型时在 _build_prompt 里另截 300。
                     "body": body[:1500], "url": "", "site": ""})
    rows.sort(key=lambda x: x["ts"])
    return rows[-limit:]


def collect_feed(db: Session, minutes: int = 60, limit: int = 30) -> list[dict]:
    """第四腿：news_feed 里已入库的其他媒体源（东财 / 新浪 / tushare / 公众号）。

    为什么需要它：简报原来只读「财联社实时 + 境外 RSS + DeepSeek 搜索」，
    而 news_feed 每天另有 700+ 条（2026-11-07 实测 tushare 515、sina 119、
    eastmoney 31、wechat 27）**一条都没进过总结**，采了等于白采。

    配额按内容质量分，不是一视同仁：
    - wechat 有 500 字正文，多是券商宏观/策略深度 → 8 条全取
    - eastmoney / sina / tushare 只有标题但信息密度尚可 → 各 8~10 条
    - cls 排掉（collect_flash 已实时抓，会重复）
    - deepseek 排掉（搜索单独一腿）、guba 排掉（散户喊单，噪音）
    - fn_% 排掉（境外单独一腿）
    """
    from app.models import NewsFeedItem

    since = datetime.now() - timedelta(minutes=minutes)
    skip = ("cls", "deepseek", "guba")
    try:
        rows = (db.query(NewsFeedItem)
                .filter(NewsFeedItem.published_at >= since,
                        NewsFeedItem.source.notin_(skip),
                        ~NewsFeedItem.source.like("fn_%"))
                .order_by(NewsFeedItem.published_at.desc())
                .limit(limit * 8)
                .all())
    except Exception as e:  # noqa: BLE001
        logger.info("读取媒体源失败：%s", str(e)[:80])
        return []

    # tushare 资讯只有标题、泛财经/科技新闻居多（实测有「Claude 订阅额度虚标」
    # 这类与 A 股无关的），配额压到 4；公众号有正文且多是券商深度，给足 8。
    quota = {"wechat": 8, "eastmoney": 10, "sina": 8, "tushare": 4}
    by_src: dict[str, list] = {}
    for r in rows:
        by_src.setdefault(r.source, []).append(r)

    out: list[dict] = []
    seen_title: set[str] = set()
    for src, items in by_src.items():
        for r in items[:quota.get(src, 6)]:
            ts = int(r.published_at.timestamp()) if r.published_at else 0
            title = (r.title or "").strip()
            body = (r.summary or "").strip()
            if _noise_filter(title + body):
                continue
            # 同标题去重：新浪同一条稿子会带不同 url 重复入库
            # （实测「腾讯反弹，段永平：还拿着」一次进两条），白占语料。
            if title in seen_title:
                continue
            seen_title.add(title)
            out.append({"src": "媒体", "ts": ts, "title": title,
                        "body": body[:1500],
                        "url": r.url or "",
                        "site": r.source_label or src})
    out.sort(key=lambda x: x["ts"])
    return out[-limit:]


def collect_search(queries: tuple[str, ...] | list[str] | None = None,
                   per_query: int = 5, minutes: int = 60) -> list[dict]:
    """DeepSeek 网页搜索补充（二手但覆盖面广）。

    ⚠️ `after:` 必须跟着**取数窗口**走，不能写死「今天」。
    2026-11-08 凌晨实测：`after:2026-11-08` 返回 0 条，`after:2026-11-07`
    返回 3 条 —— 服务端确实按日期过滤，而 07:02 那轮（最需要隔夜要闻的一轮）
    当天还几乎没有新闻，写死「今天」等于让搜索腿天天在早间空转。
    改成用窗口起点：600 分钟的隔夜轮 → after:昨天，盘中 60 分钟 → after:今天。
    搜索语法由服务端原样透传（支持 site: / after: / lang: 等）。
    """
    from app.services import ds_search

    if not ds_search.available():
        return []
    qs = queries or ds_search.DEFAULT_QUERIES
    since = (datetime.now() - timedelta(minutes=max(minutes, 60))).date().isoformat()
    out: list[dict] = []
    seen: set[str] = set()
    for q in qs:
        hits = ds_search.search(
            f"{q} after:{since}",
            count=per_query,
            ori_question="A 股今日热点资讯，用于量化投研简报",
        )
        for it in hits:
            key = it.get("url") or it.get("title") or ""
            if not key or key in seen:
                continue
            seen.add(key)
            if _noise_filter(it.get("title", "") + it.get("summary", "")):
                continue
            try:
                ts = int(datetime.fromisoformat(it["date"]).timestamp()) if it.get("date") else 0
            except Exception:  # noqa: BLE001
                ts = 0
            out.append({"src": "搜索", "ts": ts, "title": it.get("title", ""),
                        "body": it.get("summary", ""), "url": it.get("url", ""),
                        "site": it.get("site", "")})
    return out


def collect_foreign(hours: float = 1.0, limit: int = 40) -> list[dict]:
    """读 news_feed 中境外源（source like fn_%），按 published_at 倒序，编号留给调用方打 O*。

    hours 与 HotBrief 取数窗对齐：盘中约 1h，隔夜窗用 max(minutes/60,1) 且 cap 12h。
    """
    from sqlalchemy import select
    from app.database import SessionLocal
    from app.models import NewsFeedItem

    hours = max(float(hours), 0.25)
    hours = min(hours, 12.0)
    limit = max(1, min(int(limit or 40), 40))
    cutoff = datetime.now() - timedelta(hours=hours)
    db = SessionLocal()
    try:
        rows = db.execute(
            select(NewsFeedItem)
            .where(NewsFeedItem.source.like("fn_%"))
            .where(NewsFeedItem.published_at >= cutoff)
            .order_by(NewsFeedItem.published_at.desc().nullslast(),
                      NewsFeedItem.id.desc())
            .limit(limit)
        ).scalars().all()
    except Exception as e:  # noqa: BLE001
        logger.info("境外日爬取数失败：%s", str(e)[:80])
        return []
    finally:
        db.close()

    out: list[dict] = []
    for r in rows:
        title = (r.title or "").strip()
        body = (r.summary or "").strip()
        if _noise_filter(title + body):
            continue
        ts = int(r.published_at.timestamp()) if r.published_at else 0
        out.append({
            "src": "境外",
            "ts": ts,
            "title": title,
            "body": body[:1500],
            "url": r.url or "",
            "site": r.source_label or r.source or "境外",
            "source_key": r.source or "",
        })
    return out


def collect_inputs(minutes: int = 60, limit: int | None = None,
                   use_search: bool | None = None,
                   db: Session | None = None) -> dict:
    """四腿取数。返回 {"flash", "foreign", "search", "media"}。

    顺序：快讯 → 境外日爬 → 搜索 → 媒体（已入库的其他源）。
    `limit=None` 交给 collect_flash 按窗口自动定。
    境外 hours = max(minutes/60, 1) 且 cap 12h，与隔夜窗对齐。
    `db` 为空时跳过媒体腿（保持向后兼容，纯函数场景仍可只取前三条腿）。
    """
    if use_search is None:
        use_search = os.getenv("QUANT_HOT_BRIEF_SEARCH", "1") != "0"
    flash = collect_flash(minutes=minutes, limit=limit)
    foreign_hours = min(12.0, max(minutes / 60.0, 1.0))
    foreign = collect_foreign(hours=foreign_hours, limit=40)
    # 搜索的 after: 也要跟着窗口走（见 collect_search 注释）
    search = collect_search(minutes=minutes) if use_search else []
    media = collect_feed(db, minutes=minutes) if db is not None else []
    return {"flash": flash, "foreign": foreign, "search": search,
            "media": media, "foreign_hours": foreign_hours}


def _strip_fence(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()


def _bigrams(s: str) -> set[str]:
    t = "".join(ch for ch in (s or "") if ch.strip())
    return {t[i:i + 2] for i in range(len(t) - 1)} or ({t} if t else set())


def _too_similar(a: str, b: str, thresh: float = 0.32) -> bool:
    """两条主题是否几乎在说同一件事（字符二元组 Jaccard）。

    模型会把「国常会完善资本市场稳市机制」改写成「国常会定调稳市机制」，
    字面不同但同一条，这种必须在代码里兜住 —— 光靠提示词约束不住
    （2026-11-07 实测：同一份输入连跑两次，模型把上条 8 个主题几乎原样重报）。

    阈值 0.32 来自实测分布（中文短主题改写后 J 远低于英文的直觉）：
        应判重：0.33 / 0.35 / 0.46 / 0.54 / 0.55 / 0.71
        不应判重：全部 0.00（不同题材的中文短句几乎不共享二元组）
    两簇完全分离，取 0.32 即可，不需要更复杂的规则。
    """
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return False
    return len(x & y) / len(x | y) >= thresh


def _inputs_snapshot(flash: list[dict], search: list[dict],
                     foreign: list[dict] | None = None,
                     media: list[dict] | None = None) -> list[dict]:
    """本轮输入的定位信息 + 正文快照。

    - 快讯 F*：财联社电报正文（已截 1500）
    - 境外 O*：RSS/X 摘要（截 1500）
    - 搜索 S*：DeepSeek `/v1/tools/search` 返回的 summary

    体积由 `_prune_old_inputs` 在 3 天后剥掉 body 只留定位信息。
    """
    out: list[dict] = []
    for i, x in enumerate(flash, 1):
        out.append({"ref": f"F{i}", "kind": "快讯", "ts": int(x.get("ts") or 0),
                    "title": str(x.get("title") or "")[:140],
                    "site": "财联社电报", "url": "",
                    "body": str(x.get("body") or "")[:1500]})
    for i, x in enumerate(foreign or [], 1):
        out.append({"ref": f"O{i}", "kind": "境外", "ts": int(x.get("ts") or 0),
                    "title": str(x.get("title") or "")[:140],
                    "site": str(x.get("site") or "境外"),
                    "url": str(x.get("url") or ""),
                    "body": str(x.get("body") or "")[:1500]})
    for i, x in enumerate(search, 1):
        out.append({"ref": f"S{i}", "kind": "搜索", "ts": int(x.get("ts") or 0),
                    "title": str(x.get("title") or "")[:140],
                    "site": str(x.get("site") or "网页"),
                    "url": str(x.get("url") or ""),
                    "body": str(x.get("body") or "")[:1500]})
    for i, x in enumerate(media or [], 1):
        out.append({"ref": f"M{i}", "kind": "媒体", "ts": int(x.get("ts") or 0),
                    "title": str(x.get("title") or "")[:140],
                    "site": str(x.get("site") or "媒体"),
                    "url": str(x.get("url") or ""),
                    "body": str(x.get("body") or "")[:1500]})
    return out


def _prune_old_inputs(db: Session, days: int = 3) -> int:
    """剥掉 N 天前正文快照里的 body，只留定位信息。

    不整条清空：`inputs_json` 的原始用途是「这条结论是从哪看到的」，
    标题 + 链接 + 时间必须长期留着。正文才是有时效的（说的是当时的版本）。
    """
    cutoff = datetime.now() - timedelta(days=days)
    try:
        rows = db.query(HotBrief).filter(
            HotBrief.created_at < cutoff,
            HotBrief.inputs_json.like("%body%"),
        ).all()
    except Exception as e:  # noqa: BLE001
        logger.info("查询待清理快照失败：%s", str(e)[:60])
        return 0
    n = 0
    for r in rows:
        try:
            items = json.loads(r.inputs_json or "[]")
        except Exception:  # noqa: BLE001
            continue
        if not any("body" in x for x in items):
            continue
        for x in items:
            x.pop("body", None)
        r.inputs_json = json.dumps(items, ensure_ascii=False)
        n += 1
    if n:
        db.commit()
    return n


def input_detail(db: Session, ref: str, run_id: int | None = None,
                 full: bool = False) -> dict:
    """单条输入的正文详情。

    `full=True` 才实时调 `/v1/tools/fetch` 抓整页 Markdown —— 这一步要联网、
    按次计费（0.005 元），所以只在用户点「抓全文」时走，不在页面加载时预取。
    """
    ref = (ref or "").strip().upper()
    if not ref:
        return {"error": "缺少 ref"}
    q = select(HotBrief)
    if run_id:
        q = q.where(HotBrief.id == run_id)
    else:
        q = q.order_by(HotBrief.id.desc()).limit(1)
    row = db.execute(q).scalar_one_or_none()
    if row is None:
        return {"error": "没有找到简报记录"}
    try:
        items = json.loads(row.inputs_json or "[]")
    except Exception:  # noqa: BLE001
        items = []
    hit = next((x for x in items if str(x.get("ref", "")).upper() == ref), None)
    if hit is None:
        return {"error": f"这一轮输入里没有 {ref}", "run_id": row.id}
    out = dict(hit)
    out["run_id"] = row.id
    if full:
        url = str(hit.get("url") or "")
        if not url.startswith("http"):
            out["error"] = "这一条没有原文链接（财联社快讯只有正文快照）"
        else:
            from app.services import ds_search

            got = ds_search.fetch(url)
            if got.get("error"):
                out["error"] = got["error"]
            else:
                out["content"] = got.get("content") or ""
                out["content_title"] = got.get("title") or ""
    return out


def _ref_map(flash: list[dict], search: list[dict],
             foreign: list[dict] | None = None,
             media: list[dict] | None = None) -> dict[str, dict]:
    """输入编号 → 人类可读来源。F=快讯，O=境外日爬，S=搜索，M=已入库媒体。

    让模型回填编号、由代码翻译成来源，而不是让它直接写「据财联社」——
    后者模型会编（把 S/O 条目写成「财联社」是常见幻觉）。
    """
    m: dict[str, dict] = {}
    for i, _ in enumerate(flash, 1):
        m[f"F{i}"] = {"kind": "快讯", "label": "财联社电报", "url": ""}
    for i, x in enumerate(foreign or [], 1):
        site = (x.get("site") or "境外").strip()
        m[f"O{i}"] = {"kind": "境外", "label": site,
                      "url": x.get("url") or ""}
    for i, x in enumerate(search, 1):
        site = (x.get("site") or "网页").strip()
        m[f"S{i}"] = {"kind": "搜索", "label": f"DeepSeek · {site}",
                      "url": x.get("url") or ""}
    for i, x in enumerate(media or [], 1):
        site = (x.get("site") or "媒体").strip()
        m[f"M{i}"] = {"kind": "媒体", "label": site, "url": x.get("url") or ""}
    return m


def _resolve_src(raw, ref_map: dict[str, dict]) -> dict:
    """把模型给的 "F2,S1" 解析成 {kind, label, url, refs}。"""
    # M 也要在里面：媒体腿是 2026-11-08 加的，漏了 M 会让所有引用
    # 东财/新浪/公众号的条目整条掉进「未标注来源」。
    refs = re.findall(r"[FSOMfsom]\s*\d+", str(raw or ""))
    refs = [r.replace(" ", "").upper() for r in refs]
    known = [(r, ref_map[r]) for r in refs if r in ref_map]
    if not known:
        return {"source_kind": "", "source_label": "未标注来源",
                "source_url": "", "source_refs": [], "source_all": [],
                "sources": []}
    kinds, labels, url, sources = [], [], "", []
    for r, info in known:
        if info["kind"] not in kinds:
            kinds.append(info["kind"])
        if info["label"] not in labels:
            labels.append(info["label"])
        if not url and info["url"]:
            url = info["url"]
        sources.append({"ref": r, "kind": info["kind"],
                        "label": info["label"], "url": info["url"]})
    # 标签要短：三个站点全列出来会把主题挤走。完整清单留在 sources / refs 里，
    # 前端悬停看全量。
    if len(labels) <= 2:
        label = " + ".join(labels)
    else:
        label = f"{labels[0]} 等 {len(labels)} 个来源"
    return {"source_kind": "+".join(kinds),
            "source_label": label,
            "source_url": url,
            "source_refs": [r for r, _ in known],
            "source_all": labels,
            "sources": sources}


def _llm_merge(cfg: dict, items: list[dict]) -> list[dict]:
    """用 LLM 把「同一件事的不同说法」合并掉，返回保留的条目。

    为什么不用字符相似度：实测「暴雪黄色预警覆盖东北华北」vs「京津冀暴雪与强冷空气」
    的字符二元组 Jaccard 只有 0.05，但显然是同一次冷空气过程；反过来
    「国常会定调稳市」和「券商互换便利落地」字面也有一点重合，却是两件事。
    这类判断只能靠语义。

    失败/输出异常时**原样返回**（宁可重复，不要误删）。
    """
    from app.services.llm_config import chat

    if len(items) <= 2:
        return items
    lines = [f"[{i}] {str(it.get('theme') or '')}｜{str(it.get('why') or '')[:50]}"
             for i, it in enumerate(items, 1)]
    try:
        text = chat(cfg, [
            {"role": "system", "content": "你是严谨的资讯去重助手，只输出 JSON。"},
            {"role": "user", "content": MERGE_PROMPT.format(items="\n".join(lines))},
        ], temperature=0.0, max_tokens=400)
    except Exception as e:  # noqa: BLE001
        logger.info("LLM 去重调用失败，保留全部：%s", str(e)[:60])
        return items

    raw = _strip_fence(text)
    i, j = raw.find("{"), raw.rfind("}")
    if i >= 0 and j > i:
        raw = raw[i:j + 1]
    try:
        keep = json.loads(raw).get("keep") or []
        idx = sorted({int(k) for k in keep if isinstance(k, (int, float, str))})
        idx = [k for k in idx if 1 <= k <= len(items)]
    except Exception:  # noqa: BLE001
        return items
    if not idx or len(idx) >= len(items):
        return items
    logger.info("今日合并去重：%d 条 → %d 条", len(items), len(idx))
    return [items[k - 1] for k in idx]


def _digest_refresh_async() -> None:
    """每轮简报后异步刷新「当日全景」（flash，约 30~60 秒）。

    老刘 2026-11-10：「今日综述（4 段 · 937 字，每轮重写）和当日全景有重复，
    能不能和一下，每一轮重写更新当日全景就好了」。

    原来两份稿子做同一件事：
      - hot_brief.summary  每轮 937 字（8 轮/天）
      - brief_digest       每天 21:45 一次 1680 字
    现在只留后者，并让它随每轮简报更新；21:45 仍由 v4-pro 出最终版
    （flash 版会被 `run(fast=False)` 覆盖，反过来不会）。

    ⚠️ 异步 + 吞异常：全景刷新失败绝不能影响简报本身的成功判定。
    """
    import threading

    def _run() -> None:
        try:
            from app.database import SessionLocal
            from app.services.brief_digest import run as run_digest

            db = SessionLocal()
            try:
                r = run_digest(db, fast=True)
                if r.get("ok"):
                    logger.info("当日全景已刷新（%s）：%d 字 / %d 条要点",
                                r.get("model"), r.get("chars"), r.get("n_items"))
                elif r.get("skipped"):
                    logger.info("当日全景跳过：%s", r.get("skipped"))
                else:
                    logger.info("当日全景刷新失败：%s", str(r.get("error"))[:120])
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001
            logger.info("当日全景刷新异常：%s", str(e)[:140])

    threading.Thread(target=_run, daemon=True, name="digest-refresh").start()


def _parse(text: str, ref_map: dict[str, dict] | None = None,
           ) -> tuple[list[dict], str, str]:
    raw = _strip_fence(text)
    i, j = raw.find("{"), raw.rfind("}")
    if i >= 0 and j > i:
        raw = raw[i:j + 1]
    try:
        d = json.loads(raw)
    except Exception:  # noqa: BLE001
        return [], "", ""
    items = d.get("items") or []
    if not isinstance(items, list):
        items = []
    clean = []
    for it in items[:8]:
        if not isinstance(it, dict):
            continue
        try:
            strength = int(it.get("strength") or 3)
        except Exception:  # noqa: BLE001
            strength = 3
        row = {
            "theme": str(it.get("theme") or "")[:40],
            "direction": str(it.get("direction") or "neutral"),
            "sectors": [str(x)[:12] for x in (it.get("sectors") or [])][:3],
            "tickers": [str(x)[:12] for x in (it.get("tickers") or [])][:5],
            "horizon": str(it.get("horizon") or ""),
            "why": str(it.get("why") or "")[:120],
            "strength": max(1, min(5, strength)),
        }
        row.update(_resolve_src(it.get("src"), ref_map or {}))
        clean.append(row)
    # 综述给足长度：350~600 字是目标，上限放到 1600 字符防截断
    # summary 恒为空串：字段保留是为了兼容旧数据与前端结构（2026-11-10 合并
    # 到 brief_digest 后不再生成，见 prompt 里的说明）
    return clean, str(d.get("mainline") or "")[:200], ""


def _today_rows(db: Session, day: str | None = None) -> list[HotBrief]:
    """某自然日已经生成的所有简报，按 id 升序。`day` 缺省为今天。

    ⚠️ 必须支持指定日期：`brief_digest._gather(db, day)` 补跑历史日报时，
    原来它是「先取今天、再按 day 过滤」—— 传历史日期必然过滤成空，
    结果是「补跑历史日报永远失败」（2026-11-09 发现）。
    """
    if day:
        try:
            d = datetime.strptime(str(day)[:10], "%Y-%m-%d").date()
        except ValueError:
            d = datetime.now().date()
    else:
        d = datetime.now().date()
    start = datetime.combine(d, dtime.min)
    end = datetime.combine(d, dtime.max)
    return list(db.execute(
        select(HotBrief)
        .where(HotBrief.created_at >= start, HotBrief.created_at <= end)
        .order_by(HotBrief.id)
    ).scalars().all())


def _today_themes(db: Session, limit: int = 40) -> tuple[list[str], int]:
    """今日前面各轮已经报过的主题（跨轮累计）。

    ⚠️ 2026-11-07 踩过的坑：原来只跟「上一条」比，于是搜索类条目和快讯类条目
    会轮流出现 —— 第 1 轮全是搜索、第 2 轮全是快讯（搜索被压掉）、第 3 轮
    搜索条目因为「上一条里没有」又全部复活。表现为「DeepSeek 的内容一会儿有
    一会儿没有」。必须跟**今日全量**比，而不是上一轮。
    """
    themes: list[str] = []
    for row in _today_rows(db):
        try:
            items = json.loads(row.items_json or "[]")
        except Exception:  # noqa: BLE001
            items = []
        for it in items:
            t = str(it.get("theme") or "")
            if t and not any(_too_similar(t, x) for x in themes):
                themes.append(t)
    return themes[:limit], len(themes)


def _build_prompt(flash: list[dict], search: list[dict], minutes: int,
                  recent: list[str] | None = None,
                  n_prev: int = 0,
                  foreign: list[dict] | None = None,
                  foreign_hours: float = 1.0,
                  media: list[dict] | None = None) -> str:
    flash_corpus = "\n".join(
        f"[F{i}] {x['title']}｜{x['body'][:300]}" for i, x in enumerate(flash, 1)
    ) or "（本轮无新快讯）"
    foreign = foreign or []
    fh = max(1, int(round(foreign_hours)))
    if foreign:
        foreign_corpus = "\n".join(
            f"[O{i}] {x['title']}｜{x.get('site') or '境外'}｜{str(x.get('body') or '')[:300]}"
            for i, x in enumerate(foreign, 1)
        )
        foreign_block = FOREIGN_BLOCK.format(
            hours=fh, n_foreign=len(foreign), corpus=foreign_corpus)
    else:
        foreign_block = FOREIGN_BLOCK_EMPTY.format(hours=fh)
    if search:
        search_corpus = "\n".join(
            f"[S{i}] {x['title']}（{x['site'] or '-'} {x['ts'] and datetime.fromtimestamp(x['ts']).strftime('%m-%d') or '-'}）"
            f"｜{x['body'][:600]}"
            for i, x in enumerate(search, 1)
        )
        search_block = SEARCH_BLOCK.format(n_search=len(search), corpus=search_corpus)
    else:
        search_block = ""
    media = media or []
    if media:
        # 没有正文的源（东财/新浪/tushare 只有标题）就不留空的 `｜`，
        # 否则语料里一片「标题｜」，模型会以为正文丢了。
        media_lines = []
        for i, x in enumerate(media, 1):
            stamp = (datetime.fromtimestamp(x["ts"]).strftime("%m-%d %H:%M")
                     if x.get("ts") else "-")
            body = (x.get("body") or "").strip()
            media_lines.append(
                f"[M{i}] {x['title']}（{x.get('site') or '-'} {stamp}）"
                + (f"｜{body[:400]}" if body else "")
            )
        media_block = MEDIA_BLOCK.format(
            hours=max(1, int(round(minutes / 60))),
            n_media=len(media), corpus="\n".join(media_lines))
    else:
        media_block = ""
    if recent:
        recent_block = RECENT_BLOCK.format(
            n_prev=n_prev,
            themes="\n".join(f"- {t}" for t in recent),
        )
    else:
        recent_block = ""
    return USER_TEMPLATE.format(minutes=minutes, n_flash=len(flash),
                                flash_corpus=flash_corpus,
                                foreign_block=foreign_block,
                                search_block=search_block + media_block,
                                recent_block=recent_block)


def run_brief(db: Session, minutes: int = 60,
              use_search: bool | None = None) -> dict:
    """取数 → 调模型 → 落 hot_brief。返回摘要 dict。"""
    from app.services.llm_config import LLMUnavailable, chat, resolve_active

    inputs = collect_inputs(minutes=minutes, use_search=use_search, db=db)
    flash, search = inputs["flash"], inputs["search"]
    foreign = inputs.get("foreign") or []
    media = inputs.get("media") or []
    foreign_hours = float(inputs.get("foreign_hours") or max(minutes / 60.0, 1.0))
    rec = HotBrief(created_at=datetime.now(),
                   n_input=len(flash) + len(foreign) + len(search) + len(media),
                   n_flash=len(flash), n_search=len(search),
                   n_foreign=len(foreign),
                   n_items=0, items_json="[]",
                   inputs_json=json.dumps(
                       _inputs_snapshot(flash, search, foreign, media),
                       ensure_ascii=False),
                   model="")
    # 让「全部资讯」页里也有 DeepSeek 这个分类：搜索结果本就是原始资讯，
    # 之前只存在 hot_brief.inputs_json 里，无法按来源检索。
    if search:
        try:
            from app.services import news_feed

            news_feed.ingest(db, "deepseek", [
                {"key": x.get("url") or x.get("title"), "title": x.get("title"),
                 "summary": x.get("body"), "url": x.get("url"),
                 "published_at": x.get("ts") or None,
                 "extra": {"site": x.get("site")}}
                for x in search
            ])
        except Exception as e:  # noqa: BLE001
            logger.info("灌入 DeepSeek 结果失败：%s", str(e)[:60])

    if not flash and not foreign and not search and not media:
        rec.error = "窗口内没有任何资讯（快讯 / 境外 / 搜索 / 媒体均为空）"
        db.add(rec)
        db.commit()
        return {"ok": False, "error": rec.error, "id": rec.id}

    cfg = resolve_active()
    if not cfg.get("configured"):
        rec.error = "大模型通道未配置"
        db.add(rec)
        db.commit()
        return {"ok": False, "error": rec.error, "id": rec.id}
    rec.model = cfg.get("model") or ""
    if flash:
        rec.window_start = datetime.fromtimestamp(flash[0]["ts"])
        rec.window_end = datetime.fromtimestamp(flash[-1]["ts"])

    recent, n_prev = _today_themes(db)
    prompt = _build_prompt(flash, search, minutes, recent=recent, n_prev=n_prev,
                           foreign=foreign, foreign_hours=foreign_hours,
                           media=media)

    try:
        text = chat(cfg, [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ], temperature=0.3, max_tokens=2800)
    except LLMUnavailable as e:
        rec.error = f"大模型不可用：{e}"
        db.add(rec)
        db.commit()
        return {"ok": False, "error": rec.error, "id": rec.id}
    except Exception as e:  # noqa: BLE001
        rec.error = f"{type(e).__name__}: {str(e)[:200]}"
        db.add(rec)
        db.commit()
        return {"ok": False, "error": rec.error, "id": rec.id}

    items, mainline, summary = _parse(
        text, _ref_map(flash, search, foreign, media))
    # 代码兜底去重：与**今日累计**主题高度相似的丢掉（提示词约束不住时这里收口）。
    # 只跟「上一轮」比是不够的 —— 会让搜索类和快讯类条目轮流复活（见 _today_themes 注释）。
    if items and recent:
        kept = [it for it in items
                if not any(_too_similar(it["theme"], prev) for prev in recent)]
        if len(kept) != len(items):
            logger.info("跨轮去重：%d 条 → %d 条（与今日已报主题重复）", len(items), len(kept))
        items = kept
    # —— 今日累计合并（LLM 语义去重）——
    # 每轮把「今日已有」+「本轮新增」交给模型挑一遍，结果落到 today_items_json。
    # 之所以在生成时就合并、而不是查询时现算：接口会被频繁调用，
    # 每次调 LLM 既慢又费钱；生成一小时一次，正合适。
    tag = datetime.now().isoformat(timespec="minutes")
    prev_today = _latest_today_items(db)
    fresh = []
    for it in items:
        if any(_too_similar(it["theme"], p.get("theme", "")) for p in prev_today):
            continue
        row = dict(it)
        row["first_seen"] = tag
        fresh.append(row)
    merged = prev_today + fresh
    if len(merged) > 2:
        merged = _llm_merge(cfg, merged)
    # 补齐 first_seen（模型只挑不删字段，但老数据可能没有）
    for it in merged:
        it.setdefault("first_seen", tag)

    rec.items_json = json.dumps(items, ensure_ascii=False)
    rec.today_items_json = json.dumps(merged, ensure_ascii=False)
    rec.mainline = mainline
    rec.summary = summary
    rec.n_items = len(items)
    if not items:
        # 模型没吐 JSON（真失败），或者本轮确实无新增（mainline 有值 = 正常）
        rec.error = "" if mainline else "模型未返回可解析内容"
    db.add(rec)
    db.commit()
    # 每轮简报后刷新「当日全景」（异步、flash、不覆盖 21:45 的 v4-pro 版）
    _digest_refresh_async()
    # 只保留最新一轮的 today_items_json：旧轮次的合并结果没人读（_latest_today_items
    # 只取最后一个非空），留着白占空间 —— 25 条 × 24 轮/天 ≈ 340KB/天。
    try:
        start_today = datetime.combine(datetime.now().date(), dtime.min)
        db.query(HotBrief).filter(
            HotBrief.created_at >= start_today,
            HotBrief.id != rec.id,
            HotBrief.today_items_json != "[]",
        ).update({HotBrief.today_items_json: "[]"}, synchronize_session=False)
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.info("清理旧轮次 today_items_json 失败：%s", str(e)[:60])

    # 3 天前的正文快照剥掉 body（保留 ref/标题/链接用于回溯）
    try:
        pruned = _prune_old_inputs(db)
        if pruned:
            logger.info("清理 %d 轮历史输入正文快照", pruned)
    except Exception as e:  # noqa: BLE001
        logger.info("清理历史正文快照失败：%s", str(e)[:60])

    # ok 的语义是「这次运行成功」，不是「有产出」。本轮无实质新增是正常结论 ——
    # 若把空结果当失败，调度器会在 :02/:05 反复重试同一小时、还天天告警。
    # 真正的失败只有两种：模型没吐出可解析 JSON（mainline 为空），或调用抛异常。
    ran_ok = bool(items) or bool(mainline) or bool(summary)
    return {"ok": ran_ok, "id": rec.id, "n_input": rec.n_input,
            "n_flash": rec.n_flash, "n_search": rec.n_search,
            "n_foreign": getattr(rec, "n_foreign", 0) or 0,
            "n_items": len(items), "mainline": mainline,
            "summary_len": len(summary), "error": rec.error}


def _latest_today_items(db: Session) -> list[dict]:
    """今天最近一轮已合并好的主题列表（没有就返回空）。"""
    for r in reversed(_today_rows(db)):
        try:
            rows = json.loads(r.today_items_json or "[]")
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            return rows
    return []


def _digest_day(db: Session, day: str) -> str:
    """该日是否有全景综述；有则返回日期，否则空串。"""
    if not day:
        return ""
    from app.models import BriefDigest

    try:
        row = db.query(BriefDigest).filter(BriefDigest.day == day,
                                           BriefDigest.error == "").first()
        return row.day if row else ""
    except Exception:  # noqa: BLE001
        return ""


def today_brief(db: Session) -> dict:
    """今日累计视图：把今天各轮的条目合并去重，附最新综述。

    为什么需要它：一轮只报「本小时新增」，卡片若只显示最新一轮，
    就会一会儿全是 DeepSeek 搜索条目、一会儿全是财联社快讯 —— 用户看到的是
    「DeepSeek 的内容看不到」。今日累计把两边都摆出来，并标出每条首次出现时间。
    """
    rows = _today_rows(db)
    if not rows:
        return {}

    # 优先用生成时已做好的 LLM 语义合并结果；没有（老记录）才回退到字符去重。
    merged = _latest_today_items(db)
    if not merged:
        seen: list[str] = []
        for r in rows:
            try:
                items = json.loads(r.items_json or "[]")
            except Exception:  # noqa: BLE001
                items = []
            for it in items:
                t = str(it.get("theme") or "")
                if not t or any(_too_similar(t, x) for x in seen):
                    continue
                seen.append(t)
                row = dict(it)
                row["first_seen"] = (r.created_at.isoformat(timespec="minutes")
                                     if r.created_at else "")
                merged.append(row)
    merged.sort(key=lambda x: (x.get("first_seen") or ""), reverse=True)

    last = rows[-1]
    summary = next((x.summary for x in reversed(rows) if x.summary), "")
    mainline = next((x.mainline for x in reversed(rows) if x.mainline), "")
    try:
        inputs = json.loads(last.inputs_json or "[]")
    except Exception:  # noqa: BLE001
        inputs = []

    # 「未标注」是加来源功能之前生成的旧条目（source_kind 为空），单独计数，
    # 否则四个数字加起来对不上总数，看着像丢数据。
    split = {"快讯": 0, "境外": 0, "搜索": 0, "媒体": 0, "混合": 0, "未标注": 0}
    for it in merged:
        k = it.get("source_kind") or ""
        if "+" in k:
            split["混合"] += 1
        elif "境外" in k and "快讯" not in k and "搜索" not in k:
            split["境外"] += 1
        elif "搜索" in k:
            split["搜索"] += 1
        elif "快讯" in k:
            split["快讯"] += 1
        elif "媒体" in k:
            split["媒体"] += 1
        elif "境外" in k:
            split["境外"] += 1
        else:
            split["未标注"] += 1

    try:
        latest_items = json.loads(last.items_json or "[]")
    except Exception:  # noqa: BLE001
        latest_items = []

    return {
        "date": last.created_at.date().isoformat() if last.created_at else "",
        "created_at": last.created_at.isoformat(timespec="seconds") if last.created_at else None,
        "n_briefs": len(rows),
        # 「本小时新增」= 最近一轮真正新报的条目。老刘的预期是「每小时看这一小时的新消息」，
        # 之前只给累计视图，看不出哪些是本小时新出现的。
        "latest_items": latest_items,
        "latest_at": last.created_at.strftime("%H:%M") if last.created_at else "",
        "n_latest": len(latest_items),
        "mainline": mainline,
        "summary": summary,
        # 当天是否已有「当日全景」（brief_digest）。有的话前端就把本卡的
        # 主线 + 综述收起来 —— 两个 800~1500 字的「今日整体」上下叠着，
        # 讲的都是国常会/回购利率/科技筹码出清，读两遍纯属浪费（2026-11-08
        # 老刘反馈「当日全景、热点简报是不是有些重复」）。
        "digest_day": _digest_day(db, last.created_at.date().isoformat()
                                  if last.created_at else ""),
        "items": merged,
        "n_items": len(merged),
        "source_split": split,
        "inputs": inputs,
        "n_input": last.n_input,
        "n_flash": last.n_flash,
        "n_search": last.n_search,
        "n_foreign": getattr(last, "n_foreign", 0) or 0,
        "model": last.model,
        "error": "" if merged or summary else (last.error or ""),
    }


def latest_brief(db: Session) -> dict | None:
    row = db.execute(
        select(HotBrief).where(HotBrief.n_items > 0).order_by(HotBrief.id.desc()).limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return _to_dict(row)


def list_briefs(db: Session, limit: int = 24) -> list[dict]:
    rows = db.execute(
        select(HotBrief).order_by(HotBrief.id.desc()).limit(min(max(limit, 1), 200))
    ).scalars().all()
    return [_to_dict(r, with_body=False) for r in rows]


def _to_dict(r: HotBrief, with_body: bool = True) -> dict:
    try:
        items = json.loads(r.items_json or "[]")
    except Exception:  # noqa: BLE001
        items = []
    try:
        inputs = json.loads(r.inputs_json or "[]")
    except Exception:  # noqa: BLE001
        inputs = []
    if not with_body:
        # 历史列表只给定位信息：正文快照 20KB/轮，24 轮就 500KB，
        # 而列表页根本不用展开阅读。
        inputs = [{k: v for k, v in x.items() if k != "body"} for x in inputs]
    return {
        "id": r.id,
        "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else None,
        "window_start": r.window_start.isoformat(timespec="minutes") if r.window_start else None,
        "window_end": r.window_end.isoformat(timespec="minutes") if r.window_end else None,
        "n_input": r.n_input,
        "n_flash": r.n_flash,
        "n_search": r.n_search,
        "n_foreign": getattr(r, "n_foreign", 0) or 0,
        "n_items": r.n_items,
        "items": items,
        "inputs": inputs,
        "mainline": r.mainline,
        "summary": r.summary,
        "model": r.model,
        "error": r.error,
    }
