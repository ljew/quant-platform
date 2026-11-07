"""每小时热点简报：双来源资讯 → LLM 筛选 → 结构化「对 A 股有没有实质影响」。

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
from datetime import date, datetime, timedelta

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

USER_TEMPLATE = """下面是两个来源的资讯。

【一】一手快讯（财联社电报，过去 {minutes} 分钟，共 {n_flash} 条）
{flash_corpus}
{search_block}{recent_block}
请筛出**对 A 股有实质影响**的，按下述结构输出。

筛选标准（严格执行）：
- 能落到「行业 / 板块 / 个股 / 资金面」的才留
- 剔除：纯人物动态、无数据的产品发布、重复报道、无具体数字的预测性喊话
- 两个来源说的是同一件事，合并成一条
- 来源二里明显是旧闻（事件发生时间早于最近 3 天）直接丢弃
- 优先：货币政策、财政、监管、产业政策；有金额/增速/产能/价格数字的
- 最多 8 条

每条字段：
- theme: 主题，不超过 20 字
- direction: bullish / bearish / neutral（对 A 股整体或对应板块）
- sectors: 受影响板块，申万一级或二级，最多 3 个，数组
- tickers: 明确提到的 A 股公司名或代码，没有就空数组
- horizon: 1-3日 / 1-4周 / 长期
- why: 影响路径，一句话说清「因为什么 → 所以什么」，不超过 40 字
- strength: 1-5 整数，1=可忽略，5=可能改变趋势
- src: 这条来自哪些输入条目，用输入的编号，例如 "F3"、"S2"、"F1,S2"。
  合并了多个来源就把编号都写上，用英文逗号分隔。**必须填真实编号，不要编造。**

另外给 mainline：100 字以内，说清当前市场在交易什么逻辑（不是罗列，是判断）。

只输出 JSON，不要任何解释、不要 markdown 代码块：
{{"items": [...], "mainline": "..."}}
"""

SEARCH_BLOCK = """
【二】网页搜索补充（共 {n_search} 条；可能含二手转载，注意核对时效）
{corpus}
"""

RECENT_BLOCK = """
【三】上一条简报（{prev_time}）已经报过这些主题
{themes}

除非出现**实质新增信息**（新数字、新进展、方向反转），否则不要重复输出上面的主题。
本条只报真正的新增变化。

🔴 **如果本轮没有任何需要新增的内容，items 必须返回空数组 []**，
并在 mainline 里写「本轮无实质新增」。不要为了填满条目而重复上条主题、
或把无关内容降级硬塞进来 —— 空结果是有价值的结论。
"""


def _noise_filter(text: str) -> bool:
    """粗筛掉明显无信息量的条目，减少 token 浪费。"""
    t = (text or "").strip()
    if len(t) < 8:
        return True
    junk = ("点击查看", "扫码", "广告", "招聘", "转载", "免责声明")
    return any(k in t for k in junk)


def collect_flash(minutes: int = 60, limit: int = 60) -> list[dict]:
    """拉最近 minutes 分钟的财联社电报（一手快讯，时效最强）。"""
    from app.datahub.extractors.cls_telegraph import ClsTelegraphExtractor

    try:
        df = ClsTelegraphExtractor().fetch(rn=50, pages=2)
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
                     "body": body[:300], "url": "", "site": ""})
    rows.sort(key=lambda x: x["ts"])
    return rows[-limit:]


def collect_search(queries: tuple[str, ...] | list[str] | None = None,
                   per_query: int = 5) -> list[dict]:
    """DeepSeek 网页搜索补充（二手但覆盖面广）。

    查询带 `after:{今天}` —— 简报是小时级的，只要当天的结果；
    搜索语法由服务端原样透传（支持 site: / after: / lang: 等）。
    """
    from app.services import ds_search

    if not ds_search.available():
        return []
    qs = queries or ds_search.DEFAULT_QUERIES
    today = date.today().isoformat()
    out: list[dict] = []
    seen: set[str] = set()
    for q in qs:
        hits = ds_search.search(
            f"{q} after:{today}",
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


def collect_inputs(minutes: int = 60, limit: int = 60,
                   use_search: bool | None = None) -> dict:
    """两条腿取数。返回 {"flash": [...], "search": [...]}。"""
    if use_search is None:
        use_search = os.getenv("QUANT_HOT_BRIEF_SEARCH", "1") != "0"
    flash = collect_flash(minutes=minutes, limit=limit)
    search = collect_search() if use_search else []
    return {"flash": flash, "search": search}


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


def _inputs_snapshot(flash: list[dict], search: list[dict]) -> list[dict]:
    """本轮输入的定位信息（编号/类型/时间/标题/站点/链接）。

    不存正文 —— 目的是「能找回原文」，存标题 + 链接 + 时间即可。
    56 条约 5KB，一天 24 轮 ≈ 120KB，可接受。
    """
    out: list[dict] = []
    for i, x in enumerate(flash, 1):
        out.append({"ref": f"F{i}", "kind": "快讯", "ts": int(x.get("ts") or 0),
                    "title": str(x.get("title") or "")[:140],
                    "site": "财联社电报", "url": ""})
    for i, x in enumerate(search, 1):
        out.append({"ref": f"S{i}", "kind": "搜索", "ts": int(x.get("ts") or 0),
                    "title": str(x.get("title") or "")[:140],
                    "site": str(x.get("site") or "网页"),
                    "url": str(x.get("url") or "")})
    return out


def _ref_map(flash: list[dict], search: list[dict]) -> dict[str, dict]:
    """输入编号 → 人类可读来源。F=财联社快讯，S=DeepSeek 搜索。

    让模型回填编号、由代码翻译成来源，而不是让它直接写「据财联社」——
    后者模型会编（把 S 条目写成「财联社」是常见幻觉）。
    """
    m: dict[str, dict] = {}
    for i, _ in enumerate(flash, 1):
        m[f"F{i}"] = {"kind": "快讯", "label": "财联社电报", "url": ""}
    for i, x in enumerate(search, 1):
        site = (x.get("site") or "网页").strip()
        m[f"S{i}"] = {"kind": "搜索", "label": f"DeepSeek · {site}",
                      "url": x.get("url") or ""}
    return m


def _resolve_src(raw, ref_map: dict[str, dict]) -> dict:
    """把模型给的 "F2,S1" 解析成 {kind, label, url, refs}。"""
    refs = re.findall(r"[FSfs]\s*\d+", str(raw or ""))
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


def _parse(text: str, ref_map: dict[str, dict] | None = None,
           ) -> tuple[list[dict], str]:
    raw = _strip_fence(text)
    i, j = raw.find("{"), raw.rfind("}")
    if i >= 0 and j > i:
        raw = raw[i:j + 1]
    try:
        d = json.loads(raw)
    except Exception:  # noqa: BLE001
        return [], ""
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
    return clean, str(d.get("mainline") or "")[:300]


def _recent_themes(db: Session, limit: int = 12) -> tuple[list[str], str]:
    """上一份有内容的简报报过哪些主题。用于让本轮只报“新增变化”。

    没有这一步的话，网页搜索在同一交易日内结果高度重复（同一篇「国常会」文章
    会连续几小时都排在前列），模型就会每小时把同一条再报一遍。
    """
    row = db.execute(
        select(HotBrief).where(HotBrief.n_items > 0).order_by(HotBrief.id.desc()).limit(1)
    ).scalar_one_or_none()
    if row is None:
        return [], ""
    try:
        items = json.loads(row.items_json or "[]")
    except Exception:  # noqa: BLE001
        items = []
    themes = [str(i.get("theme") or "") for i in items if i.get("theme")][:limit]
    ts = row.created_at.strftime("%m-%d %H:%M") if row.created_at else ""
    return themes, ts


def _build_prompt(flash: list[dict], search: list[dict], minutes: int,
                  recent: list[str] | None = None,
                  recent_at: str = "") -> str:
    flash_corpus = "\n".join(
        f"[F{i}] {x['title']}｜{x['body']}" for i, x in enumerate(flash, 1)
    ) or "（本轮无新快讯）"
    if search:
        search_corpus = "\n".join(
            f"[S{i}] {x['title']}（{x['site'] or '-'} {x['ts'] and datetime.fromtimestamp(x['ts']).strftime('%m-%d') or '-'}）"
            f"｜{x['body']}"
            for i, x in enumerate(search, 1)
        )
        search_block = SEARCH_BLOCK.format(n_search=len(search), corpus=search_corpus)
    else:
        search_block = ""
    if recent:
        recent_block = RECENT_BLOCK.format(
            prev_time=recent_at or "上一次",
            themes="\n".join(f"- {t}" for t in recent),
        )
    else:
        recent_block = ""
    return USER_TEMPLATE.format(minutes=minutes, n_flash=len(flash),
                                flash_corpus=flash_corpus,
                                search_block=search_block,
                                recent_block=recent_block)


def run_brief(db: Session, minutes: int = 60,
              use_search: bool | None = None) -> dict:
    """取数 → 调模型 → 落 hot_brief。返回摘要 dict。"""
    from app.services.llm_config import LLMUnavailable, chat, resolve_active

    inputs = collect_inputs(minutes=minutes, use_search=use_search)
    flash, search = inputs["flash"], inputs["search"]
    rec = HotBrief(created_at=datetime.now(), n_input=len(flash) + len(search),
                   n_flash=len(flash), n_search=len(search),
                   n_items=0, items_json="[]",
                   inputs_json=json.dumps(_inputs_snapshot(flash, search), ensure_ascii=False),
                   model="")
    if not flash and not search:
        rec.error = "窗口内没有任何资讯（快讯与搜索均为空）"
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

    recent, recent_at = _recent_themes(db)
    prompt = _build_prompt(flash, search, minutes, recent=recent, recent_at=recent_at)

    try:
        text = chat(cfg, [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ], temperature=0.3, max_tokens=2000)
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

    items, mainline = _parse(text, _ref_map(flash, search))
    # 代码兜底去重：与上条简报主题高度相似的丢掉（提示词约束不住时这里收口）
    if items and recent:
        kept = [it for it in items
                if not any(_too_similar(it["theme"], prev) for prev in recent)]
        if len(kept) != len(items):
            logger.info("跨轮去重：%d 条 → %d 条（与上条主题重复）", len(items), len(kept))
        items = kept
    rec.items_json = json.dumps(items, ensure_ascii=False)
    rec.mainline = mainline
    rec.n_items = len(items)
    if not items:
        # 两种可能：模型没吐 JSON（真失败）；或本轮确实无新增（正常）。
        # 后者不该记成错误，否则监控里会天天告警。
        rec.error = "" if mainline else "模型未返回可解析条目"
    db.add(rec)
    db.commit()
    # ok 的语义是「这次运行成功」，不是「有产出」。本轮无实质新增是正常结论 ——
    # 若把空结果当失败，调度器会在 :02/:05 反复重试同一小时、还天天告警。
    # 真正的失败只有两种：模型没吐出可解析 JSON（mainline 为空），或调用抛异常。
    ran_ok = bool(items) or bool(mainline)
    return {"ok": ran_ok, "id": rec.id, "n_input": rec.n_input,
            "n_flash": rec.n_flash, "n_search": rec.n_search,
            "n_items": len(items), "mainline": mainline, "error": rec.error}


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
    return [_to_dict(r) for r in rows]


def _to_dict(r: HotBrief) -> dict:
    try:
        items = json.loads(r.items_json or "[]")
    except Exception:  # noqa: BLE001
        items = []
    try:
        inputs = json.loads(r.inputs_json or "[]")
    except Exception:  # noqa: BLE001
        inputs = []
    return {
        "id": r.id,
        "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else None,
        "window_start": r.window_start.isoformat(timespec="minutes") if r.window_start else None,
        "window_end": r.window_end.isoformat(timespec="minutes") if r.window_end else None,
        "n_input": r.n_input,
        "n_flash": r.n_flash,
        "n_search": r.n_search,
        "n_items": r.n_items,
        "items": items,
        "inputs": inputs,
        "mainline": r.mainline,
        "model": r.model,
        "error": r.error,
    }
