#!/usr/bin/env python3
"""脚本层 Tushare token 的唯一解析入口。源码内不放任何明文密钥。

为什么要有这个模块
------------------
2026-11 之前 fetch_fund_holdings / fetch_fund_nav_daily / fetch_sw_daily
三个脚本各自把密钥写成默认参数：

    TOKEN = os.getenv("TUSHARE_TOKEN", "<56 位明文>")

两个毛病：
1. 仓库/日志/截图一旦带出源码，等于密钥外泄，换 token 还得改三处代码；
2. 容器实际注入的变量名是 ``QUANT_TUSHARE_TOKEN``，脚本只认
   ``TUSHARE_TOKEN`` → 环境变量形同摆设，线上一直在悄悄用那个明文串。
   想轮换密钥时改了 env 却不生效，属于最难查的一类故障。

解析顺序（与 backend/app/config.py 的 Settings 口径对齐）
---------------------------------------------------------
1. ``QUANT_TUSHARE_TOKEN`` —— 平台标准名，docker-compose / .env 注入
2. ``TUSHARE_TOKEN``      —— tushare 官方习惯名，兼容手工调试
3. 项目根 ``.env``        —— 宿主机脱离容器直接跑脚本时兜底

都没有就 ``SystemExit(2)``，绝不回退到任何内置值。

用法
----
    from _tushare_token import require_token, require_pro

    pro = require_pro()                 # 需要客户端时
    ts.set_token(require_token())       # 需要自己 set_token 时
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_KEYS = ("QUANT_TUSHARE_TOKEN", "TUSHARE_TOKEN")

# backend/scripts/_tushare_token.py -> 项目根（宿主机 /Users/xx/quant-platform，
# 容器内是 /，因此容器里拿不到这个文件，只能靠环境变量 —— 那正是期望行为）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOTENV = PROJECT_ROOT / ".env"


def _from_dotenv() -> str:
    """读项目根 .env 里的 token。只认上面两个键，不解析行内注释以外的语法。"""
    if not DOTENV.exists():
        return ""
    try:
        text = DOTENV.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() in ENV_KEYS:
            return val.strip().strip('"').strip("'")
    return ""


def resolve_token() -> str:
    """返回可用的 token；找不到时返回空串（不抛异常，便于自检/打印）。"""
    for key in ENV_KEYS:
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return _from_dotenv()


def require_token() -> str:
    """返回 token；缺失则打印指引并以 exit code 2 退出。"""
    token = resolve_token()
    if token:
        return token
    print(
        "缺少 Tushare token：请设置环境变量 QUANT_TUSHARE_TOKEN（或 TUSHARE_TOKEN），"
        f"或在 {DOTENV} 写入 QUANT_TUSHARE_TOKEN=...；源码内不再保留默认密钥。",
        file=sys.stderr,
    )
    raise SystemExit(2)


def require_pro():
    """返回已绑定 token 的 tushare pro 客户端。"""
    import tushare as ts

    return ts.pro_api(require_token())
