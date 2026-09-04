"""平台全局配置。

开发环境默认 SQLite（零安装即可运行）；生产环境将 DATABASE_URL 改为
Postgres+TimescaleDB 连接串即可，业务代码无需改动。
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：backend/ 的上级
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 用户提供的 Tushare token：环境变量 QUANT_TUSHARE_TOKEN 优先，
# 缺失时回退到下方默认值（本地开发可直接用）。生产建议放入 .env 不要提交。
TUSHARE_TOKEN = os.getenv(
    "QUANT_TUSHARE_TOKEN",
    "d2684763c95c5f46e0cf65dee253d0559ad6ed2ec0b05b12ae557e99",
)

# 默认开发数据库：项目内 SQLite 文件（自动创建）
DEFAULT_SQLITE = f"sqlite:///{DATA_DIR / 'quant_dev.db'}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QUANT_", env_file=str(BASE_DIR / ".env"), extra="ignore"
    )

    # —— 应用 ——
    app_name: str = "Quant Platform"
    debug: bool = True
    api_prefix: str = "/api/v1"

    # —— Tushare（指数成分/行情兜底数据源）——
    tushare_token: str = TUSHARE_TOKEN

    # —— 数据库 ——
    # 开发用 SQLite；生产示例：
    # postgresql://quant:quant@localhost:5432/quant
    # postgresql://quant:quant@localhost:5433/quant  (TimescaleDB)
    database_url: str = DEFAULT_SQLITE
    db_echo: bool = False

    # —— 数据源 ——
    # 增量更新的起始基准年（首次拉全量历史时的最早年份）
    history_start_year: int = 2010
    # 单次批量拉取的标的上限（防止一次性过大）
    batch_size: int = 200

    # —— 调度 ——
    # 是否启用数据调度（每交易日 19:00 ETL 自动日更 + 断供自愈 + 指数成分月度快照）。
    # 由 .env / 环境变量 QUANT_DATA_SCHEDULE 控制；Docker 已置 1，本地建议开启。
    data_schedule: bool = False

    # —— CORS ——
    cors_origins: list[str] = ["*"]

    # —— 前端静态目录 ——
    # 默认托管 Vite React 完整平台构建产物 web/dist（左侧导航 QUANT·DESK 七页）。
    # 构建产物缺失时 main.py 自动退回 Phase1 原生原型 frontend/（单页行情看板）。
    # 可用环境变量 QUANT_FRONTEND_DIR 显式覆盖。
    frontend_dir: str = str(BASE_DIR / "web" / "dist")

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


settings = Settings()
