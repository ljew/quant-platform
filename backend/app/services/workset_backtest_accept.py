"""W48-2 工作集短名单回测验收：编排、汇总、快照、过期。

逐成员一本中证800 多头 Top50 簿，复用已冻 W46-1 成本 `fragile` 与 W46-2
`final=stricter(agg,legacy)`。不改裁判，不 bump `n_tested`，不调用 admit/remove，
不实例化 enhanced_factor / csi800_enhanced，不把多成员合成一本组合簿。
"""
from __future__ import annotations

import json
import logging
import traceback
from datetime import date, datetime
from typing import Any, Callable, Iterable, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.cost_defaults import COMMISSION, SLIPPAGE
from app.core.strategies.workset_long_topn import (
    WorksetMemberLongTopN,
    peek_last_run_stats,
    reset_last_run_stats,
)
from app.models import FactorRegistry, FactorWorkset, FactorWorksetAcceptRun
from app.schemas import BacktestRequest, CostSensitivityRequest, WalkForwardRequest
from app.services.cost_sensitivity import run_cost_sensitivity
from app.services.walk_forward import run_walk_forward

logger = logging.getLogger("quant.workset_accept")

ACCEPT_RULES_VERSION = "W48-2-v1"
ACCEPT_TOP_N = 50
ACCEPT_REBALANCE_PERIOD = 21
ACCEPT_MAX_WEIGHT = 0.05
ACCEPT_INDEX_CODE = "000906"
ACCEPT_INDEX_SYMBOL = "sh000906"
ACCEPT_DEFAULT_START = "2023-01-01"
ACCEPT_DEFAULT_END = "2025-06-30"
ACCEPT_DEFAULT_CASH = 1_000_000.0
ACCEPT_ADJ = "qfq"

# 成员四态（产品字面）
VERDICT_UNEVAL = "无法按回测口径验收"
VERDICT_INSUFFICIENT = "样本不足"
VERDICT_REJECT = "拒绝"
VERDICT_PASS = "通过"

# 集合四态
COL_REJECT = "拒绝"
COL_PASS = "通过"
COL_SHORT = "未齐"
COL_UNKNOWN = "无法判定"

RUNNABLE = {VERDICT_PASS, VERDICT_REJECT}

_FORBIDDEN_IF_PRESENT = frozenset({
    "strategy", "grid_pick", "cost_source",
    "members", "registry_ids", "member_ids", "registry_id",
})
_FROZEN_EQUAL = {
    "top_n": ACCEPT_TOP_N,
    "rebalance_period": ACCEPT_REBALANCE_PERIOD,
    "max_weight": ACCEPT_MAX_WEIGHT,
    "n_windows": 3,
    "oos_ratio": 0.3,
    "scheme": "expanding",
    "wf_mode": "walk_forward",
    "wf_n_windows": 3,
    "wf_oos_ratio": 0.3,
    "wf_scheme": "expanding",
    "wf_metric": "sharpe",
    "index_code": ACCEPT_INDEX_CODE,
    "index_symbol": ACCEPT_INDEX_SYMBOL,
    "min_is_bars": 60,
    "min_oos_bars": 20,
    "commission": COMMISSION,
    "slippage": SLIPPAGE,
    "base_commission": COMMISSION,
    "base_slippage": SLIPPAGE,
}
_FROZEN_FALSE = frozenset({
    "neutralize_industry", "neutralize_marketcap", "short",
    "neutralize",
})


class AcceptError(Exception):
    """路由层映射为 HTTPException（带 error_code）。"""

    def __init__(self, status: int, error_code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.message = message
        self.extra = extra

    def to_detail(self) -> dict[str, Any]:
        d: dict[str, Any] = {"error_code": self.error_code, "message": self.message}
        d.update(self.extra)
        return d


class WorksetAcceptRequest(BaseModel):
    """POST 体：只接受区间 / 复权 / 初始资金；写死规则不可改。"""

    model_config = ConfigDict(extra="allow")

    start: Optional[str] = None
    end: Optional[str] = None
    adj: Optional[str] = None
    initial_cash: Optional[float] = None


def migrate_factor_workset_accept(conn) -> None:
    """SQLite migrate：建薄表（由 database._migrate_sqlite 在工作集迁移之后调用）。"""
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS factor_workset_accept_run ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "created_at DATETIME NOT NULL, "
        "finished_at DATETIME, "
        "status TEXT NOT NULL, "
        "job_id TEXT UNIQUE, "
        "start TEXT NOT NULL, "
        "end TEXT NOT NULL, "
        "adj TEXT NOT NULL DEFAULT 'qfq', "
        "initial_cash REAL NOT NULL DEFAULT 1000000, "
        "rules_version TEXT NOT NULL, "
        "index_code TEXT NOT NULL DEFAULT '000906', "
        "index_symbol TEXT NOT NULL DEFAULT 'sh000906', "
        "top_n INTEGER NOT NULL DEFAULT 50, "
        "rebalance_period INTEGER NOT NULL DEFAULT 21, "
        "max_weight REAL NOT NULL DEFAULT 0.05, "
        "cost_commission REAL NOT NULL, "
        "cost_slippage REAL NOT NULL, "
        "wf_mode TEXT NOT NULL DEFAULT 'walk_forward', "
        "wf_n_windows INTEGER NOT NULL DEFAULT 3, "
        "wf_oos_ratio REAL NOT NULL DEFAULT 0.3, "
        "wf_scheme TEXT NOT NULL DEFAULT 'expanding', "
        "wf_metric TEXT NOT NULL DEFAULT 'sharpe', "
        "snapshot_json TEXT NOT NULL, "
        "members_json TEXT, "
        "collection_verdict TEXT, "
        "error_message TEXT, "
        "note TEXT"
        ")"
    ))


def build_member_meta(expr: str) -> tuple[dict, dict]:
    """内部 meta + 参数。cls 为薄壳；lookback 只为取数窗口，不是启用动量因子。"""
    meta = {
        "cls": WorksetMemberLongTopN,
        "multi_asset": True,
        "index_code": ACCEPT_INDEX_CODE,
        "index_symbol": ACCEPT_INDEX_SYMBOL,
        "pool_mode": "index",
        "key": "_workset_member_long_topn",
        "name": "工作集成员多头TopN",
        "default_params": {},
    }
    params = {
        "expr": expr,
        "top_n": ACCEPT_TOP_N,
        "rebalance_period": ACCEPT_REBALANCE_PERIOD,
        "max_weight": ACCEPT_MAX_WEIGHT,
        "max_position_pct": ACCEPT_MAX_WEIGHT,
        "momentum_lookback": 126,
        "beta_lookback": 126,
        "tail_lookback": 121,
        "vol_lookback": 61,
        "warmup_days": 126,
    }
    return meta, params


def snapshot_fingerprint(members: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for m in members:
        rows.append({
            "registry_id": m.get("registry_id"),
            "expr": m.get("expr"),
            "name": m.get("name"),
            "added_at": _iso(m.get("added_at")),
            "rating": m.get("rating"),
            "t_stat": m.get("t_stat"),
        })
    return {"n": len(rows), "members": rows}


def _iso(v: Any) -> Any:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def is_stale(
    row: FactorWorksetAcceptRun,
    current_workset: Iterable[dict[str, Any]],
    request_range: Optional[dict[str, Any]] = None,
) -> tuple[bool, Optional[str]]:
    """读时过期：工作集成员/表达式变化，或规则版本 / 区间 / adj 不一致。"""
    try:
        snap = json.loads(row.snapshot_json or "{}")
    except json.JSONDecodeError:
        snap = {}
    snap_set = {
        (int(m["registry_id"]), str(m.get("expr") or ""))
        for m in (snap.get("members") or [])
        if m.get("registry_id") is not None
    }
    cur_set = {
        (int(m["registry_id"]), str(m.get("expr") or ""))
        for m in current_workset
        if m.get("registry_id") is not None
    }
    if snap_set != cur_set:
        return True, "工作集已变更，结论已过期，请重跑"
    if (row.rules_version or "") != ACCEPT_RULES_VERSION:
        return True, "与当前区间/口径不一致，请重跑"
    if request_range:
        rs = request_range.get("start")
        re = request_range.get("end")
        radj = request_range.get("adj")
        if rs and str(rs) != str(row.start):
            return True, "与当前区间/口径不一致，请重跑"
        if re and str(re) != str(row.end):
            return True, "与当前区间/口径不一致，请重跑"
        if radj and str(radj) != str(row.adj):
            return True, "与当前区间/口径不一致，请重跑"
    return False, None


def aggregate_collection(n: int, member_results: Iterable[Any]) -> str:
    """集合状态机（产品 §4.4 一字不改）：先拒绝，再无可跑→无法判定，再 n<3→未齐，否则通过。"""
    verdicts = []
    for r in member_results:
        if isinstance(r, dict):
            verdicts.append(r.get("verdict"))
        else:
            verdicts.append(getattr(r, "verdict", None))
    if any(v == VERDICT_REJECT for v in verdicts):
        return COL_REJECT
    runnable = [v for v in verdicts if v in RUNNABLE]
    if n <= 0 or not runnable:
        return COL_UNKNOWN
    if n < 3:
        return COL_SHORT
    # 可跑者至此全是「通过」（拒绝已先行），n≥3
    return COL_PASS


def _parse_dates(start: Optional[str], end: Optional[str]) -> tuple[str, str]:
    s = (start or ACCEPT_DEFAULT_START).strip()
    e = (end or ACCEPT_DEFAULT_END).strip()
    try:
        sd = date.fromisoformat(s)
        ed = date.fromisoformat(e)
    except ValueError as exc:
        raise AcceptError(400, "invalid_range", f"日期格式错误: {exc}") from exc
    if sd > ed:
        raise AcceptError(400, "invalid_range", "开始日期晚于结束日期")
    return s, e


def _approx(a: Any, b: Any, tol: float = 1e-12) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


def check_forbidden_params(payload: Any) -> None:
    """显式改写死规则或 adj≠qfq → 400 rules_param_forbidden。"""
    if payload is None:
        return
    if isinstance(payload, BaseModel):
        data = payload.model_dump()
        extra = dict(getattr(payload, "model_extra", None) or {})
        data.update(extra)
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        return
    # 去掉允许字段
    for k in ("start", "end", "initial_cash"):
        data.pop(k, None)

    if "adj" in data and data["adj"] is not None and str(data["adj"]).strip() != ACCEPT_ADJ:
        raise AcceptError(
            400, "rules_param_forbidden",
            f"验收口径写死 adj={ACCEPT_ADJ}，不能改为 {data['adj']}",
        )
    data.pop("adj", None)

    for k in _FORBIDDEN_IF_PRESENT:
        if k in data and data[k] is not None:
            raise AcceptError(
                400, "rules_param_forbidden",
                f"不能指定 {k}：验收必须对当前工作集全集快照按写死规则进行",
            )
    for k in _FROZEN_FALSE:
        if k in data and data[k] is not None and data[k] is not False and data[k] != 0:
            raise AcceptError(
                400, "rules_param_forbidden",
                f"不能开启 {k}：本切片无空头、无行业/市值中性",
            )
    if "mode" in data and data["mode"] is not None:
        mode = str(data["mode"]).strip()
        if mode not in ("walk_forward", "multipliers"):
            raise AcceptError(400, "rules_param_forbidden", f"不能改 mode={mode}")
    if "multipliers" in data and data["multipliers"] is not None:
        got = [float(x) for x in data["multipliers"]]
        if got != [1.0, 2.0]:
            raise AcceptError(400, "rules_param_forbidden", "成本倍数写死为 ×1/×2")
    if "metric" in data and data["metric"] is not None:
        if str(data["metric"]).strip() not in ("sharpe", "excess_return"):
            raise AcceptError(400, "rules_param_forbidden", f"不能改 metric={data['metric']}")
    for k, frozen in _FROZEN_EQUAL.items():
        if k in data and data[k] is not None:
            val = data[k]
            if isinstance(frozen, float) or isinstance(val, float):
                ok = _approx(val, frozen)
            else:
                ok = val == frozen or (isinstance(frozen, int) and _approx(val, frozen))
            if not ok:
                raise AcceptError(
                    400, "rules_param_forbidden",
                    f"不能改 {k}={val}（写死为 {frozen}）",
                )


def rules_echo() -> dict[str, Any]:
    return {
        "universe": "中证800",
        "index_symbol": ACCEPT_INDEX_SYMBOL,
        "top_n": ACCEPT_TOP_N,
        "rebalance_period": ACCEPT_REBALANCE_PERIOD,
        "max_weight": ACCEPT_MAX_WEIGHT,
        "short": False,
        "neutralize_industry": False,
        "neutralize_marketcap": False,
        "cost": {
            "commission": COMMISSION,
            "slippage": SLIPPAGE,
            "multipliers": [1.0, 2.0],
        },
        "walk_forward": {
            "mode": "walk_forward",
            "n_windows": 3,
            "oos_ratio": 0.3,
            "scheme": "expanding",
            "metric": "sharpe",
        },
    }


def judge_member(
    *,
    unevaluable_reason: Optional[str] = None,
    wf_final: Optional[str] = None,
    wf_insufficient_reason: Optional[str] = None,
    fragile: Optional[bool] = None,
    double_computed: Optional[bool] = None,
    cost_note: Optional[str] = None,
) -> dict[str, Any]:
    """成员四态，判定顺序：无法验收 → 样本不足 → 拒绝 → 通过。"""
    reject_reasons: list[str] = []
    if unevaluable_reason:
        return {
            "verdict": VERDICT_UNEVAL,
            "reject_reasons": [],
            "reason": unevaluable_reason,
        }
    if wf_final == "未启用":
        return {
            "verdict": VERDICT_UNEVAL,
            "reject_reasons": [],
            "reason": "WF final 为「未启用」（配置错误），无法按回测口径验收",
        }
    if wf_final == "通过" and double_computed is False:
        return {
            "verdict": VERDICT_UNEVAL,
            "reject_reasons": [],
            "reason": "未能计算成本×2 超额，无法确认非 fragile，不得标通过",
        }
    if wf_final == VERDICT_INSUFFICIENT or wf_insufficient_reason:
        return {
            "verdict": VERDICT_INSUFFICIENT,
            "reject_reasons": [],
            "reason": wf_insufficient_reason or "滚动 OOS 样本不足",
        }
    if wf_final == "过拟合":
        reject_reasons.append("wf_overfit")
    elif wf_final == "未通过":
        reject_reasons.append("wf_fail")
    if fragile:
        reject_reasons.append("fragile")
    if reject_reasons:
        return {
            "verdict": VERDICT_REJECT,
            "reject_reasons": reject_reasons,
            "reason": _reject_reason_text(reject_reasons, cost_note),
        }
    if wf_final == "通过" and fragile is False and double_computed is True:
        return {"verdict": VERDICT_PASS, "reject_reasons": [], "reason": None}
    # 兜底：信息不齐当无法验收，不得标通过
    return {
        "verdict": VERDICT_UNEVAL,
        "reject_reasons": [],
        "reason": "验收信息不足，无法按回测口径验收",
    }


def _reject_reason_text(reasons: list[str], cost_note: Optional[str]) -> str:
    labels = {
        "wf_overfit": "过拟合",
        "wf_fail": "未通过",
        "fragile": "成本翻倍后脆弱",
    }
    parts = [labels.get(r, r) for r in reasons]
    text = "、".join(parts)
    if "fragile" in reasons and cost_note:
        return f"{text}（{cost_note}）"
    return text


def _cost_payload(result: Any) -> dict[str, Any]:
    if result is None:
        return {"fragile": None, "default_excess": None, "double_excess": None, "note": None}
    summary = getattr(result, "summary", None)
    cells = list(getattr(result, "cells", None) or [])
    default_excess = None
    double_excess = None
    for c in cells:
        m = getattr(c, "multiplier", None)
        if getattr(c, "is_default", False) or (m is not None and _approx(m, 1.0)):
            default_excess = getattr(c, "excess_return", None)
        if m is not None and _approx(m, 2.0):
            double_excess = getattr(c, "excess_return", None)
    if summary is not None and double_excess is None:
        # build_summary 可能有加倍格但不在 cells（失败则为 None）
        pass
    note = getattr(summary, "note", None) if summary is not None else None
    fragile = bool(getattr(summary, "fragile", False)) if summary is not None else None
    double_computed = double_excess is not None and (
        note is None or "未能计算" not in str(note)
    )
    if double_excess is None:
        double_computed = False
    return {
        "fragile": fragile,
        "default_excess": default_excess,
        "double_excess": double_excess,
        "note": note,
        "double_computed": double_computed,
    }


def _wf_payload(result: Any) -> dict[str, Any]:
    if result is None:
        return {
            "final": None, "agg": None, "legacy": None,
            "windows": [], "fragile_regime": None,
        }
    summary = getattr(result, "summary", None)
    windows = []
    for w in getattr(result, "windows", None) or []:
        if hasattr(w, "model_dump"):
            windows.append(w.model_dump(by_alias=True))
        elif isinstance(w, dict):
            windows.append(w)
    return {
        "final": getattr(summary, "final_verdict", None) if summary else None,
        "agg": getattr(summary, "agg_verdict", None) if summary else None,
        "legacy": getattr(summary, "legacy_verdict", None) if summary else None,
        "windows": windows,
        "fragile_regime": getattr(summary, "fragile_regime", None) if summary else None,
    }


def _default_portfolio_runner(db: Session, req: BacktestRequest, meta: dict, params: dict):
    from app.routers.strategy import _run_portfolio
    return _run_portfolio(db, req, meta, params)


def _load_wf_bar_dates(db: Session, start: str, end: str) -> list[str]:
    """切窗日历必须来自 sh000906，不得沿用页面 sh600519。"""
    from app.routers.strategy import _load_index_bars
    bars = _load_index_bars(
        db, ACCEPT_INDEX_SYMBOL,
        date.fromisoformat(start), date.fromisoformat(end), ACCEPT_ADJ,
    ) or []
    out: list[str] = []
    for b in bars:
        d = b["date"] if isinstance(b, dict) else getattr(b, "date", None)
        if d is None:
            continue
        out.append(d if isinstance(d, str) else d.isoformat())
    return out


def run_member(
    db: Session,
    member: dict[str, Any],
    start: str,
    end: str,
    *,
    adj: str = ACCEPT_ADJ,
    initial_cash: float = ACCEPT_DEFAULT_CASH,
    portfolio_runner: Optional[Callable] = None,
    bar_dates: Optional[list[str]] = None,
    load_bar_dates: Optional[Callable] = None,
) -> dict[str, Any]:
    """一人：默认成本 ×1/×2 + 默认 3 窗 WF，映射成员四态。一次只含一个 expr。"""
    expr = member.get("expr") or ""
    registry_id = member.get("registry_id")
    name = member.get("name")
    meta, params = build_member_meta(expr)
    assert meta["cls"] is WorksetMemberLongTopN
    assert params["expr"] == expr

    n_held = None
    n_valid = None
    uneval = None
    cost_result = None
    wf_result = None
    wf_insufficient = None
    run_pf = portfolio_runner or (
        lambda req, m=meta, p=params: _default_portfolio_runner(db, req, m, p)
    )

    def _bt(commission: float, slippage: float, s: str = start, e: str = end) -> Any:
        nonlocal n_held, n_valid
        req = BacktestRequest(
            symbol=ACCEPT_INDEX_SYMBOL,
            start=s,
            end=e,
            strategy="_workset_member_long_topn",
            params=params,
            initial_cash=initial_cash,
            commission=commission,
            slippage=slippage,
            adj=adj,
        )
        reset_last_run_stats()
        raw = run_pf(req)
        stats = peek_last_run_stats()
        nv = getattr(raw, "n_valid", None)
        nh = getattr(raw, "n_held", None)
        if nv is None:
            nv = stats.get("n_valid_max")
        if nh is None:
            nh = stats.get("n_held_max")
        if nv is not None:
            n_valid = int(nv) if n_valid is None else max(int(n_valid), int(nv))
        if nh is not None:
            n_held = int(nh) if n_held is None else max(int(n_held), int(nh))
        return raw

    # —— 成本（先跑；×2 失败不单独跳过 WF，除非已能判定无法验收）——
    cost_req = CostSensitivityRequest(
        symbol=ACCEPT_INDEX_SYMBOL,
        start=start,
        end=end,
        strategy="_workset_member_long_topn",
        params=params,
        initial_cash=initial_cash,
        adj=adj,
        mode="multipliers",
        multipliers=[1.0, 2.0],
        base_commission=COMMISSION,
        base_slippage=SLIPPAGE,
        metric="excess_return",
    )
    try:
        cost_result = run_cost_sensitivity(cost_req, lambda c, s: _bt(c, s))
    except HTTPException as exc:
        uneval = f"组合回测装配失败: {_detail(exc)}"
    except Exception as exc:  # noqa: BLE001
        uneval = f"成本敏感计算失败: {exc}"

    if uneval is None and (n_valid is not None and int(n_valid) <= 0):
        uneval = "全程无有效因子值，无法按下单规则验收"
        # 0 成交且无有效值：不得顶替净值，不再跑 WF
    if uneval is None and cost_result is None:
        uneval = "成本敏感无结果，无法按回测口径验收"

    cost = _cost_payload(cost_result)

    skip_wf = uneval is not None
    if not skip_wf:
        dates = bar_dates
        if dates is None:
            loader = load_bar_dates or (lambda: _load_wf_bar_dates(db, start, end))
            try:
                dates = loader()
            except HTTPException as exc:
                wf_insufficient = f"切窗失败: {_detail(exc)}"
                dates = []
            except Exception as exc:  # noqa: BLE001
                wf_insufficient = f"切窗失败: {exc}"
                dates = []
        wf_req = WalkForwardRequest(
            symbol=ACCEPT_INDEX_SYMBOL,
            start=start,
            end=end,
            strategy="_workset_member_long_topn",
            params=params,
            initial_cash=initial_cash,
            commission=None,  # 服务内用 COMMISSION/SLIPPAGE
            slippage=None,
            adj=adj,
            mode="walk_forward",
            n_windows=3,
            oos_ratio=0.3,
            scheme="expanding",
            metric="sharpe",
            min_is_bars=60,
            min_oos_bars=20,
        )
        if wf_insufficient is None:
            try:
                wf_result = run_walk_forward(
                    wf_req, dates or [],
                    lambda s, e: _bt(COMMISSION, SLIPPAGE, s, e),
                )
            except HTTPException as exc:
                wf_insufficient = f"切窗失败或样本不足: {_detail(exc)}"
            except Exception as exc:  # noqa: BLE001
                # 不让整次验收 500，落到该成员
                uneval = uneval or f"滚动 OOS 执行失败: {exc}"

    wf = _wf_payload(wf_result)
    judged = judge_member(
        unevaluable_reason=uneval,
        wf_final=wf.get("final"),
        wf_insufficient_reason=wf_insufficient,
        fragile=cost.get("fragile"),
        double_computed=cost.get("double_computed"),
        cost_note=cost.get("note"),
    )
    return {
        "registry_id": registry_id,
        "name": name,
        "expr": expr,
        "verdict": judged["verdict"],
        "reject_reasons": judged["reject_reasons"],
        "reason": judged["reason"],
        "n_held": n_held,
        "n_valid": n_valid,
        "wf": wf,
        "cost": {k: cost[k] for k in ("fragile", "default_excess", "double_excess", "note")},
        "meta_cls": meta["cls"].__name__,
    }


def _detail(exc: HTTPException) -> str:
    d = exc.detail
    if isinstance(d, dict):
        return str(d.get("message") or d)
    return str(d)


def workset_available(db: Session) -> bool:
    try:
        from app.services import factor_workset as fw  # noqa: F401
        db.execute(select(FactorWorkset.id).limit(1))
        return True
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def _list_members(db: Session) -> dict[str, Any]:
    from app.services.factor_workset import list_workset
    return list_workset(db)


def _live_workset_public(ws: dict[str, Any]) -> dict[str, Any]:
    members = []
    for m in ws.get("members") or []:
        members.append({
            "registry_id": m.get("registry_id"),
            "name": m.get("name"),
            "expr": m.get("expr"),
            "expr_excerpt": m.get("expr_excerpt"),
            "rating": m.get("rating"),
            "t_stat": m.get("t_stat"),
            "added_at": _iso(m.get("added_at")),
        })
    return {
        "n": ws.get("n", len(members)),
        "cap": ws.get("cap", 5),
        "target_min": ws.get("target_min", 3),
        "target_hint": ws.get("target_hint"),
        "members": members,
    }


def start_accept(
    db: Session,
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    adj: Optional[str] = None,
    initial_cash: Optional[float] = None,
    payload: Any = None,
    sync: bool = False,
    run_member_fn: Optional[Callable] = None,
    submit: Optional[Callable] = None,
    session_factory: Optional[Callable] = None,
) -> dict[str, Any]:
    """空集拒绝；否则拍照建行并启动任务（默认异步）。"""
    if payload is not None:
        check_forbidden_params(payload)
        if isinstance(payload, BaseModel):
            if start is None:
                start = payload.start
            if end is None:
                end = payload.end
            if adj is None:
                adj = payload.adj
            if initial_cash is None:
                initial_cash = payload.initial_cash
        elif isinstance(payload, dict):
            start = start if start is not None else payload.get("start")
            end = end if end is not None else payload.get("end")
            adj = adj if adj is not None else payload.get("adj")
            initial_cash = initial_cash if initial_cash is not None else payload.get("initial_cash")

    if adj is None:
        adj = ACCEPT_ADJ
    if str(adj).strip() != ACCEPT_ADJ:
        raise AcceptError(400, "rules_param_forbidden", f"验收口径写死 adj={ACCEPT_ADJ}")
    s, e = _parse_dates(start, end)
    cash = float(initial_cash) if initial_cash is not None else ACCEPT_DEFAULT_CASH

    if not workset_available(db):
        raise AcceptError(400, "workset_unavailable", "工作集服务不可用（需要 W48-1）")

    running = db.execute(
        select(FactorWorksetAcceptRun).where(
            FactorWorksetAcceptRun.status.in_(("pending", "running"))
        ).order_by(FactorWorksetAcceptRun.id.desc())
    ).scalars().first()
    if running is not None:
        raise AcceptError(
            409, "job_running",
            "已有进行中的验收任务，请等待当前任务结束",
            job_id=running.job_id,
            run_id=running.id,
        )

    ws = _list_members(db)
    members = list(ws.get("members") or [])
    n = int(ws.get("n") or len(members))
    if n == 0 or not members:
        raise AcceptError(400, "empty_workset", "工作集为空，请先到因子实验室申请入集")

    snap = snapshot_fingerprint(members)
    now = datetime.utcnow()
    row = FactorWorksetAcceptRun(
        created_at=now,
        finished_at=None,
        status="running",
        job_id=None,
        start=s,
        end=e,
        adj=ACCEPT_ADJ,
        initial_cash=cash,
        rules_version=ACCEPT_RULES_VERSION,
        index_code=ACCEPT_INDEX_CODE,
        index_symbol=ACCEPT_INDEX_SYMBOL,
        top_n=ACCEPT_TOP_N,
        rebalance_period=ACCEPT_REBALANCE_PERIOD,
        max_weight=ACCEPT_MAX_WEIGHT,
        cost_commission=COMMISSION,
        cost_slippage=SLIPPAGE,
        wf_mode="walk_forward",
        wf_n_windows=3,
        wf_oos_ratio=0.3,
        wf_scheme="expanding",
        wf_metric="sharpe",
        snapshot_json=json.dumps(snap, ensure_ascii=False),
        members_json=None,
        collection_verdict=None,
        error_message=None,
        note=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    run_id = row.id

    if sync:
        _run_accept_job(db, run_id, run_member_fn=run_member_fn)
        db.refresh(row)
        return _run_public(row, current_members=members, request_range={"start": s, "end": e, "adj": adj}, include_ok=True)

    from app.core import task_queue

    def _job(_task_id: str = "") -> int:
        factory = session_factory
        if factory is None:
            from app.database import SessionLocal
            factory = SessionLocal
        sdb = factory()
        try:
            r = sdb.get(FactorWorksetAcceptRun, run_id)
            if r is not None and _task_id:
                r.job_id = _task_id
                sdb.commit()
            _run_accept_job(sdb, run_id, _task_id=_task_id, run_member_fn=run_member_fn)
            return run_id
        finally:
            sdb.close()

    submit_fn = submit or task_queue.submit
    job_id = submit_fn("workset-accept", _job)
    row.job_id = job_id
    db.commit()
    return {
        "ok": True,
        "job_id": job_id,
        "status": "running",
        "run_id": run_id,
        "snapshot": snap,
        "start": s,
        "end": e,
        "rules_version": ACCEPT_RULES_VERSION,
    }


def _run_accept_job(
    db: Session,
    run_id: int,
    *,
    _task_id: str = "",
    run_member_fn: Optional[Callable] = None,
) -> None:
    row = db.get(FactorWorksetAcceptRun, run_id)
    if row is None:
        return
    from app.core import task_queue
    try:
        _run_accept_job_body(db, row, _task_id=_task_id, run_member_fn=run_member_fn)
    except Exception as exc:  # noqa: BLE001
        logger.exception("workset accept job failed run_id=%s", run_id)
        try:
            row.status = "error"
            row.error_message = str(exc)
            row.finished_at = datetime.utcnow()
            db.commit()
        except Exception:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:
                pass
        raise


def _run_accept_job_body(
    db: Session,
    row: FactorWorksetAcceptRun,
    *,
    _task_id: str = "",
    run_member_fn: Optional[Callable] = None,
) -> None:
    try:
        snap = json.loads(row.snapshot_json or "{}")
    except json.JSONDecodeError:
        snap = {"n": 0, "members": []}
    members = list(snap.get("members") or [])
    results: list[dict[str, Any]] = []
    fn = run_member_fn or run_member
    total = len(members)
    from app.core import task_queue
    for i, m in enumerate(members):
        if _task_id:
            try:
                task_queue.update_progress(
                    _task_id, (i / max(total, 1)),
                    f"验收中（{i}/{total}）… 中证800 组合回测可能需要数分钟",
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            if fn is run_member:
                one = run_member(
                    db, m, row.start, row.end,
                    adj=row.adj, initial_cash=row.initial_cash,
                )
            else:
                # 测试假 runner：签名 (db, member, start, end) 或只收 member
                try:
                    one = fn(db, m, row.start, row.end)
                except TypeError:
                    one = fn(m)
        except Exception as exc:  # noqa: BLE001
            logger.exception("workset accept member failed: %s", m.get("registry_id"))
            one = {
                "registry_id": m.get("registry_id"),
                "name": m.get("name"),
                "expr": m.get("expr"),
                "verdict": VERDICT_UNEVAL,
                "reject_reasons": [],
                "reason": f"成员执行异常: {exc}",
                "n_held": None,
                "n_valid": None,
                "wf": _wf_payload(None),
                "cost": {"fragile": None, "default_excess": None, "double_excess": None, "note": None},
            }
        results.append(one)
        row.members_json = json.dumps(results, ensure_ascii=False, default=str)
        db.commit()

    n = int(snap.get("n") or len(members))
    collection = aggregate_collection(n, results)
    row.members_json = json.dumps(results, ensure_ascii=False, default=str)
    row.collection_verdict = collection
    row.status = "done"
    row.finished_at = datetime.utcnow()
    row.error_message = None
    db.commit()
    if _task_id:
        try:
            task_queue.update_progress(_task_id, 1.0, "验收完成")
        except Exception:  # noqa: BLE001
            pass


def get_latest_accept(
    db: Session,
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    job_id: Optional[str] = None,
) -> dict[str, Any]:
    available = workset_available(db)
    live: dict[str, Any] | None = None
    current_members: list[dict[str, Any]] = []
    if available:
        try:
            ws = _list_members(db)
            live = _live_workset_public(ws)
            current_members = list(ws.get("members") or [])
        except Exception:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            available = False

    row = None
    if job_id:
        row = db.execute(
            select(FactorWorksetAcceptRun).where(FactorWorksetAcceptRun.job_id == job_id)
        ).scalars().first()
    if row is None:
        running = db.execute(
            select(FactorWorksetAcceptRun).where(
                FactorWorksetAcceptRun.status.in_(("pending", "running"))
            ).order_by(FactorWorksetAcceptRun.id.desc())
        ).scalars().first()
        if running is not None:
            row = running
        else:
            row = db.execute(
                select(FactorWorksetAcceptRun).order_by(FactorWorksetAcceptRun.id.desc())
            ).scalars().first()

    rng = {}
    if start:
        rng["start"] = start
    if end:
        rng["end"] = end
    out: dict[str, Any] = {
        "ok": True,
        "workset_available": available,
        "workset": live,
        "run": None,
    }
    if row is None:
        return out
    out["run"] = _run_public(row, current_members=current_members, request_range=rng or None)
    return out


def _run_public(
    row: FactorWorksetAcceptRun,
    *,
    current_members: list[dict[str, Any]],
    request_range: Optional[dict[str, Any]] = None,
    include_ok: bool = False,
) -> dict[str, Any]:
    stale, stale_reason = is_stale(row, current_members, request_range)
    try:
        snap = json.loads(row.snapshot_json or "{}")
    except json.JSONDecodeError:
        snap = {"n": 0, "members": []}
    try:
        members = json.loads(row.members_json) if row.members_json else []
    except json.JSONDecodeError:
        members = []
    collection = None
    if row.collection_verdict:
        n = int(snap.get("n") or 0)
        counts = {
            "n": n,
            "n_pass": sum(1 for m in members if m.get("verdict") == VERDICT_PASS),
            "n_reject": sum(1 for m in members if m.get("verdict") == VERDICT_REJECT),
            "n_insufficient": sum(1 for m in members if m.get("verdict") == VERDICT_INSUFFICIENT),
            "n_unevaluable": sum(1 for m in members if m.get("verdict") == VERDICT_UNEVAL),
            "verdict": row.collection_verdict,
        }
        collection = counts
    members_done = len(members)
    members_total = int(snap.get("n") or 0)
    payload: dict[str, Any] = {
        "run_id": row.id,
        "job_id": row.job_id,
        "status": row.status,
        "stale": bool(stale),
        "stale_reason": stale_reason,
        "start": row.start,
        "end": row.end,
        "adj": row.adj,
        "rules_version": row.rules_version,
        "rules": rules_echo(),
        "snapshot": snap,
        "collection": collection,
        "members": members,
        "error_message": row.error_message,
        "created_at": _iso(row.created_at),
        "finished_at": _iso(row.finished_at),
    }
    if row.status in ("pending", "running"):
        payload["members_done"] = members_done
        payload["members_total"] = members_total
        payload["message"] = (
            f"验收中（{members_done}/{members_total}）… 中证800 组合回测可能需要数分钟"
        )
    if include_ok:
        payload["ok"] = True
        payload["job_id"] = row.job_id
        payload["run_id"] = row.id
    return payload


def http_error(err: AcceptError) -> HTTPException:
    return HTTPException(status_code=err.status, detail=err.to_detail())
