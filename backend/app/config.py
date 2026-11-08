"""平台全局配置。

开发环境默认 SQLite（零安装即可运行）；生产环境将 DATABASE_URL 改为
Postgres+TimescaleDB 连接串即可，业务代码无需改动。
"""
from __future__ import annotations

import logging
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：backend/ 的上级
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Tushare token：只从环境变量 / .env 的 QUANT_TUSHARE_TOKEN 读取（Settings 字段）。
# 源码中不放任何默认密钥字面量；缺失时为空字符串 → 禁用 Tushare，数据源走 akshare 等兜底。

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
    tushare_token: str = ""

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

    # —— 大模型（因子自然语言生成）——
    # 走 OpenAI 兼容协议：DeepSeek / 火山方舟(豆包) / DashScope 兼容模式 都适用。
    # 例：
    #   DeepSeek   base_url=https://api.deepseek.com/v1        model=deepseek-chat
    #   火山方舟    base_url=https://ark.cn-beijing.volces.com/api/v3  model=<推理接入点ID>
    #   通义千问    base_url=https://dashscope.aliyuncs.com/compatible-mode/v1  model=qwen-plus
    # 留空 llm_api_key 即关闭该功能：前端自动隐藏 AI 模式，手写表达式路径不受影响。
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"

    # —— CORS ——
    cors_origins: list[str] = ["*"]

    # —— 前端静态目录 ——
    # 托管 Vite React 完整平台构建产物 web/dist（QUANT·DESK v2.0 四入口导航）。
    # 可用环境变量 QUANT_FRONTEND_DIR 显式覆盖。
    frontend_dir: str = str(BASE_DIR / "web" / "dist")

    # —— 公众号模块数据目录 ——
    # 存放 knowledge.db（全文库）、vault/（Markdown 镜像）、state/。
    # 容器内代码在 /app、数据挂载在 /data，compose 用 QUANT_WECHAT_DATA_DIR=/data/wechat 覆盖；
    # 本机运行则自动落到 <项目根>/data/wechat。
    wechat_data_dir: str = str(DATA_DIR / "wechat")

    # 公众号每日增量抓取调度（每天 08:20 / 20:40 两次，只抓近 7 天）。
    # crawl_content.py 只读挂载的 wewe-rss.db，不依赖宿主机 HTTP 服务，所以能跑在容器里。
    wechat_schedule: bool = True

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


settings = Settings()

_log = logging.getLogger(__name__)
if not (settings.tushare_token or "").strip():
    _log.warning(
        "QUANT_TUSHARE_TOKEN 未配置：Tushare 相关能力将不可用（不会回退到源码内置密钥）"
    )
