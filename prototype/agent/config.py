"""原型配置：从 quant-platform 只读拿 LLM 凭据与 API 地址。

设计约束（重要）：
  · 只读。这里绝不允许写 quant-platform 的库 —— 双写会让 SQLite 真损坏。
  · 零依赖。只用标准库，保证 `python3 -m agent.cli` 在任何机器上能直接跑。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

HOME = Path.home()
QUANT_ROOT = Path(os.getenv("QUANT_ROOT", HOME / "quant-platform"))
DB_PATH = QUANT_ROOT / "data" / "quant_dev.db"

# quant-platform 的 HTTP 入口。原型走 API 而非直连数据库，
# 这样「工具」和生产用的是同一套口径（含 PIT、复权、降级链）。
API_BASE = os.getenv("QUANT_API", "http://127.0.0.1:8000/api/v1")
API_TIMEOUT = float(os.getenv("QUANT_API_TIMEOUT", "20"))

# 审计库（原型自己的，与 quant-platform 无关）
AUDIT_DB = Path(__file__).resolve().parent.parent / "data" / "audit.db"


def llm_config() -> dict:
    """读 llm_providers 表的第一行（is_default 优先）。

    为什么直连库而不是调 API：`/llm/providers` 返回的是 mask 过的 key，
    拿不到明文就没法真的发请求。这里是只读，且带 5 秒超时。
    """
    if not DB_PATH.exists():
        raise RuntimeError(f"找不到 quant-platform 数据库：{DB_PATH}")

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute(
            "select name, base_url, api_key, model from llm_providers "
            "order by is_default desc, id limit 1"
        ).fetchone()
    finally:
        conn.close()

    if not row:
        raise RuntimeError("llm_providers 表为空 —— 先在平台页面上配一个大模型通道")

    name, base_url, api_key, model = row
    if not api_key:
        raise RuntimeError(f"通道 {name} 没有 api_key")

    return {
        "name": name,
        "base_url": (base_url or "https://api.deepseek.com/v1").rstrip("/"),
        "api_key": api_key,
        "model": model or "deepseek-chat",
    }


# 每个智能体用哪个模型 —— 抄 Hermes 的 per-agent 分级，成本能降一半。
# 规则：数据提取用小模型，推理/写作用大模型。
MODEL_TIERS = {
    "cheap": os.getenv("AGENT_MODEL_CHEAP", ""),   # 空 = 用默认
    "strong": os.getenv("AGENT_MODEL_STRONG", ""),  # 空 = 用默认
}


def model_for(tier: str = "strong") -> str:
    cfg = llm_config()
    return MODEL_TIERS.get(tier) or cfg["model"]
