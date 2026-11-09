"""多重检验累计状态（W47-3 / M3-6 报告层）。

跨 run 持久化 n_tested → 喂既有 luck_bar 抬门槛；Bonferroni α 仅展示。
不做 PBO/CSCV；不改 luck_bar 公式常数。
"""
from __future__ import annotations

import math
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import FactorMineResult, FactorMtState

SCOPE_GLOBAL = "global"

MT_NOTE = (
    "多重检验：评级门槛按累计检验次数抬升（luck_bar）；"
    "Bonferroni α 仅展示不二次抬门"
)

MT_FORMULA = (
    "luck_bar=sqrt(2*ln(max(N,2))); "
    "t_exc=max(2,luck_bar+0.3); "
    "t_use=max(1.5,luck_bar-0.3)"
)


def _seed_from_results(db: Session) -> int:
    n = db.execute(select(func.count()).select_from(FactorMineResult)).scalar()
    return int(n or 0)


def ensure_mt_row(db: Session, scope: str = SCOPE_GLOBAL) -> FactorMtState:
    """确保 scope 行存在；首次用 COUNT(factor_mine_results) 种子。"""
    row = db.get(FactorMtState, scope)
    if row is not None:
        return row
    n = _seed_from_results(db)
    row = FactorMtState(
        scope=scope,
        n_tested=n,
        updated_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_n_tested(db: Session, scope: str = SCOPE_GLOBAL) -> int:
    """读当前累计 N（评级前）；无行则种子后返回。"""
    return int(ensure_mt_row(db, scope).n_tested)


def bump_n_tested(db: Session, scope: str = SCOPE_GLOBAL, delta: int = 1) -> int:
    """原子自增；返回 bump 后累计值。"""
    ensure_mt_row(db, scope)
    # SQLite 友好：先读再写（个人台并发低；busy_timeout 兜底）
    row = db.get(FactorMtState, scope)
    assert row is not None
    row.n_tested = int(row.n_tested) + int(delta)
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return int(row.n_tested)


def bonferroni_alpha(n_tested: int) -> float:
    return 0.05 / max(int(n_tested), 1)


def enrich_mt_fields(
    result: dict,
    *,
    n_tested: int,
    n_tested_cum: int,
    scope: str = SCOPE_GLOBAL,
) -> dict:
    """给 mine 报告补多重检验字段；顶层与 multiple_testing 块一致。

    不改 rating / luck_bar 公式——只附加展示与累计元数据。
    若 result 已有 luck_bar / t_thresholds（mine_factor 写出），原样复用。
    """
    if not isinstance(result, dict):
        return result
    n = max(int(n_tested), 1)
    luck = result.get("luck_bar")
    if luck is None:
        luck = round(math.sqrt(2 * math.log(max(n, 2))), 3)
    thr = result.get("t_thresholds")
    if not isinstance(thr, dict):
        t_exc = max(2.0, float(luck) + 0.3)
        t_use = max(1.5, float(luck) - 0.3)
        thr = {"优秀": round(t_exc, 2), "可用": round(t_use, 2)}
    alpha = bonferroni_alpha(n)
    block = {
        "scope": scope,
        "n_tested": n,
        "n_tested_cum": int(n_tested_cum),
        "luck_bar": luck,
        "t_thresholds": thr,
        "bonferroni_alpha": alpha,
        "formula": MT_FORMULA,
    }
    result["n_tested"] = n
    result["n_tested_cum"] = int(n_tested_cum)
    result["luck_bar"] = luck
    result["t_thresholds"] = thr
    result["bonferroni_alpha"] = alpha
    result["mt_note"] = MT_NOTE
    result["multiple_testing"] = block
    return result


def attach_mt_replay(cached: dict, db: Session, scope: str = SCOPE_GLOBAL) -> dict:
    """cache 回放：不 bump、不改历史 rating；附当前累计 + 提示。"""
    if not isinstance(cached, dict):
        return cached
    n_cum = get_n_tested(db, scope)
    n_then = cached.get("n_tested")
    try:
        n_then_i = int(n_then) if n_then is not None else None
    except (TypeError, ValueError):
        n_then_i = None
    cached["n_tested_cum"] = n_cum
    cached["mt_stale"] = True
    if n_then_i is not None:
        cached["mt_replay_note"] = (
            f"本条评级基于当时 n_tested={n_then_i}；当前累计 n_tested_cum={n_cum}"
        )
    else:
        cached["mt_replay_note"] = f"本条为历史回放；当前累计 n_tested_cum={n_cum}"
    # 补展示块（历史 luck_bar/门槛保留；cum 用当前）
    if "bonferroni_alpha" not in cached and n_then_i is not None:
        cached["bonferroni_alpha"] = bonferroni_alpha(n_then_i)
    if "mt_note" not in cached:
        cached["mt_note"] = MT_NOTE
    thr = cached.get("t_thresholds") if isinstance(cached.get("t_thresholds"), dict) else None
    luck = cached.get("luck_bar")
    cached["multiple_testing"] = {
        "scope": scope,
        "n_tested": n_then_i if n_then_i is not None else cached.get("n_tested"),
        "n_tested_cum": n_cum,
        "luck_bar": luck,
        "t_thresholds": thr,
        "bonferroni_alpha": cached.get("bonferroni_alpha"),
        "formula": MT_FORMULA,
        "stale": True,
        "replay_note": cached.get("mt_replay_note"),
    }
    return cached


def migrate_factor_mt_state(conn) -> None:
    """SQLite migrate：建表 + 必要时种子（由 database._migrate_sqlite 调用）。"""
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS factor_mt_state ("
        "scope TEXT PRIMARY KEY, "
        "n_tested INTEGER NOT NULL DEFAULT 0, "
        "updated_at DATETIME)"
    ))
    row = conn.execute(
        text("SELECT n_tested FROM factor_mt_state WHERE scope = :s"),
        {"s": SCOPE_GLOBAL},
    ).fetchone()
    if row is None:
        n = conn.execute(text("SELECT COUNT(*) FROM factor_mine_results")).scalar()
        n = int(n or 0)
        conn.execute(
            text(
                "INSERT INTO factor_mt_state (scope, n_tested, updated_at) "
                "VALUES (:s, :n, :t)"
            ),
            {"s": SCOPE_GLOBAL, "n": n, "t": datetime.utcnow().isoformat(sep=" ")},
        )
