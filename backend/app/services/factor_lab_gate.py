"""W45-lab：因子实验室注册表 / 研究卡启用门禁。

启用（register 入库 + toggle→enabled + 研究卡→enabled）须最近一次 mine report：
- oos.verdict == "通过"
- rating ∈ {"优秀", "可用"}
- 若表达式依赖 earnings_surprise / pead：pead_pit.mode != "legacy_static"
"""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

ALLOWED_RATINGS = frozenset({"优秀", "可用"})
_ES_RE = re.compile(r"\bearnings_surprise\b|\bpead\b", re.IGNORECASE)


def expr_depends_on_es(expr: str) -> bool:
    return bool(_ES_RE.search(expr or ""))


def load_mine_report(
    db: Session,
    *,
    source_id: int | None = None,
    expr: str | None = None,
) -> dict[str, Any] | None:
    """取最近一次可用挖掘报告（优先 source_id，其次同表达式最新）。"""
    from app.models import FactorMineResult

    row = None
    if source_id is not None:
        row = db.get(FactorMineResult, source_id)
    if row is None and expr:
        row = db.execute(
            select(FactorMineResult)
            .where(FactorMineResult.expr == expr.strip())
            .order_by(FactorMineResult.id.desc())
            .limit(1)
        ).scalar_one_or_none()
    if row is None:
        return None
    try:
        report = json.loads(row.result_json or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(report, dict) or not report.get("ok"):
        return None
    # 补齐落库字段，便于门禁读 rating（result_json 内通常已有）
    report.setdefault("rating", row.rating)
    report.setdefault("id", row.id)
    report.setdefault("expr", row.expr)
    return report


def check_enable_gate(
    report: dict[str, Any] | None,
    *,
    expr: str = "",
) -> list[str]:
    """返回失败原因列表；空列表表示通过。"""
    reasons: list[str] = []
    if not report:
        reasons.append("缺少最近一次挖掘报告（请先在因子实验室完成挖掘）")
        return reasons

    rating = str(report.get("rating") or "")
    if rating not in ALLOWED_RATINGS:
        reasons.append(f"评级须为优秀/可用，当前={rating or '空'}")

    oos = report.get("oos") or {}
    verdict = oos.get("verdict") if isinstance(oos, dict) else None
    if verdict != "通过":
        reasons.append(f"OOS 须通过，当前={verdict or '无'}")
    elif not oos.get("enabled"):
        reasons.append("OOS 不得关闭")
    if int(oos.get("n", 0) or 0) < 20:
        reasons.append(f"OOS 有效截面须≥20，当前={oos.get('n', 0)}")
    oos_t = oos.get("t_stat")
    if oos_t is None or float(oos_t) < 2.0:
        reasons.append(f"OOS Newey–West t 值须≥2.0，当前={oos_t}")
    if float(oos.get("ic_mean", 0.0) or 0.0) <= 0:
        reasons.append("OOS IC 必须为正")

    wf = report.get("walk_forward") or {}
    if wf.get("verdict") != "通过" or int(wf.get("n_folds", 0) or 0) < 3:
        reasons.append("IS 内 walk-forward 须至少 3 折且通过")

    universe = report.get("universe_pit") or {}
    if (universe.get("mode") not in ("pit_index_membership", "custom_lifecycle")
            or not universe.get("lifecycle_filter")):
        reasons.append("股票池必须使用历史成分及上市/退市日期过滤")
    valuation = report.get("valuation_pit") or {}
    if valuation.get("mode") != "asof":
        reasons.append("估值必须按历史截面 as-of 取值，禁止最新快照前视回退")

    use_expr = expr or str(report.get("expr") or "")
    if expr_depends_on_es(use_expr):
        pead = report.get("pead_pit") or {}
        mode = pead.get("mode") if isinstance(pead, dict) else None
        if mode == "legacy_static":
            reasons.append("表达式依赖 earnings_surprise，pead_pit 不可为 legacy_static")

    return reasons


def assert_enable_allowed(
    db: Session,
    *,
    expr: str,
    source_id: int | None = None,
) -> dict[str, Any]:
    """门禁通过则返回 report；失败抛 ValueError（消息可直接给 HTTP 400）。"""
    report = load_mine_report(db, source_id=source_id, expr=expr)
    reasons = check_enable_gate(report, expr=expr)
    if reasons:
        raise ValueError("；".join(reasons))
    assert report is not None
    return report
