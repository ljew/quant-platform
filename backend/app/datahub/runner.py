"""每日数据管道：配置化源抽取 → Bronze → 清洗/打分 → Gold 因子。

步骤由 STEPS 注册表声明（可 only 指定子集）；每次运行记录到
pipeline_runs / pipeline_run_steps 供监控页展示。
"""
from __future__ import annotations

import logging
import os
import time
import traceback
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import func, select

from app.datahub.ns_vars import TEXT_FACTOR_ENABLED
from app.datahub.registry import write_bronze
from app.datahub.source_config import get_source
from app.database import SessionLocal, init_db
from app.models import (FactorDaily, KlineDaily, MpSentimentDaily, NewsMarketDaily, NewsStockDaily,
                        SocialHeatDaily,
                        PipelineRun, PipelineStepLog, Stock, IndexKlineDaily,
                        FactorRegistry, FactorMinedDaily)

logger = logging.getLogger(__name__)

# ============ 步骤实现 ============


def step_extract_index_kline(db) -> int:
    src = get_source("core_index_kline")
    if not src:
        return 0
    from app.services.etl import extract_index_incremental

    stats: dict = {"errors": []}
    try:
        n = extract_index_incremental(db, date.today(), stats=stats)
    except Exception as e:  # noqa: BLE001
        stats["errors"].append(f"{type(e).__name__}: {str(e)[:120]}")
        n = 0
    _STEP_STATS["extract_index_kline"] = stats
    return n


def _bronze_dir(dataset: str) -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "..", "data", "raw", dataset)


def _latest_close_snapshot(db) -> pd.DataFrame | None:
    last = db.execute(select(KlineDaily.trade_date)
                      .order_by(KlineDaily.trade_date.desc()).limit(1)).scalar()
    if not last:
        return None
    rows = db.execute(
        select(KlineDaily.symbol, KlineDaily.trade_date, KlineDaily.open,
               KlineDaily.high, KlineDaily.low, KlineDaily.close,
               KlineDaily.volume, KlineDaily.amount)
        .where(KlineDaily.trade_date == last)
    ).all()
    if not rows:
        return None
    return pd.DataFrame(rows, columns=["symbol", "trade_date", "open", "high",
                                       "low", "close", "volume", "amount"])


def step_extract_stock_kline(db) -> int:
    src = get_source("stock_kline_core")
    if not src:
        return 0
    from app.services.etl import extract_kline_incremental, get_kline_universe

    params = src.get("params", {})
    # 抽取域 = 全A 在市股票（库内 kline_daily 主口径为全A 未复权原始价），
    # **不是**核心池 —— 用核心池会让「全A 回填到某日」之后的每一天都只补 ~1800 只，
    # 形成断崖缺口，并连带让因子步骤判定「覆盖不足」而拒绝计算。
    syms = get_kline_universe(db)
    lookback = int(params.get("lookback_days", 30))
    sd = date.today() - timedelta(days=lookback)
    stats: dict = {"errors": []}
    try:
        n = extract_kline_incremental(db, syms, sd, date.today(), stats=stats)
    except Exception as e:  # noqa: BLE001
        stats["errors"].append(f"{type(e).__name__}: {str(e)[:120]}")
        n = 0
    _STEP_STATS["extract_stock_kline"] = stats
    try:  # Bronze 快照落盘（失败不影响主流程）
        df = _latest_close_snapshot(db)
        if df is not None and len(df):
            write_bronze("market/bars_daily", df)
    except Exception:  # noqa: BLE001
        pass
    return n


def step_extract_attributes(db) -> int:
    if not get_source("stock_attributes"):
        return 0
    from app.services.etl import extract_attributes

    return extract_attributes(db)


def _bronze_articles_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "..", "data", "raw", "text", "wechat_articles")


def _last_article_fetched() -> int:
    base = _bronze_articles_dir()
    best = 0
    if os.path.isdir(base):
        for f in sorted(os.listdir(base))[-3:]:
            if f.endswith(".parquet"):
                try:
                    df = pd.read_parquet(os.path.join(base, f), columns=["fetched_at"])
                    if len(df):
                        best = max(best, int(df["fetched_at"].max()))
                except Exception:  # noqa: BLE001
                    pass
    return best


def step_extract_wechat_articles(db) -> int:
    """公众号语料增量：fetched_at 高于上次批次 → bronze parquet。"""
    src = get_source("wechat_articles")
    if not src:
        return 0
    db_path = src.get("params", {}).get("db_path", "")
    body_chars = int(src.get("params", {}).get("body_chars", 4000))
    # 配置里留空/失效时，跟随 settings.wechat_data_dir（容器 /data/wechat、本机 data/wechat）。
    # 2026-11-07 之前 sources.yaml 写死的是旧 Xiaoan 项目路径，容器内不存在 →
    # 本步骤静默 return 0，**公众号语料从未进入情绪管道**（用户反馈「加上公众号内容」的根因）。
    if not db_path or not os.path.exists(db_path):
        from app.config import settings

        cand = os.path.join(settings.wechat_data_dir, "knowledge.db")
        db_path = cand if os.path.exists(cand) else ""
    if not db_path:
        return 0
    import sqlite3

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        since = _last_article_fetched()
        rows = con.execute(
            f"select id, mp_name, title, publish_time, substr(content_text,1,{body_chars}), fetched_at "
            "from articles where fetched_at > ? order by fetched_at", (since,)
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=["article_id", "mp_name", "title", "publish_time",
                                     "content_text", "fetched_at"])
    write_bronze("text/wechat_articles", df,
                 batch=datetime.now().strftime("%Y%m%d%H%M%S"))
    return len(df)


def step_extract_eastmoney_news(db) -> int:
    """东财全市场新闻流 → Bronze + 市场情绪 + 个股情绪。"""
    src = get_source("eastmoney_news")
    if not src:
        return 0
    from datetime import datetime as _dt

    import pandas as pd
    from app.datahub.extractors.eastmoney_news import EastmoneyNewsExtractor
    from app.datahub.scorers.dict_v1 import DictScorerV1
    from app.datahub.cleaners.core import (load_name_map, match_stock_mentions,
                                           upsert_stock_sentiment)

    params = src.get("params", {})
    pages = int(params.get("pages", 8))
    page_size = int(params.get("page_size", 100))
    try:
        df = EastmoneyNewsExtractor().fetch(pages=pages, page_size=page_size)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"东财新闻抓取失败: {e}")
    if df.empty:
        return 0
    write_bronze("text/eastmoney_news", df,
                 batch=_dt.now().strftime("%Y%m%d%H%M%S"))

    from app.datahub.cleaners.core import load_processed, save_processed

    processed = load_processed()
    new_mask = df["article_id"].map(lambda x: x not in processed)
    df = df[new_mask]
    if df.empty:
        return 0
    scorer = DictScorerV1()
    texts = list((df["title"].fillna("") + "\n" + df["content_text"].fillna("")))
    scored = scorer.score(texts)
    processed |= set(df["article_id"])
    save_processed(processed)
    dates = pd.to_datetime(df["publish_time"], unit="s").dt.strftime("%Y-%m-%d")
    fin_mask = [int(any(k in t[:600] for k in scorer.fin_keywords)) for t in texts]

    # —— 市场级情绪（累加到 news_market_daily）——
    scored["date"] = dates.values
    scored["fin"] = fin_mask
    day = (scored[scored["fin"] > 0]
           .groupby("date").agg(n=("fin", "sum"), bull=("bull", "sum"), bear=("bear", "sum")))
    for d_str, r in day.iterrows():
        d2 = _dt.strptime(d_str, "%Y-%m-%d").date()
        row = db.execute(select(NewsMarketDaily).where(NewsMarketDaily.date == d2)).scalar()
        bull, bear, n = int(r.bull), int(r.bear), int(r.n)
        if row is None:
            db.add(NewsMarketDaily(date=d2, n_articles=n, n_finance=n,
                                   bull_score=bull, bear_score=bear,
                                   net_sentiment=round((bull - bear) / max(bull + bear, 1), 5)))
        else:
            row.n_articles = int(row.n_articles or 0) + n
            row.n_finance = int(row.n_finance or 0) + n
            row.bull_score = int(row.bull_score or 0) + bull
            row.bear_score = int(row.bear_score or 0) + bear
            row.net_sentiment = round((row.bull_score - row.bear_score)
                                      / max(row.bull_score + row.bear_score, 1), 5)
    db.commit()

    # —— 个股级情绪（公司简称匹配）——
    name_map = load_name_map(db)
    stock_rows: dict = {}
    for i, t in enumerate(texts):
        hits = match_stock_mentions(t, name_map)
        if not hits:
            continue
        bull, bear = int(scored.iloc[i]["bull"]), int(scored.iloc[i]["bear"])
        if bull + bear == 0:
            continue
        d_str = dates.iloc[i]
        for sym in hits:
            rec = stock_rows.setdefault((sym, d_str), [0, 0, 0])
            rec[0] += 1
            rec[1] += bull
            rec[2] += bear
    n_stock = upsert_stock_sentiment(db, stock_rows, NewsStockDaily) if stock_rows else 0
    return len(df) + n_stock


def _score_media_text(db, df, tag: str) -> int:
    """媒体类文本通用流程：去重 → 词典打分 → 累加 news_market_daily → 个股匹配。

    tushare 资讯、财联社电报、东财新闻都是「媒体口径」，走同一套，
    保证 `news_market_daily` 里不会出现两种算法拼出来的数。
    """
    from datetime import datetime as _dt

    from app.datahub.cleaners.core import (load_name_map, load_processed,
                                           match_stock_mentions, save_processed,
                                           upsert_stock_sentiment)
    from app.datahub.scorers.dict_v1 import DictScorerV1

    if df is None or df.empty:
        return 0
    processed = load_processed()
    df = df[~df["article_id"].map(lambda x: x in processed)]
    if df.empty:
        return 0

    scorer = DictScorerV1()
    # 去掉正文里的 $xxx$ 标记（财联社的个股/板块标注），否则会污染词频
    body = df["content_text"].fillna("").str.replace(r"\$([^$]+)\$", r"\1", regex=True)
    texts = list(df["title"].fillna("") + "\n" + body)
    scored = scorer.score(texts)
    processed |= set(df["article_id"])
    save_processed(processed)
    dates = pd.to_datetime(df["publish_time"], unit="s").dt.strftime("%Y-%m-%d")

    scored["date"] = dates.values
    day = scored.groupby("date").agg(n=("bull", "size"),
                                     bull=("bull", "sum"), bear=("bear", "sum"))
    for d_str, r in day.iterrows():
        d2 = _dt.strptime(str(d_str), "%Y-%m-%d").date()
        row = db.execute(select(NewsMarketDaily).where(NewsMarketDaily.date == d2)).scalar()
        bull, bear, n = int(r.bull), int(r.bear), int(r.n)
        if row is None:
            db.add(NewsMarketDaily(date=d2, n_articles=n, n_finance=n,
                                   bull_score=bull, bear_score=bear,
                                   net_sentiment=round((bull - bear) / max(bull + bear, 1), 5)))
        else:
            row.n_articles = int(row.n_articles or 0) + n
            row.n_finance = int(row.n_finance or 0) + n
            row.bull_score = int(row.bull_score or 0) + bull
            row.bear_score = int(row.bear_score or 0) + bear
            row.net_sentiment = round((row.bull_score - row.bear_score)
                                      / max(row.bull_score + row.bear_score, 1), 5)
    db.commit()

    name_map = load_name_map(db)
    stock_rows: dict = {}
    for i, t in enumerate(texts):
        hits = match_stock_mentions(t, name_map)
        if not hits:
            continue
        bull, bear = int(scored.iloc[i]["bull"]), int(scored.iloc[i]["bear"])
        if bull + bear == 0:
            continue
        d_str = dates.iloc[i]
        for sym in hits:
            rec = stock_rows.setdefault((sym, d_str), [0, 0, 0])
            rec[0] += 1
            rec[1] += bull
            rec[2] += bear
    n_stock = upsert_stock_sentiment(db, stock_rows, NewsStockDaily) if stock_rows else 0
    logger.info("媒体文本 %s：%d 篇 → 市场情绪 + 个股 %d 条", tag, len(df), n_stock)
    return len(df) + n_stock


def step_extract_cls_telegraph(db) -> int:
    """财联社电报 → Bronze + 市场/个股情绪。

    三个别处没有的优势：接口直接给 `stock_list`（相关个股）、`plate_list`（板块）、
    `level`（A/B/C 重要等级）。采集器会把个股名拼进正文，让简称匹配直接命中。
    """
    src = get_source("cls_telegraph")
    if not src:
        return 0
    from datetime import datetime as _dt

    from app.datahub.extractors.cls_telegraph import ClsTelegraphExtractor

    params = src.get("params", {})
    try:
        df = ClsTelegraphExtractor().fetch(rn=int(params.get("rn", 100)),
                                           pages=int(params.get("pages", 2)),
                                           min_level=str(params.get("min_level", "")))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"财联社电报抓取失败: {e}")
    if df.empty:
        return 0
    write_bronze("text/cls_telegraph", df, batch=_dt.now().strftime("%Y%m%d%H%M%S"))
    return _score_media_text(db, df, "财联社")


def step_extract_tushare_news(db) -> int:
    """tushare 资讯（major_news 深度新闻 + cctv_news 新闻联播）→ Bronze + 情绪。

    与东财/财联社共用 `_score_media_text` 一套口径，都进 `news_market_daily`。
    `pro.news(src=...)` 各源实测 0 行（权限不够），所以只接这两个能用的接口。
    """
    src = get_source("tushare_news")
    if not src:
        return 0
    from datetime import datetime as _dt

    from app.datahub.extractors.tushare_news import TushareNewsExtractor

    params = src.get("params", {})
    try:
        df = TushareNewsExtractor().fetch(days=int(params.get("days", 3)),
                                          major_rows=int(params.get("major_rows", 500)))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"tushare 资讯抓取失败: {e}")
    if df.empty:
        return 0
    write_bronze("text/tushare_news", df, batch=_dt.now().strftime("%Y%m%d%H%M%S"))
    return _score_media_text(db, df, "tushare资讯")


def _upsert_social_heat(db, df, tag: str) -> int:
    """社交关注度聚合：按 (date, source) 覆盖写入 social_heat_daily。"""
    from datetime import datetime as _dt

    if df is None or df.empty:
        return 0
    n = 0
    for (d_str, source), g in df.groupby(["date", "source"]):
        d2 = _dt.strptime(str(d_str), "%Y-%m-%d").date()
        top10 = float(g.nlargest(min(10, len(g)), "heat")["heat"].sum())
        row = db.execute(select(SocialHeatDaily).where(
            SocialHeatDaily.date == d2, SocialHeatDaily.source == source)).scalar()
        vals = dict(n_items=int(len(g)), n_finance=int(g["is_fin"].sum()),
                    heat_total=float(g["heat"].sum()), heat_top10=top10)
        if row is None:
            db.add(SocialHeatDaily(date=d2, source=source, **vals))
        else:
            for k, v in vals.items():
                setattr(row, k, v)
        n += 1
    db.commit()
    logger.info("社交热度 %s：%d 个平台日", tag, n)
    return n


def step_extract_social_heat(db) -> int:
    """雪球 A 股热股 + 微博热搜 → 关注度日频（social_heat_daily）。

    只有热度没有正文，所以**不做多空打分**，只统计条数/热度/财经占比。
    明细落 bronze parquet 留档，便于回溯「哪条热搜被算成财经」。
    """
    src = get_source("social_heat")
    if not src:
        return 0
    from datetime import datetime as _dt

    from app.datahub.extractors.social_heat import SocialHeatExtractor

    params = src.get("params", {})
    try:
        df = SocialHeatExtractor().fetch(xq_size=int(params.get("xq_size", 50)),
                                         weibo_size=int(params.get("weibo_size", 60)))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"社交热度抓取失败: {e}")
    if df.empty:
        return 0
    write_bronze("text/social_heat", df, batch=_dt.now().strftime("%Y%m%d%H%M%S"))
    return _upsert_social_heat(db, df, "雪球+微博")


def _my_symbols(db, max_n: int = 12) -> list[str]:
    """股吧要看的票 = 模拟盘持仓 + 今日 NEW 信号。

    老刘 2026-11-08 先说要「只看自己的票」，随后补一句「不要清理，重点的票
    看看也行」—— 所以最终口径是**并集**：持仓 + 今日 NEW 信号排前面优先抓，
    雪球热股照样补在后面（extractor 里合并）。
    """
    import json as _json

    from sqlalchemy import text as _text

    out: list[str] = []
    try:
        row = db.execute(_text(
            "SELECT holdings_json FROM portfolio_state "
            "ORDER BY as_of_date DESC LIMIT 1")).scalar()
        for h in (_json.loads(row) if row else []):
            s = str((h or {}).get("symbol") or "").lower()
            if s and s not in out:
                out.append(s)
    except Exception:  # noqa: BLE001
        pass
    if len(out) < max_n:
        try:
            rows = db.execute(_text(
                "SELECT symbol FROM signal_actions WHERE action='NEW' "
                "AND date=(SELECT MAX(date) FROM signal_actions) "
                "ORDER BY resonance DESC LIMIT :n"), {"n": max_n}).scalars().all()
            for s in rows:
                s = str(s or "").lower()
                if s and s not in out:
                    out.append(s)
        except Exception:  # noqa: BLE001
            pass
    return out[:max_n]


def step_extract_guba_heat(db) -> int:
    """东财股吧：**自己持仓/候选票**的帖子热度 → social_heat_daily。

    不做多空打分：股吧内容以喊单、标题党为主，词典打分没有意义。
    """
    src = get_source("guba_hot")
    if not src:
        return 0
    from datetime import datetime as _dt

    from app.datahub.extractors.guba_hot import GubaHotExtractor

    params = src.get("params", {})
    mine = _my_symbols(db, max_n=int(params.get("top_n", 12)))
    try:
        # 我的票 + 热股并集：extractor 里会把自己的票排前面、再用热股补足
        df = GubaHotExtractor().fetch(top_n=int(params.get("top_n", 10)),
                                      max_posts=int(params.get("max_posts", 40)),
                                      symbols=mine or None)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"股吧抓取失败: {e}")
    if df.empty:
        return 0
    write_bronze("text/guba_hot", df, batch=_dt.now().strftime("%Y%m%d%H%M%S"))
    return _upsert_social_heat(db, df, "股吧")


def step_extract_announcements(db) -> int:
    """东财全市场公告 → Bronze + 个股情绪（codes 精确关联）。"""
    src = get_source("eastmoney_announcements")
    if not src:
        return 0
    from datetime import datetime as _dt

    import pandas as pd
    from app.datahub.extractors.eastmoney_announcements import EastmoneyAnnouncementExtractor
    from app.datahub.scorers.dict_v1 import DictScorerV1
    from app.datahub.cleaners.core import upsert_stock_sentiment

    params = src.get("params", {})
    try:
        df = EastmoneyAnnouncementExtractor().fetch(
            pages=int(params.get("pages", 5)), page_size=int(params.get("page_size", 100)))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"公告抓取失败: {e}")
    if df.empty:
        return 0
    write_bronze("text/announcements", df,
                 batch=_dt.now().strftime("%Y%m%d%H%M%S"))

    from app.datahub.cleaners.core import load_processed, save_processed

    processed = load_processed()
    new_mask = df["article_id"].map(lambda x: x not in processed)
    df = df[new_mask]
    if df.empty:
        return 0
    scorer = DictScorerV1()
    texts = list(df["title"].fillna(""))
    scored = scorer.score(texts)
    dates = pd.to_datetime(df["publish_time"], unit="s").dt.strftime("%Y-%m-%d")

    processed |= set(df["article_id"])
    save_processed(processed)

    stock_rows: dict = {}
    for i, sym_str in enumerate(df["symbols"].fillna("").items()):
        syms = [x for x in sym_str[1].split("|") if x]
        if not syms:
            continue
        bull, bear = int(scored.iloc[i]["bull"]), int(scored.iloc[i]["bear"])
        if bull + bear == 0:
            continue
        d_str = dates.iloc[i]
        for sym in syms:
            rec = stock_rows.setdefault((sym, d_str), [0, 0, 0])
            rec[0] += 1
            rec[1] += bull
            rec[2] += bear
    n_stock = upsert_stock_sentiment(db, stock_rows, NewsStockDaily) if stock_rows else 0
    return len(df) + n_stock


def step_score_sentiment(db) -> int:
    """Silver/Gold：合并最近 bronze 批次文章 → 词典 v1 打分 → news_market_daily 重算。"""
    from app.datahub.scorers.dict_v1 import DictScorerV1

    base = _bronze_articles_dir()
    if not os.path.isdir(base):
        return 0
    frames = []
    files = sorted(f for f in os.listdir(base) if f.endswith(".parquet"))[-5:]
    for f in files:
        try:
            frames.append(pd.read_parquet(os.path.join(base, f)))
        except Exception:  # noqa: BLE001
            pass
    if not frames:
        return 0
    df = pd.concat(frames).drop_duplicates(subset=["article_id"])
    scorer = DictScorerV1()
    texts = list((df["title"].fillna("") + "\n"
                  + df["content_text"].fillna("").str.slice(0, 3000)))
    scored = scorer.score(texts)
    scored["date_str"] = pd.to_datetime(df["publish_time"], unit="s").dt.strftime("%Y-%m-%d").values
    fin_mask = [int(any(k in t[:600] for k in scorer.fin_keywords))
                for t in (df["title"].fillna("") + "\n"
                          + df["content_text"].fillna("").str.slice(0, 600))]
    scored["fin"] = fin_mask
    day = (scored[scored["fin"] > 0]
           .groupby("date_str").agg(n_finance=("fin", "sum"),
                                    bull=("bull", "sum"), bear=("bear", "sum")))
    n = 0
    for d_str, r in day.iterrows():
        d2 = datetime.strptime(d_str, "%Y-%m-%d").date()
        senti = (r.bull - r.bear) / max(r.bull + r.bear, 1)
        # ⚠️ 写 **公众号专属表** mp_sentiment_daily，不再动 news_market_daily。
        # 2026-11-07 之前这里直接覆盖 news_market_daily 的 bull/bear —— 而东财新闻那个
        # step 是累加，两者互相冲：公众号每天 30~60 篇会把东财 100~500 篇的结果抹掉。
        row = db.execute(select(MpSentimentDaily).where(MpSentimentDaily.date == d2)).scalar()
        if row is None:
            db.add(MpSentimentDaily(date=d2, n_articles=int(r.n_finance),
                                    n_finance=int(r.n_finance), bull_score=int(r.bull),
                                    bear_score=int(r.bear), net_sentiment=round(senti, 5)))
        else:
            row.n_articles = int(r.n_finance)
            row.n_finance = int(r.n_finance)
            row.bull_score = int(r.bull)
            row.bear_score = int(r.bear)
            row.net_sentiment = round(senti, 5)
        n += 1
    db.commit()
    return n


def step_sync_duckdb(db) -> int:
    """分析库同步（Gold 层写入后）。

    ⚠️ 用 sync_all 而不是 sync_after_seed：后者把异常吞成一行 print，
    结果是「步骤绿着、分析库压根没动」。这里捕获后上报 note + 打容器日志，
    步骤会显示「完成 0 行（跳过：xxx）」，看得出发生了什么。

    返回值改成**实际搬运的总行数**（以前恒 0）。恒 0 的坏处是「绿着但没干活」
    和「绿着干了活」在页面上长得一模一样 —— 2026-11-11 分析库冻结 26 小时
    却没人发现，就是栽在这。
    """
    from app.services.duckdb_sync import sync_all

    try:
        res = sync_all()
    except Exception as e:  # noqa: BLE001
        note = f"跳过：{str(e)[:90]}"
        _STEP_STATS.setdefault("sync_duckdb", {})["note"] = note
        print(f"[sync_duckdb] {note}", flush=True)
        return 0
    return int(sum(v[1] for v in res.values()))


def step_compute_factors(db) -> int:
    """Gold：核心池截面因子计算（复用 ETL）。

    补齐「K 线已就绪、但因子缺失」的**所有**交易日，而不是只算最新一天 ——
    否则一旦断供数日，因子将永远追不上行情（每次跑只前进一天）。
    覆盖不足的交易日（盘中半截数据）跳过，避免算出残缺截面。
    """
    from app.services.etl import compute_factor_cross_section, _get_universe

    syms = _get_universe(db)
    since = date.today() - timedelta(days=45)

    # K 线每日覆盖标的数
    cover: dict[date, int] = {}
    for d, c in db.execute(
        select(KlineDaily.trade_date, func.count(func.distinct(KlineDaily.symbol)))
        .where(KlineDaily.trade_date >= since)
        .group_by(KlineDaily.trade_date)
    ).all():
        cover[d] = int(c or 0)
    if not cover:
        return 0
    # 基准 = 全表峰值（K 线主口径为**全A**，约 5550 只/日）。因子本身只算核心池，
    # 但覆盖度必须按全A 校验：某日若只有核心池入库（~1800），说明那是半截数据，
    # 宁可跳过也不能拿它算截面。⚠️ 因此抽取步骤绝不能退回核心池口径，否则
    # 这里会静默地把每一天都判为「覆盖不足」，因子永远追不上行情。
    full = max(cover.values())
    have = {d for d, in db.execute(
        select(FactorDaily.trade_date).where(FactorDaily.trade_date >= since).distinct()
    ).all()}
    # 只有覆盖达 90% 的交易日才算（盘中当天只有几十只，不能拿来算截面）
    todo = sorted(d for d, c in cover.items() if c >= full * 0.9 and d not in have)
    if not todo:
        return 0

    total = 0
    for d in todo:
        try:
            total += compute_factor_cross_section(db, syms, d) or 0
        except Exception:  # noqa: BLE001
            logger.warning("因子计算 %s 失败", d, exc_info=True)
    return total


# ============ 步骤注册与执行器 ============
def step_clean_bars(db) -> int:
    """Silver：K线清洗 + 质检报告。"""
    from app.datahub.cleaners.core import clean_bars, write_report

    df = _latest_close_snapshot(db)
    if df is None:
        return 0
    cleaned, metrics = clean_bars(df)
    report = {"bars": {k: v for k, v in metrics.items() if k != "silver_file"}}
    write_report(None, report)
    return len(cleaned)


def step_clean_text(db) -> int:
    """Silver：文章去重清洗 → text_cleaned。"""
    import os as _os
    from app.datahub.cleaners.core import clean_articles, latest_run_dir, write_report

    base = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__)))), "..", "data", "raw", "text", "wechat_articles")
    frames = []
    if _os.path.isdir(base):
        for f in sorted(_os.listdir(base)):
            if f.endswith(".parquet"):
                try:
                    frames.append(pd.read_parquet(_os.path.join(base, f)))
                except Exception:  # noqa: BLE001
                    pass
    _, metrics = clean_articles(frames)
    rep = read_report = None
    del rep
    prev = None
    try:
        from app.datahub.cleaners.core import read_latest_report
        prev = read_latest_report()
    except Exception:  # noqa: BLE001
        pass
    sections = (prev or {}).get("sections", {})
    sections["text"] = metrics
    if "bars" in sections or metrics:
        write_report(None, sections)
    return metrics.get("rows_out", 0)


def step_compute_mined_factors(db) -> int:
    """Gold：启用状态注册因子的最新截面 → factor_mined_daily（长表，幂等覆盖）。

    「启用」以研究卡为准（research_card_svc.enabled_factor_names）：注册表
    status=='enabled' 且卡状态 enabled 才计算；无卡的按旧行为保底并告警。
    """
    from app.core.engine.factor_expr import eval_factor
    from app.services.research_card_svc import enabled_factor_names

    names = set(enabled_factor_names(db))
    enabled = [r for r in db.execute(select(FactorRegistry).where(
        FactorRegistry.status == "enabled")).scalars().all() if r.name in names]
    if not enabled:
        return 0
    syms = db.execute(select(Stock.symbol)).scalars().all()
    last = db.execute(select(KlineDaily.trade_date)
                      .order_by(KlineDaily.trade_date.desc()).limit(1)).scalar()
    if not last:
        return 0

    n = 0
    axis = db.execute(select(KlineDaily.trade_date).distinct()
                      .order_by(KlineDaily.trade_date)).all()
    axis = [a[0] for a in axis]
    try:
        idx0 = max(0, len(axis) - 131)
        slice_dates = axis[idx0:]
    except Exception:  # noqa: BLE001
        return 0
    aligned: dict[str, dict] = {}
    for sym2, td2, c2 in db.execute(select(KlineDaily.symbol, KlineDaily.trade_date,
                                           KlineDaily.close).where(
            KlineDaily.adj == "none", KlineDaily.trade_date >= slice_dates[0])):
        aligned.setdefault(sym2, {})[td2] = float(c2)
    bench_map = {}
    for td2, c2 in db.execute(select(IndexKlineDaily.trade_date, IndexKlineDaily.close).where(
            IndexKlineDaily.symbol == "sh000906",
            IndexKlineDaily.trade_date >= slice_dates[0]).order_by(IndexKlineDaily.trade_date)):
        bench_map[td2] = float(c2)

    # 个股新闻情绪 lookup（当日或近3日均值）
    from app.models import NewsStockDaily

    news_hist: dict[str, dict] = {}
    for ns_sym, ns_d, ns_v in db.execute(
        select(NewsStockDaily.symbol, NewsStockDaily.date, NewsStockDaily.net_sentiment)
    ).all():
        if ns_v is not None:
            news_hist.setdefault(ns_sym, {})[ns_d] = float(ns_v)

    def _news_lookup(sym2: str) -> float | None:
        hist = news_hist.get(sym2)
        if not hist or not last:
            return None
        vals = [hist[d2] for d2 in hist if 0 <= (last - d2).days <= 3]
        return (sum(vals) / len(vals)) if vals else None

    snap_slice = [d for d in slice_dates]
    rows_by_fac: dict[str, list] = {}
    for fac in enabled:
        vals: list[tuple[str, float]] = []
        for sym2 in syms:
            amap = aligned.get(sym2)
            if not amap or len(amap) < 55:
                continue
            seg = [amap.get(d2) for d2 in snap_slice[-126:] if d2 in amap]
            mkt_b = [bench_map.get(d2) for d2 in snap_slice[-len(seg):]]
            try:
                # 文本变量按总开关注入（关闭时该键不存在，用到它的表达式会被静默跳过）
                v = eval_factor(fac.expr, {"c_m": seg, "c_r": seg, "c_v": seg,
                                           "c_b": seg, "c_t": seg, "mkt_b": mkt_b,
                                           "pe_ttm": None, "pb": None,
                                           "market_cap": None, "roe": None,
                                           "revenue_yoy": None, "profit_yoy": None,
                                           "earnings_surprise": None,
                                           **({"news_senti": _news_lookup(sym2)}
                                              if TEXT_FACTOR_ENABLED else {})})
            except Exception:  # noqa: BLE001
                v = None
            if v is not None:
                import math as _m
                if _m.isfinite(v):
                    vals.append((sym2, v))
        for sym2, v in vals:
            rows_by_fac.setdefault(fac.name, []).append(
                FactorMinedDaily(factor_name=fac.name, date=last, symbol=sym2, value=v))
        n += len(vals)
    # 幂等写入：Core API 删除当日同名因子行（立即生效），再批量插入
    from sqlalchemy import delete as sa_delete

    for fname, objs in rows_by_fac.items():
        db.execute(sa_delete(FactorMinedDaily).where(
            FactorMinedDaily.factor_name == fname,
            FactorMinedDaily.date == last))
        db.add_all(objs)
    db.commit()
    return n


STEPS = [
    ("extract_index_kline", step_extract_index_kline),
    ("extract_stock_kline", step_extract_stock_kline),
    ("extract_attributes", step_extract_attributes),
    ("clean_bars", step_clean_bars),
    ("extract_eastmoney_news", step_extract_eastmoney_news),
    ("extract_announcements", step_extract_announcements),
    ("extract_wechat_articles", step_extract_wechat_articles),
    ("extract_cls_telegraph", step_extract_cls_telegraph),
    ("extract_tushare_news", step_extract_tushare_news),
    ("extract_social_heat", step_extract_social_heat),
    ("extract_guba_heat", step_extract_guba_heat),
    ("clean_text", step_clean_text),
    ("score_sentiment", step_score_sentiment),
    ("compute_mined_factors", step_compute_mined_factors),
    ("compute_factors", step_compute_factors),
    ("sync_duckdb", step_sync_duckdb),
]


# ─────────────────── 步骤后置断言（数据不变量）───────────────────
#
# 为什么不是「0 行就告警」：实测 run 2433/2434/2435，compute_factors 连续三次
# 0 行，而 factor_daily 每天都在正常更新 1808 行 —— 它返回 0 只表示「没有需要
# 补的日子」，是正常 no-op。按 0 行告警会造出一屏噪音，正好把这个页面废掉。
#
# 真正该问的是：**这一步跑完之后，数据事实成立吗？**
# 下面两条是绝对不变量，任何时刻都该成立，且各自对应一个真实发生过的故障：
#
#   · sync_duckdb   → 分析库与 SQLite 的最新日必须一致
#     2026-11-11 分析库冻结 26 小时，步骤照样「完成 0 行」绿色通过
#   · compute_factors → 因子最新日必须追上行情
#     因子落后一天不报任何错，只会让信号/今日观点/回测全部基于旧截面

def _post_duckdb_synced(db) -> tuple[bool, str]:
    """不变量：DuckDB 的 kline 最新日 == SQLite 的。

    行数一致不代表搬的是最新数据，所以要比日期，不比行数。
    """
    from app.config import DATA_DIR

    sqlite_latest = db.execute(select(func.max(KlineDaily.trade_date))).scalar()
    if not sqlite_latest:
        return False, "SQLite 的 kline_daily 为空"
    try:
        import duckdb

        con = duckdb.connect(str(DATA_DIR / "quant.duckdb"), read_only=True)
        try:
            duck_latest = con.execute(
                "select max(trade_date) from kline_daily").fetchone()[0]
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        return False, f"分析库打不开（{type(e).__name__}）"

    if str(duck_latest) < str(sqlite_latest):
        return False, f"分析库停在 {duck_latest}，SQLite 已到 {sqlite_latest}"
    return True, ""


def _post_factor_fresh(db) -> tuple[bool, str]:
    """不变量：factor_daily 的最新日 >= kline_daily 的最新日。

    compute_factors 是「补齐所有缺失日」，正常跑完时因子必须已经追上行情；
    追不上只有两种可能 —— 判据算错（覆盖度基准）或写库失败，两种都不能算绿。
    """
    k = db.execute(select(func.max(KlineDaily.trade_date))).scalar()
    f = db.execute(select(func.max(FactorDaily.trade_date))).scalar()
    if not k:
        return False, "行情表为空"
    if not f:
        return False, "因子表为空"
    if str(f) < str(k):
        return False, f"因子停在 {f}，行情已到 {k}"
    return True, ""


# 步骤 → 后置断言。只给「有绝对不变量可验」的步骤挂，不乱加。
_POSTCHECK = {
    "compute_factors": _post_factor_fresh,
    "sync_duckdb": _post_duckdb_synced,
}


def init_models():
    """确保管道记录表存在。"""
    from app.database import Base, engine

    init_db()
    Base.metadata.create_all(engine, tables=[
        PipelineRun.__table__, PipelineStepLog.__table__])


# 步骤执行过程中收集的细粒度统计（失败明细等）。
# step 函数把结果写入 _STEP_STATS[name]，run_pipeline 读取后写入 PipelineStepLog.message，
# 避免「接口限流导致全军覆没、管道却报 SUCCESS」的静默失败。
_STEP_STATS: dict[str, dict] = {}


def run_pipeline(trigger: str = "manual", only: list[str] | None = None,
                 stop_on_fail: bool = True) -> int:
    """执行管道；每步落 pipeline_run_steps，run 状态含耗时/行数/错误。返回 run_id。"""
    from app.database import Base, engine

    init_db()
    Base.metadata.create_all(engine, tables=[
        PipelineRun.__table__, PipelineStepLog.__table__])
    run_row = PipelineRun(trigger=trigger, status="RUNNING")
    with SessionLocal() as db:
        db.add(run_row)
        db.commit()
        db.refresh(run_row)
        run_id = run_row.id
        steps_ok = steps_fail = steps_warn = 0
        first_error = None
        for name, fn in STEPS:
            if only and name not in only:
                continue
            log = PipelineStepLog(run_id=run_id, name=name, status="OK")
            t0 = time.time()
            try:
                _STEP_STATS.pop(name, None)
                rows = int(fn(db) or 0)
                log.rows = rows
                # 步骤上报的细粒度统计（限流/失败明细）
                st = _STEP_STATS.pop(name, None)
                errs = (st or {}).get("errors") or []
                if errs:
                    uniq = sorted(set(errs))
                    log.message = f"{rows} 行 · {len(errs)} 处失败：" + "；".join(uniq[:2])
                    if rows == 0:
                        # 什么都没干成 —— 不能算成功
                        log.status = "FAIL"
                        steps_fail += 1
                        first_error = first_error or uniq[0]
                    else:
                        log.status = "WARN"
                        steps_ok += 1
                else:
                    extra = ""
                    if st and st.get("skipped_all_fresh"):
                        extra = "（数据已最新，无需补）"
                    elif st and st.get("need_dates"):
                        extra = f"（补 {len(st['need_dates'])} 个交易日）"
                    elif st and st.get("note"):
                        # 备注型：这步没干成活，但不是数据的问题（如分析库扩展缺），
                        # 既不该绿着糊弄人，也不该把整条管道标成失败
                        extra = f"（{st['note']}）"
                    log.message = f"{name} 完成 {rows} 行{extra}"
                    steps_ok += 1

                # 后置断言：不看返回行数，看数据事实（见 _POSTCHECK 的注释）
                check = _POSTCHECK.get(name)
                if check is not None:
                    try:
                        passed, why = check(db)
                    except Exception as e:  # noqa: BLE001
                        passed, why = False, f"断言本身出错 {type(e).__name__}: {e}"
                    if not passed:
                        log.status = "WARN"
                        log.message = f"{log.message} ⚠ 数据不一致：{why}"
                        steps_ok -= 1
                        steps_warn += 1
            except Exception as e:  # noqa: BLE001
                log.status = "FAIL"
                log.message = f"{type(e).__name__}: {e} {traceback.format_exc()[-300:]}"
                steps_fail += 1
                first_error = first_error or str(e)
                db.add(log)
                db.commit()
                if stop_on_fail:
                    break
            finally:
                log.duration_sec = round(time.time() - t0, 2)
                db.add(log)
                db.commit()
        run_row.status = "FAILED" if steps_fail else ("SUCCESS" if steps_ok else "SKIPPED")
        run_row.finished_at = datetime.utcnow()
        run_row.error = first_error
        db.commit()
        return run_id


if __name__ == "__main__":
    print(f"run_id={run_pipeline('manual')}")
