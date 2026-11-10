"""当日全景综述：把一天的全部资讯压成一份可读的判断。

与 `hot_brief` 的分工（两者不重复，是不同层次）：
- `hot_brief`（每轮 8 次，flash）：本时段的增量 —— 这几小时有什么新的、怎么解读
- `brief_digest`（每天 1 次，v4-pro）：全天全景 —— 一天在交易什么、
  各条线索怎么互相印证或矛盾、明天该盯什么

为什么要单独做：每轮综述天生只看得到自己那个窗口（60~600 分钟），
却要写「今日综述」，这是语义错位。2026-11-08 实测：一天 1481 条原始资讯，
真正进入简报语料的只有 54%，且分散在 8 个互不相看的窗口里。

模型选择（2026-11-08 实测同一份语料）：
| 模型           | max_tokens | reasoning | 正文 | finish |
| deepseek-flash | 6000       | 5381      | 902  | length（截断）|
| deepseek-v4-pro| 12000      | 5625      | 1075 | stop（完整）|
两个模型的 reasoning 差不多，但 v4-pro 的长文组织明显更紧（数字挂得更实、
矛盾信号敢点出来）。代价是 102 秒 vs 37 秒 —— 一天一次、后台跑，可以接受。
⚠️ max_tokens 必须给到 12000：推理固定吃 5000+，留给正文的才是剩下的。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import BriefDigest

logger = logging.getLogger(__name__)

DIGEST_MODEL = "deepseek-v4-pro"
DIGEST_MAX_TOKENS = 12000
DIGEST_TIMEOUT = 300

SYSTEM = """你是一名 A 股策略分析师，每天收盘后给基金经理写一页纸的当日复盘。

读者只看「能不能落地」：他不缺资讯，缺的是「今天这些东西连起来说明什么」。
所以你写的每一句都要有判断或因果，不能是资讯的复述。

硬规则：
- 每条判断尽量挂一个具体数字（点位、金额、增速、家数、比例）
- 相互矛盾的信号必须点出来，不要为了顺滑而强行统一
- 不确定就写「无法判断」并说明缺什么，绝不编造
- 禁止「综上所述」「值得关注」「需持续跟踪」「谨慎乐观」这类空话
- 不写免责声明、不写风险提示套话
- 只输出正文，不要标题、不要 markdown 标记"""

PROMPT = """下面是 {day} 全天的资讯，分三部分：已提炼要点、资讯总量与热度、各轮盘中判断。

【一】全天要点（前序环节从原始资讯中提炼并去重，共 {n_items} 条）
{items_block}

【二】全天资讯规模
原始资讯 {n_news} 条。来源分布：{src_stat}
被提及最多的个股：{hot_stocks}
要点涉及的板块（按出现次数）：{sector_stat}

【三】盘中各轮的即时判断（按时间，供你参考市场认知的演变，可以推翻）
{mainlines}

请写一份当日复盘，**800~1100 字**，5 段，依次回答：

① 今天市场在交易什么 —— 一句话点题，然后说清这个判断的依据。
   如果今天其实是「没有主线、各炒各的」，就直接这么说，不要硬凑主线。

② 政策与宏观 —— 哪些表态/文件/数据真正改变了预期？写了具体数字。
   只是重复既有口径的，说明它没有增量。

③ 产业与板块 —— 哪几条线索在共振（互相印证）？哪几条是伪相关？
   驱动的持续性怎么看（一次性事件 / 有持续数据跟踪 / 产能周期）。

④ 资金与筹码 —— 从资讯能看到的资金动向（ETF 申赎、回购、投顾仓位、
   龙虎榜、北向），以及它们和板块表现是否一致。不一致要指明。

⑤ 接下来盯什么 —— 未来 1~2 周会证伪或证实当前判断的**具体**事件或数据，
   要能落到日期或指标名，不要写「关注政策变化」这种。"""


def _gather(db: Session, day: str) -> dict:
    """汇总当天的三层素材。"""
    from app.services.hot_brief import _latest_today_items, _today_rows

    # 直接按日期取，不要「先取今天再过滤」——那样补跑历史日报永远为空
    rows = _today_rows(db, day=day)
    items = _latest_today_items(db) if not day else []
    if day and rows:
        # 指定日期时不能复用「今日」缓存，从该日各轮重新合并
        items = _merge_rows(rows)

    src_rows = db.execute(text(
        "select source, count(*) c from news_feed where date(published_at) = :d "
        "group by source order by c desc"), {"d": day}).fetchall()
    src_stat = "、".join(f"{s} {c}" for s, c in src_rows) or "无"
    n_news = sum(c for _, c in src_rows)

    # 个股热度：三路合并。单独用任何一路都不够 ——
    # ① news_feed.symbols 只有股吧会填（其它源入库时没抽），覆盖极窄；
    # ② 要点里的 tickers 是模型从正文认出来的，最准但只覆盖写进要点的；
    # ③ 标题匹配兜底，只扫市值前 1500 只（否则 267 条 × 5000 只太慢）。
    stock_hits: Counter[str] = Counter()

    sym_rows = db.execute(text(
        "select symbols, count(*) c from news_feed "
        "where date(published_at) = :d and symbols is not null and symbols != '' "
        "group by symbols"), {"d": day}).fetchall()
    if sym_rows:
        codes = [x for x, _ in sym_rows]
        ph = ",".join(f":s{i}" for i in range(len(codes)))
        params = {f"s{i}": c for i, c in enumerate(codes)}
        name_map = dict(db.execute(
            text(f"select symbol, name from stocks where symbol in ({ph})"), params).fetchall())
        for sym, c in sym_rows:
            stock_hits[name_map.get(sym) or sym] += c

    # 模型给的 tickers 常是 6 位代码（601012）而不是名称，直接计数会出现
    # 「隆基绿能(4)、601012(2)」这种同一只票占两行的榜单。先统一翻成名称。
    tickers: list[str] = []
    for it in _items_for_stats(rows):
        for tk in it.get("tickers") or []:
            tk = str(tk).strip()
            if 2 <= len(tk) <= 8:
                tickers.append(tk)
    code2name: dict[str, str] = {}
    codes = [t for t in tickers if re.fullmatch(r"\d{6}", t)]
    if codes:
        try:
            ph = ",".join(f":c{i}" for i in range(len(codes)))
            params = {f"c{i}": c for i, c in enumerate(codes)}
            name_rows = db.execute(text(
                f"select symbol, name from stocks where substr(symbol, -6) in ({ph})"),
                params).fetchall()
            for sym, nm in name_rows:
                code2name[sym[-6:]] = nm
        except Exception as e:  # noqa: BLE001
            logger.info("代码转名称失败：%s", str(e)[:80])
    for tk in tickers:
        stock_hits[code2name.get(tk) or tk] += 2    # 模型明确点名，权重加倍

    try:
        title_rows = db.execute(text(
            "select title from news_feed where date(published_at) = :d"),
            {"d": day}).fetchall()
        big = db.execute(text(
            "select name from stocks order by coalesce(market_cap, 0) desc limit 1500"
        )).fetchall()
        names = [n for (n,) in big if n and len(n) >= 3]
        for (t,) in title_rows:
            t = t or ""
            for n in names:
                if n in t:
                    stock_hits[n] += 1
    except Exception as e:  # noqa: BLE001
        logger.info("标题匹配个股失败：%s", str(e)[:80])

    hot_stocks = "、".join(f"{n}({c})" for n, c in stock_hits.most_common(12)) or "无"

    sector_counter: Counter[str] = Counter()
    for it in items:
        for sec in it.get("sectors") or []:
            sector_counter[str(sec).strip()] += 1
    sector_stat = "、".join(f"{s}({c})" for s, c in sector_counter.most_common(10)) or "无"

    items_block = "\n".join(
        f"{i}. [{it.get('direction')}/强度{it.get('strength')}/{it.get('horizon')}] "
        f"{it.get('theme')}｜板块：{'、'.join(it.get('sectors') or []) or '-'}｜"
        f"个股：{'、'.join(it.get('tickers') or []) or '-'}｜{it.get('why') or ''}"
        for i, it in enumerate(items, 1)) or "（本日无提炼要点）"

    mainlines = "\n".join(
        f"- {r.created_at:%H:%M} {r.mainline}" for r in rows
        if r.mainline) or "（本日无盘中判断）"

    return {"items": items, "n_items": len(items), "n_news": n_news,
            "src_stat": src_stat, "hot_stocks": hot_stocks,
            "sector_stat": sector_stat, "items_block": items_block,
            "mainlines": mainlines, "n_rounds": len(rows)}


def _items_for_stats(rows: list) -> list[dict]:
    """当天各轮 items 的并集（**不去重**），用于统计提及热度。

    与 `_merge_rows` 的区别：那个喂模型（必须去重，否则同一件事说八遍），
    这个计数（重复本身就是热度信号）。
    """
    out: list[dict] = []
    for r in rows:
        try:
            out.extend(json.loads(r.items_json or "[]"))
        except Exception:  # noqa: BLE001
            continue
    return out


def _merge_rows(rows: list) -> list[dict]:
    """把指定日期的各轮 items 按主题合并（复用简报的字符相似度兜底）。"""
    from app.services.hot_brief import _too_similar

    merged: list[dict] = []
    for r in rows:
        try:
            its = json.loads(r.items_json or "[]")
        except Exception:  # noqa: BLE001
            its = []
        for it in its:
            theme = str(it.get("theme") or "")
            if not theme or any(_too_similar(theme, m.get("theme", "")) for m in merged):
                continue
            row = dict(it)
            row["first_seen"] = r.created_at.isoformat(timespec="minutes") if r.created_at else ""
            merged.append(row)
    return merged


def run(db: Session, day: str | None = None, fast: bool = False) -> dict:
    """生成（或重新生成）某日的全景综述。幂等：同一天覆盖。

    `fast=True`：简报每轮跑完后刷新用（老刘 2026-11-10 要求把「简报里的
    今日综述」和「当日全景」合并成一份）。差异：
      - 直接用 flash（跳过 v4-pro 的 70~100 秒），只试 1 次
      - **不覆盖当天已有的 v4-pro 完整版** —— 21:45 精修过之后，
        再来的盘中刷新不该把它冲掉
    """
    from app.services.llm_config import chat, resolve_active

    day = day or datetime.now().date().isoformat()
    data = _gather(db, day)
    # 没有提炼要点就不生成：那说明当天简报一轮都没跑成，此时硬写只能靠
    # news_feed 的统计数字凑，出来的东西全是空话，不如不写（也免得覆盖掉
    # 之前手动生成的好版本）。
    if not data["items"]:
        return {"ok": False,
                "error": f"{day} 没有提炼要点（简报未生成？资讯 {data['n_news']} 条），跳过"}

    cfg = resolve_active()
    if not cfg.get("configured"):
        return {"ok": False, "error": "大模型通道未配置"}

    if fast:
        # 当天已有 v4-pro 完整版 → 直接跳过，别用短版覆盖长版
        try:
            exist = db.query(BriefDigest).filter(BriefDigest.day == day).first()
        except Exception:  # noqa: BLE001
            exist = None
        if exist is not None and "pro" in (exist.model or "").lower():
            return {"ok": False, "skipped": "已有 v4-pro 完整版，盘中不覆盖"}

    prompt = PROMPT.format(day=day, **{k: v for k, v in data.items()
                                       if k in ("n_items", "items_block", "n_news",
                                                "src_stat", "hot_stocks", "sector_stat",
                                                "mainlines")})
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt}]
    t0 = datetime.now()

    def _try(model: str, max_tokens: int, attempts: int) -> tuple[str, str]:
        """返回 (正文, 错误)。长请求必须重试 —— v4-pro 要跑 70~100 秒，

        实测偶发 `SSL: UNEXPECTED_EOF_WHILE_READING`（连接被中途切断），
        一次失败就放弃会让日报隔三差五缺失。
        """
        c = dict(cfg)
        c["model"] = model
        last = ""
        for i in range(attempts):
            try:
                return chat(c, msgs, temperature=0.35,
                            max_tokens=max_tokens, timeout=DIGEST_TIMEOUT), ""
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {str(e)[:160]}"
                logger.info("%s 第 %d/%d 次失败：%s", model, i + 1, attempts, last)
                if i + 1 < attempts:
                    time.sleep(4)
        return "", last

    if fast:
        body, err = _try("deepseek-flash", 8000, 1)
        used_model = "deepseek-flash"
    else:
        body, err = _try(DIGEST_MODEL, DIGEST_MAX_TOKENS, 2)
        used_model = DIGEST_MODEL
    if not body:
        # v4-pro 彻底不通时退回 flash：宁可质量降一档，也别让当天没有复盘。
        # flash 的 reasoning 也要 5000+，所以 max_tokens 给 8000 而不是默认值。
        logger.warning("v4-pro 生成失败，退回 flash：%s", err)
        body, err2 = _try("deepseek-flash", 8000, 2)
        used_model = "deepseek-flash"
        if not body:
            return {"ok": False, "error": f"v4-pro {err}；flash {err2}"}

    body = (body or "").strip()
    if len(body) < 80:
        return {"ok": False, "error": f"模型输出过短（{len(body)} 字）"}

    mainline = body.split("\n")[0].strip()[:120]
    stats = {"n_items": data["n_items"], "n_news": data["n_news"],
             "n_rounds": data["n_rounds"], "src_stat": data["src_stat"],
             "hot_stocks": data["hot_stocks"], "sector_stat": data["sector_stat"],
             "seconds": int((datetime.now() - t0).total_seconds())}

    row = db.query(BriefDigest).filter(BriefDigest.day == day).first()
    if row is None:
        row = BriefDigest(day=day, created_at=datetime.now())
        db.add(row)
    row.summary = body
    row.mainline = mainline
    row.stats_json = json.dumps(stats, ensure_ascii=False)
    row.model = used_model
    row.n_items = data["n_items"]
    row.n_news = data["n_news"]
    row.error = ""
    row.updated_at = datetime.now()
    db.commit()

    return {"ok": True, "day": day, "chars": len(body), "model": used_model,
            "n_items": data["n_items"], "n_news": data["n_news"],
            "seconds": stats["seconds"]}


def latest(db: Session, before: str | None = None) -> dict:
    """最近一份综述。`before` 给日期时返回该日及之前最近的一份。

    为什么需要 before：早上 7 点看首页时，今天还没收盘、没有今日综述，
    应该展示**上一份**（昨天收盘后写的），而不是空白。
    """
    q = db.query(BriefDigest).filter(BriefDigest.error == "")
    if before:
        q = q.filter(BriefDigest.day <= before)
    row = q.order_by(BriefDigest.day.desc()).first()
    if row is None:
        return {}
    return _to_dict(row)


def list_digests(db: Session, limit: int = 30) -> list[dict]:
    rows = (db.query(BriefDigest)
            .order_by(BriefDigest.day.desc()).limit(limit).all())
    return [_to_dict(r, with_body=False) for r in rows]


def _to_dict(row: BriefDigest, with_body: bool = True) -> dict:
    try:
        stats = json.loads(row.stats_json or "{}")
    except Exception:  # noqa: BLE001
        stats = {}
    out = {"day": row.day, "mainline": row.mainline or "",
           "model": row.model or "", "n_items": row.n_items,
           "n_news": row.n_news, "created_at": (row.created_at.isoformat(timespec="seconds")
                                                if row.created_at else ""),
           "stats": stats}
    if with_body:
        out["summary"] = row.summary or ""
        out["chars"] = len(row.summary or "")
    return out
