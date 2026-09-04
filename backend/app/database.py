"""SQLAlchemy 引擎、Session 工厂与 Base。

设计上兼容 SQLite（开发）与 PostgreSQL+TimescaleDB（生产）。
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

# SQLite 是单写者模型：后端 API、数据调度器、模拟盘调度器、手动补数脚本共享同一个
# 库文件，任何并发写入都会立刻抛 "database is locked"。设置 busy_timeout 让写操作
# 排队等待（而不是直接失败），这是多进程共享 SQLite 的必要配置。
connect_args = {"check_same_thread": False, "timeout": 30} if settings.is_sqlite else {}

engine = create_engine(
    settings.database_url,
    echo=settings.db_echo,
    future=True,
    connect_args=connect_args,
    pool_pre_ping=True,
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


def init_db() -> None:
    """创建所有表。生产环境若使用 TimescaleDB，另见 services/timescale.py。"""
    # 导入模型以确保注册到 Base.metadata
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_sqlite()


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
    try:
        with engine.connect() as conn:
            for table, expected in (("backtests", backtests_expected), ("stocks", stocks_expected), ("paper_tasks", paper_tasks_expected)):
                cols = {
                    r[1]
                    for r in conn.execute(text(f"PRAGMA table_info({table})"))
                }
                for col, ddl in expected.items():
                    if col not in cols:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
            conn.commit()
    except Exception:
        # 表尚未创建或结构差异，交给 create_all 兜底
        pass
