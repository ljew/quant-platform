"""W46-3 今日稳健摘要 · 告警推导（与前端 deriveRobustnessAlerts 对齐）。

纯函数，不读库；供 pytest 与（可选）聚合端点复用。
前端今日页优先拼已有 monitorDataflow + backtestHistory/Detail。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

SH_TZ = timezone(timedelta(hours=8))

WARN_OOS = {"过拟合", "未通过", "样本不足"}


def days_ago_shanghai(latest: str | None, *, today: date | None = None) -> int | None:
    if not latest:
        return None
    try:
        d = date.fromisoformat(latest[:10])
    except ValueError:
        return None
    now = today or datetime.now(SH_TZ).date()
    return (now - d).days


def derive_robustness_alerts(
    *,
    kline_latest: str | None = None,
    factor_latest: str | None = None,
    cost_profile: dict[str, Any] | None = None,
    oos: dict[str, Any] | None = None,
    has_eval: bool = False,
    today: date | None = None,
) -> list[dict[str, str]]:
    """返回 [{level, text, tab, action}, ...]。tab=btopt|datasys。"""
    out: list[dict[str, str]] = []
    k_ago = days_ago_shanghai(kline_latest, today=today)
    f_ago = days_ago_shanghai(factor_latest, today=today)
    if (k_ago is not None and k_ago > 3) or (f_ago is not None and f_ago > 3):
        out.append(
            {
                "level": "warn",
                "text": "行情/因子数据滞后超过 3 个交易日口径的自然日，请先核对数据中心新鲜度",
                "tab": "datasys",
                "action": "查看数据中心 →",
            }
        )

    if not has_eval:
        out.append(
            {
                "level": "info",
                "text": "暂无回测评估记录；到「回测寻优」跑一次后，这里会显示成本/OOS 徽章摘要",
                "tab": "btopt",
                "action": "去回测寻优 →",
            }
        )
        return out

    verdict = (oos or {}).get("verdict")
    if verdict in WARN_OOS:
        out.append(
            {
                "level": "warn",
                "text": f"最近评估 OOS·{verdict}——建议在回测复盘跑「滚动验证」核对多窗稳定性",
                "tab": "btopt",
                "action": "打开滚动 OOS →",
            }
        )
    elif (oos or {}).get("enabled") is False or oos is None:
        out.append(
            {
                "level": "info",
                "text": "最近回测未带 OOS 门禁；可在回测复盘启用滚动验证",
                "tab": "btopt",
                "action": "打开滚动 OOS →",
            }
        )
    elif (oos or {}).get("fragile_regime") is True:
        out.append(
            {
                "level": "warn",
                "text": "滚动验证标记脆弱 regime——跨窗结论不一致，建议复核",
                "tab": "btopt",
                "action": "打开滚动 OOS →",
            }
        )

    src = (cost_profile or {}).get("source")
    if src == "grid_pick":
        out.append(
            {
                "level": "info",
                "text": "成本口径来自网格选中（grid_pick）；可回成本敏感网格对照 ×1/×2",
                "tab": "btopt",
                "action": "打开成本网格 →",
            }
        )
    elif cost_profile:
        out.append(
            {
                "level": "info",
                "text": "可用成本敏感网格扫描佣金×滑点，筛掉低摩擦才成立的 alpha",
                "tab": "btopt",
                "action": "打开成本网格 →",
            }
        )

    return out
