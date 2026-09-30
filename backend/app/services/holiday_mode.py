"""节假日静默模式：非交易日自动把「数据停更」类告警降级为正常。

背景：健康检查里的新鲜度/管道运行阈值按自然日计算，周末和法定节假日
数据停更是预期行为，不应拉低健康分、不应进告警列表。

模式（data/holiday_mode.json {"mode": ...}）：
- auto（默认）：今天不是交易日 → 自动静默（覆盖周末 + 法定节假日）
- on  ：强制静默（长假外出时手动开）
- off ：强制关闭（排查问题时不屏蔽任何告警）

被静默的检查器（MUTE_METRICS）：freshness / pipeline_last_status /
quality_report_age。静默时检查仍执行、数值照常显示，只是状态记 ok 并打
muted 标记，不进 alerts。其余检查（DuckDB 一致性、数据量、成分快照等）
不受影响——真正的故障照常报警。
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CFG_PATH = os.path.join(_PROJECT_ROOT, "data", "holiday_mode.json")

_VALID = ("auto", "on", "off")

MUTE_METRICS = {"freshness", "pipeline_last_status", "quality_report_age"}


def _read_cfg() -> dict:
    try:
        with open(_CFG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("mode") in _VALID:
            return cfg
    except Exception:  # noqa: BLE001
        pass
    return {"mode": "auto"}


def set_mode(mode: str) -> dict:
    if mode not in _VALID:
        raise ValueError(f"mode 必须是 {_VALID} 之一")
    cfg = {"mode": mode}
    os.makedirs(os.path.dirname(_CFG_PATH), exist_ok=True)
    with open(_CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    return state()


def _next_trading_day(after: date) -> str | None:
    from app.core.trading_calendar import is_trading_day
    for i in range(1, 16):
        d = after + timedelta(days=i)
        if is_trading_day(d):
            return d.isoformat()
    return None


def state() -> dict:
    """当前静默状态（供健康报告与 API 使用）。"""
    from app.core.trading_calendar import is_trading_day
    cfg = _read_cfg()
    mode = cfg["mode"]
    today = date.today()
    trading = is_trading_day(today)
    active = mode == "on" or (mode == "auto" and not trading)
    return {
        "mode": mode,
        "active": active,
        "today": today.isoformat(),
        "trading_day": trading,
        "next_trading_day": None if trading else _next_trading_day(today),
    }
