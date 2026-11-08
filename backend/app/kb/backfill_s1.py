"""S1 存量回填：三路文本 → doc_document（统一 ID + PIT 定级 + 跨源去重）。

用法（在 backend/ 目录下）：
    python -m app.kb.backfill_s1 --dry-run 100     # 只处理 100 个 parquet，验证逻辑
    python -m app.kb.backfill_s1                   # 全量回填
    python -m app.kb.backfill_s1 --reset           # 先清空本脚本写入的文档再重建

设计要点：
① doc_id = sha1(source|source_doc_id)，可重入；重复执行靠 INSERT OR IGNORE 幂等
② raw 层存在大量「抓取快照」重复（实测 98.8%），必须先按 article_id 去重再入库
③ 公告无正文（中位 43 字符），新闻为摘要（中位 147 字符），只有公众号是全文
④ 公告 symbols 混有可转债/B股代码，必须按前缀剔除，只保留 A 股
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import html
import json
import os
import re
import sys
import time

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.database import engine
from app.kb.pit import compute_pit, now_local_str

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

# ---------------------------------------------------------------- 源配置
# priority 越小越优先作为转载组的主文档（公告是一手信息，优先）
SOURCES = {
    "eastmoney_ann": dict(
        glob="data/raw/text/announcements/*.parquet",
        doc_type="announcement", priority=0,
    ),
    "eastmoney_news": dict(
        glob="data/raw/text/eastmoney_news/*.parquet",
        doc_type="news", priority=1,
    ),
    "wechat": dict(
        glob="data/raw/text/wechat_articles/*.parquet",
        doc_type="news", priority=2,
    ),
}

# ---------------------------------------------------------------- 文本规范化
_RE_TAG = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"\s+")
_RE_NORM_ID = re.compile(r"[^0-9A-Za-z_\-]")


def norm_text(s) -> str:
    """去 HTML 标签、反转义实体、压缩空白。"""
    if not isinstance(s, str):
        return ""
    s = html.unescape(s)
    s = _RE_TAG.sub(" ", s)
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    return _RE_WS.sub(" ", s).strip()


def make_doc_id(source: str, source_doc_id: str) -> str:
    return hashlib.sha1(f"{source}|{source_doc_id}".encode("utf-8")).hexdigest()[:32]


def make_content_hash(title: str, body: str, day: str) -> str:
    """跨源转载去重键。

    正文缺失/过短时（如公众号只抓到标题、公告只有标题行），标题相同的不同期内容
    无法区分——例如「全球通一级市场信息快讯」每天一期、标题完全一样、正文全空，
    若只按标题哈希会把 49 期并成 1 篇。故此时必须把日期纳入哈希。
    """
    if len(body) < 30:
        return hashlib.sha1(f"{title}\x1f{day}".encode("utf-8")).hexdigest()[:32]
    return hashlib.sha1(f"{title}\x1f{body[:6000]}".encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------- 证券代码
_BOND_PREFIX = {"110", "111", "113", "118", "123", "127", "128"}   # 可转债
_B_SHARE_PREFIX = {"200", "900"}                                    # B 股
_RE_CODE = re.compile(r"^\d{6}$")


def to_entity_id(code: str) -> str:
    """'600519' → 'sh600519'。

    必须沿用全库既有命名：kline_daily / factor_daily / stocks / index_kline_daily
    一律是 sh/sz 前缀式。若此处另起 '.SH' 后缀式，之后每张表 join 都要做一次映射，
    且漏做时表现为「静默零匹配」——最难查的一类 bug。
    """
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return f"sh{code}"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return f"sz{code}"
    if code.startswith(("920", "8", "4")):
        return f"bj{code}"
    return f"sz{code}"


def parse_symbols(raw) -> list[str]:
    """'110090|600933' → ['sh600933']（剔除转债/B股，保留 A 股正股）。"""
    if not isinstance(raw, str) or not raw.strip():
        return []
    out = []
    for tok in re.split(r"[|,;\s]+", raw):
        tok = tok.strip()
        if not _RE_CODE.match(tok):
            continue
        if tok[:3] in _BOND_PREFIX or tok[:3] in _B_SHARE_PREFIX:
            continue
        out.append(to_entity_id(tok))
    return sorted(set(out))


# ---------------------------------------------------------------- 读取
def read_source(code: str, cfg: dict, limit: int | None, log) -> tuple[pd.DataFrame, list[str]]:
    """逐文件读取 + 按 article_id 去重（raw 层是抓取快照，重复率约 99%）。"""
    files = sorted(glob.glob(os.path.join(_PROJECT_ROOT, cfg["glob"])))
    if limit:
        files = files[-limit:]
    frames, bad, seen = [], [], set()
    raw_rows = 0
    t0 = time.time()
    for i, path in enumerate(files, 1):
        try:
            cols = ["article_id", "title", "content_text", "publish_time", "fetched_at"]
            if code == "eastmoney_ann":
                cols.append("symbols")
            if code == "wechat":
                cols.append("mp_name")
            if code == "eastmoney_news":
                cols.append("source")
            df = pd.read_parquet(path, columns=cols)
        except Exception:  # noqa: BLE001 —— 损坏文件跳过并记录
            bad.append(os.path.basename(path))
            continue
        raw_rows += len(df)
        df = df[~df["article_id"].isin(seen)]
        df = df.drop_duplicates(subset=["article_id"], keep="first")
        seen.update(df["article_id"].tolist())
        if len(df):
            frames.append(df)
        if i % 500 == 0:
            log(f"  [{code}] {i}/{len(files)} 文件 · 已收 {sum(len(f) for f in frames):,} 篇 · {time.time()-t0:.0f}s")

    if not frames:
        return pd.DataFrame(), bad
    out = pd.concat(frames, ignore_index=True)
    log(f"  [{code}] 读取完成：{len(files)} 文件 / 原始 {raw_rows:,} 行 / 唯一 {len(out):,} 篇 / 坏文件 {len(bad)}")
    return out, bad


# ---------------------------------------------------------------- 主流程
def build_docs(code: str, cfg: dict, df: pd.DataFrame, now: str) -> pd.DataFrame:
    """规范化为 doc_document 行。"""
    title = df["title"].map(norm_text)
    body = df["content_text"].map(norm_text)

    # 发布时间缺失时退回抓取时间
    pub = pd.to_numeric(df["publish_time"], errors="coerce").fillna(0)
    fetched = pd.to_numeric(df["fetched_at"], errors="coerce").fillna(0)
    fallback = pub <= 0
    pub = np.where(fallback, fetched, pub)

    pit = compute_pit(pd.Series(pub))
    seen_at = pd.to_datetime(pd.Series(fetched), unit="s") + pd.Timedelta(hours=8)
    day = pit["available_at"].str.slice(0, 10)

    out = pd.DataFrame({
        "doc_id": [make_doc_id(code, str(a)) for a in df["article_id"]],
        "content_hash": [make_content_hash(t, b, d) for t, b, d in zip(title, body, day)],
        "source": code,
        "source_doc_id": df["article_id"].astype(str).str.slice(0, 128),
        "doc_type": cfg["doc_type"],
        "title": title.str.slice(0, 400),
        "body": body,
        "body_len": body.str.len(),
        "published_at": pit["available_at"],
        "first_seen_at": seen_at.dt.strftime("%Y-%m-%d %H:%M:%S"),
        "available_at": pit["available_at"],
        "trade_date_applicable": pit["trade_date_applicable"],
        "link_status": "pending",
        "extract_status": "pending",
    })

    # 来源附加信息进 raw_json（媒体名 / 公众号名）
    extra = pd.Series([{} for _ in range(len(df))])
    if code == "eastmoney_news" and "source" in df:
        extra = pd.Series([{"media": norm_text(s)} for s in df["source"]])
    elif code == "wechat" and "mp_name" in df:
        extra = pd.Series([{"mp_name": norm_text(s)} for s in df["mp_name"]])
    out["raw_json"] = extra.map(lambda d: json.dumps(d, ensure_ascii=False) if d else None)

    # 质量标记
    flag = np.where(out["body_len"] == 0, "empty",
                    np.where(out["body_len"] < 30, "short", "ok"))
    flag = np.where(fallback, "no_pub_time", flag)
    out["quality_flag"] = flag

    # 公告自带证券代码 → market_scope 与链接状态
    if code == "eastmoney_ann":
        syms = df["symbols"].map(parse_symbols) if "symbols" in df else pd.Series([[]] * len(df))
        out["market_scope"] = np.where(syms.map(len) > 0, "stock", "market")
        out["_symbols"] = syms
    else:
        out["market_scope"] = "market"
        out["_symbols"] = [[] for _ in range(len(df))]

    return out


INSERT_SQL = """
INSERT INTO doc_document
 (doc_id, content_hash, source, source_doc_id, doc_type, title, body, body_len,
  published_at, first_seen_at, available_at, trade_date_applicable, market_scope,
  link_status, extract_status, quality_flag, raw_json)
VALUES
 (:doc_id, :content_hash, :source, :source_doc_id, :doc_type, :title, :body, :body_len,
  :published_at, :first_seen_at, :available_at, :trade_date_applicable, :market_scope,
  :link_status, :extract_status, :quality_flag, :raw_json)
ON CONFLICT(doc_id) DO UPDATE SET
  content_hash          = excluded.content_hash,
  published_at          = excluded.published_at,
  available_at          = excluded.available_at,
  trade_date_applicable = excluded.trade_date_applicable
"""


def insert_docs(conn, df: pd.DataFrame) -> int:
    cols = ["doc_id", "content_hash", "source", "source_doc_id", "doc_type", "title", "body",
            "body_len", "published_at", "first_seen_at", "available_at",
            "trade_date_applicable", "market_scope", "link_status", "extract_status",
            "quality_flag", "raw_json"]
    payload = df[cols].astype(object).where(pd.notna(df[cols]), None).to_dict("records")
    # ⚠️ 每批不能太大：17 个字段 × 2000 行 = 34000 个绑定变量，超过 SQLite 的
    # 32766 上限会直接报错；而且单条事务持锁太久，会把并发的写者饿死
    # （2026-11-08 实测：和公告正文回补同时跑就是 database is locked）。
    n = 0
    for i in range(0, len(payload), 500):
        conn.execute(text(INSERT_SQL), payload[i:i + 500])
        n += len(payload[i:i + 500])
    conn.commit()
    return n


def link_by_source_code(conn, df: pd.DataFrame) -> int:
    """公告自带证券代码 → 直接写入 doc_entity（match_type=source_code，置信度 1.0）。

    这是唯一不依赖实体链接算法的精确关联，S2 之前先把这块拿到手。
    """
    rows = []
    for doc_id, syms, title in zip(df["doc_id"], df["_symbols"], df["title"]):
        for s in syms:
            rows.append({"doc_id": doc_id, "entity_type": "stock", "entity_id": s,
                         "role": "subject", "confidence": 1.0,
                         "match_type": "source_code", "evidence": title[:200],
                         "span_start": None, "span_end": None})
    if not rows:
        return 0
    sql = """
    INSERT OR IGNORE INTO doc_entity
      (doc_id, entity_type, entity_id, role, confidence, match_type, evidence, span_start, span_end)
    VALUES (:doc_id, :entity_type, :entity_id, :role, :confidence, :match_type, :evidence, :span_start, :span_end)
    """
    for i in range(0, len(rows), 2000):
        conn.execute(text(sql), rows[i:i + 2000])
    conn.commit()
    return len(rows)


def assign_dup_groups(conn) -> tuple[int, int]:
    """按 content_hash 归并转载组，选出主文档。返回 (组数, 被标记为非主文档数)。"""
    df = pd.read_sql("SELECT doc_id, content_hash, source, published_at FROM doc_document", conn)
    prio = {k: v["priority"] for k, v in SOURCES.items()}
    # 先清零：content_hash 依赖 trade_date（正文过短时），PIT 规则修正后分组会变，
    # 不重置会留下「已不属于任何重复组却仍带旧 dup_group_id」的脏数据。
    conn.execute(text("UPDATE doc_document SET dup_group_id = NULL, is_canonical = 1"))
    conn.commit()
    df["prio"] = df["source"].map(prio).fillna(9)
    df["published_at"] = df["published_at"].fillna("9999")
    grp = df.groupby("content_hash")
    dup = [(h, g) for h, g in grp if len(g) > 1]
    if not dup:
        return 0, 0

    updates = []
    for h, g in dup:
        g = g.sort_values(["prio", "published_at"])
        canon = g.iloc[0]["doc_id"]
        for _, r in g.iterrows():
            updates.append({"doc_id": r["doc_id"], "grp": canon,
                            "canon": 1 if r["doc_id"] == canon else 0})

    conn.execute(text("UPDATE doc_document SET dup_group_id = :grp, is_canonical = :canon "
                      "WHERE doc_id = :doc_id"), updates)
    conn.commit()
    n_dup = int(sum(1 for u in updates if u["canon"] == 0))
    return len(dup), n_dup


def seed_entities(conn) -> int:
    """从 stocks 表播种 ent_entity（S2 会补别名，这里先建骨架）。

    stocks.symbol 已是 sh600519 前缀式，直接沿用；只收 sh/sz/bj + 6 位数字的规范代码，
    避免把指数或异常代码混进个股实体表。
    """
    sql = """
    INSERT OR IGNORE INTO ent_entity
      (entity_id, entity_type, name, full_name, aliases_json, industry_code,
       list_date, delist_date, is_active, updated_at)
    SELECT symbol, 'stock', name, NULL, '[]', NULL, NULL, NULL, 1, :now
    FROM stocks
    WHERE name IS NOT NULL
      AND symbol GLOB '[sb][hzy][0-9][0-9][0-9][0-9][0-9][0-9]'
    """
    r = conn.execute(text(sql), {"now": now_local_str()})
    conn.commit()
    # 注意：不能返回 r.rowcount —— 重跑时全部命中 OR IGNORE，rowcount=0 会被误读成「播种失败」
    return int(conn.execute(
        text("SELECT count(*) FROM ent_entity WHERE entity_type='stock'")).scalar() or 0)


def rebuild_fts(conn) -> None:
    conn.execute(text("INSERT INTO doc_fts(doc_fts) VALUES('rebuild')"))
    conn.commit()


def qa_report(conn) -> dict:
    q = lambda s: pd.read_sql(s, conn)  # noqa: E731
    tot = q("SELECT count(*) n FROM doc_document")["n"][0]
    by_src = q("SELECT source, count(*) n, round(avg(body_len),0) avg_len, "
               "min(trade_date_applicable) d0, max(trade_date_applicable) d1 "
               "FROM doc_document GROUP BY source ORDER BY n DESC")
    canon = q("SELECT is_canonical, count(*) n FROM doc_document GROUP BY is_canonical")
    quality = q("SELECT quality_flag, count(*) n FROM doc_document GROUP BY quality_flag ORDER BY n DESC")
    daily = q("SELECT trade_date_applicable, count(*) n FROM doc_document "
              "WHERE is_canonical=1 GROUP BY 1 ORDER BY 1")
    scope = q("SELECT market_scope, count(*) n FROM doc_document GROUP BY 1")
    ent = q("SELECT count(DISTINCT doc_id) n, count(DISTINCT entity_id) e FROM doc_entity")
    pit = q("SELECT count(*) n FROM doc_document WHERE available_at IS NULL OR trade_date_applicable IS NULL")
    # PIT 泄漏：信号可用时刻晚于其所属截面的收盘时点，即用到了「事后才知道的信息」
    leak = q("SELECT count(*) n FROM doc_document WHERE julianday(available_at) > "
             "julianday(trade_date_applicable || ' 15:00:00')")
    return dict(total=int(tot), by_source=by_src, canonical=canon, quality=quality,
                daily=daily, scope=scope, entity=ent, pit_null=int(pit["n"][0]),
                pit_leak=int(leak["n"][0]))


def write_qa(conn, rep: dict, run_id: str) -> None:
    rows = []
    now = now_local_str()
    rows.append(dict(run_id=run_id, check_type="doc_total", target="doc_document",
                     trade_date=None, metric=float(rep["total"]), threshold=None,
                     passed=1, detail=json.dumps({"by_source": rep["by_source"].to_dict("records")},
                                                 ensure_ascii=False, default=str), created_at=now))
    rows.append(dict(run_id=run_id, check_type="pit_null", target="doc_document",
                     trade_date=None, metric=float(rep["pit_null"]), threshold=0.0,
                     passed=1 if rep["pit_null"] == 0 else 0,
                     detail="available_at / trade_date_applicable 空值数", created_at=now))
    rows.append(dict(run_id=run_id, check_type="pit_leak", target="doc_document",
                     trade_date=None, metric=float(rep["pit_leak"]), threshold=0.0,
                     passed=1 if rep["pit_leak"] == 0 else 0,
                     detail="可用时刻晚于所属截面收盘(15:00)的条数，非 0 即有前视", created_at=now))
    sql = """INSERT INTO qa_check (run_id, check_type, target, trade_date, metric, threshold,
             passed, detail, created_at) VALUES (:run_id,:check_type,:target,:trade_date,
             :metric,:threshold,:passed,:detail,:created_at)"""
    conn.execute(text(sql), rows)
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", type=int, default=0, help="每源只处理最后 N 个 parquet 文件")
    ap.add_argument("--reset", action="store_true", help="先删除三源已入库文档（含关联表）")
    ap.add_argument("--only", default="", help="只处理指定源，逗号分隔")
    args = ap.parse_args()

    def log(m):
        print(m, flush=True)

    t0 = time.time()
    run_id = f"s1_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}"
    log(f"=== S1 存量回填 {run_id} ===")

    with engine.connect() as conn:
        if args.reset:
            log("清空三源文档 ...")
            conn.execute(text("DELETE FROM doc_entity WHERE doc_id IN "
                              "(SELECT doc_id FROM doc_document WHERE source IN "
                              "('eastmoney_ann','eastmoney_news','wechat'))"))
            conn.execute(text("DELETE FROM doc_document WHERE source IN "
                              "('eastmoney_ann','eastmoney_news','wechat')"))
            conn.commit()

    total_ins = 0
    for code, cfg in SOURCES.items():
        if args.only and code not in args.only.split(","):
            continue
        log(f"\n--- {code} ---")
        df, bad = read_source(code, cfg, args.dry_run or None, log)
        if df.empty:
            log("  无数据，跳过")
            continue
        docs = build_docs(code, cfg, df, now_local_str())
        with engine.connect() as conn:
            n = insert_docs(conn, docs)
            log(f"  入库 {n:,} 篇（INSERT OR IGNORE，重复自动跳过）")
            if code == "eastmoney_ann":
                m = link_by_source_code(conn, docs)
                log(f"  公告代码直连 doc_entity：{m:,} 条")
        total_ins += len(docs)

    with engine.connect() as conn:
        log("\n--- 转载组归并 ---")
        n_grp, n_dup = assign_dup_groups(conn)
        log(f"  重复组 {n_grp:,} 个 / 非主文档 {n_dup:,} 篇")

        log("--- 播种 ent_entity ---")
        n_ent = seed_entities(conn)
        log(f"  stocks → ent_entity：{n_ent:,} 行")

        rebuild_fts(conn)
        log("  FTS5 索引重建完成")

        rep = qa_report(conn)
        write_qa(conn, rep, run_id)

    # ---------------------------------------------------------- 报告
    log("\n" + "=" * 66)
    log("S1 验收指标")
    log("=" * 66)
    log(f"文档总数（去重后入库）  {rep['total']:,}")
    log(f"\n按来源：")
    for r in rep["by_source"].to_dict("records"):
        log(f"  {r['source']:16s} {r['n']:7,} 篇 | 均长 {r['avg_len']:.0f} 字 | "
            f"{r['d0']} → {r['d1']}")
    log(f"\n主文档/转载：")
    for r in rep["canonical"].to_dict("records"):
        log(f"  is_canonical={int(r['is_canonical'])}  {r['n']:7,} 篇")
    log(f"\n质量标记：")
    for r in rep["quality"].to_dict("records"):
        log(f"  {r['quality_flag']:12s} {r['n']:7,}")
    log(f"\n市场范围：")
    for r in rep["scope"].to_dict("records"):
        log(f"  {r['market_scope']:10s} {r['n']:7,}")
    log(f"\n实体链接：{int(rep['entity']['n'][0]):,} 篇有实体 / 涉及 {int(rep['entity']['e'][0]):,} 个标的")
    log(f"PIT 空值：{rep['pit_null']}  |  PIT 泄漏：{rep['pit_leak']}"
        f" {'✅' if rep['pit_leak'] == 0 else '❌ 存在前视'}")

    d = rep["daily"]
    if len(d):
        log(f"\n适用于交易的日期数：{len(d)} 天"
            f"（{d['trade_date_applicable'].min()} → {d['trade_date_applicable'].max()}）")
        log(f"日均主文档数：{d['n'].mean():.0f} 篇 | 中位 {d['n'].median():.0f} | 最大 {d['n'].max()}")
    log(f"\n总耗时 {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
