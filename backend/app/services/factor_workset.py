"""少而正交工作集（W48-1 / M3-5）。

容量硬顶 5；质量复用 factor_lab_gate；相关为最新截面成员两两 raw Spearman。
入集 / 预览 / 移出不 bump n_tested，不自动 enabled，不改正交算法与 luck_bar。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import FactorDaily, FactorRegistry, FactorWorkset
from app.services.factor_lab_gate import check_enable_gate, load_mine_report
from app.services.factor_mining import (
    CsContext,
    eval_raw_values_on_cs,
    load_latest_cs_context,
    pairwise_raw_spearman,
)

WORKSET_CAP = 5
WORKSET_TARGET_MIN = 3
WORKSET_CORR_ABS_MAX = 0.50
WORKSET_MIN_PAIRS = 20

WORKSET_NOTE = (
    "少而正交工作集：最多保留 3–5 个低相关因子。"
    "质量看残差评级（正交开启时），相关看 raw；不是风险平价，也不改变多重检验计数。"
)

DISABLED_REASON = "已拒绝（disabled），请先在生命周期改回 candidate 再申请"
CORR_UNDETERMINED_REASON = "相关无法判定，暂不入集"

# 进程内缓存：(asof, expr) → 截面 raw 值。admit/remove 时失效。
_eval_cache: dict[tuple[str, str], dict[str, float]] = {}


def invalidate_workset_cache() -> None:
    _eval_cache.clear()


def migrate_factor_workset(conn) -> None:
    """SQLite migrate：建薄表（由 database._migrate_sqlite 调用）。"""
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS factor_workset ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "registry_id INTEGER NOT NULL REFERENCES factor_registry(id), "
        "added_at DATETIME NOT NULL, "
        "note TEXT NOT NULL DEFAULT '', "
        "source_mine_id INTEGER, "
        "CONSTRAINT uq_fw_registry UNIQUE (registry_id)"
        ")"
    ))


class CapReachedError(Exception):
    """提交前 COUNT>5，应由路由映射为 HTTP 409。"""


# ---------------------------------------------------------------------------
# BEGIN IMMEDIATE / 写锁
# ---------------------------------------------------------------------------
def begin_immediate(db: Session) -> None:
    """写入路径升级为写锁。

    SQLAlchemy 2 在首条语句上会自动 ``BEGIN``（SQLite 为 DEFERRED）。
    对已开启的事务再发 ``BEGIN IMMEDIATE`` 会失败，因此用 0 行 UPDATE
    立刻拿到 RESERVED 锁，效果等同于写入路径上的 BEGIN IMMEDIATE。
    若会话尚未开启事务，则在驱动连接上先发 ``BEGIN IMMEDIATE``，
    再让 Session 认领该事务。
    """
    bind = db.get_bind()
    dialect = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect != "sqlite":
        if not db.in_transaction():
            db.begin()
        return

    if db.in_transaction():
        db.execute(text("UPDATE factor_workset SET id = id WHERE 0"))
        return

    # 尚未 autobegin：先在即将被 Session 使用的连接上 IMMEDIATE。
    # connection() 会 autobegin DEFERRED，所以改走 bind.connect 不可行（会拿到另一条连接）。
    # 这里主动 begin 后再立刻用 0 行写升级；同时尝试驱动级 IMMEDIATE（若已在事务中则忽略）。
    conn = db.connection()
    try:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
    except Exception:  # noqa: BLE001
        db.execute(text("UPDATE factor_workset SET id = id WHERE 0"))


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _latest_asof(db: Session):
    return db.execute(
        select(FactorDaily.trade_date).order_by(FactorDaily.trade_date.desc()).limit(1)
    ).scalar()


def _expr_excerpt(expr: str, limit: int = 48) -> str:
    text_e = (expr or "").strip()
    if len(text_e) <= limit:
        return text_e
    return text_e[: limit - 1] + "…"


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat(timespec="seconds")


def _t_of(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _strictly_greater(t_cand: float | None, t_mem: float | None) -> bool:
    return t_cand is not None and t_mem is not None and t_cand > t_mem


def _metrics(db: Session, reg: FactorRegistry) -> dict[str, Any]:
    report = load_mine_report(db, source_id=reg.source_id, expr=reg.expr)
    rating = None
    t_stat = None
    max_abs_corr = None
    if isinstance(report, dict):
        rating = report.get("rating")
        t_stat = _t_of(report.get("t_stat"))
        mac = report.get("max_abs_corr")
        try:
            max_abs_corr = float(mac) if mac is not None else None
        except (TypeError, ValueError):
            max_abs_corr = None
    return {
        "report": report,
        "rating": rating,
        "t_stat": t_stat,
        "max_abs_corr": max_abs_corr,
    }


def _count_members(db: Session) -> int:
    n = db.execute(select(func.count()).select_from(FactorWorkset)).scalar()
    return int(n or 0)


def _load_members(db: Session) -> list[dict[str, Any]]:
    """JOIN 注册表，忽略孤儿行；按 added_at、registry_id 稳定排序。"""
    rows = db.execute(
        select(FactorWorkset, FactorRegistry)
        .join(FactorRegistry, FactorRegistry.id == FactorWorkset.registry_id)
        .order_by(FactorWorkset.added_at.asc(), FactorRegistry.id.asc())
    ).all()
    out: list[dict[str, Any]] = []
    for ws, reg in rows:
        m = _metrics(db, reg)
        out.append({
            "id": ws.id,
            "registry_id": reg.id,
            "name": reg.name,
            "expr": reg.expr,
            "expr_excerpt": _expr_excerpt(reg.expr),
            "status": reg.status,
            "rating": m["rating"],
            "t_stat": m["t_stat"],
            "added_at": _iso(ws.added_at),
            "added_at_dt": ws.added_at,
            "max_abs_corr": m["max_abs_corr"],
            "source_id": reg.source_id,
            "source_mine_id": ws.source_mine_id,
            "ws": ws,
            "reg": reg,
            "report": m["report"],
        })
    return out


def _cached_eval(ctx: CsContext, expr: str) -> dict[str, float]:
    asof = ctx.asof.isoformat() if getattr(ctx, "asof", None) else ""
    key = (asof, (expr or "").strip())
    hit = _eval_cache.get(key)
    if hit is not None:
        return hit
    vals = eval_raw_values_on_cs(ctx, expr)
    _eval_cache[key] = vals
    return vals


def compute_pair_rho(ctx: CsContext, expr_a: str, expr_b: str) -> tuple[float | None, int]:
    """成员/候选两两 raw Spearman；供单测也可直接打桩。"""
    a = _cached_eval(ctx, expr_a)
    b = _cached_eval(ctx, expr_b)
    return pairwise_raw_spearman(a, b)


def compute_set_corr(
    ctx: CsContext | None,
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """上三角两两相关。items 需含 registry_id / name / expr。"""
    pairs: list[dict[str, Any]] = []
    undetermined = False
    if len(items) <= 1:
        return [], False
    if ctx is None:
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                pairs.append({
                    "a": a["registry_id"],
                    "b": b["registry_id"],
                    "rho": None,
                    "n_pairs": 0,
                })
        return pairs, True
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            rho, n_pairs = compute_pair_rho(ctx, a["expr"], b["expr"])
            if rho is None:
                undetermined = True
            pairs.append({
                "a": a["registry_id"],
                "b": b["registry_id"],
                "rho": rho,
                "n_pairs": n_pairs,
            })
    return pairs, undetermined


def compute_candidate_pairs(
    ctx: CsContext | None,
    cand_expr: str,
    members: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool, float | None]:
    """候选 vs 每个现有成员。返回 (pairs, undetermined, max_abs_corr_ws)。"""
    if not members:
        return [], False, None
    if ctx is None:
        pairs = [{
            "member_registry_id": m["registry_id"],
            "name": m["name"],
            "rho": None,
            "n_pairs": 0,
        } for m in members]
        return pairs, True, None
    pairs: list[dict[str, Any]] = []
    undetermined = False
    abs_vals: list[float] = []
    for m in members:
        rho, n_pairs = compute_pair_rho(ctx, cand_expr, m["expr"])
        if rho is None:
            undetermined = True
        else:
            abs_vals.append(abs(rho))
        pairs.append({
            "member_registry_id": m["registry_id"],
            "name": m["name"],
            "rho": rho,
            "n_pairs": n_pairs,
        })
    max_abs = max(abs_vals) if abs_vals else None
    return pairs, undetermined, max_abs


def _tie_tuple(member: dict[str, Any]) -> tuple:
    added = member.get("added_at") or ""
    return (added, int(member["registry_id"]))


def _pick_weakest(members: list[dict[str, Any]]) -> dict[str, Any]:
    """主路 t 最低；并列时更早 added_at，再比更小 registry_id。缺 t 视为最弱。"""
    def key(m: dict[str, Any]):
        t = m.get("t_stat")
        tkey = t if t is not None else float("-inf")
        return (tkey, *_tie_tuple(m))
    return min(members, key=key)


def _pick_max_corr_member(
    pairs: list[dict[str, Any]],
    members_by_id: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    """与候选 |ρ| 最大的成员；并列破平同 _pick_weakest 的时间/id 规则。"""
    scored = [p for p in pairs if p.get("rho") is not None]
    if not scored:
        return None

    def key(p: dict[str, Any]):
        mid = p["member_registry_id"]
        m = members_by_id[mid]
        return (-abs(float(p["rho"])), *_tie_tuple(m))

    best = min(scored, key=key)
    return members_by_id[best["member_registry_id"]]


def _new_set_conflicts(
    ctx: CsContext | None,
    candidate: dict[str, Any],
    remaining: list[dict[str, Any]],
) -> tuple[bool, list[dict[str, Any]], list[dict[str, Any]]]:
    """替换后重算新集合全部两两。返回 (ok, new_pairs, conflict_members)。"""
    items = remaining + [candidate]
    if len(items) <= 1:
        return True, [], []
    pairs, undetermined = compute_set_corr(ctx, items)
    conflicts: list[dict[str, Any]] = []
    cand_id = candidate["registry_id"]
    by_id = {x["registry_id"]: x for x in items}
    ok = (not undetermined)
    for p in pairs:
        rho = p.get("rho")
        if rho is None or abs(float(rho)) >= WORKSET_CORR_ABS_MAX:
            ok = False
            other = p["b"] if p["a"] == cand_id else (p["a"] if p["b"] == cand_id else None)
            # 列出与候选冲突者；留任之间的冲突也标出两端
            if other is not None:
                mem = by_id.get(other)
                if mem:
                    conflicts.append({
                        "registry_id": mem["registry_id"],
                        "name": mem["name"],
                        "rho": rho,
                    })
            else:
                for rid in (p["a"], p["b"]):
                    mem = by_id.get(rid)
                    if mem and mem["registry_id"] != cand_id:
                        conflicts.append({
                            "registry_id": mem["registry_id"],
                            "name": mem["name"],
                            "rho": rho,
                        })
    # 去重
    seen: set[int] = set()
    uniq: list[dict[str, Any]] = []
    for c in conflicts:
        if c["registry_id"] in seen:
            continue
        seen.add(c["registry_id"])
        uniq.append(c)
    return ok, pairs, uniq


def _blank_corr(asof) -> dict[str, Any]:
    return {
        "asof": asof.isoformat() if asof else None,
        "max_abs_corr_ws": None,
        "undetermined": False,
        "pairs": [],
    }


def _preview_base(
    *,
    decision: str,
    error_code: str | None,
    reasons: list[str],
    quality: dict[str, Any],
    in_set: bool,
    n: int,
    t_stat: float | None,
    rating: str | None,
    corr: dict[str, Any],
    suggestion: dict[str, Any] | None = None,
    ok: bool | None = None,
) -> dict[str, Any]:
    if ok is None:
        ok = decision in {"admit", "replace", "in_set"}
    return {
        "ok": ok,
        "decision": decision,
        "error_code": error_code,
        "reasons": reasons,
        "quality": quality,
        "in_set": in_set,
        "n": n,
        "cap": WORKSET_CAP,
        "t_stat": t_stat,
        "rating": rating,
        "corr": corr,
        "suggestion": suggestion,
    }


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------
def list_workset(db: Session) -> dict[str, Any]:
    members = _load_members(db)
    n = len(members)
    asof = _latest_asof(db)
    ctx = load_latest_cs_context(db) if n > 1 else None
    # n>1 才需要矩阵；无 as-of 时 ctx 为 None → undetermined
    matrix, undetermined = compute_set_corr(ctx, members) if n > 1 else ([], False)
    target_hint = "尚未达到目标 3–5" if n < WORKSET_TARGET_MIN else None
    public_members = [{
        "id": m["id"],
        "registry_id": m["registry_id"],
        "name": m["name"],
        "expr": m["expr"],
        "expr_excerpt": m["expr_excerpt"],
        "status": m["status"],
        "rating": m["rating"],
        "t_stat": m["t_stat"],
        "added_at": m["added_at"],
        "max_abs_corr": m["max_abs_corr"],
        "source_id": m["source_id"],
    } for m in members]
    return {
        "ok": True,
        "n": n,
        "cap": WORKSET_CAP,
        "target_min": WORKSET_TARGET_MIN,
        "target_hint": target_hint,
        "asof": asof.isoformat() if asof else None,
        "corr_threshold": WORKSET_CORR_ABS_MAX,
        "corr_undetermined": undetermined,
        "members": public_members,
        "corr_matrix": matrix,
        "note": WORKSET_NOTE,
    }


# ---------------------------------------------------------------------------
# 预览状态机（无写）
# ---------------------------------------------------------------------------
class RegistryNotFound(LookupError):
    """路由层映射 404。"""


def preview_admit(db: Session, registry_id: int) -> dict[str, Any]:
    """无副作用：不写 workset、不改 registry、不 bump n_tested。"""
    return _evaluate(db, registry_id)


def _evaluate(db: Session, registry_id: int) -> dict[str, Any]:
    reg = db.get(FactorRegistry, int(registry_id))
    if reg is None:
        raise RegistryNotFound("不存在")

    members = _load_members(db)
    n = len(members)
    metrics = _metrics(db, reg)
    rating = metrics["rating"]
    t_cand = metrics["t_stat"]
    asof = _latest_asof(db)
    quality = {"passed": True, "reasons": []}

    in_ids = {m["registry_id"] for m in members}
    if reg.id in in_ids:
        return _preview_base(
            decision="in_set",
            error_code=None,
            reasons=[],
            quality=quality,
            in_set=True,
            n=n,
            t_stat=t_cand,
            rating=rating,
            corr=_blank_corr(asof),
        )

    if reg.status == "disabled":
        return _preview_base(
            decision="reject",
            error_code="disabled",
            reasons=[DISABLED_REASON],
            quality=quality,
            in_set=False,
            n=n,
            t_stat=t_cand,
            rating=rating,
            corr=_blank_corr(asof),
            ok=False,
        )

    reasons = check_enable_gate(metrics["report"], expr=reg.expr)
    quality = {"passed": not reasons, "reasons": reasons}
    if reasons:
        return _preview_base(
            decision="reject",
            error_code="gate_failed",
            reasons=list(reasons),
            quality=quality,
            in_set=False,
            n=n,
            t_stat=t_cand,
            rating=rating,
            corr=_blank_corr(asof),
            ok=False,
        )

    ctx = load_latest_cs_context(db)
    pairs, undetermined, max_abs = compute_candidate_pairs(ctx, reg.expr, members)
    corr = {
        "asof": (ctx.asof.isoformat() if ctx and ctx.asof else (asof.isoformat() if asof else None)),
        "max_abs_corr_ws": max_abs,
        "undetermined": undetermined,
        "pairs": pairs,
    }

    # 5a. 有对端却无法判定
    if members and undetermined:
        return _preview_base(
            decision="reject",
            error_code="corr_undetermined",
            reasons=[CORR_UNDETERMINED_REASON],
            quality=quality,
            in_set=False,
            n=n,
            t_stat=t_cand,
            rating=rating,
            corr=corr,
            ok=False,
        )

    corr_pass = (not members) or (
        max_abs is not None and max_abs < WORKSET_CORR_ABS_MAX
    )
    # 无成员：相关门自然通过（空集第 1 人只过质量门）
    # 有成员且全部可判定：max_abs 必有值（至少一对）

    candidate_item = {
        "registry_id": reg.id,
        "name": reg.name,
        "expr": reg.expr,
        "t_stat": t_cand,
        "added_at": None,
    }

    # 5b. 未满且相关门通过 → 直接入集
    if n < WORKSET_CAP and corr_pass:
        return _preview_base(
            decision="admit",
            error_code=None,
            reasons=[],
            quality=quality,
            in_set=False,
            n=n,
            t_stat=t_cand,
            rating=rating,
            corr=corr,
        )

    members_by_id = {m["registry_id"]: m for m in members}

    # 5c. 满员且相关门通过：建议替换最低 t（须严格大于；新集合须过门）
    if n >= WORKSET_CAP and corr_pass:
        y = _pick_weakest(members)
        t_y = y.get("t_stat")
        if _strictly_greater(t_cand, t_y):
            remaining = [m for m in members if m["registry_id"] != y["registry_id"]]
            ok_new, new_pairs, conflicts = _new_set_conflicts(ctx, candidate_item, remaining)
            suggestion = {
                "replace_registry_id": y["registry_id"],
                "replace_name": y["name"],
                "reason": f"工作集已满，建议替换最弱成员 {y['name']}",
                "rho": next(
                    (p["rho"] for p in pairs if p["member_registry_id"] == y["registry_id"]),
                    None,
                ),
                "t_candidate": t_cand,
                "t_member": t_y,
                "new_set_corr_ok": ok_new,
                "new_set_pairs": [
                    {
                        "member_registry_id": p["a"] if p["b"] == reg.id else p["b"],
                        "name": members_by_id.get(
                            p["a"] if p["b"] == reg.id else p["b"], {}
                        ).get("name") or candidate_item["name"],
                        "rho": p["rho"],
                        "n_pairs": p["n_pairs"],
                    }
                    for p in new_pairs
                    if p["a"] == reg.id or p["b"] == reg.id
                ],
                "conflict_members": conflicts,
            }
            if ok_new:
                return _preview_base(
                    decision="replace",
                    error_code=None,
                    reasons=[],
                    quality=quality,
                    in_set=False,
                    n=n,
                    t_stat=t_cand,
                    rating=rating,
                    corr=corr,
                    suggestion=suggestion,
                )
            suggestion["new_set_corr_ok"] = False
            return _preview_base(
                decision="reject",
                error_code="replace_still_conflicts",
                reasons=["替换后新集合仍存在 |ρ|≥0.50 或无法判定的成员对"],
                quality=quality,
                in_set=False,
                n=n,
                t_stat=t_cand,
                rating=rating,
                corr=corr,
                suggestion=suggestion,
                ok=False,
            )
        return _preview_base(
            decision="reject",
            error_code="cap_full_weak",
            reasons=["工作集已满，候选增量不足"],
            quality=quality,
            in_set=False,
            n=n,
            t_stat=t_cand,
            rating=rating,
            corr=corr,
            ok=False,
        )

    # 5d. 相关门未过（存在 |ρ|≥0.50）
    x = _pick_max_corr_member(pairs, members_by_id)
    if x is None:
        # 理论上有成员且未 undetermined 时不会到这
        return _preview_base(
            decision="reject",
            error_code="corr_undetermined",
            reasons=[CORR_UNDETERMINED_REASON],
            quality=quality,
            in_set=False,
            n=n,
            t_stat=t_cand,
            rating=rating,
            corr=corr,
            ok=False,
        )
    rho_x = next(
        (p["rho"] for p in pairs if p["member_registry_id"] == x["registry_id"]),
        None,
    )
    t_x = x.get("t_stat")
    if _strictly_greater(t_cand, t_x):
        remaining = [m for m in members if m["registry_id"] != x["registry_id"]]
        ok_new, new_pairs, conflicts = _new_set_conflicts(ctx, candidate_item, remaining)
        rho_disp = abs(float(rho_x)) if rho_x is not None else None
        reason = (
            f"与 {x['name']} raw |ρ|={rho_disp:.2f}，保留更强者，替换 {x['name']}"
            if rho_disp is not None
            else f"与 {x['name']} 高相关，保留更强者，替换 {x['name']}"
        )
        suggestion = {
            "replace_registry_id": x["registry_id"],
            "replace_name": x["name"],
            "reason": reason,
            "rho": rho_x,
            "t_candidate": t_cand,
            "t_member": t_x,
            "new_set_corr_ok": ok_new,
            "new_set_pairs": [
                {
                    "member_registry_id": p["a"] if p["b"] == reg.id else p["b"],
                    "name": ({m["registry_id"]: m for m in remaining}.get(
                        p["a"] if p["b"] == reg.id else p["b"], {}
                    ) or {}).get("name"),
                    "rho": p["rho"],
                    "n_pairs": p["n_pairs"],
                }
                for p in new_pairs
                if p["a"] == reg.id or p["b"] == reg.id
            ],
            "conflict_members": conflicts,
        }
        if ok_new:
            return _preview_base(
                decision="replace",
                error_code=None,
                reasons=[],
                quality=quality,
                in_set=False,
                n=n,
                t_stat=t_cand,
                rating=rating,
                corr=corr,
                suggestion=suggestion,
            )
        suggestion["new_set_corr_ok"] = False
        return _preview_base(
            decision="reject",
            error_code="replace_still_conflicts",
            reasons=["替换后新集合仍存在 |ρ|≥0.50 或无法判定的成员对"],
            quality=quality,
            in_set=False,
            n=n,
            t_stat=t_cand,
            rating=rating,
            corr=corr,
            suggestion=suggestion,
            ok=False,
        )

    return _preview_base(
        decision="reject",
        error_code="corr_too_high",
        reasons=[
            f"与 {x['name']} raw |ρ|={abs(float(rho_x)):.2f}，对方主路 t 更强或同等，不能直接入集"
            if rho_x is not None
            else f"与 {x['name']} 相关过高，且对方主路 t 更强或同等"
        ],
        quality=quality,
        in_set=False,
        n=n,
        t_stat=t_cand,
        rating=rating,
        corr=corr,
        ok=False,
    )


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
def _insert_member(db: Session, reg: FactorRegistry, note: str = "") -> FactorWorkset:
    row = FactorWorkset(
        registry_id=reg.id,
        added_at=datetime.utcnow(),
        note=note or "",
        source_mine_id=reg.source_id,
    )
    db.add(row)
    db.flush()
    return row


def admit(
    db: Session,
    registry_id: int,
    *,
    confirm: bool,
    replace_registry_id: int | None = None,
    note: str = "",
) -> dict[str, Any]:
    """事务内重算后再写；不信任过期 preview。"""
    begin_immediate(db)
    try:
        decision = _evaluate(db, registry_id)
        if decision["decision"] == "in_set":
            db.commit()
            out = dict(decision)
            out["ok"] = True
            out["already"] = True
            out["workset"] = list_workset(db)
            return out

        if not confirm:
            db.rollback()
            out = dict(decision)
            out["ok"] = False
            out["decision"] = "reject"
            out["error_code"] = "confirm_required"
            out["reasons"] = ["写入须确认（confirm=true）"]
            return out

        if decision["decision"] == "reject":
            db.rollback()
            return dict(decision)

        sug = decision.get("suggestion") or {}
        expected_replace = sug.get("replace_registry_id") if decision["decision"] == "replace" else None

        if decision["decision"] == "admit":
            if replace_registry_id is not None:
                db.rollback()
                out = dict(decision)
                out["ok"] = False
                out["decision"] = "reject"
                out["error_code"] = "replace_mismatch"
                out["reasons"] = ["当前决策为直接入集，不应携带 replace_registry_id"]
                return out
            reg = db.get(FactorRegistry, int(registry_id))
            assert reg is not None
            try:
                _insert_member(db, reg, note=note)
            except IntegrityError:
                db.rollback()
                ws = list_workset(db)
                return {
                    **decision,
                    "ok": True,
                    "decision": "in_set",
                    "already": True,
                    "in_set": True,
                    "workset": ws,
                    "n": ws["n"],
                }

        elif decision["decision"] == "replace":
            if replace_registry_id != expected_replace:
                db.rollback()
                out = dict(decision)
                out["ok"] = False
                out["decision"] = "reject"
                out["error_code"] = "replace_mismatch"
                out["reasons"] = ["替换对象与服务端此刻建议不一致"]
                return out
            old = db.execute(
                select(FactorWorkset).where(
                    FactorWorkset.registry_id == int(replace_registry_id)
                )
            ).scalar_one_or_none()
            if old is not None:
                db.delete(old)
                db.flush()
            reg = db.get(FactorRegistry, int(registry_id))
            assert reg is not None
            try:
                _insert_member(db, reg, note=note)
            except IntegrityError:
                db.rollback()
                ws = list_workset(db)
                return {
                    **decision,
                    "ok": True,
                    "decision": "in_set",
                    "already": True,
                    "in_set": True,
                    "workset": ws,
                    "n": ws["n"],
                }
        else:
            db.rollback()
            out = dict(decision)
            out["ok"] = False
            return out

        n = _count_members(db)
        if n > WORKSET_CAP:
            db.rollback()
            raise CapReachedError("工作集已满，无法写入第 6 人")

        invalidate_workset_cache()
        db.commit()
        ws = list_workset(db)
        out = dict(decision)
        out["ok"] = True
        out["workset"] = ws
        out["n"] = ws["n"]
        return out
    except CapReachedError:
        raise
    except RegistryNotFound:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def remove_member(db: Session, registry_id: int) -> dict[str, Any]:
    """移出，无门禁；不在集内幂等成功。"""
    begin_immediate(db)
    try:
        row = db.execute(
            select(FactorWorkset).where(FactorWorkset.registry_id == int(registry_id))
        ).scalar_one_or_none()
        already = row is None
        if row is not None:
            db.delete(row)
            db.flush()
        invalidate_workset_cache()
        db.commit()
        ws = list_workset(db)
        out = {
            "ok": True,
            "removed_registry_id": int(registry_id),
            "n": ws["n"],
            "workset": ws,
        }
        if already:
            out["already_absent"] = True
        return out
    except Exception:
        db.rollback()
        raise
