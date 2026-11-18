"""W48-3 工作集短名单模拟盘观察：同口径逐本建盘。

读未过期验收快照中 verdict=通过 的成员，按与验收相同的交易规则和默认成本
各建（或打开）一本 PaperTask。复用 W48-2 build_member_meta / is_stale /
get_latest_accept（原样调用，不改过期条件与集合状态机）。

不把薄壳写入 STRATEGY_REGISTRY，不用 enhanced_factor 等顶替，不 bump
n_tested，不改 FactorRegistry.enabled，不合成组合簿，不把完整 expr 只写入
params_json String(1024)。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.cost_defaults import COMMISSION, SLIPPAGE
from app.models import FactorWorksetAcceptRun, PaperTask
from app.services.workset_backtest_accept import (
    ACCEPT_ADJ,
    ACCEPT_DEFAULT_CASH,
    ACCEPT_INDEX_CODE,
    ACCEPT_INDEX_SYMBOL,
    ACCEPT_MAX_WEIGHT,
    ACCEPT_REBALANCE_PERIOD,
    ACCEPT_RULES_VERSION,
    ACCEPT_TOP_N,
    VERDICT_PASS,
    is_stale,
    workset_available,
)

logger = logging.getLogger("quant.workset_observe")

WORKSET_OBSERVE_KEY = "_workset_member_long_topn"
WORKSET_OBSERVE_LABEL = "工作集观察"
STALE_REASON = "快照已过期"
INDEX_NO_BARS_MSG = f"{ACCEPT_INDEX_CODE} 还没有任何指数行情，先跑一次 ETL"

# 显式传入即冲突（成员子集 / 内部 key / 完整 expr / 任意起始日）
_FORBIDDEN_IF_PRESENT = frozenset({
    "strategy", "strategy_key",
    "start_date", "end_date", "end",
    "expr", "params_json",
    "members", "registry_ids", "member_ids", "registry_id",
})
_FROZEN_EQUAL = {
    "top_n": ACCEPT_TOP_N,
    "rebalance_period": ACCEPT_REBALANCE_PERIOD,
    "max_weight": ACCEPT_MAX_WEIGHT,
    "max_position_pct": ACCEPT_MAX_WEIGHT,
    "index_code": ACCEPT_INDEX_CODE,
    "index_symbol": ACCEPT_INDEX_SYMBOL,
    "commission": COMMISSION,
    "slippage": SLIPPAGE,
}
_FROZEN_FALSE = frozenset({
    "neutralize_industry", "neutralize_marketcap", "short", "neutralize",
})


class ObserveError(Exception):
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


class WorksetObserveRequest(BaseModel):
    """POST 体：只收 run_id；写死规则 / 成本 / 起始日不可改。未知字段忽略。"""

    model_config = ConfigDict(extra="allow")

    run_id: Optional[int] = None


def http_error(err: ObserveError) -> HTTPException:
    return HTTPException(status_code=err.status, detail=err.to_detail())


def migrate_paper_workset_observe(conn) -> None:
    """SQLite 加法迁移：观察绑定列 + 幂等唯一索引。由 _migrate_sqlite 在验收迁移之后调用。"""
    tables = {r[0] for r in conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ))}
    if "paper_tasks" not in tables:
        return
    cols = {r[1] for r in conn.execute(text("PRAGMA table_info(paper_tasks)"))}
    for col, ddl in (
        ("workset_observe_run_id", "INTEGER"),
        ("workset_observe_registry_id", "INTEGER"),
        ("workset_observe_expr", "TEXT"),
        ("workset_observe_rules_version", "TEXT"),
    ):
        if col not in cols:
            conn.execute(text(f"ALTER TABLE paper_tasks ADD COLUMN {col} {ddl}"))
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_workset_observe "
        "ON paper_tasks (workset_observe_run_id, workset_observe_registry_id) "
        "WHERE workset_observe_run_id IS NOT NULL "
        "AND workset_observe_registry_id IS NOT NULL"
    ))


def _approx(a: Any, b: Any, tol: float = 1e-12) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


def check_observe_forbidden_params(payload: Any) -> None:
    """显式改写死规则 / 非 qfq / 非默认成本 / 传入 start_date 等 → rules_param_forbidden。"""
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
    data.pop("run_id", None)

    if "adj" in data and data["adj"] is not None and str(data["adj"]).strip() != ACCEPT_ADJ:
        raise ObserveError(
            400, "rules_param_forbidden",
            f"观察口径写死 adj={ACCEPT_ADJ}，不能改为 {data['adj']}",
        )
    data.pop("adj", None)

    if "initial_cash" in data and data["initial_cash"] is not None:
        if not _approx(data["initial_cash"], ACCEPT_DEFAULT_CASH):
            raise ObserveError(
                400, "rules_param_forbidden",
                f"initial_cash 锁死为 {int(ACCEPT_DEFAULT_CASH)}，不能改为 {data['initial_cash']}",
            )
        data.pop("initial_cash", None)

    for k in _FORBIDDEN_IF_PRESENT:
        if k in data and data[k] is not None:
            raise ObserveError(
                400, "rules_param_forbidden",
                f"不能指定 {k}：观察通道只接受 run_id，规则与起始日从验收快照锁死",
            )
    for k in _FROZEN_FALSE:
        if k in data and data[k] is not None and data[k] is not False and data[k] != 0:
            raise ObserveError(
                400, "rules_param_forbidden",
                f"不能开启 {k}：本切片无空头、无行业/市值中性",
            )
    for k, frozen in _FROZEN_EQUAL.items():
        if k in data and data[k] is not None:
            val = data[k]
            if k == "index_code" and str(val).strip() in (ACCEPT_INDEX_CODE, ACCEPT_INDEX_SYMBOL):
                continue
            if isinstance(frozen, float) or isinstance(val, float):
                ok = _approx(val, frozen)
            else:
                ok = val == frozen or (isinstance(frozen, int) and _approx(val, frozen))
            if not ok:
                raise ObserveError(
                    400, "rules_param_forbidden",
                    f"不能改 {k}={val}（写死为 {frozen}）",
                )


def build_observe_task_name(member_name: str, run_id: int, registry_id: int) -> str:
    """产品公式：工作集观察 · {成员名} · run#{run_id}。超长截断成员名并保留 run_id / r{registry_id}。"""
    name = (member_name or "").strip() or f"#{registry_id}"
    prefix = "工作集观察 · "
    suffix = f" · run#{run_id}"
    full = f"{prefix}{name}{suffix}"
    if len(full) <= 64:
        return full
    tail = f" · run#{run_id} · r{registry_id}"
    budget = 64 - len(prefix) - len(tail)
    if budget < 1:
        compact_prefix = "工作集观察·"
        compact_tail = f"·run#{run_id}·r{registry_id}"
        budget = max(1, 64 - len(compact_prefix) - len(compact_tail))
        return (compact_prefix + name[:budget] + compact_tail)[:64]
    return f"{prefix}{name[:budget]}{tail}"


def _as_iso_date(v: Any) -> Optional[str]:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return str(v.isoformat())[:10]
    s = str(v).strip()
    return s[:10] if s else None


def _last_csi800_date(db: Session) -> Optional[str]:
    from app.routers.paper import _last_index_date
    return _as_iso_date(_last_index_date(db, ACCEPT_INDEX_CODE))


def _latest_completed_run(db: Session) -> Optional[FactorWorksetAcceptRun]:
    return db.execute(
        select(FactorWorksetAcceptRun)
        .where(FactorWorksetAcceptRun.status == "done")
        .order_by(FactorWorksetAcceptRun.id.desc())
    ).scalars().first()


def _current_workset_members(db: Session) -> list[dict[str, Any]]:
    if not workset_available(db):
        return []
    try:
        from app.services.factor_workset import list_workset
        ws = list_workset(db)
        return list(ws.get("members") or [])
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return []


def _collection_from_run(row: FactorWorksetAcceptRun, members: list[dict[str, Any]]) -> dict[str, Any]:
    from app.services.workset_backtest_accept import (
        VERDICT_INSUFFICIENT, VERDICT_REJECT, VERDICT_UNEVAL,
    )
    try:
        snap = json.loads(row.snapshot_json or "{}")
    except json.JSONDecodeError:
        snap = {}
    n = int(snap.get("n") or len(members) or 0)
    return {
        "verdict": row.collection_verdict or "",
        "n": n,
        "n_pass": sum(1 for m in members if m.get("verdict") == VERDICT_PASS),
        "n_reject": sum(1 for m in members if m.get("verdict") == VERDICT_REJECT),
        "n_insufficient": sum(1 for m in members if m.get("verdict") == VERDICT_INSUFFICIENT),
        "n_unevaluable": sum(1 for m in members if m.get("verdict") == VERDICT_UNEVAL),
    }


def _run_members_and_snap(row: FactorWorksetAcceptRun) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        snap = json.loads(row.snapshot_json or "{}")
    except json.JSONDecodeError:
        snap = {"n": 0, "members": []}
    try:
        members = json.loads(row.members_json) if row.members_json else []
    except json.JSONDecodeError:
        members = []
    return members, snap


def _member_expr_and_name(member: dict[str, Any], snap: dict[str, Any]) -> tuple[str, str, int]:
    rid = int(member["registry_id"])
    snap_by_id = {
        int(m["registry_id"]): m
        for m in (snap.get("members") or [])
        if m.get("registry_id") is not None
    }
    sm = snap_by_id.get(rid) or {}
    expr = member.get("expr")
    if expr is None or expr == "":
        expr = sm.get("expr") or ""
    name = (member.get("name") or sm.get("name") or f"#{rid}")
    return str(expr), str(name), rid


def _validate_locked_rules(row: FactorWorksetAcceptRun) -> None:
    if (row.adj or ACCEPT_ADJ) != ACCEPT_ADJ:
        raise ObserveError(
            400, "rules_param_forbidden",
            f"该次验收 adj={row.adj}，观察只接受 {ACCEPT_ADJ}",
        )
    if not _approx(row.cost_commission, COMMISSION) or not _approx(row.cost_slippage, SLIPPAGE):
        raise ObserveError(
            400, "rules_param_forbidden",
            "该次验收成本不是默认万 2.5+万 1，不能按同口径观察",
        )
    if (row.index_code or ACCEPT_INDEX_CODE) != ACCEPT_INDEX_CODE:
        raise ObserveError(
            400, "rules_param_forbidden",
            f"该次验收指数 {row.index_code} 与写死中证800 不一致",
        )


def resolve_observe_run(db: Session, run_id: Optional[int] = None) -> FactorWorksetAcceptRun:
    """只消费最新一次已完成验收；指定 run_id 必须就是那次。不回退更早 run。"""
    try:
        latest = _latest_completed_run(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("workset accept table unavailable: %s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise ObserveError(
            400, "accept_unavailable",
            "工作集验收服务不可用（需要 W48-2 迁移）",
        ) from exc

    if latest is None:
        # 可能有未完成/失败的记录，一并视为未就绪
        any_row = db.execute(
            select(FactorWorksetAcceptRun).order_by(FactorWorksetAcceptRun.id.desc())
        ).scalars().first()
        if any_row is None:
            raise ObserveError(400, "accept_not_ready", "尚无已完成的工作集验收，请先到验收块跑一次")
        if any_row.status == "error":
            raise ObserveError(400, "accept_not_ready", "验收未完成或失败，无法建盘")
        raise ObserveError(400, "accept_not_ready", "验收尚未完成，无法建盘")

    if run_id is not None:
        row = db.get(FactorWorksetAcceptRun, int(run_id))
        if row is None:
            raise ObserveError(400, "accept_not_ready", f"验收 run#{run_id} 不存在")
        if row.id != latest.id:
            # 不回退更早一次；即使更早「看起来未过期」也不建
            raise ObserveError(400, "snapshot_stale", "结论已过期，请先重跑")
        if row.status != "done":
            if row.status == "error":
                raise ObserveError(400, "accept_not_ready", "验收未完成或失败，无法建盘")
            raise ObserveError(400, "accept_not_ready", "验收尚未完成，无法建盘")
        return row
    return latest


def compute_task_stale(db: Session, task: PaperTask) -> tuple[bool, Optional[str]]:
    """读时过期：绑定 run 的 is_stale 为真，或已不是最新一次已完成。"""
    run_id = getattr(task, "workset_observe_run_id", None)
    if run_id is None:
        return False, None
    row = db.get(FactorWorksetAcceptRun, int(run_id))
    if row is None:
        return True, STALE_REASON
    current = _current_workset_members(db)
    stale, _reason = is_stale(row, current)
    latest = _latest_completed_run(db)
    if stale or latest is None or latest.id != row.id:
        return True, STALE_REASON
    return False, None


def is_observe_task(task: Any) -> bool:
    return (
        getattr(task, "workset_observe_run_id", None) is not None
        and getattr(task, "workset_observe_registry_id", None) is not None
    )


def observe_public_fields(db: Session, task: PaperTask) -> dict[str, Any]:
    """列表/详情加法字段。既有非观察任务这些为 null / 缺省。"""
    run_id = getattr(task, "workset_observe_run_id", None)
    reg_id = getattr(task, "workset_observe_registry_id", None)
    if run_id is None and reg_id is None:
        return {
            "workset_observe_run_id": None,
            "workset_observe_registry_id": None,
            "workset_observe_stale": False,
            "workset_observe_stale_reason": None,
        }
    stale, reason = compute_task_stale(db, task)
    return {
        "workset_observe": True,
        "workset_observe_run_id": run_id,
        "workset_observe_registry_id": reg_id,
        "workset_observe_stale": stale,
        "workset_observe_stale_reason": reason,
        "strategy_label": WORKSET_OBSERVE_LABEL,
    }


def _find_existing(db: Session, run_id: int, registry_id: int) -> Optional[PaperTask]:
    return db.execute(
        select(PaperTask).where(
            PaperTask.workset_observe_run_id == run_id,
            PaperTask.workset_observe_registry_id == registry_id,
        )
    ).scalars().first()


def _short_params_json() -> str:
    """短规则回显，不含 expr；解析不得用它覆盖 TEXT 列。"""
    return json.dumps({
        "top_n": ACCEPT_TOP_N,
        "rebalance_period": ACCEPT_REBALANCE_PERIOD,
        "max_weight": ACCEPT_MAX_WEIGHT,
        "index_code": ACCEPT_INDEX_CODE,
    }, ensure_ascii=False)


def _create_one(
    db: Session,
    *,
    row: FactorWorksetAcceptRun,
    registry_id: int,
    name: str,
    expr: str,
    start_date: str,
) -> tuple[PaperTask, bool]:
    """写入 TEXT 列并回读比对。失败时删除该行，不截断开盘。返回 (task, created)。"""
    existing = _find_existing(db, row.id, registry_id)
    if existing is not None:
        return existing, False
    if expr is None or expr == "":
        raise ObserveError(400, "cannot_observe", "快照表达式为空，无法完整保存，未开盘")

    task = PaperTask(
        name=build_observe_task_name(name, row.id, registry_id),
        strategy_key=WORKSET_OBSERVE_KEY,
        kind="portfolio",
        symbols="",
        index_code=ACCEPT_INDEX_CODE,
        params_json=_short_params_json(),
        initial_cash=float(ACCEPT_DEFAULT_CASH),
        commission=COMMISSION,
        slippage=SLIPPAGE,
        adj=ACCEPT_ADJ,
        start_date=start_date,
        enabled=True,  # 仅纸面调度开关，不是 FactorRegistry.enabled
        workset_observe_run_id=row.id,
        workset_observe_registry_id=registry_id,
        workset_observe_expr=expr,
        workset_observe_rules_version=row.rules_version or ACCEPT_RULES_VERSION,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    fresh = db.get(PaperTask, task.id)
    stored = None if fresh is None else fresh.workset_observe_expr
    if stored != expr:
        if fresh is not None:
            db.delete(fresh)
            db.commit()
        raise ObserveError(
            400, "cannot_observe",
            "表达式回读与快照不一致，未截断开盘",
        )
    return fresh or task, True


def create_workset_observe(
    db: Session,
    *,
    run_id: Optional[int] = None,
    payload: Any = None,
) -> dict[str, Any]:
    """为未过期已完成快照中全部「通过」成员建盘或打开已有任务。同步写库，不跑引擎。"""
    if payload is not None:
        check_observe_forbidden_params(payload)
        if run_id is None:
            if isinstance(payload, BaseModel):
                run_id = payload.run_id
            elif isinstance(payload, dict) and payload.get("run_id") is not None:
                run_id = int(payload["run_id"])

    if not workset_available(db):
        raise ObserveError(
            400, "accept_unavailable",
            "工作集验收服务不可用（需要 W48-1 / W48-2）",
        )

    row = resolve_observe_run(db, run_id)
    _validate_locked_rules(row)

    current = _current_workset_members(db)
    stale, reason = is_stale(row, current)
    if stale:
        raise ObserveError(400, "snapshot_stale", "结论已过期，请先重跑")

    members, snap = _run_members_and_snap(row)
    passed = [m for m in members if m.get("verdict") == VERDICT_PASS]
    if not passed:
        raise ObserveError(400, "no_passed_members", "没有已通过的成员，无法建盘")

    start_date = _last_csi800_date(db)
    if not start_date:
        raise ObserveError(400, "index_no_bars", INDEX_NO_BARS_MSG)

    tasks_out: list[dict[str, Any]] = []
    created_ids: list[int] = []
    failures: list[dict[str, Any]] = []
    for m in passed:
        try:
            expr, mname, rid = _member_expr_and_name(m, snap)
            task, created = _create_one(
                db, row=row, registry_id=rid, name=mname, expr=expr, start_date=start_date,
            )
            item = {
                "registry_id": rid,
                "name": task.name,
                "task_id": task.id,
                "created": created,
                "existing": not created,
                "expr_complete": True,
            }
            tasks_out.append(item)
            if created:
                created_ids.append(task.id)
        except ObserveError as err:
            if err.error_code == "cannot_observe":
                rid = int(m.get("registry_id") or 0)
                failures.append({
                    "registry_id": rid,
                    "name": m.get("name") or f"#{rid}",
                    "task_id": None,
                    "created": False,
                    "existing": False,
                    "expr_complete": False,
                    "error": err.message,
                })
            else:
                raise

    if not tasks_out and failures:
        raise ObserveError(
            400, "cannot_observe",
            "无法完整保存表达式，未能按同口径建盘：" + "；".join(
                f"{f['name']}: {f.get('error')}" for f in failures
            ),
        )

    tasks_out.extend(failures)
    collection = _collection_from_run(row, members)
    return {
        "ok": True,
        "run_id": row.id,
        "stale": False,
        "collection": collection,
        "start_date": start_date,
        "rules_version": row.rules_version or ACCEPT_RULES_VERSION,
        "tasks": tasks_out,
        "created_task_ids": created_ids,
        "run_started": False,
    }


def get_workset_observe(db: Session, run_id: Optional[int] = None) -> dict[str, Any]:
    """只读：集合状态 + 本轮已绑任务，供 #paper 横幅。不建盘。"""
    # 只读路径：若指定 run 不存在/未完成，仍返回可读信息，不抛成建盘错误
    latest = None
    try:
        latest = _latest_completed_run(db)
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise ObserveError(400, "accept_unavailable", "工作集验收服务不可用（需要 W48-2 迁移）")

    row: Optional[FactorWorksetAcceptRun] = None
    if run_id is not None:
        row = db.get(FactorWorksetAcceptRun, int(run_id))
    if row is None:
        row = latest
    if row is None:
        raise ObserveError(400, "accept_not_ready", "尚无已完成的工作集验收，请先到验收块跑一次")

    current = _current_workset_members(db)
    stale, reason = is_stale(row, current)
    if latest is None or latest.id != row.id:
        stale, reason = True, STALE_REASON
    elif stale:
        reason = STALE_REASON
    members, _snap = _run_members_and_snap(row)
    collection = _collection_from_run(row, members)
    tasks = db.execute(
        select(PaperTask).where(PaperTask.workset_observe_run_id == row.id)
        .order_by(PaperTask.id)
    ).scalars().all()
    return {
        "ok": True,
        "run_id": row.id,
        "stale": bool(stale),
        "stale_reason": reason if stale else None,
        "collection": collection,
        "rules_version": row.rules_version or ACCEPT_RULES_VERSION,
        "status": row.status,
        "tasks": [
            {
                "registry_id": t.workset_observe_registry_id,
                "task_id": t.id,
                "name": t.name,
                "start_date": t.start_date,
                **{k: v for k, v in observe_public_fields(db, t).items()
                   if k in ("workset_observe_stale", "workset_observe_stale_reason", "strategy_label")},
            }
            for t in tasks
        ],
    }
