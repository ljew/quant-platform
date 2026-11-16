"""数据到达 / 处理时点（每类数据「源端几点可得、平台几点处理完、卡在哪一段」）。

与另外两个页面的分工：
  · 数据健康（datahealth）——「这张表现在新鲜吗」，表维度、落后几天。
  · 数据管道（pipeline）——「这次 run 每一步跑了多久」，单次执行维度。
  · 本页（timeline）——「每天几点该到、实际几点到的、卡在哪一段」，时钟维度。

为什么必须单开一页（2026-11-16 两个真实问题，前两页都看不出来）：
  ① 基金 nav 环节排在 19:40，而 tushare 净值当晚 19:00~23:00 才陆续公布，
     排在公布之前 → 每天必然差一天。在「新鲜度」里只显示「落后 1 天」，
     看不出是**时点排错了**。
  ② 11-13 关机、11-16 开机，基金管道一条 run 都没有，state 停在 11-12，
     页面上同样是「落后 N 天」，看不出是「压根没跑」还是「跑了没抓到」。

三个时间口径（页面上的三列）：
  ready  —— 源端经验可获取时点（配置常量，页面上标「经验值」，不是承诺）
  plan   —— 平台调度的触发时点（从各 scheduler 常量读，客观）
  landed —— 这批数据处理完成、库里可查的时刻（pipeline_run_steps / fund_pipeline_state）

时区口径（踩过，别改）：
  · pipeline_runs.started_at 是 **UTC naive**（历史遗留）→ 必须 +8 转本地。
  · pipeline_run_steps 原来只有 duration_sec，没有起止时刻 →
    靠「run 起点 + 逐步骤累加 duration」还原（实测与 run.finished_at 秒级吻合）；
    新 run 起直接读 2026-11-16 新增的 started_at / finished_at 列。
  · research_runs / agent_events / fund_pipeline_state 都是**本地 naive**。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

logger = logging.getLogger("timeline")

_CN = timezone(timedelta(hours=8))
_FUND_DB = Path(os.getenv("QUANT_FUND_DB", "/data/fund_research.db"))

# 落后判定口径
_MARKET = "market"      # 交易日日频（行情/因子/信号）—— 按交易日算落后
_DAILY = "daily"        # 自然日日频（资讯/简报）—— 按自然日算落后
_PERIOD = "period"      # 披露期/月级（基金持仓、成分快照）—— 只记录不判落后
_LIVE = "live"          # 实时/轮询（资讯腿、ETF 采样）—— 只看最后触发

# ── 数据域清单 ────────────────────────────────────────────────
# via: 去哪儿读「实际落库时刻」
#   ("step",  步骤名)     ETL 16 步之一（pipeline_run_steps）
#   ("fund",  阶段key)    基金 13 阶段之一（fund_pipeline_state）
#   ("sched", 模块名)     某调度器的 get_status()
#   ("col",   列名)       表里自带时间戳列（兜底）
# ready 是**经验值**：源端什么时候能拿到。写错会在页面上直接看出来（landed 早于 ready
# 就是排期早于数据发布），所以宁可写保守些。
DOMAINS: list[dict] = [
    # ── 行情 ──
    {"g": "行情", "key": "index_kline", "name": "指数日线", "table": "index_kline_daily",
     "col": "trade_date", "kind": _MARKET, "source": "tushare index_daily",
     "ready": "17:00", "plan": "19:00", "owner": "ETL",
     "via": ("step", "extract_index_kline")},
    {"g": "行情", "key": "stock_kline", "name": "A股日线", "table": "kline_daily",
     "col": "trade_date", "kind": _MARKET, "source": "tushare daily",
     "ready": "17:00", "plan": "19:00", "owner": "ETL",
     "via": ("step", "extract_stock_kline")},
    {"g": "行情", "key": "adj_factor", "name": "复权因子", "table": "adj_factor_daily",
     "col": "trade_date", "kind": _MARKET, "source": "tushare adj_factor（限速 1 次/时）",
     "ready": "17:30", "plan": "19:00", "owner": "ETL",
     # 复权因子在 K 线抽取里顺带补（_ensure_adj_factors），跟 kline 同一步
     "via": ("step", "extract_stock_kline")},
    {"g": "行情", "key": "valuation", "name": "全市场估值", "table": "stock_valuation_daily",
     "col": "trade_date", "kind": _MARKET, "source": "tushare daily_basic",
     "ready": "17:30", "plan": "19:30", "owner": "data_scheduler 估值补齐",
     # 内存触发器，没有落库记录 → 用表里最新日期反推（见 _landed_from_table）
     "via": ("sched", "data_scheduler")},
    {"g": "行情", "key": "attributes", "name": "股票属性/行业", "table": "stocks",
     "col": "updated_at", "kind": _DAILY, "source": "tushare stock_basic",
     "ready": "随时", "plan": "19:00", "owner": "ETL",
     "via": ("step", "extract_attributes")},
    {"g": "行情", "key": "quote_snapshot", "name": "收盘快照", "table": "quote_snapshot",
     "col": "trade_date", "kind": _MARKET, "source": "tushare / 腾讯",
     "ready": "18:00", "plan": "18:30", "owner": "quote_scheduler",
     "ts": "updated_at", "via": ("sched", "quote_scheduler")},
    {"g": "行情", "key": "index_membership", "name": "指数成分(PIT)", "table": "index_membership",
     "col": "trade_date", "kind": _PERIOD, "source": "tushare index_weight（月级快照）",
     "ready": "-", "plan": "19:30 巡检", "owner": "data_scheduler",
     "via": ("sched", "data_scheduler")},

    # ── 资讯 ──
    {"g": "资讯", "key": "news_cls", "name": "财联社电报", "table": "news_feed",
     "col": "fetched_at", "kind": _LIVE, "source": "财联社",
     "ready": "实时", "plan": "每 10 分钟", "owner": "news_feed_scheduler",
     "ts": "fetched_at", "via": ("sched", "news_feed_scheduler")},
    {"g": "资讯", "key": "news_ann", "name": "东财公告", "table": "news_feed",
     "col": "fetched_at", "kind": _LIVE, "source": "东财公告",
     "ready": "盘后集中", "plan": "19:00", "owner": "ETL",
     "via": ("step", "extract_announcements")},
    {"g": "资讯", "key": "news_wechat", "name": "公众号文章", "table": "news_feed",
     "col": "fetched_at", "kind": _LIVE, "source": "微信读书官方接口",
     "ready": "不定时", "plan": "06:20·20:40", "owner": "wechat_scheduler",
     # 公众号采集落在 data/wechat/state/collect_last.json（内存状态重启就丢）
     "via": ("file", "wechat")},
    {"g": "资讯", "key": "heat", "name": "雪球/股吧热度", "table": "social_heat_daily",
     "col": "date", "kind": _DAILY, "source": "雪球 / 东财股吧",
     "ready": "盘中", "plan": "19:00", "owner": "ETL",
     "via": ("step", "extract_social_heat")},
    {"g": "资讯", "key": "sentiment", "name": "情绪打分", "table": "news_market_daily",
     "col": "date", "kind": _DAILY, "source": "本地词典 / LLM",
     "ready": "-", "plan": "19:00", "owner": "ETL",
     "via": ("step", "score_sentiment")},
    # 研报不是 nsi_scheduler（那个算的是「国家队介入强度指数」）。
    # 真正的写入方是 data_scheduler._run_text_update()，19:00 后跟 ETL 一起跑，
    # 每次拉近 2 天。所以按交易日判落后，不按自然日（否则周末必红）。
    {"g": "资讯", "key": "report", "name": "券商研报", "table": "research_report",
     "col": "publish_date", "kind": _MARKET, "source": "tushare report_rc",
     "ready": "19:00", "plan": "19:00", "owner": "data_scheduler 研报同步",
     "ts": "created_at", "via": ("sched", "data_scheduler")},
    {"g": "资讯", "key": "hot_brief", "name": "盘中简报", "table": "hot_brief",
     "col": "created_at", "kind": _LIVE, "source": "财联社+境外+DeepSeek+媒体腿",
     "ready": "-", "plan": "交易日 8 轮（7/9/10/11/13/14/16/21 时）", "owner": "hot_brief_scheduler",
     "ts": "created_at", "via": ("sched", "hot_brief_scheduler")},
    {"g": "资讯", "key": "brief_digest", "name": "每日简报", "table": "brief_digest",
     "col": "day", "kind": _DAILY, "source": "LLM 汇总（v4-pro）",
     "ready": "-", "plan": "21:45", "owner": "hot_brief_scheduler",
     "via": ("col", "created_at")},

    # ── 派生 ──
    {"g": "派生", "key": "factor", "name": "因子截面", "table": "factor_daily",
     "col": "trade_date", "kind": _MARKET, "source": "内部计算（核心池）",
     "ready": "-", "plan": "19:00", "owner": "ETL",
     "via": ("step", "compute_factors")},
    {"g": "派生", "key": "duckdb", "name": "分析库同步", "table": "kline_daily",
     "col": "trade_date", "kind": _MARKET, "source": "SQLite → DuckDB",
     "ready": "-", "plan": "19:00", "owner": "ETL",
     "via": ("step", "sync_duckdb")},
    {"g": "派生", "key": "signal", "name": "选股信号", "table": "signal_daily",
     "col": "date", "kind": _MARKET, "source": "内部选股器",
     "ready": "-", "plan": "17:30 编排", "owner": "orchestrator",
     "ts": "created_at", "via": ("sched", "orchestrator")},
    {"g": "派生", "key": "paper", "name": "模拟盘快照", "table": "paper_snapshots",
     "col": "date", "kind": _MARKET, "source": "内部撮合",
     "ready": "-", "plan": "每 30 秒", "owner": "paper_scheduler",
     "ts": "ts", "via": ("sched", "paper_scheduler")},

    # ── ETF ──
    {"g": "ETF", "key": "etf_flow", "name": "ETF 资金流", "table": "etf_flow_daily",
     "col": "trade_date", "kind": _MARKET, "source": "东财 / tushare",
     "ready": "19:00", "plan": "15:05~20:00 采样 + 19:30 日线", "owner": "etf_flow_scheduler",
     "via": ("sched", "etf_flow_scheduler")},
    {"g": "ETF", "key": "etf_share", "name": "ETF 份额", "table": "etf_share_daily",
     "col": "trade_date", "kind": _MARKET, "source": "tushare fund_share（T+1）",
     "ready": "次日 08:00", "plan": "08:30", "owner": "etf_flow_scheduler",
     "via": ("sched", "etf_flow_scheduler")},
    {"g": "ETF", "key": "etf_quote", "name": "ETF 分钟采样", "table": "etf_quote_intraday",
     "col": "sample_time", "kind": _LIVE, "source": "东财实时",
     "ready": "盘中", "plan": "盘中每 5 分钟", "owner": "etf_flow_scheduler",
     "ts": "sample_time", "via": ("sched", "etf_flow_scheduler")},

    # ── 基金（fund_research.db）──
    {"g": "基金", "key": "fund_nav", "name": "基金净值", "db": "fund", "table": "fund_nav_daily",
     "col": "nav_date", "kind": _MARKET, "source": "tushare / 天天基金（当晚陆续公布）",
     "ready": "19:00~23:00", "plan": "19:40", "owner": "fund_scheduler",
     "via": ("fund", "nav")},
    {"g": "基金", "key": "fund_nav_import", "name": "净值入库", "db": "fund", "table": "fund_nav_daily",
     "col": "nav_date", "kind": _MARKET, "source": "本地 CSV → 库",
     "ready": "-", "plan": "20:15", "owner": "fund_scheduler",
     "via": ("fund", "nav_import")},
    {"g": "基金", "key": "fund_holdings", "name": "基金持仓", "db": "fund", "table": "fund_hold_coverage",
     "col": "end_date", "kind": _PERIOD, "source": "tushare fund_portfolio（季报）",
     "ready": "披露期", "plan": "20:10（仅披露窗口）", "owner": "fund_scheduler",
     "via": ("fund", "holdings")},
    {"g": "基金", "key": "fund_csrc", "name": "监管行业配置", "db": "fund", "table": "fund_csrc_industry",
     "col": "end_date", "kind": _PERIOD, "source": "天天基金 HYPZ",
     "ready": "披露期", "plan": "20:45（仅披露窗口）", "owner": "fund_scheduler",
     "via": ("fund", "csrc")},
    {"g": "基金", "key": "fund_penetration", "name": "持仓穿透", "db": "fund", "table": "fund_industry_exposure",
     "col": "end_date", "kind": _PERIOD, "source": "内部计算",
     "ready": "-", "plan": "22:00", "owner": "fund_scheduler",
     "via": ("fund", "penetration")},
    {"g": "基金", "key": "fund_nowcast", "name": "当日持仓截面", "db": "fund", "table": "fund_industry_nowcast",
     "col": "as_of_date", "kind": _DAILY, "source": "内部推算",
     "ready": "-", "plan": "22:10", "owner": "fund_scheduler",
     "via": ("fund", "nowcast")},
    {"g": "基金", "key": "fund_style", "name": "风格截面", "db": "fund", "table": "fund_style_nowcast",
     "col": "as_of_date", "kind": _DAILY, "source": "内部计算",
     "ready": "-", "plan": "22:35", "owner": "fund_scheduler",
     "via": ("fund", "style_nowcast")},
    {"g": "基金", "key": "fund_regression", "name": "净值回归校验", "db": "fund", "table": "fund_industry_regression",
     "col": "end_date", "kind": _PERIOD, "source": "内部约束 WLS",
     "ready": "-", "plan": "22:40", "owner": "fund_scheduler",
     "via": ("fund", "regression")},
]


def _norm_date(v) -> str | None:
    """统一成 YYYY-MM-DD。基金库的日期列有 '20261113' 和 '2026-11-13' 两种写法，
    不归一化会让 date.fromisoformat 抛错 → 落后天数算不出来、状态全变 n/a。"""
    if v is None:
        return None
    s = str(v).strip()[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s or None


def _latest_col(db, table: str, col: str) -> tuple[str | None, str]:
    """取表里某列最大值，返回 (值前19位, 错误说明)。

    ⚠️ SQLite 的坑：`select max("写错的列名")` **不报错**，双引号标识符找不到时
    会退化成字符串字面量，于是返回的就是列名本身 —— 页面上表现为「最新数据日
    显示 date」这种鬼东西（2026-11-16 第一次跑就踩了 6 张表）。
    所以必须先 pragma 校验列存在，不能靠 try/except。
    """
    try:
        cols = {r[1] for r in db.execute(text(f'pragma table_info("{table}")')).fetchall()}
    except Exception as e:  # noqa: BLE001
        return None, f"读表失败 {type(e).__name__}"
    if not cols:
        return None, f"表 {table} 不存在"
    if col not in cols:
        return None, f"列 {col} 不存在"
    try:
        v = db.execute(text(f'select max("{col}") from "{table}"')).scalar()
    except Exception as e:  # noqa: BLE001
        return None, f"查询失败 {type(e).__name__}"
    return (str(v)[:19] if v is not None else None), ""


# ── 时间小工具 ────────────────────────────────────────────────
def _parse_iso(v) -> datetime | None:
    """宽松解析：'2026-11-16T14:58:13' / '2026-11-16 14:58:13.143048' / datetime。"""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace(" ", "T"))
    except ValueError:
        return None


def _utc_to_local(dt: datetime | None) -> datetime | None:
    """pipeline_runs 的时间列是 UTC naive，转本地 naive。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_CN).replace(tzinfo=None)


def _prev_trading_day(d: date) -> date:
    from app.core.trading_calendar import is_trading_day

    for _ in range(40):
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    return d


def _expected_market(now: datetime) -> date:
    """此刻「应该已经有的」最新交易日。

    16:00 前当天收盘数据还没出来 —— 这时候要求今天反而会天天误报。
    """
    from app.core.trading_calendar import is_trading_day

    d = now.date()
    if is_trading_day(d) and now.hour >= 16:
        return d
    return _prev_trading_day(d - timedelta(days=1))


def _lag_trading_days(latest: date, expected: date) -> int:
    """latest 比 expected 少几个交易日（0 = 已最新）。"""
    if latest >= expected:
        return 0
    from app.core.trading_calendar import trading_days

    try:
        return len(trading_days(latest + timedelta(days=1), expected))
    except Exception:  # noqa: BLE001
        return (expected - latest).days


def _hm(v: datetime | None) -> str | None:
    return v.strftime("%H:%M:%S") if v else None


def _first_hm(text_: str | None) -> tuple[int, int] | None:
    """从 '19:40' / '19:00~23:00' / '15:05~20:00 采样 + 19:30 日线' 里取第一个 HH:MM。"""
    if not text_:
        return None
    import re

    m = re.search(r"(\d{1,2}):(\d{2})", str(text_))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _late_minutes(landed: str | None, plan: str | None, now: datetime) -> int | None:
    """实际落库比计划晚多少分钟（同日才有意义；跨天返回 None，别拿昨天的算今天的账）。

    负数是提前。计划写成区间时取起点 —— 这会让「晚」更宽松，宁可低估不要高估。
    """
    dt = _parse_iso(landed)
    if dt is None or dt.date() != now.date():
        return None
    ph = _first_hm(plan)
    if not ph:
        return None
    return int((dt.hour * 60 + dt.minute) - (ph[0] * 60 + ph[1]))


# ── ETL 步骤时刻 ──────────────────────────────────────────────
_STEP_COLS_READY = False


def _ensure_step_cols(db) -> bool:
    """确保 pipeline_run_steps 有 started_at / finished_at 列。

    这两个列是 2026-11-16 为本页新增的（原来只有 duration_sec）。
    runner.init_models() 会补，但那只在跑管道/看监控页时才执行 ——
    本页可能在它之前被打开，所以这里自己补一次（幂等，模块级只试一次）。
    """
    global _STEP_COLS_READY
    if _STEP_COLS_READY:
        return True
    try:
        cols = {r[1] for r in db.execute(text(
            "pragma table_info(pipeline_run_steps)")).fetchall()}
    except Exception:  # noqa: BLE001
        return False
    if not cols:
        return False
    missing = [c for c in ("started_at", "finished_at") if c not in cols]
    for c in missing:
        try:
            db.execute(text(f"ALTER TABLE pipeline_run_steps ADD COLUMN {c} DATETIME"))
        except Exception as e:  # noqa: BLE001
            logger.warning("补列 pipeline_run_steps.%s 失败: %s", c, e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return False
    if missing:
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            return False
    _STEP_COLS_READY = True
    return True


def _etl_runs(db, limit: int = 6) -> list[dict]:
    """最近几次 ETL run，含每步起止时刻（本地时间）。"""
    has_cols = _ensure_step_cols(db)
    runs = db.execute(text(
        "select id, trigger, status, started_at, finished_at, error "
        "from pipeline_runs order by id desc limit :n"), {"n": limit}).fetchall()
    out: list[dict] = []
    for r in runs:
        rid, trig, status, s_at, f_at, err = r
        start = _utc_to_local(_parse_iso(s_at))
        if has_cols:
            rows = db.execute(text(
                "select name, status, duration_sec, rows, message, started_at, finished_at "
                "from pipeline_run_steps where run_id = :r order by id"), {"r": rid}).fetchall()
        else:
            # 列没补上也不能让整页 500 —— 退回只有 duration 的老口径
            rows = [(*r[:5], None, None) for r in db.execute(text(
                "select name, status, duration_sec, rows, message "
                "from pipeline_run_steps where run_id = :r order by id"), {"r": rid}).fetchall()]
        steps: list[dict] = []
        cursor = start
        for name, st_status, dur, nrows, msg, st_s, st_f in rows:
            dur = float(dur or 0.0)
            # 新 run 有真实列；老 run 只有 duration → 用「run 起点 + 累加」还原。
            # 实测 run 2437：累加 154.85s 与 run.finished_at 完全吻合（14:09:30→14:12:05）。
            s1 = _parse_iso(st_s) or cursor
            e1 = _parse_iso(st_f) or (s1 + timedelta(seconds=dur) if s1 else None)
            steps.append({
                "name": name, "status": st_status, "rows": int(nrows or 0),
                "duration_sec": round(dur, 2),
                "start": s1.isoformat(timespec="seconds") if s1 else None,
                "end": e1.isoformat(timespec="seconds") if e1 else None,
                "message": (msg or "")[:160],
            })
            cursor = e1 or cursor
        out.append({
            "run_id": rid, "trigger": trig, "status": status,
            "started_at": start.isoformat(timespec="seconds") if start else None,
            "finished_at": _utc_to_local(_parse_iso(f_at)).isoformat(timespec="seconds")
            if f_at else None,
            "error": (err or "")[:200] or None,
            "steps": steps,
        })
    return out


def _latest_run_steps(runs: list[dict]) -> dict[str, dict]:
    """最新一次 run 的步骤表 → {步骤名: 步骤信息}。"""
    return {s["name"]: s for s in (runs[0]["steps"] if runs else [])}


# ── 基金阶段时刻 ──────────────────────────────────────────────
def _wechat_last() -> str | None:
    """公众号采集的最后一次运行时刻（data/wechat/state/collect_last.json）。

    调度器的 last_run_at 只在内存里，重启即空；这个 json 是每轮都落盘的。
    """
    import json

    p = Path(os.getenv("WECHAT_DATA_DIR", "/data/wechat")) / "state" / "collect_last.json"
    try:
        with open(p, encoding="utf-8") as fh:
            return str(json.load(fh).get("at") or "")[:19].replace(" ", "T") or None
    except (OSError, ValueError):
        return None


def _fund_states() -> dict[str, dict]:
    """fund_research.db 的 fund_pipeline_state（本地时间字符串）。"""
    if not _FUND_DB.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{_FUND_DB}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {}
    try:
        rows = con.execute(
            "select stage, last_ok_at, last_fail_at, last_error, runs_total, fails_total "
            "from fund_pipeline_state").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    return {r[0]: {"last_ok_at": r[1], "last_fail_at": r[2], "last_error": r[3],
                   "runs_total": r[4] or 0, "fails_total": r[5] or 0} for r in rows}


def _fund_dates_bulk(pairs: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[str | None, str]]:
    """一次连接取多张基金表的最新日期。

    单个查法是 8 个域各开一次 sqlite 连接 —— 实测整页 0.9 秒，大半耗在开关库上。
    """
    if not _FUND_DB.exists():
        return {p: (None, "基金库不存在") for p in pairs}
    out: dict[tuple[str, str], tuple[str | None, str]] = {}
    try:
        con = sqlite3.connect(f"file:{_FUND_DB}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as e:
        return {p: (None, f"连基金库失败 {type(e).__name__}") for p in pairs}
    try:
        for table, col in pairs:
            if (table, col) in out:
                continue
            try:
                cols = {r[1] for r in con.execute(f'pragma table_info("{table}")').fetchall()}
                if not cols:
                    out[(table, col)] = (None, f"表 {table} 不存在")
                elif col not in cols:
                    out[(table, col)] = (None, f"列 {col} 不存在")
                else:
                    v = con.execute(f'select max("{col}") from "{table}"').fetchone()[0]
                    out[(table, col)] = (str(v)[:19] if v is not None else None, "")
            except sqlite3.Error as e:
                out[(table, col)] = (None, f"查询失败 {type(e).__name__}")
    finally:
        con.close()
    return out


def _fund_today_runs() -> list[dict]:
    """今天的基金管道运行记录（跨库，只读）。"""
    if not _FUND_DB.exists():
        return []
    today = date.today().isoformat()
    try:
        con = sqlite3.connect(f"file:{_FUND_DB}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(
            "select stage, status, trigger, started_at, finished_at, duration_sec, rows, error "
            "from fund_pipeline_runs where run_date = ? order by started_at", (today,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [{"stage": r[0], "status": r[1], "trigger": r[2], "started_at": r[3],
             "finished_at": r[4], "duration_sec": r[5], "rows": r[6],
             "error": (r[7] or "")[:160] or None} for r in rows]


# ── 各调度器内存状态 ──────────────────────────────────────────
_SCHED_MODULES = {
    "data_scheduler": "app.core.data_scheduler",
    "quote_scheduler": "app.core.quote_scheduler",
    "news_feed_scheduler": "app.core.news_feed_scheduler",
    "wechat_scheduler": "app.core.wechat_scheduler",
    "hot_brief_scheduler": "app.core.hot_brief_scheduler",
    "nsi_scheduler": "app.core.nsi_scheduler",
    "etf_flow_scheduler": "app.core.etf_flow_scheduler",
    "paper_scheduler": "app.core.engine.paper_scheduler",
    "foreign_news_scheduler": "app.core.foreign_news_scheduler",
    "orchestrator": "app.services.orchestrator",
}


def _sched_status() -> dict[str, dict]:
    """各调度器状态。取不到就当没有（模块没启也不该让页面 500）。"""
    out: dict[str, dict] = {}
    import importlib

    for key, mod in _SCHED_MODULES.items():
        try:
            m = importlib.import_module(mod)
            if hasattr(m, "get_status"):
                st = m.get_status()
            else:
                # orchestrator 用的是 STATE（大写），不是 _status
                st = dict(getattr(m, "_status", None) or getattr(m, "STATE", {}) or {})
            out[key] = st if isinstance(st, dict) else {}
        except Exception as e:  # noqa: BLE001
            out[key] = {"_error": f"{type(e).__name__}: {e}"}
    # 编排器的状态里 elapsed 之类是浮点，JSON 友好化交给 FastAPI
    return out


# ── 主入口 ────────────────────────────────────────────────────
_CACHE: dict = {"at": 0.0, "data": None}
_TTL = 20.0     # 前端 30 秒轮询一次，这个 TTL 保证每次都能拿到新快照、又不重复扫库


def compute_timeline(db, days: int = 1, use_cache: bool = True) -> dict:
    """返回 {now, domains[], etl_runs[], fund_today[], events[], schedule[]}。

    整页要查 30 个域 × (pragma + max) + 6 次 run 的 16 步，实测 ~0.9 秒；
    加 20 秒缓存后回落 ~10ms。页面自己 30 秒轮询，缓存不会让人看到过期数据。
    """
    if use_cache and _CACHE["data"] and time.time() - _CACHE["at"] < _TTL:
        return _CACHE["data"]
    now = datetime.now()
    exp_market = _expected_market(now)
    exp_today = now.date()

    runs = _etl_runs(db)
    steps_now = _latest_run_steps(runs)
    fund_states = _fund_states()
    sched = _sched_status()
    fund_dates = _fund_dates_bulk(
        [(d["table"], d["col"]) for d in DOMAINS if d.get("db") == "fund"]
        + [(d["table"], d["ts"]) for d in DOMAINS if d.get("db") == "fund" and d.get("ts")])

    # —— ① 数据域 ——————————————————————————————
    # 判定顺序要固定，否则口径会互相覆盖（第一版就出过：基金「已 N 天没成功」
    # 提前把 status 设成 FAIL，把后面的落后天数计算整段跳过，lag 永远是 None）：
    #   ① 表/列是否存在（硬故障）
    #   ② 落库时刻从哪来（步骤 / 基金阶段 / 调度内存 / 落盘时间戳）
    #   ③ 落后几个交易日（数据够不够新）
    #   ④ 落库是不是很久没动（管道还在不在跑）
    domains: list[dict] = []
    for dom in DOMAINS:
        row = {
            "group": dom["g"], "key": dom["key"], "name": dom["name"],
            "table": dom["table"], "source": dom["source"], "kind": dom["kind"],
            "ready": dom["ready"], "plan": dom["plan"], "owner": dom["owner"],
            "data_date": None, "landed_at": None, "landed_by": None,
            "lag_days": None, "status": "n/a", "note": "",
            "late_min": None,
        }
        hard_fail = False

        # ① 最新数据日
        if dom.get("db") == "fund":
            latest, why = fund_dates.get((dom["table"], dom["col"]), (None, ""))
        else:
            latest, why = _latest_col(db, dom["table"], dom["col"])
        if latest:
            row["data_date"] = _norm_date(latest)
        if why:
            row["note"] = why
            hard_fail = True

        # ② 实际落库时刻
        kind, ref = dom["via"]
        if kind == "file" and ref == "wechat":
            row["landed_at"], row["landed_by"] = _wechat_last(), "文件·collect_last"
        elif kind == "step":
            st = steps_now.get(ref)
            if st:
                row["landed_at"] = st["end"]
                row["landed_by"] = f"ETL·{ref}"
        elif kind == "fund":
            fs = fund_states.get(ref) or {}
            row["landed_at"] = fs.get("last_ok_at")
            row["landed_by"] = f"基金·{ref}"
        elif kind == "sched":
            st = sched.get(ref) or {}
            for k in ("last_success", "last_run_at", "last_update", "last_sample"):
                if st.get(k):
                    row["landed_at"] = st[k]
                    break
            row["landed_by"] = f"调度·{ref}"
            # 🔴 这些 last_run_at 都是**内存状态**，容器一重启就清空（11-16 实测：
            # 9 个调度器 8 个为 None）。拿不到时必须退回表里的时间戳列 ——
            # 那是落盘的、重启不丢的真实时刻；内存值只在「今天刚跑过」时更准。
            if not row["landed_at"] and dom.get("ts"):
                tc = dom["ts"]
                if dom.get("db") == "fund":
                    v, _why = fund_dates.get((dom["table"], tc), (None, ""))
                else:
                    v, _why = _latest_col(db, dom["table"], tc)
                if v:
                    row["landed_at"] = v
                    row["landed_by"] = f"表·{tc}"
        elif kind == "col":
            v, why2 = _latest_col(db, dom["table"], ref)
            row["landed_at"] = v
            row["landed_by"] = f"列·{ref}"
            if why2 and not row["note"]:
                row["note"] = why2
                hard_fail = True

        # ③ 落后几个交易日 / 自然日
        if row["data_date"] and row["kind"] in (_MARKET, _DAILY, _LIVE):
            try:
                latest_d = date.fromisoformat(row["data_date"])
            except ValueError:
                latest_d = None
            if latest_d:
                if row["kind"] == _MARKET:
                    lag = _lag_trading_days(latest_d, exp_market)
                    row["lag_days"] = lag
                    row["status"] = "OK" if lag == 0 else ("WARN" if lag <= 1 else "FAIL")
                elif row["kind"] == _DAILY:
                    lag = (exp_today - latest_d).days
                    row["lag_days"] = lag
                    row["status"] = "OK" if lag <= 0 else ("WARN" if lag <= 2 else "FAIL")
                else:   # _LIVE：看今天有没有东西进来
                    row["status"] = "OK" if latest_d == exp_today else "WARN"
        elif row["kind"] == _PERIOD:
            row["status"] = "n/a"
            row["note"] = row["note"] or "披露期/月级，不按日判落后"

        # ④ 「管道还在不在跑」—— 落库时刻本身很久没动
        #    对内存/落盘时间戳来源才有意义（ETL 步骤的 landed 就是最近一次 run，
        #    周末不跑很正常，按这个判会周末全红）。
        if kind in ("sched", "fund") and row["landed_at"]:
            ld = _parse_iso(row["landed_at"])
            if ld:
                stale = (now.date() - ld.date()).days
                if stale >= 3 and row["kind"] != _PERIOD:
                    row["note"] = row["note"] or f"最后落库 {stale} 天前"
                    if row["status"] in ("OK", "n/a"):
                        row["status"] = "WARN"
        # 基金日更环节：最后一次成功停在几天前 = 管道没跑，不是数据源没给
        if kind == "fund" and ref in _FUND_DAILY_STAGES:
            fs = fund_states.get(ref) or {}
            if fs.get("last_fail_at") and (not fs.get("last_ok_at")
                                           or fs["last_fail_at"] > fs["last_ok_at"]):
                row["note"] = f"最近一次失败 {fs['last_fail_at'][:16]}"
                row["status"] = "FAIL"
            elif fs.get("last_ok_at"):
                try:
                    ago = (now.date() - date.fromisoformat(
                        str(fs["last_ok_at"])[:10])).days
                except ValueError:
                    ago = 0
                if ago >= 1:
                    row["note"] = f"调度已 {ago} 天未成功"
                    row["status"] = "FAIL" if ago >= 2 else "WARN"

        # 落库为空：说清是「还没到点」还是「压根没记录」
        if not row["landed_at"] and not hard_fail and row["kind"] != _PERIOD:
            ph = _first_hm(dom["plan"])
            if ph and (now.hour, now.minute) < ph:
                # 还没到今天的计划时点 → 落后 1 天是正常等待，不该标黄。
                # 不这么处理，每天上午整页都是 WARN（估值 19:30、研报 19:00 都还没到）。
                row["note"] = f"未到计划时点（{dom['plan']}）"
                if row["status"] == "WARN" and (row["lag_days"] or 0) <= 1:
                    row["status"] = "OK"
            elif not row["note"]:
                row["note"] = "无落盘记录"

        if hard_fail:
            row["status"] = "FAIL"

        # 实际落库 vs 计划处理：晚多少分钟（同日才比，跨天不比）
        row["late_min"] = _late_minutes(row["landed_at"], dom["plan"], now)
        domains.append(row)

    # —— ② 今日事件流 ————————————————————————
    events: list[dict] = []
    today = exp_today.isoformat()

    for r in runs:
        start = _parse_iso(r["started_at"])
        if not start or start.date().isoformat() != today:
            continue
        events.append({"at": r["started_at"], "group": "ETL", "kind": "run_start",
                       "label": f"ETL 管道启动（{r['trigger']}）", "status": r["status"],
                       "detail": f"run #{r['run_id']}"})
        for s in r["steps"]:
            # message 里本来就带「N 行」，再拼一次会变成「7 行 · 7 行 · 1 处失败」
            events.append({"at": s["end"], "group": "ETL", "kind": "step",
                           "label": s["name"], "status": s["status"],
                           "detail": f"{s['duration_sec']}s · "
                                     + (s["message"][:70] or f"{s['rows']} 行")})
        if r["finished_at"]:
            events.append({"at": r["finished_at"], "group": "ETL", "kind": "run_end",
                           "label": f"ETL 管道结束（{r['status']}）", "status": r["status"],
                           "detail": f"run #{r['run_id']}"})

    # 编排（research_runs + agent_events 顶层 step）
    try:
        rr = db.execute(text(
            "select id, trigger, status, started_at, finished_at, data_date "
            "from research_runs where date(started_at) = :d order by id"), {"d": today}).fetchall()
        for rid, trig, status, s_at, f_at, d_date in rr:
            events.append({"at": str(s_at)[:19], "group": "编排", "kind": "run_start",
                           "label": f"研究编排启动（{trig}）", "status": status,
                           "detail": f"run #{rid} · 数据日 {d_date}"})
            if f_at:
                events.append({"at": str(f_at)[:19], "group": "编排", "kind": "run_end",
                               "label": f"研究编排结束（{status}）", "status": status,
                               "detail": f"run #{rid}"})
        ag = db.execute(text(
            "select agent, step, status, rows, duration_ms, created_at from agent_events "
            "where date(created_at) = :d and (step not like 'strategy:%') order by id"),
            {"d": today}).fetchall()
        for agent, step, status, nrows, dur_ms, at in ag:
            events.append({"at": str(at)[:19], "group": "编排", "kind": "agent",
                           "label": f"{agent}·{step}", "status": status or "ok",
                           "detail": (f"{nrows} 行 · " if nrows else "")
                                     + (f"{round(dur_ms / 1000, 1)}s" if dur_ms else "")})
    except Exception as e:  # noqa: BLE001
        events.append({"at": now.isoformat(timespec="seconds"), "group": "编排", "kind": "error",
                       "label": "读编排记录失败", "status": "FAIL",
                       "detail": f"{type(e).__name__}: {e}"})

    # 基金各阶段
    for r in _fund_today_runs():
        label = _FUND_STAGE_LABEL.get(r["stage"], r["stage"])
        if r["started_at"]:
            events.append({"at": str(r["started_at"])[:19], "group": "基金", "kind": "stage_start",
                           "label": f"{label} 开始", "status": r["status"],
                           "detail": f"{r['trigger'] or ''}"})
        if r["finished_at"]:
            events.append({"at": str(r["finished_at"])[:19], "group": "基金", "kind": "stage_end",
                           "label": f"{label} 完成（{r['status']}）", "status": r["status"],
                           "detail": f"{round(r['duration_sec'] or 0, 1)}s · {r['rows'] or 0} 行"
                                     + (f" · {r['error']}" if r["error"] else "")})

    # 调度器最近触发（只收今天）
    for key, st in sched.items():
        at = st.get("last_run_at") or st.get("last_success") or st.get("last_update")
        if not at:
            continue
        s = str(at)[:19].replace(" ", "T")
        if not s.startswith(today):
            continue
        events.append({"at": s, "group": "调度", "kind": "sched",
                       "label": _SCHED_LABEL.get(key, key), "status": "ok",
                       "detail": st.get("last_summary") if isinstance(st.get("last_summary"), str)
                       else ""})

    # ⚠️ 时间格式必须归一后再排序：research_runs 那边给的是 "2026-11-16 14:58:15"
    # （空格），ETL 走 isoformat 给的是 "2026-11-16T14:09:30"。字符串比较里
    # 空格(0x20) < 'T'(0x54)，混着排会把整段编排事件顶到 ETL 前面去 ——
    # 第一版就是这样，14:58 排在 14:09 上面。
    events = [e for e in events if e["at"]]
    for e in events:
        e["at"] = str(e["at"]).replace(" ", "T")[:19]
    events.sort(key=lambda e: e["at"])

    # —— ③ 计划表 ————————————————————————————
    schedule = [
        {"at": "06:20", "label": "公众号采集（早）", "owner": "wechat_scheduler", "group": "资讯"},
        {"at": "08:30", "label": "券商研报 + ETF 份额", "owner": "nsi / etf_flow", "group": "资讯"},
        {"at": "17:30", "label": "研究编排（8 步 DAG）", "owner": "orchestrator", "group": "派生"},
        {"at": "18:30", "label": "收盘快照", "owner": "quote_scheduler", "group": "行情"},
        {"at": "19:00", "label": "ETL 管道（16 步）", "owner": "data_scheduler", "group": "行情"},
        {"at": "19:30", "label": "估值补齐 + ETF 日线", "owner": "data_scheduler / etf_flow", "group": "行情"},
        {"at": "19:40", "label": "基金净值采集", "owner": "fund_scheduler", "group": "基金"},
        {"at": "20:15", "label": "净值入库", "owner": "fund_scheduler", "group": "基金"},
        {"at": "20:20", "label": "公众号采集（晚）", "owner": "wechat_scheduler", "group": "资讯"},
        {"at": "21:45", "label": "每日简报（LLM）", "owner": "hot_brief_scheduler", "group": "资讯"},
        {"at": "22:00~23:30", "label": "基金穿透 / 风格 / 回归", "owner": "fund_scheduler", "group": "基金"},
    ]

    # —— 汇总 ——
    counts: dict[str, int] = {}
    for d in domains:
        counts[d["status"]] = counts.get(d["status"], 0) + 1

    out = {
        "now": now.isoformat(timespec="seconds"),
        "expected_market_date": exp_market.isoformat(),
        "is_trading_day": _is_td(now.date()),
        "counts": counts,
        "domains": domains,
        "etl_runs": runs,
        "fund_today": _fund_today_runs(),
        "fund_states": fund_states,
        "schedulers": {k: _jsonable(v) for k, v in sched.items()},
        "events": events,
        "schedule": schedule,
    }
    _CACHE.update({"at": time.time(), "data": out})
    return out


_FUND_STAGE_LABEL = {
    "nav": "基金净值", "nav_import": "净值入库", "holdings": "基金持仓",
    "csrc": "行业配置", "fof": "FOF 报告", "penetration": "持仓穿透",
    "nowcast": "当日截面", "style": "风格穿透", "style_nowcast": "当日风格",
    "regression": "净值回归", "fof_penetration": "FOF 穿透",
    "fof_check": "FOF 校验", "fof_diagnosis": "FOF 诊断",
}

# 每天必跑的基金环节（持仓/行业配置是披露期才动，不在这里）
_FUND_DAILY_STAGES = {"nav", "nav_import", "nowcast", "style_nowcast", "regression"}

_SCHED_LABEL = {
    "data_scheduler": "ETL 调度器", "quote_scheduler": "收盘快照", "news_feed_scheduler": "资讯轮询",
    "wechat_scheduler": "公众号采集", "hot_brief_scheduler": "简报/日报", "nsi_scheduler": "NSI 国家队指数",
    "etf_flow_scheduler": "ETF 采集", "foreign_news_scheduler": "境外新闻", "orchestrator": "研究编排",
    "paper_scheduler": "模拟盘撮合",
}


def _is_td(d: date) -> bool:
    try:
        from app.core.trading_calendar import is_trading_day

        return bool(is_trading_day(d))
    except Exception:  # noqa: BLE001
        return d.weekday() < 5


def _jsonable(v):
    """状态字典里可能有 datetime / 自定义对象，统一转 JSON 友好。"""
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)
