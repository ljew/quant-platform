"""W48-1 / M3-5：少而正交工作集——状态机、硬顶、不 bump、相关口径。"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import FactorMineResult, FactorMtState, FactorRegistry, FactorWorkset
from app.services import factor_workset as ws
from app.services.factor_mining import pairwise_raw_spearman, _spearman
from app.services.factor_mt import bump_n_tested, get_n_tested


BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _report(**kw):
    base = {
        "ok": True,
        "expr": "roc(c_m, 20)",
        "rating": "优秀",
        "ic_mean": 0.03,
        "icir": 0.5,
        "t_stat": 2.5,
        "max_abs_corr": 0.42,
        "n_periods": 40,
        "oos": {"verdict": "通过", "enabled": True, "n": 30,
                "t_stat": 2.5, "ic_mean": 0.03, "nw_lags": 3},
        "walk_forward": {"verdict": "通过", "n_folds": 3},
        "universe_pit": {"mode": "pit_index_membership", "lifecycle_filter": True},
        "pead_pit": {"mode": "asof", "scale": "raw_pp"},
        "valuation_pit": {"mode": "asof"},
        "cost_profile": {"commission": 0.00025},
    }
    base.update(kw)
    return base


def _add_mine(db: Session, *, expr: str, rating="优秀", t_stat=3.0, **kw) -> FactorMineResult:
    report = _report(expr=expr, rating=rating, t_stat=t_stat, **kw)
    if "oos" not in kw:
        report["oos"] = {
            "verdict": "通过", "enabled": True, "n": 30,
            "t_stat": t_stat, "ic_mean": 0.03, "nw_lags": 3,
        }
    row = FactorMineResult(
        name=kw.get("name", expr[:20]),
        expr=expr,
        rating=rating,
        ic_mean=0.03,
        icir=0.5,
        result_json=json.dumps(report, ensure_ascii=False),
        params_json="sig",
        created_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _add_reg(
    db: Session, *, name: str, expr: str, status="candidate",
    t_stat=3.0, rating="优秀", source=None,
) -> FactorRegistry:
    if source is None:
        source = _add_mine(db, expr=expr, rating=rating, t_stat=t_stat, name=name)
    row = FactorRegistry(
        name=name, expr=expr, status=status,
        source_id=source.id, ic_mean=0.03,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _vec(n=40, start=0.0, step=1.0) -> dict[str, float]:
    return {f"s{i:03d}": start + i * step for i in range(n)}


class FakeCtx:
    asof = date(2026, 11, 10)


def _patch_corr(rho_map: dict, n_pairs: int = 80, ctx_ok: bool = True):
    """rho_map: frozenset({id_or_expr_a, id_or_expr_b}) 或 (name_a, name_b) — 我们按 expr 匹配。"""

    def pair_rho(ctx, expr_a, expr_b):
        if expr_a == expr_b:
            return 1.0, n_pairs
        # try both orders
        key = (expr_a, expr_b)
        if key in rho_map:
            val = rho_map[key]
        elif (expr_b, expr_a) in rho_map:
            val = rho_map[(expr_b, expr_a)]
        else:
            val = 0.1
        if val is None:
            return None, 5 if n_pairs < 20 else 5
        return float(val), n_pairs

    def load_ctx(db):
        return FakeCtx() if ctx_ok else None

    return (
        patch.object(ws, "compute_pair_rho", side_effect=pair_rho),
        patch.object(ws, "load_latest_cs_context", side_effect=load_ctx),
    )


# ---------------------------------------------------------------------------
# pairwise 函数（不打桩）
# ---------------------------------------------------------------------------
class TestPairwiseRawSpearman:
    def test_n19_undetermined(self):
        a = _vec(19)
        b = _vec(19, start=2.0)
        rho, n = pairwise_raw_spearman(a, b)
        assert rho is None and n == 19

    def test_n20_perfect(self):
        a = _vec(20)
        b = _vec(20, start=5.0)  # 同向平移，秩相同
        rho, n = pairwise_raw_spearman(a, b)
        assert n == 20
        assert rho == pytest.approx(1.0, abs=1e-6)

    def test_n20_opposite(self):
        a = _vec(20)
        b = {k: -v for k, v in a.items()}
        rho, n = pairwise_raw_spearman(a, b)
        assert n == 20
        assert rho == pytest.approx(-1.0, abs=1e-6)

    def test_matches_spearman_rounding(self):
        a = _vec(30, step=1.0)
        b = {f"s{i:03d}": float((i * 3) % 17) for i in range(30)}
        rho, n = pairwise_raw_spearman(a, b)
        raw = _spearman([a[k] for k in a if k in b], [b[k] for k in a if k in b])
        assert n == 30
        assert rho == round(raw, 4)

    def test_n_below_20_not_zero(self):
        """禁止把样本不足当成 ρ=0 放行。"""
        a, b = _vec(4), _vec(4, start=10)
        rho, n = pairwise_raw_spearman(a, b)
        assert n == 4 and rho is None
        # 底层 _spearman(n<5) 才返回 0.0；工作集路径不得用到
        assert _spearman(list(a.values()), list(b.values())) == 0.0


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------
class TestAdmitEmptyAndDirect:
    def test_empty_first_member_no_asof(self, db: Session):
        """空集第 1 人只过质量门，不因缺 as-of 被拒。"""
        reg = _add_reg(db, name="A", expr="expr_a", t_stat=3.2)
        with patch.object(ws, "load_latest_cs_context", return_value=None):
            prev = ws.preview_admit(db, reg.id)
        assert prev["decision"] == "admit"
        assert prev["ok"] is True
        n_ws = db.execute(select(func.count()).select_from(FactorWorkset)).scalar()
        assert n_ws == 0  # 预览无写

        before_n = get_n_tested(db)
        with patch.object(ws, "load_latest_cs_context", return_value=None):
            out = ws.admit(db, reg.id, confirm=True)
        assert out["ok"] is True and out["decision"] == "admit"
        assert out["workset"]["n"] == 1
        assert get_n_tested(db) == before_n
        # 不自动 enabled
        db.refresh(reg)
        assert reg.status == "candidate"

    def test_preview_no_side_effects(self, db: Session):
        reg = _add_reg(db, name="A", expr="expr_a")
        before_status = reg.status
        before_mt = get_n_tested(db)
        with patch.object(ws, "load_latest_cs_context", return_value=None):
            ws.preview_admit(db, reg.id)
        assert db.execute(select(func.count()).select_from(FactorWorkset)).scalar() == 0
        db.refresh(reg)
        assert reg.status == before_status
        assert get_n_tested(db) == before_mt

    def test_confirm_false_no_write(self, db: Session):
        reg = _add_reg(db, name="A", expr="expr_a")
        with patch.object(ws, "load_latest_cs_context", return_value=None):
            out = ws.admit(db, reg.id, confirm=False)
        assert out["ok"] is False
        assert out["error_code"] == "confirm_required"
        assert db.execute(select(func.count()).select_from(FactorWorkset)).scalar() == 0

    def test_admit_idempotent(self, db: Session):
        reg = _add_reg(db, name="A", expr="expr_a")
        with patch.object(ws, "load_latest_cs_context", return_value=None):
            ws.admit(db, reg.id, confirm=True)
            out = ws.admit(db, reg.id, confirm=True)
        assert out["decision"] == "in_set" and out["already"] is True
        assert db.execute(select(func.count()).select_from(FactorWorkset)).scalar() == 1


class TestQualityAndStatus:
    def test_gate_failed(self, db: Session):
        reg = _add_reg(db, name="弱因子", expr="expr_weak", rating="弱", t_stat=0.5)
        # 覆盖报告评级为弱
        mine = db.get(FactorMineResult, reg.source_id)
        report = json.loads(mine.result_json)
        report["rating"] = "弱"
        mine.rating = "弱"
        mine.result_json = json.dumps(report, ensure_ascii=False)
        db.commit()
        prev = ws.preview_admit(db, reg.id)
        assert prev["decision"] == "reject"
        assert prev["error_code"] == "gate_failed"
        assert any("评级" in r for r in prev["reasons"])

    def test_disabled_rejected(self, db: Session):
        reg = _add_reg(db, name="D", expr="expr_d", status="disabled")
        prev = ws.preview_admit(db, reg.id)
        assert prev["error_code"] == "disabled"
        assert prev["ok"] is False
        with patch.object(ws, "load_latest_cs_context", return_value=None):
            out = ws.admit(db, reg.id, confirm=True)
        assert out["ok"] is False and out["error_code"] == "disabled"
        db.refresh(reg)
        assert reg.status == "disabled"
        assert db.execute(select(func.count()).select_from(FactorWorkset)).scalar() == 0

    def test_enabled_can_join(self, db: Session):
        reg = _add_reg(db, name="E", expr="expr_e", status="enabled")
        with patch.object(ws, "load_latest_cs_context", return_value=None):
            out = ws.admit(db, reg.id, confirm=True)
        assert out["ok"] is True and out["decision"] == "admit"
        db.refresh(reg)
        assert reg.status == "enabled"  # 入集不改状态

    def test_not_registered_raises(self, db: Session):
        with pytest.raises(ws.RegistryNotFound):
            ws.preview_admit(db, 99999)


class TestCorrGate:
    def _seed_two(self, db):
        a = _add_reg(db, name="A", expr="expr_a", t_stat=2.1)
        with patch.object(ws, "load_latest_cs_context", return_value=None):
            ws.admit(db, a.id, confirm=True)
        b = _add_reg(db, name="B", expr="expr_b", t_stat=3.4)
        return a, b

    def test_corr_pass_0_4999(self, db: Session):
        a, b = self._seed_two(db)
        p1, p2 = _patch_corr({("expr_b", "expr_a"): 0.4999, ("expr_a", "expr_b"): 0.4999})
        with p1, p2:
            prev = ws.preview_admit(db, b.id)
        assert prev["decision"] == "admit"
        assert prev["corr"]["max_abs_corr_ws"] == pytest.approx(0.4999)

    def test_corr_too_high_same_or_weaker_t(self, db: Session):
        a, b = self._seed_two(db)
        # B t=3.4 > A 2.1 would be replace; make B weaker
        mine = db.get(FactorMineResult, b.source_id)
        report = json.loads(mine.result_json)
        report["t_stat"] = 2.0
        mine.result_json = json.dumps(report)
        db.commit()
        p1, p2 = _patch_corr({("expr_b", "expr_a"): 0.62, ("expr_a", "expr_b"): 0.62})
        with p1, p2:
            prev = ws.preview_admit(db, b.id)
            out = ws.admit(db, b.id, confirm=True)
        assert prev["error_code"] == "corr_too_high"
        assert out["ok"] is False
        assert db.execute(select(func.count()).select_from(FactorWorkset)).scalar() == 1

    def test_high_corr_replace_stronger(self, db: Session):
        a, b = self._seed_two(db)  # B t=3.4 > A 2.1
        p1, p2 = _patch_corr({("expr_b", "expr_a"): 0.62, ("expr_a", "expr_b"): 0.62})
        with p1, p2:
            prev = ws.preview_admit(db, b.id)
            assert prev["decision"] == "replace"
            assert prev["suggestion"]["replace_registry_id"] == a.id
            out = ws.admit(db, b.id, confirm=True, replace_registry_id=a.id)
        assert out["ok"] is True and out["decision"] == "replace"
        ids = {m["registry_id"] for m in out["workset"]["members"]}
        assert ids == {b.id}

    def test_same_t_no_replace(self, db: Session):
        a, b = self._seed_two(db)
        # 把 B 的 t 改成与 A 相同 2.1
        mine = db.get(FactorMineResult, b.source_id)
        report = json.loads(mine.result_json)
        report["t_stat"] = 2.1
        mine.result_json = json.dumps(report)
        db.commit()
        p1, p2 = _patch_corr({("expr_b", "expr_a"): 0.70, ("expr_a", "expr_b"): 0.70})
        with p1, p2:
            prev = ws.preview_admit(db, b.id)
        assert prev["error_code"] == "corr_too_high"
        assert prev["decision"] == "reject"

    def test_undetermined_n_pairs(self, db: Session):
        a, b = self._seed_two(db)
        p1, p2 = _patch_corr({("expr_b", "expr_a"): None, ("expr_a", "expr_b"): None})
        with p1, p2:
            prev = ws.preview_admit(db, b.id)
            out = ws.admit(db, b.id, confirm=True)
        assert prev["error_code"] == "corr_undetermined"
        assert "无法判定" in "".join(prev["reasons"])
        assert out["ok"] is False
        assert db.execute(select(func.count()).select_from(FactorWorkset)).scalar() == 1


class TestCapAndReplace:
    def _fill(self, db, n=5, t0=2.0):
        regs = []
        for i in range(n):
            r = _add_reg(db, name=f"M{i}", expr=f"expr_m{i}", t_stat=t0 + i * 0.1)
            with patch.object(ws, "load_latest_cs_context", return_value=None):
                # 空到满：只在空集或互相低相关时能直接入。先 stub 低相关。
                rho = {}
                for a in regs:
                    rho[(r.expr, a.expr)] = 0.1
                    rho[(a.expr, r.expr)] = 0.1
                p1, p2 = _patch_corr(rho)
                with p1, p2:
                    out = ws.admit(db, r.id, confirm=True)
                assert out["ok"], out
            regs.append(r)
        return regs

    def test_cap_full_replace_weakest(self, db: Session):
        regs = self._fill(db, 5, t0=2.0)  # t = 2.0 .. 2.4，最弱 M0
        cand = _add_reg(db, name="NEW", expr="expr_new", t_stat=3.5)
        rho = {("expr_new", f"expr_m{i}"): 0.2 for i in range(5)}
        rho.update({(f"expr_m{i}", "expr_new"): 0.2 for i in range(5)})
        # 留任之间保持低相关
        for i in range(5):
            for j in range(i + 1, 5):
                rho[(f"expr_m{i}", f"expr_m{j}")] = 0.15
                rho[(f"expr_m{j}", f"expr_m{i}")] = 0.15
        p1, p2 = _patch_corr(rho)
        with p1, p2:
            prev = ws.preview_admit(db, cand.id)
            assert prev["decision"] == "replace"
            assert prev["suggestion"]["replace_registry_id"] == regs[0].id
            out = ws.admit(db, cand.id, confirm=True,
                           replace_registry_id=regs[0].id)
        assert out["ok"] is True
        ids = {m["registry_id"] for m in out["workset"]["members"]}
        assert regs[0].id not in ids and cand.id in ids
        assert out["workset"]["n"] == 5

    def test_cap_full_weak(self, db: Session):
        regs = self._fill(db, 5, t0=3.0)  # 最低 t=3.0
        cand = _add_reg(db, name="WEAK", expr="expr_weak2", t_stat=2.5)
        rho = {("expr_weak2", f"expr_m{i}"): 0.2 for i in range(5)}
        rho.update({(f"expr_m{i}", "expr_weak2"): 0.2 for i in range(5)})
        p1, p2 = _patch_corr(rho)
        with p1, p2:
            prev = ws.preview_admit(db, cand.id)
            out = ws.admit(db, cand.id, confirm=True)
        assert prev["error_code"] == "cap_full_weak"
        assert out["ok"] is False
        assert out["workset"]["n"] == 5 if "workset" in out else \
            db.execute(select(func.count()).select_from(FactorWorkset)).scalar() == 5
        assert db.execute(select(func.count()).select_from(FactorWorkset)).scalar() == 5

    def test_replace_still_conflicts(self, db: Session):
        # n=2，高相关替换 A，但与留任 C 仍 |ρ|≥0.50
        a = _add_reg(db, name="A", expr="expr_a", t_stat=2.0)
        c = _add_reg(db, name="C", expr="expr_c", t_stat=2.2)
        with patch.object(ws, "load_latest_cs_context", return_value=None):
            ws.admit(db, a.id, confirm=True)
        p1, p2 = _patch_corr({
            ("expr_c", "expr_a"): 0.1, ("expr_a", "expr_c"): 0.1,
        })
        with p1, p2:
            ws.admit(db, c.id, confirm=True)
        cand = _add_reg(db, name="B", expr="expr_b", t_stat=4.0)
        # B vs A 高相关，B 更强 → 考虑替换 A；但 B vs C = 0.70
        p1, p2 = _patch_corr({
            ("expr_b", "expr_a"): 0.80, ("expr_a", "expr_b"): 0.80,
            ("expr_b", "expr_c"): 0.70, ("expr_c", "expr_b"): 0.70,
            ("expr_a", "expr_c"): 0.10, ("expr_c", "expr_a"): 0.10,
        })
        with p1, p2:
            prev = ws.preview_admit(db, cand.id)
            out = ws.admit(db, cand.id, confirm=True,
                           replace_registry_id=a.id)
        assert prev["error_code"] == "replace_still_conflicts"
        assert prev["suggestion"]["replace_registry_id"] == a.id
        assert prev["suggestion"]["new_set_corr_ok"] is False
        assert any(c_["registry_id"] == c.id for c_ in prev["suggestion"]["conflict_members"])
        assert out["ok"] is False
        assert db.execute(select(func.count()).select_from(FactorWorkset)).scalar() == 2

    def test_replace_mismatch(self, db: Session):
        regs = self._fill(db, 5, t0=2.0)
        cand = _add_reg(db, name="NEW", expr="expr_new", t_stat=3.5)
        rho = {("expr_new", f"expr_m{i}"): 0.2 for i in range(5)}
        rho.update({(f"expr_m{i}", "expr_new"): 0.2 for i in range(5)})
        for i in range(5):
            for j in range(i + 1, 5):
                rho[(f"expr_m{i}", f"expr_m{j}")] = 0.15
        p1, p2 = _patch_corr(rho)
        with p1, p2:
            out = ws.admit(db, cand.id, confirm=True,
                           replace_registry_id=regs[-1].id)  # 不是最弱
        assert out["ok"] is False and out["error_code"] == "replace_mismatch"
        assert db.execute(select(func.count()).select_from(FactorWorkset)).scalar() == 5

    def test_tie_break_earlier_added_at(self, db: Session):
        """同 t 并列时替换更早 added_at（满员相关通过、候选 t 严格更大）。"""
        r1 = _add_reg(db, name="OLD", expr="expr_old", t_stat=2.0)
        with patch.object(ws, "load_latest_cs_context", return_value=None):
            ws.admit(db, r1.id, confirm=True)
        # 保证 added_at 有先后
        row1 = db.execute(select(FactorWorkset)).scalar_one()
        row1.added_at = datetime(2026, 1, 1, 0, 0, 0)
        db.commit()
        others = []
        for i in range(4):
            r = _add_reg(db, name=f"N{i}", expr=f"expr_n{i}", t_stat=2.0 if i == 0 else 2.5)
            rho = {("expr_n0", "expr_old"): 0.1, ("expr_old", "expr_n0"): 0.1} if i == 0 else {}
            # low corr among all current + this
            current = [r1, *others]
            for a in current:
                rho[(r.expr, a.expr)] = 0.1
                rho[(a.expr, r.expr)] = 0.1
            p1, p2 = _patch_corr(rho)
            with p1, p2:
                assert ws.admit(db, r.id, confirm=True)["ok"]
            others.append(r)
        # 现在 5 人：OLD t=2.0 added earlier, N0 t=2.0 added later, N1-3 t=2.5
        cand = _add_reg(db, name="CAND", expr="expr_cand", t_stat=3.0)
        rho = {("expr_cand", "expr_old"): 0.2, ("expr_old", "expr_cand"): 0.2}
        for r in others:
            rho[("expr_cand", r.expr)] = 0.2
            rho[(r.expr, "expr_cand")] = 0.2
        for i, a in enumerate([r1, *others]):
            for b in [r1, *others][i + 1:]:
                rho[(a.expr, b.expr)] = 0.1
                rho[(b.expr, a.expr)] = 0.1
        p1, p2 = _patch_corr(rho)
        with p1, p2:
            prev = ws.preview_admit(db, cand.id)
        assert prev["decision"] == "replace"
        assert prev["suggestion"]["replace_registry_id"] == r1.id  # 更早 added_at

    def test_sequential_fifth_then_sixth_not_six(self, db: Session):
        """n=4 时连续两次直接入集：第二笔不得把集合写成 6。"""
        regs = self._fill(db, 4, t0=2.0)
        c1 = _add_reg(db, name="C1", expr="expr_c1", t_stat=2.8)
        c2 = _add_reg(db, name="C2", expr="expr_c2", t_stat=2.9)
        rho = {}
        for r in regs:
            rho[("expr_c1", r.expr)] = 0.1
            rho[(r.expr, "expr_c1")] = 0.1
            rho[("expr_c2", r.expr)] = 0.1
            rho[(r.expr, "expr_c2")] = 0.1
        rho[("expr_c1", "expr_c2")] = 0.1
        rho[("expr_c2", "expr_c1")] = 0.1
        p1, p2 = _patch_corr(rho)
        with p1, p2:
            a1 = ws.admit(db, c1.id, confirm=True)
            a2 = ws.admit(db, c2.id, confirm=True)
        assert a1["ok"] is True and a1["workset"]["n"] == 5
        # 第二笔：满员且相关通过、t 可能走替换或 cap_full_weak，但 n 绝不能变成 6
        n = db.execute(select(func.count()).select_from(FactorWorkset)).scalar()
        assert n == 5
        if a2["ok"] and a2["decision"] == "replace":
            assert a2["workset"]["n"] == 5
        else:
            # 未带 replace_registry_id 时，满员决策为 replace 会因 mismatch 或需确认对象
            # 若决策是 replace 但未带 id → replace_mismatch，不写
            assert n == 5


class TestRemoveAndBump:
    def test_remove_idempotent(self, db: Session):
        reg = _add_reg(db, name="A", expr="expr_a")
        with patch.object(ws, "load_latest_cs_context", return_value=None):
            ws.admit(db, reg.id, confirm=True)
        out = ws.remove_member(db, reg.id)
        assert out["ok"] is True and out["n"] == 0
        out2 = ws.remove_member(db, reg.id)
        assert out2["ok"] is True and out2.get("already_absent") is True

    def test_no_bump_on_all_paths(self, db: Session):
        get_n_tested(db)
        bump_n_tested(db, delta=3)
        before = get_n_tested(db)
        assert before == 3
        a = _add_reg(db, name="A", expr="expr_a", t_stat=2.0)
        b = _add_reg(db, name="B", expr="expr_b", t_stat=3.5)
        with patch("app.services.factor_mt.bump_n_tested") as spy:
            with patch.object(ws, "load_latest_cs_context", return_value=None):
                ws.preview_admit(db, a.id)
                ws.admit(db, a.id, confirm=True)
            p1, p2 = _patch_corr({
                ("expr_b", "expr_a"): 0.7, ("expr_a", "expr_b"): 0.7,
            })
            with p1, p2:
                ws.preview_admit(db, b.id)
                ws.admit(db, b.id, confirm=True, replace_registry_id=a.id)
            ws.remove_member(db, b.id)
            spy.assert_not_called()
        assert get_n_tested(db) == before
        # 工作集模块源码不得引用 bump
        src = Path(ws.__file__).read_text(encoding="utf-8")
        assert "bump_n_tested" not in src


class TestSourceInvariants:
    def test_ortho_and_luck_bar_untouched(self):
        mining = (BACKEND / "app/services/factor_mining.py").read_text(encoding="utf-8")
        ortho = (BACKEND / "app/services/factor_ortho.py").read_text(encoding="utf-8")
        mt = (BACKEND / "app/services/factor_mt.py").read_text(encoding="utf-8")
        # 14 列相关与优秀门 0.8
        assert "FACTOR_COLUMNS" in mining
        assert "< 0.8" in mining
        # luck_bar 常数写法保留
        assert "luck_bar + 0.3" in mining
        assert "luck_bar - 0.3" in mining
        assert "max(2.0, luck_bar + 0.3)" in mining
        assert "max(1.5, luck_bar - 0.3)" in mining
        assert "ORTHO_EXPRS" in ortho
        assert "def orthogonalize_cross_section" in ortho
        # 阈值写死
        src = Path(ws.__file__).read_text(encoding="utf-8")
        assert "WORKSET_CORR_ABS_MAX = 0.50" in src
        # mt 公式块仍在
        assert "t_exc=max(2,luck_bar+0.3)" in mt or "+0.3" in mt


class TestRouter:
    def test_preview_404(self, db: Session):
        from app.routers.factor import WorksetPreviewPayload, workset_preview
        with pytest.raises(HTTPException) as ei:
            workset_preview(WorksetPreviewPayload(registry_id=4242), db)
        assert ei.value.status_code == 404
        assert "不存在" in str(ei.value.detail)

    def test_get_empty(self, db: Session):
        from app.routers.factor import workset_get
        out = workset_get(db)
        assert out["ok"] is True and out["n"] == 0
        assert out["members"] == []
        assert "少而正交" in out["note"]
