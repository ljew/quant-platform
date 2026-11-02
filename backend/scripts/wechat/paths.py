"""公众号模块统一路径配置。

所有路径都从本文件位置向上推算，默认指向 quant-platform 内的数据目录，
并保留环境变量覆盖能力（便于临时指向别的库做验证）。

目录约定：
  vendor/wechat-rss/       wewe-rss 服务（Node，:4000，负责拉文章列表）
  data/wechat/             本模块全部数据（已 gitignore）
    ├── wewe-rss.db        上游列表库（wewe-rss 写，只读）
    ├── knowledge.db       全文库（本模块写，SQLite + FTS5）
    ├── vault/             Markdown 镜像（可按公众号目录用 Obsidian 打开）
    └── state/             抓取断点状态
"""
from __future__ import annotations

import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
PROJECT_ROOT = BASE.parents[2]  # scripts/wechat -> scripts -> backend -> quant-platform

DATA_DIR = Path(os.environ.get("WECHAT_DATA_DIR", PROJECT_ROOT / "data/wechat"))

# —— 数据库 ——
WEWE_DB = Path(os.environ.get("WEWE_DB_PATH", DATA_DIR / "wewe-rss.db"))
KNOW_DB = Path(os.environ.get("KNOWLEDGE_DB_PATH", DATA_DIR / "knowledge.db"))
VAULT_DIR = Path(os.environ.get("KNOWLEDGE_VAULT_DIR", DATA_DIR / "vault"))

# —— 状态文件 ——
STATE_DIR = Path(os.environ.get("WECHAT_STATE_DIR", DATA_DIR / "state"))
CRAWL_STATE = Path(os.environ.get("KNOWLEDGE_STATE_FILE", STATE_DIR / "crawl_state.json"))
BACKFILL_STATE = Path(os.environ.get("BACKFILL_STATE_FILE", STATE_DIR / "backfill_state.json"))
CRAWL_LOG = Path(os.environ.get("WECHAT_CRAWL_LOG", STATE_DIR / "crawl.log"))

# —— wewe-rss 服务 ——
WEWE_SERVER_DIR = Path(os.environ.get("WEWE_SERVER_DIR", PROJECT_ROOT / "vendor/wechat-rss"))
WEWE_ENV = WEWE_SERVER_DIR / ".env"
WEWE_URL = os.environ.get("WXMON_URL", f"http://127.0.0.1:{os.environ.get('WXMON_PORT', '4000')}")

# —— 重点关注名单（订阅时用于核对）——
WATCH_LIST = Path(os.environ.get("WECHAT_WATCH_LIST", PROJECT_ROOT / "config/wechat-watchlist.txt"))


def ensure_dirs() -> None:
    """确保写路径存在。"""
    for d in (DATA_DIR, VAULT_DIR, STATE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def read_auth_code() -> str:
    """读取 wewe-rss 的 AUTH_CODE。未配置时返回空串（服务端免鉴权模式）。"""
    from_env = os.environ.get("WXMON_AUTH_CODE") or os.environ.get("AUTH_CODE")
    if from_env:
        return from_env.strip()
    if WEWE_ENV.exists():
        for line in WEWE_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("AUTH_CODE"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""
