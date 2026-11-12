"""启动自检 —— 六项「能不能真的干活」，而不是「进程还活着」。

为什么需要它（2026-11-12 复盘）：

    容器的 healthcheck 探 /openapi.json，只要 HTTP 层还活着就判健康。
    但过去两天出的每一个故障，HTTP 都是 200：

    · DuckDB 分析库冻结 26 小时          → 接口 200（读的是 SQLite）
    · SQLite WAL 坏掉、全站查库 500      → openapi 200（它不查库）
    · 信号停在两天前                     → /signal/dates 200（返回旧的）
    · paper_trades 0 行                   → /paper/tasks 200（返回空列表）
    · 编排器连崩 4 次                     → /agent/state 200（状态字段是旧的）

    「进程活着」和「能干活」是两件事。这份自检补的是后者的最小集合 ——
    每一条都对应一个真实发生过的故障，不是凑数的健康项。

用法：
    GET /api/v1/monitor/selfcheck          # 走 60 秒缓存
    GET /api/v1/monitor/selfcheck?fresh=1  # 强制重跑
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta

from sqlalchemy import text

logger = logging.getLogger(__name__)

# 核心路由：少一条就说明某个 router 没挂上（或 include 被条件跳过）
_CORE_ROUTES = (
    "/health",
    "/api/v1/strategy/strategies",
    "/api/v1/paper/tasks",
    "/api/v1/monitor/framework-health",
    "/api/v1/monitor/table-freshness",
    "/api/v1/agent/state",
    "/api/v1/signal/dates",
    "/api/v1/news/stream",
    "/api/v1/research/cards",
)

# 必须有数据才算健康的表（用 limit 1 判非空，不 count 千万行）
_CORE_TABLES = (
    "kline_daily",
    "index_kline_daily",
    "factor_daily",
    "stocks",
)

# 启动后应当存活的调度线程。缺任意一条 = 那个模块的定时任务不会跑。
_EXPECT_THREADS = (
    "data-scheduler",
    "wechat-scheduler",
    "news-feed",
    "foreign-news",
    "hot-brief",
    "fund-scheduler",
)

# 数据最多落后几个交易日仍算正常（含当日未就绪的容差）
_MAX_LAG_DAYS = 2

_CACHE: dict = {}
_CACHE_TTL = 60.0


def _mk(name: str, status: str, value: str, expect: str) -> dict:
    return {"name": name, "status": status, "value": value, "expect": expect}


# ─────────────────────────── 六项检查 ───────────────────────────


def _check_schema() -> dict:
    """应用 schema：路由数量 + 核心路由齐全。

    对应故障：2026-11-12 13:02 加的路由用字符串前向引用，重启后
    /openapi.json 500 —— 容器 healthcheck 探的正是它，于是永远 starting。
    """
    try:
        from app.main import app

        paths = app.openapi().get("paths") or {}
    except Exception as e:  # noqa: BLE001
        return _mk("应用 schema", "FAIL",
                   f"openapi 生成失败：{type(e).__name__}: {str(e)[:80]}",
                   "可生成且核心路由齐全")

    missing = [r for r in _CORE_ROUTES if r not in paths]
    if missing:
        return _mk("应用 schema", "FAIL",
                   f"{len(paths)} 条路由，缺 {', '.join(missing[:3])}",
                   "可生成且核心路由齐全")
    if len(paths) < 200:
        return _mk("应用 schema", "WARN", f"{len(paths)} 条路由（预期 ≥200）",
                   "可生成且核心路由齐全")
    return _mk("应用 schema", "OK", f"{len(paths)} 条路由，核心 9 条齐全",
               "可生成且核心路由齐全")


def _check_db_write() -> dict:
    """SQLite 能拿到写锁。

    对应故障：WAL 坏掉时报 disk I/O error，所有写库接口 500，但根因是
    「读也能开」—— 所以必须真的试一次 BEGIN IMMEDIATE（拿写锁），
    而不是 select 1。用完 ROLLBACK，不留痕迹。
    """
    from app.database import engine

    try:
        t0 = time.time()
        raw = engine.raw_connection()
        try:
            cur = raw.cursor()
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("ROLLBACK")
        finally:
            raw.close()
        ms = int((time.time() - t0) * 1000)
        return _mk("数据库写锁", "OK", f"BEGIN IMMEDIATE 成功（{ms}ms）",
                   "能拿到写锁")
    except Exception as e:  # noqa: BLE001
        return _mk("数据库写锁", "FAIL",
                   f"{type(e).__name__}: {str(e)[:90]}",
                   "能拿到写锁")


def _check_duckdb() -> dict:
    """分析库可读且非空。

    对应故障：DuckDB 冻结 26 小时，所有读它的接口静默降级到 SQLite，
    表面上一切正常 —— 直到信号停更两天才被发现。
    """
    try:
        import duckdb

        from app.config import DATA_DIR

        path = str(DATA_DIR / "quant.duckdb")
        t0 = time.time()
        con = duckdb.connect(path, read_only=True)
        try:
            n, latest = con.execute(
                "select count(*), max(trade_date) from kline_daily").fetchone()
        finally:
            con.close()
        ms = int((time.time() - t0) * 1000)
    except Exception as e:  # noqa: BLE001
        return _mk("分析库可读", "FAIL",
                   f"{type(e).__name__}: {str(e)[:90]}", "可只读打开且有数据")

    if not n:
        return _mk("分析库可读", "FAIL", "kline_daily 为空", "可只读打开且有数据")
    return _mk("分析库可读", "OK", f"{n:,} 行 · 最新 {latest}（{ms}ms）",
               "可只读打开且有数据")


def _check_core_tables(db) -> dict:
    """核心表存在且非空。

    对应故障：paper_trades 从建库起 0 行（增量水位写错，3112 笔成交从没扫过），
    而 /paper/tasks 一直 200。
    """
    empty, missing = [], []
    for tbl in _CORE_TABLES:
        try:
            got = db.execute(text(f'select 1 from "{tbl}" limit 1')).first()
        except Exception:  # noqa: BLE001
            missing.append(tbl)
            continue
        if got is None:
            empty.append(tbl)

    if missing:
        return _mk("核心表非空", "FAIL", f"表不存在：{', '.join(missing)}",
                   "四张核心表都有数据")
    if empty:
        return _mk("核心表非空", "FAIL", f"空表：{', '.join(empty)}",
                   "四张核心表都有数据")
    return _mk("核心表非空", "OK", f"{len(_CORE_TABLES)} 张均有数据",
               "四张核心表都有数据")


def _check_threads() -> dict:
    """关键调度线程存活。

    对应故障：公众号调度器因依赖缺失起不来，被 try/except 吞成一行 warning；
    公众号采集整整两天没跑，页面上看不出来。
    """
    alive = {t.name for t in threading.enumerate() if t.is_alive()}
    dead = [n for n in _EXPECT_THREADS if n not in alive]
    if dead:
        status = "WARN" if len(dead) <= 2 else "FAIL"
        return _mk("调度线程", status, f"未启动：{', '.join(dead)}",
                   f"{len(_EXPECT_THREADS)} 个关键线程在跑")
    return _mk("调度线程", "OK", f"{len(_EXPECT_THREADS)} 个均在跑", "全部在跑")


def _expected_data_date() -> date | None:
    """此刻「本应已落库」的最新交易日。

    19:00 前算上一个交易日：当天的行情要等 ETL 跑完才有，
    15:00 看到「最新是昨天」是正常的，不能算故障。
    """
    from app.core.trading_calendar import is_trading_day

    now = datetime.now()
    d = now.date() if now.hour >= 19 else now.date() - timedelta(days=1)
    for _ in range(10):
        try:
            if is_trading_day(d):
                return d
        except Exception:  # noqa: BLE001
            if d.weekday() < 5:
                return d
        d -= timedelta(days=1)
    return None


def _check_data_fresh(db) -> dict:
    """行情最新日不落后。

    对应故障：11-12 一整天行情没入库（两处「今天跑过就跳过」判据太宽），
    而所有接口都 200，今日观点只是「静静地停在 11-10」。
    """
    try:
        got = db.execute(text("select max(trade_date) from kline_daily")).scalar()
    except Exception as e:  # noqa: BLE001
        return _mk("行情新鲜度", "FAIL", f"读取失败：{type(e).__name__}",
                   f"落后 ≤{_MAX_LAG_DAYS} 个交易日")

    if not got:
        return _mk("行情新鲜度", "FAIL", "无数据", f"落后 ≤{_MAX_LAG_DAYS} 个交易日")

    try:
        latest = date.fromisoformat(str(got)[:10])
    except ValueError:
        return _mk("行情新鲜度", "FAIL", f"非法日期值 {got!r}",
                   f"落后 ≤{_MAX_LAG_DAYS} 个交易日")

    exp = _expected_data_date()
    if exp is None:
        return _mk("行情新鲜度", "WARN", f"最新 {latest}（算不出应有日期）",
                   f"落后 ≤{_MAX_LAG_DAYS} 个交易日")

    lag = (exp - latest).days
    if lag <= 0:
        return _mk("行情新鲜度", "OK", f"最新 {latest}（已到 {exp}）",
                   f"落后 ≤{_MAX_LAG_DAYS} 个交易日")
    if lag <= _MAX_LAG_DAYS:
        return _mk("行情新鲜度", "WARN", f"最新 {latest}，落后 {lag} 天（应到 {exp}）",
                   f"落后 ≤{_MAX_LAG_DAYS} 个交易日")
    return _mk("行情新鲜度", "FAIL", f"最新 {latest}，落后 {lag} 天（应到 {exp}）",
               f"落后 ≤{_MAX_LAG_DAYS} 个交易日")


# ─────────────────────────── 入口 ───────────────────────────

_RANK = {"OK": 0, "WARN": 1, "FAIL": 2}


def run_selfcheck(fresh: bool = False) -> dict:
    """跑一遍六项自检。默认走 60 秒缓存（DuckDB 打开约 300ms，别每次请求都开）。"""
    now = time.time()
    if not fresh and _CACHE and (now - _CACHE.get("_ts", 0)) < _CACHE_TTL:
        return {k: v for k, v in _CACHE.items() if k != "_ts"}

    t0 = time.time()
    from app.database import SessionLocal

    checks: list[dict] = []
    db = SessionLocal()
    try:
        checks.append(_check_schema())
        checks.append(_check_db_write())
        checks.append(_check_duckdb())
        checks.append(_check_core_tables(db))
        checks.append(_check_threads())
        checks.append(_check_data_fresh(db))
    finally:
        db.close()

    worst = "OK"
    for c in checks:
        if _RANK[c["status"]] > _RANK[worst]:
            worst = c["status"]

    result = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "worst": worst,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "checks": checks,
    }
    _CACHE.clear()
    _CACHE.update(result)
    _CACHE["_ts"] = now
    return result


def log_selfcheck() -> None:
    """启动时跑一次并落日志 —— 让「起是起来了，但能不能干活」在日志里有据可查。"""
    try:
        r = run_selfcheck(fresh=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("启动自检本身失败：%s: %s", type(e).__name__, e)
        return

    bad = [c for c in r["checks"] if c["status"] != "OK"]
    if not bad:
        logger.info("启动自检 %s（%sms）：六项全过", r["worst"], r["elapsed_ms"])
        return
    for c in bad:
        log = logger.error if c["status"] == "FAIL" else logger.warning
        log("启动自检 %s · %s：%s（期望 %s）",
            r["worst"], c["name"], c["value"], c["expect"])
