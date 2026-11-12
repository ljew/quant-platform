"""SQLAlchemy 引擎、Session 工厂与 Base。

设计上兼容 SQLite（开发）与 PostgreSQL+TimescaleDB（生产）。
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings

# SQLite 是单写者模型：后端 API、数据调度器、模拟盘调度器、手动补数脚本共享同一个
# 库文件，任何并发写入都会立刻抛 "database is locked"。设置 busy_timeout 让写操作
# 排队等待（而不是直接失败），这是多进程共享 SQLite 的必要配置。
connect_args = {"check_same_thread": False, "timeout": 30} if settings.is_sqlite else {}

# 2026-10-03 血泪教训：默认 QueuePool(5+10) 会被多个常驻调度线程（数据/模拟盘/
# 流水线/自动研究）+ 请求线程池耗尽，表现为 /health 秒回、一切查库接口超时（"半死"，
# 只能重启）。SQLite 本地库建连极廉价，用 NullPool（每会话新建连接）从根上消除
# 连接池耗尽与泄漏级联；并发写由 busy_timeout 兜底（WAL 下读写本就不互斥）。
engine = create_engine(
    settings.database_url,
    echo=settings.db_echo,
    future=True,
    connect_args=connect_args,
    poolclass=NullPool if settings.is_sqlite else None,
    pool_pre_ping=not settings.is_sqlite,
)

if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        """SQLite 多进程并发加固：WAL + busy_timeout。

        血泪教训（2026-09-04）：journal_mode=delete 时写事务持 EXCLUSIVE 锁会阻塞
        **所有读**。大回填（seed_index_history 写 385 万行等）期间前端查库请求
        全部卡在 busy_timeout 排队上，SQLAlchemy 连接池被占满 → 后端"半死"：
        非库接口(实时行情)正常、一切查库接口超时，只能重启恢复。WAL 让读写并发
        （写不再阻塞读），从根上消除该问题；synchronous=NORMAL 是 WAL 推荐档。
        注意：WAL 为库文件级持久设置，首连生效后所有进程（含直连 sqlite3 脚本）
        均按 WAL 运行。
        """
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI 依赖：请求级数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


import time

from sqlalchemy.exc import OperationalError


def init_db() -> None:
    """创建所有表。生产环境若使用 TimescaleDB，另见 services/timescale.py。

    对 `disk I/O error` 重试三次：容器刚重建的那几十秒里，Docker 卷（macOS 上是
    gRPC-FUSE）对**新开连接**会间歇性报 I/O 错误，而宿主的 `PRAGMA quick_check`
    是 ok 的、已有连接也照常读 —— 属于挂载层抖动，不是库坏。
    2026-11-10 实测：重启后 9 秒触发的手动 ETL 就死在这一步，
    子进程直接抛异常退出，看起来像「管道坏了」。
    """
    # 导入模型以确保注册到 Base.metadata
    from app import models  # noqa: F401

    last: Exception | None = None
    for attempt in range(3):
        try:
            Base.metadata.create_all(bind=engine)
            _migrate_sqlite()
            return
        except OperationalError as e:      # noqa: PERF203
            if "disk I/O error" not in str(e) and "unable to open" not in str(e):
                raise
            last = e
            time.sleep(2)
    if last is not None:
        raise last


def _migrate_sqlite() -> None:
    """为已存在的 SQLite 表补充新增列（开发态演进用）。"""
    if not settings.is_sqlite:
        return
    from sqlalchemy import text

    # backtests 表需补充的组合回测列
    backtests_expected = {
        "multi_asset": "BOOLEAN DEFAULT 0",
        "universe_size": "INTEGER DEFAULT 0",
        "symbols_used": "INTEGER DEFAULT 0",
        "benchmark_symbol": "VARCHAR(16) DEFAULT ''",
        "benchmark_total_return": "FLOAT DEFAULT 0",
        "excess_return": "FLOAT DEFAULT 0",
        "info_ratio": "FLOAT DEFAULT 0",
        "extra_json": "TEXT DEFAULT '{}'",
    }
    # stocks 表需补充的基本面截面列
    stocks_expected = {
        "market_cap": "FLOAT",
        "pe_ttm": "FLOAT",
        "pb": "FLOAT",
        "roe": "FLOAT",
        "revenue_yoy": "FLOAT",
        "profit_yoy": "FLOAT",
    }
    # paper_tasks 表（模拟盘）演进补列
    paper_tasks_expected = {
        "start_date": "VARCHAR(10)",
        "equity_curve_json": "TEXT DEFAULT '[]'",
        "factor_analysis_json": "TEXT DEFAULT '{}'",
    }
    # factor_mine_results 表：挖掘结果入参签名（用于重复表达式查重）
    factor_mine_expected = {"params_json": "TEXT DEFAULT ''"}
    # research_runs 表：人工闸门倒计时（2026-09-30 P2）
    research_runs_expected = {"gate_deadline": "TIMESTAMP"}
    # hot_brief 表：输入来源拆分（2026-11-07 接入 DeepSeek 搜索后区分快讯/搜索）
    hot_brief_expected = {"n_flash": "INTEGER DEFAULT 0", "n_search": "INTEGER DEFAULT 0",
                          "n_foreign": "INTEGER DEFAULT 0",
                          "inputs_json": "TEXT DEFAULT '[]'", "summary": "TEXT DEFAULT ''",
                          "today_items_json": "TEXT DEFAULT '[]'"}
    # 实盘成交/公司行动：稳定外部标识用于幂等写入。NULL 不参与 SQLite 唯一约束。
    live_fill_expected = {"external_id": "VARCHAR(128)"}
    live_ca_expected = {"external_id": "VARCHAR(128)"}
    auto_research_expected = {"schedule_key": "VARCHAR(32)", "attempt": "INTEGER DEFAULT 1"}
    try:
        with engine.connect() as conn:
            for table, expected in (
                ("backtests", backtests_expected),
                ("stocks", stocks_expected),
                ("paper_tasks", paper_tasks_expected),
                ("factor_mine_results", factor_mine_expected),
                ("research_runs", research_runs_expected),
                ("hot_brief", hot_brief_expected),
                ("live_fill", live_fill_expected),
                ("live_corporate_action", live_ca_expected),
                ("auto_research_runs", auto_research_expected),
            ):
                cols = {
                    r[1]
                    for r in conn.execute(text(f"PRAGMA table_info({table})"))
                }
                for col, ddl in expected.items():
                    if col not in cols:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_live_fill_external "
                "ON live_fill(strategy_key, source, external_id) "
                "WHERE external_id IS NOT NULL"))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_live_ca_external "
                "ON live_corporate_action(strategy_key, source, external_id) "
                "WHERE external_id IS NOT NULL"))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_auto_research_schedule "
                "ON auto_research_runs(job, schedule_key) "
                "WHERE schedule_key IS NOT NULL"))
            # W47-3：多重检验累计表 + 种子
            from app.services.factor_mt import migrate_factor_mt_state
            migrate_factor_mt_state(conn)
            from app.services.factor_workset import migrate_factor_workset
            migrate_factor_workset(conn)
            from app.services.workset_backtest_accept import migrate_factor_workset_accept
            migrate_factor_workset_accept(conn)
            conn.commit()
    except Exception as exc:
        # 实盘账本结构不明时宁可拒绝启动，不能带着半套迁移继续记账。
        raise RuntimeError("SQLite schema migration failed") from exc
