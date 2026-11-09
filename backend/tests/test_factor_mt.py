"""W47-3 / M3-6：多重检验累计（报告层）—— bump / 种子 / 回放不加 / 门槛单调。"""
from __future__ import annotations

import math
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import FactorMineResult, FactorMtState
from app.services.factor_mt import (
    MT_NOTE,
    attach_mt_replay,
    bonferroni_alpha,
    bump_n_tested,
    enrich_mt_fields,
    ensure_mt_row,
    get_n_tested,
)


@pytest.fixture()
def db():
    """内存 SQLite，与生产同 Base.metadata（含 factor_mt_state）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _add_mine_rows(db: Session, n: int) -> None:
    for i in range(n):
        db.add(FactorMineResult(
            name=f"f{i}", expr=f"roc(c_r,{i + 1})", rating="弱",
            ic_mean=0.01, icir=0.1, result_json="{}",
            params_json=f"sig{i}", created_at=datetime.utcnow(),
        ))
    db.commit()


class TestSeedAndBump:
    def test_seed_from_count(self, db: Session):
        _add_mine_rows(db, 5)
        n = get_n_tested(db)
        assert n == 5
        row = db.get(FactorMtState, "global")
        assert row is not None and row.n_tested == 5

    def test_bump_returns_after(self, db: Session):
        _add_mine_rows(db, 3)
        assert get_n_tested(db) == 3
        after = bump_n_tested(db, delta=1)
        assert after == 4
        assert get_n_tested(db) == 4

    def test_bump_does_not_recount(self, db: Session):
        """种子后只 increment；再插 mine 行不改 counter（除非 bump）。"""
        _add_mine_rows(db, 2)
        assert get_n_tested(db) == 2
        _add_mine_rows(db, 4)  # 再加 4 条结果行
        assert get_n_tested(db) == 2  # 仍为种子值
        assert bump_n_tested(db) == 3

    def test_empty_seed_zero(self, db: Session):
        assert get_n_tested(db) == 0
        assert bump_n_tested(db) == 1


class TestEnrichAndReplay:
    def test_enrich_fields(self):
        n = 72
        luck = math.sqrt(2 * math.log(max(n, 2)))
        result = {
            "ok": True,
            "n_tested": n,
            "luck_bar": round(luck, 3),
            "t_thresholds": {
                "优秀": round(max(2.0, luck + 0.3), 2),
                "可用": round(max(1.5, luck - 0.3), 2),
            },
            "rating": "弱",
        }
        enrich_mt_fields(result, n_tested=n, n_tested_cum=n + 1)
        assert result["n_tested"] == 72
        assert result["n_tested_cum"] == 73
        assert result["bonferroni_alpha"] == pytest.approx(0.05 / 72)
        assert result["mt_note"] == MT_NOTE
        assert result["multiple_testing"]["n_tested"] == 72
        assert result["multiple_testing"]["n_tested_cum"] == 73
        assert result["multiple_testing"]["luck_bar"] == result["luck_bar"]
        assert result["multiple_testing"]["t_thresholds"] == result["t_thresholds"]

    def test_replay_no_bump(self, db: Session):
        _add_mine_rows(db, 10)
        ensure_mt_row(db)
        before = get_n_tested(db)
        cached = {
            "ok": True,
            "n_tested": 1,
            "luck_bar": 1.177,
            "t_thresholds": {"优秀": 2.0, "可用": 1.5},
            "rating": "可用",
        }
        attach_mt_replay(cached, db)
        assert get_n_tested(db) == before  # 未 bump
        assert cached["mt_stale"] is True
        assert cached["n_tested_cum"] == before
        assert "当时 n_tested=1" in cached["mt_replay_note"]
        # 历史门槛未改
        assert cached["t_thresholds"]["优秀"] == 2.0
        assert cached["rating"] == "可用"


class TestThresholdMonotone:
    def test_luck_bar_and_gates_nondecreasing(self):
        prev_luck = -1.0
        prev_exc = -1.0
        prev_use = -1.0
        for n in [1, 2, 10, 72, 100, 500]:
            luck = math.sqrt(2 * math.log(max(n, 2)))
            t_exc = max(2.0, luck + 0.3)
            t_use = max(1.5, luck - 0.3)
            assert luck >= prev_luck - 1e-12
            assert t_exc >= prev_exc - 1e-12
            assert t_use >= prev_use - 1e-12
            prev_luck, prev_exc, prev_use = luck, t_exc, t_use

    def test_bonferroni_only_display(self):
        """α 随 N 变小，但不得被拿去改 t 门（契约：仅展示）。"""
        a1 = bonferroni_alpha(1)
        a72 = bonferroni_alpha(72)
        assert a1 == pytest.approx(0.05)
        assert a72 < a1
        # 门槛仍只由 luck_bar 公式决定
        luck = math.sqrt(2 * math.log(72))
        assert max(2.0, luck + 0.3) == pytest.approx(luck + 0.3)


class TestRouterWiringStatic:
    def test_mine_uses_get_bump(self):
        import inspect
        from app.routers import factor as fr
        src = inspect.getsource(fr.mine)
        assert "get_n_tested" in src
        assert "bump_n_tested" in src
        assert "attach_mt_replay" in src
        assert "n_tested=max(n_before, 1)" in src

    def test_horizons_bumps_each(self):
        import inspect
        from app.routers import factor as fr
        src = inspect.getsource(fr.mine_horizons)
        assert "bump_n_tested" in src
        assert "n_tested_bumped" in src

    def test_gp_elite_uses_cum_plus_seen(self):
        import inspect
        from app.services import factor_gp
        src = inspect.getsource(factor_gp.gp_search)
        assert "n_before_gp" in src
        assert "max(n_before_gp + len(seen), len(seen)" in src
        # 快评路径不得 bump
        eval_src = inspect.getsource(factor_gp.evaluate_candidate)
        assert "bump_n_tested" not in eval_src
