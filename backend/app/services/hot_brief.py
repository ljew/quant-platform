"""每小时热点简报：一手快讯 → LLM 筛选 → 结构化「对 A 股有没有实质影响」。

## 为什么不让模型自己上网搜

2026-11-07 实测 DeepSeek API：`tools: [{type:"web_search"}]` 返回 **422**
（`unknown variant web_search, expected function`），`/search` 等端点 404 ——
它的 tools 只支持 function calling，**没有联网能力**。「联网搜索」是网页版/App 的
产品功能，没开放成 API。

而且即便能搜，模型拿到的是二手转载，比财联社一手电报慢、还不可追溯。
所以这里改成：**平台自己采一手快讯 → 模型只负责筛选与判断**。

## 提示词取向（按老刘的偏好）

- 不做摘要，只做影响判断：每条必须能落到板块/个股/资金面
- 没有具体数字支撑的「看好/警惕」一律丢弃 —— 这类话在研报里最廉价
- 拿不准就少说，宁可 3 条也不凑 8 条
- 明确给方向和强度，避免「可能影响、需关注」这种免责表述
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HotBrief

SYSTEM_PROMPT = """你是一名 A 股量化投研分析师，服务于一个只看「能不能落地」的研究流程。
你的输出会直接用于判断当日交易方向，所以：

1. 不做新闻摘要。读者已经看过原文，他要的是判断。
2. 没有具体数字支撑的「看好 / 警惕 / 值得关注」一律丢弃 —— 这类话最廉价。
3. 拿不准就少说。宁可给 3 条真的，也不要凑 8 条凑数。
4. 不要写「可能影响」「需关注」这种免责表述，要给出明确方向。
5. 只关心 A 股：海外消息只有在能传导到 A 股板块/个股时才保留。
"""

USER_TEMPLATE = """下面是过去 {minutes} 分钟新增的财经快讯，共 {n} 条。

请筛出**对 A 股有实质影响**的，按下述结构输出。

筛选标准（严格执行）：
- 能落到「行业 / 板块 / 个股 / 资金面」的才留
- 剔除：纯人物动态、无数据的产品发布、重复报道、无具体数字的预测性喊话
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

另外给 mainline：100 字以内，说清当前市场在交易什么逻辑（不是罗列，是判断）。

只输出 JSON，不要任何解释、不要 markdown 代码块：
{{"items": [...], "mainline": "..."}}

快讯原文：
{corpus}
"""


def _noise_filter(text: str) -> bool:
    """粗筛掉明显无信息量的条目，减少 token 浪费。"""
    t = (text or "").strip()
    if len(t) < 8:
        return True
    junk = ("点击查看", "扫码", "广告", "招聘", "转载", "免责声明")
    return any(k in t for k in junk)


def collect_inputs(minutes: int = 60, limit: int = 60) -> list[dict]:
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
        rows.append({"ts": ts, "title": title, "body": body[:400]})
    rows.sort(key=lambda x: x["ts"])
    return rows[-limit:]


def _strip_fence(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()


def _parse(text: str) -> tuple[list[dict], str]:
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
        clean.append({
            "theme": str(it.get("theme") or "")[:40],
            "direction": str(it.get("direction") or "neutral"),
            "sectors": [str(x)[:12] for x in (it.get("sectors") or [])][:3],
            "tickers": [str(x)[:12] for x in (it.get("tickers") or [])][:5],
            "horizon": str(it.get("horizon") or ""),
            "why": str(it.get("why") or "")[:120],
            "strength": max(1, min(5, strength)),
        })
    return clean, str(d.get("mainline") or "")[:300]


def run_brief(db: Session, minutes: int = 60) -> dict:
    """拉一次快讯 → 调模型 → 落 hot_brief。返回摘要 dict。"""
    from app.services.llm_config import LLMUnavailable, chat, resolve_active

    inputs = collect_inputs(minutes=minutes)
    rec = HotBrief(created_at=datetime.now(), n_input=len(inputs), n_items=0,
                   items_json="[]", model="")
    if not inputs:
        rec.error = "窗口内没有新快讯"
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
    rec.window_start = datetime.fromtimestamp(inputs[0]["ts"])
    rec.window_end = datetime.fromtimestamp(inputs[-1]["ts"])

    corpus = "\n".join(
        f"[{i}] {x['title']}｜{x['body']}" for i, x in enumerate(inputs, 1)
    )
    prompt = USER_TEMPLATE.format(minutes=minutes, n=len(inputs), corpus=corpus[:12000])

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

    items, mainline = _parse(text)
    rec.items_json = json.dumps(items, ensure_ascii=False)
    rec.mainline = mainline
    rec.n_items = len(items)
    if not items:
        rec.error = "模型未返回可解析条目"
    db.add(rec)
    db.commit()
    return {"ok": bool(items), "id": rec.id, "n_input": len(inputs),
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
    return {
        "id": r.id,
        "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else None,
        "window_start": r.window_start.isoformat(timespec="minutes") if r.window_start else None,
        "window_end": r.window_end.isoformat(timespec="minutes") if r.window_end else None,
        "n_input": r.n_input,
        "n_items": r.n_items,
        "items": items,
        "mainline": r.mainline,
        "model": r.model,
        "error": r.error,
    }
