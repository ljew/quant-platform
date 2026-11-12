"""W48-2 工作集短名单验收：状态机、快照过期、假 runner、不 bump、不顶替。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.cost_defaults import COMMISSION, SLIPPAGE
from app.core.strategies.workset_long_topn import WorksetMemberLongTopN
from app.database import Base
from app.models import FactorRegistry, FactorWorkset, FactorWorksetAcceptRun
from app.services import workset_backtest_accept as acc
from app.services.cost_sensitivity import run_cost_sensitivity
from app.services.factor_mt import bump_n_tested, get_n_tested
from app.services.walk_forward import run_walk_forward, stricter


BACKEND = Path(__file__).resolve().parents[1]
SERVICE_SRC = (BACKEND / "app/services/workset_backtest_accept.py").read_text(encoding="utf-8")


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


# ---------------------------------------------------------------------------
# 集合状态机（纯函数）
# ---------------------------------------------------------------------------
def _m(verdict: str, **kw) -> dict:
    return {"verdict": verdict, **kw}


def test_aggregate_n2_all_pass_is_short():
    assert acc.aggregate_collection(2, [_m("通过"), _m("通过")]) == "未齐"


def test_aggregate_n3_all_pass_is_pass():
    assert acc.aggregate_collection(3, [_m("通过"), _m("通过"), _m("通过")]) == "通过"


def test_aggregate_n3_two_pass_one_uneval_is_pass():
    """写死测例：不得补严成必须全员可跑。"""
    assert acc.aggregate_collection(3, [
        _m("通过"), _m("通过"), _m("无法按回测口径验收"),
    ]) == "通过"


def test_aggregate_one_fragile_or_wf_fail_rejects():
    assert acc.aggregate_collection(3, [_m("通过"), _m("拒绝"), _m("通过")]) == "拒绝"


def test_aggregate_n2_one_reject_is_reject_not_short():
    assert acc.aggregate_collection(2, [_m("通过"), _m("拒绝")]) == "拒绝"


def test_aggregate_n3_all_uneval_or_insufficient_unknown():
    assert acc.aggregate_collection(3, [
        _m("无法按回测口径验收"), _m("样本不足"), _m("无法按回测口径验收"),
    ]) == "无法判定"


def test_aggregate_empty_unknown():
    assert acc.aggregate_collection(0, []) == "无法判定"


# ---------------------------------------------------------------------------
# 成员判定顺序
# ---------------------------------------------------------------------------
def test_judge_pass_requires_wf_and_not_fragile_and_double():
    got = acc.judge_member(wf_final="通过", fragile=False, double_computed=True)
    assert got["verdict"] == "通过"


def test_judge_fragile_rejects_even_if_wf_pass():
    got = acc.judge_member(
        wf_final="通过", fragile=True, double_computed=True,
        cost_note="仅低摩擦成立：默认格超额>0，但成本翻倍后超额≤0",
    )
    assert got["verdict"] == "拒绝"
    assert "fragile" in got["reject_reasons"]
    assert "成本翻倍后脆弱" in (got["reason"] or "")


def test_judge_wf_overfit_or_fail_rejects():
    a = acc.judge_member(wf_final="过拟合", fragile=False, double_computed=True)
    b = acc.judge_member(wf_final="未通过", fragile=False, double_computed=True)
    assert a["verdict"] == "拒绝" and "wf_overfit" in a["reject_reasons"]
    assert b["verdict"] == "拒绝" and "wf_fail" in b["reject_reasons"]


def test_judge_wf_pass_but_double_missing_is_uneval_not_pass():
    got = acc.judge_member(wf_final="通过", fragile=False, double_computed=False)
    assert got["verdict"] == "无法按回测口径验收"
    assert "不得标通过" in (got["reason"] or "")


def test_judge_disabled_is_uneval():
    got = acc.judge_member(wf_final="未启用", fragile=False, double_computed=True)
    assert got["verdict"] == "无法按回测口径验收"


def test_judge_insufficient():
    got = acc.judge_member(wf_final="样本不足", fragile=False, double_computed=True)
    assert got["verdict"] == "样本不足"


def test_judge_uneval_first():
    got = acc.judge_member(
        unevaluable_reason="表达式无法求值",
        wf_final="通过", fragile=False, double_computed=True,
    )
    assert got["verdict"] == "无法按回测口径验收"


# ---------------------------------------------------------------------------
# 过期
# ---------------------------------------------------------------------------
def _row(db: Session, members, *, start="2023-01-01", end="2025-06-30", adj="qfq", ver=None):
    snap = acc.snapshot_fingerprint(members)
    r = FactorWorksetAcceptRun(
        created_at=datetime.utcnow(),
        status="done",
        start=start, end=end, adj=adj,
        initial_cash=1_000_000,
        rules_version=ver or acc.ACCEPT_RULES_VERSION,
        cost_commission=COMMISSION, cost_slippage=SLIPPAGE,
        snapshot_json=json.dumps(snap, ensure_ascii=False),
        members_json=json.dumps([{"registry_id": m["registry_id"], "verdict": "通过"} for m in members]),
        collection_verdict="通过" if len(members) >= 3 else "未齐",
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def test_stale_when_member_added_or_removed(db: Session):
    members = [{"registry_id": 1, "expr": "a"}, {"registry_id": 2, "expr": "b"}]
    row = _row(db, members)
    stale, reason = acc.is_stale(row, members)
    assert stale is False
    stale, reason = acc.is_stale(row, members + [{"registry_id": 3, "expr": "c"}])
    assert stale is True
    assert "工作集已变更" in (reason or "")
    stale, _ = acc.is_stale(row, [members[0]])
    assert stale is True


def test_stale_when_same_id_expr_changed(db: Session):
    members = [{"registry_id": 1, "expr": "roc(c_m, 20)"}]
    row = _row(db, members)
    stale, reason = acc.is_stale(row, [{"registry_id": 1, "expr": "roc(c_m, 60)"}])
    assert stale is True
    assert "工作集已变更" in (reason or "")


def test_stale_when_range_or_rules_differ(db: Session):
    members = [{"registry_id": 1, "expr": "a"}]
    row = _row(db, members)
    stale, reason = acc.is_stale(row, members, {"start": "2020-01-01", "end": "2025-06-30"})
    assert stale is True
    assert "区间" in (reason or "")
    row2 = _row(db, members, ver="old")
    stale, reason = acc.is_stale(row2, members)
    assert stale is True


# ---------------------------------------------------------------------------
# API / start_accept（假 runner）
# ---------------------------------------------------------------------------
def _ws_members(n: int) -> list[dict]:
    return [
        {"registry_id": i + 1, "expr": f"roc(c_m, {10 + i})", "name": f"因子{i+1}",
         "rating": "可用", "t_stat": 2.1, "added_at": "2026-11-11T04:01:00"}
        for i in range(n)
    ]


def _fake_member(verdict: str):
    def fn(db, member, start, end):
        return {
            "registry_id": member["registry_id"],
            "name": member.get("name"),
            "expr": member["expr"],
            "verdict": verdict,
            "reject_reasons": ["wf_fail"] if verdict == "拒绝" else [],
            "reason": None if verdict == "通过" else "x",
            "n_held": 50 if verdict != "无法按回测口径验收" else 0,
            "n_valid": 100 if verdict != "无法按回测口径验收" else 0,
            "wf": {"final": "通过" if verdict == "通过" else "未通过",
                   "agg": "通过", "legacy": "通过", "windows": [], "fragile_regime": False},
            "cost": {"fragile": False, "default_excess": 0.1, "double_excess": 0.05, "note": "ok"},
        }
    return fn


def _mixed_fn(mapping: dict[int, str]):
    base = {}
    def fn(db, member, start, end):
        v = mapping[int(member["registry_id"])]
        return _fake_member(v)(db, member, start, end)
    return fn


def _patch_ws(members):
    payload = {"n": len(members), "cap": 5, "target_min": 3, "members": members}
    return patch.multiple(
        acc,
        workset_available=lambda db: True,
        _list_members=lambda db: payload,
    )


def test_empty_post_400_no_row(db: Session):
    with _patch_ws([]), pytest.raises(acc.AcceptError) as ei:
        acc.start_accept(db, sync=True)
    assert ei.value.error_code == "empty_workset"
    assert ei.value.status == 400
    n = db.execute(select(func.count()).select_from(FactorWorksetAcceptRun)).scalar()
    assert int(n or 0) == 0


def test_forbidden_params_400():
    with pytest.raises(acc.AcceptError) as ei:
        acc.check_forbidden_params({"adj": "hfq"})
    assert ei.value.error_code == "rules_param_forbidden"
    with pytest.raises(acc.AcceptError) as ei:
        acc.check_forbidden_params({"top_n": 30})
    assert ei.value.error_code == "rules_param_forbidden"
    with pytest.raises(acc.AcceptError) as ei:
        acc.check_forbidden_params({"neutralize_industry": True})
    assert ei.value.error_code == "rules_param_forbidden"
    with pytest.raises(acc.AcceptError) as ei:
        acc.check_forbidden_params({"commission": 0.001})
    assert ei.value.error_code == "rules_param_forbidden"
    with pytest.raises(acc.AcceptError) as ei:
        acc.check_forbidden_params({"members": [1]})
    assert ei.value.error_code == "rules_param_forbidden"
    with pytest.raises(acc.AcceptError) as ei:
        acc.check_forbidden_params({"n_windows": 5})
    assert ei.value.error_code == "rules_param_forbidden"
    # 与冻结值相同可以过
    acc.check_forbidden_params({"adj": "qfq", "top_n": 50, "n_windows": 3})


def test_workset_unavailable_400(db: Session):
    with patch.object(acc, "workset_available", return_value=False):
        with pytest.raises(acc.AcceptError) as ei:
            acc.start_accept(db, sync=True)
        assert ei.value.error_code == "workset_unavailable"
        assert ei.value.status == 400
    out = acc.get_latest_accept(db)
    # 可用探测在真实空库上表是存在的；显式测返回字段形状
    with patch.object(acc, "workset_available", return_value=False):
        out = acc.get_latest_accept(db)
        assert out["workset_available"] is False
        assert out["run"] is None


def test_job_running_409(db: Session):
    members = _ws_members(3)
    row = _row(db, members)
    row.status = "running"
    row.job_id = "abc123"
    db.commit()
    with _patch_ws(members), pytest.raises(acc.AcceptError) as ei:
        acc.start_accept(db, sync=True)
    assert ei.value.status == 409
    assert ei.value.error_code == "job_running"
    assert ei.value.extra.get("job_id") == "abc123"


def test_start_n2_all_pass_collection_short(db: Session):
    members = _ws_members(2)
    with _patch_ws(members):
        out = acc.start_accept(db, sync=True, run_member_fn=_fake_member("通过"))
    assert out["status"] == "done"
    assert out["collection"]["verdict"] == "未齐"
    assert out["collection"]["n"] == 2


def test_start_n3_all_pass_collection_pass(db: Session):
    members = _ws_members(3)
    with _patch_ws(members):
        out = acc.start_accept(db, sync=True, run_member_fn=_fake_member("通过"))
    assert out["collection"]["verdict"] == "通过"


def test_start_n3_two_pass_one_uneval_collection_pass(db: Session):
    members = _ws_members(3)
    fn = _mixed_fn({1: "通过", 2: "通过", 3: "无法按回测口径验收"})
    with _patch_ws(members):
        out = acc.start_accept(db, sync=True, run_member_fn=fn)
    assert out["collection"]["verdict"] == "通过"
    assert out["collection"]["n_pass"] == 2
    assert out["collection"]["n_unevaluable"] == 1


def test_start_one_reject_collection_reject(db: Session):
    members = _ws_members(3)
    fn = _mixed_fn({1: "通过", 2: "拒绝", 3: "通过"})
    with _patch_ws(members):
        out = acc.start_accept(db, sync=True, run_member_fn=fn)
    assert out["collection"]["verdict"] == "拒绝"
    reasons = [m["reject_reasons"] for m in out["members"] if m["verdict"] == "拒绝"]
    assert reasons and "wf_fail" in reasons[0]


def test_start_n2_one_reject_is_reject(db: Session):
    members = _ws_members(2)
    fn = _mixed_fn({1: "通过", 2: "拒绝"})
    with _patch_ws(members):
        out = acc.start_accept(db, sync=True, run_member_fn=fn)
    assert out["collection"]["verdict"] == "拒绝"


def test_start_n3_all_uneval_unknown(db: Session):
    members = _ws_members(3)
    with _patch_ws(members):
        out = acc.start_accept(db, sync=True, run_member_fn=_fake_member("无法按回测口径验收"))
    assert out["collection"]["verdict"] == "无法判定"


def test_unevaluable_member_does_not_substitute_others_still_run(db: Session):
    members = _ws_members(3)
    seen = []

    def fn(db, member, start, end):
        seen.append(member["registry_id"])
        if member["registry_id"] == 2:
            return _fake_member("无法按回测口径验收")(db, member, start, end)
        return _fake_member("通过")(db, member, start, end)

    with _patch_ws(members):
        out = acc.start_accept(db, sync=True, run_member_fn=fn)
    assert seen == [1, 2, 3]
    by = {m["registry_id"]: m for m in out["members"]}
    assert by[2]["verdict"] == "无法按回测口径验收"
    assert by[1]["verdict"] == "通过" and by[3]["verdict"] == "通过"
    # 没有用别人的净值顶替
    assert by[2]["n_held"] == 0
    assert out["collection"]["verdict"] == "通过"


def test_get_latest_stale_after_workset_change(db: Session):
    members = _ws_members(3)
    with _patch_ws(members):
        acc.start_accept(db, sync=True, run_member_fn=_fake_member("通过"))
    later = members + [{"registry_id": 9, "expr": "x", "name": "新"}]
    with _patch_ws(later):
        out = acc.get_latest_accept(db)
    assert out["run"]["stale"] is True
    assert "过期" in (out["run"]["stale_reason"] or "")


def test_n_tested_not_bumped_and_no_enable_or_remove(db: Session):
    # 真实注册表 + 工作集行，确认验收不改它们、不 bump
    regs = []
    for i in range(3):
        r = FactorRegistry(name=f"f{i}", expr=f"roc(c_m, {i+5})", status="candidate")
        db.add(r)
        db.flush()
        db.add(FactorWorkset(registry_id=r.id, added_at=datetime.utcnow(), note=""))
        regs.append(r)
    db.commit()
    n0 = get_n_tested(db)
    members = [
        {"registry_id": r.id, "expr": r.expr, "name": r.name}
        for r in regs
    ]
    with patch.object(acc, "bump_n_tested", create=True) as spy_local, \
         patch("app.services.factor_mt.bump_n_tested", wraps=bump_n_tested) as spy:
        with _patch_ws(members):
            acc.start_accept(db, sync=True, run_member_fn=_fake_member("通过"))
        assert spy.call_count == 0
    assert get_n_tested(db) == n0
    db.expire_all()
    for r in regs:
        fresh = db.get(FactorRegistry, r.id)
        assert fresh is not None and fresh.status == "candidate"
        assert db.execute(
            select(FactorWorkset).where(FactorWorkset.registry_id == r.id)
        ).scalars().first() is not None


# ---------------------------------------------------------------------------
# 真实 cost / WF + 假 portfolio runner
# ---------------------------------------------------------------------------
class FakeBt:
    def __init__(self, *, excess=0.12, sharpe=0.5, trades=8, n_bars=80, n_valid=80, n_held=50):
        self.total_return = excess + 0.05
        self.annual_return = 0.1
        self.sharpe = sharpe
        self.max_drawdown = -0.1
        self.excess_return = excess
        self.trade_count = trades
        self.final_equity = 1_100_000
        self.n_bars = n_bars
        self.n_valid = n_valid
        self.n_held = n_held
        self.equity_curve = []


def _dates(n=200, start="2023-01-03"):
    from datetime import date, timedelta
    d = date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def test_run_member_default_cost_multipliers_and_bar_dates(db: Session):
    seen_cost: list[tuple[float, float]] = []
    seen_wf_symbols: list[str] = []
    loaded_symbol = {}

    def pf(req):
        seen_cost.append((float(req.commission), float(req.slippage)))
        # ×2 用正超额，避免 fragile
        if req.commission > COMMISSION + 1e-12:
            return FakeBt(excess=0.04, sharpe=0.3)
        return FakeBt(excess=0.12, sharpe=0.6)

    def loader():
        loaded_symbol["sym"] = acc.ACCEPT_INDEX_SYMBOL
        return _dates()

    member = {"registry_id": 1, "expr": "roc(c_m, 20)", "name": "动量20"}
    with patch("app.services.workset_backtest_accept.run_cost_sensitivity", wraps=run_cost_sensitivity) as cspy, \
         patch("app.services.workset_backtest_accept.run_walk_forward", wraps=run_walk_forward) as wspy:
        out = acc.run_member(
            db, member, "2023-01-01", "2025-06-30",
            portfolio_runner=pf,
            load_bar_dates=loader,
        )
    # ×1 为万 2.5+万 1，×2 为两倍；与 grid_pick 无关
    assert (COMMISSION, SLIPPAGE) in seen_cost
    assert (COMMISSION * 2, SLIPPAGE * 2) in seen_cost
    assert all(c in (COMMISSION, COMMISSION * 2) and s in (SLIPPAGE, SLIPPAGE * 2)
               for c, s in seen_cost)
    assert loaded_symbol["sym"] == "sh000906"
    assert out["meta_cls"] == "WorksetMemberLongTopN"
    assert out["cost"]["fragile"] is False
    assert wspy.called
    wf_req = wspy.call_args[0][0]
    assert wf_req.mode == "walk_forward"
    assert wf_req.n_windows == 3
    assert wf_req.scheme == "expanding"
    assert wf_req.metric == "sharpe"
    assert wf_req.symbol == "sh000906"
    bar_dates = wspy.call_args[0][1]
    assert bar_dates[0].startswith("2023")
    assert out["wf"]["final"] == stricter(out["wf"]["agg"], out["wf"]["legacy"])
    assert out["wf"]["final"] == "通过"
    # 一次 run_member 只有一个 expr
    assert out["expr"] == "roc(c_m, 20)"
    assert "collection_ir" not in out
    assert "组合IR" not in json.dumps(out, ensure_ascii=False)


def test_run_member_fragile_from_real_build_summary(db: Session):
    def pf(req):
        if abs(req.commission - COMMISSION * 2) < 1e-12:
            return FakeBt(excess=-0.01, sharpe=-0.2)
        return FakeBt(excess=0.12, sharpe=0.6)

    out = acc.run_member(
        db, {"registry_id": 1, "expr": "roc(c_m, 20)", "name": "m"},
        "2023-01-01", "2025-06-30",
        portfolio_runner=pf,
        bar_dates=_dates(),
    )
    assert out["cost"]["fragile"] is True
    # WF 假 runner 给正夏普 → final 通过，但 fragile → 拒绝
    assert out["verdict"] == "拒绝"
    assert "fragile" in out["reject_reasons"]


def test_run_member_eval_fail_uneval(db: Session):
    def pf(req):
        return FakeBt(n_valid=0, n_held=0, trades=0, excess=0, sharpe=0)

    out = acc.run_member(
        db, {"registry_id": 1, "expr": "not_a_factor()", "name": "bad"},
        "2023-01-01", "2025-06-30",
        portfolio_runner=pf,
        bar_dates=_dates(),
    )
    assert out["verdict"] == "无法按回测口径验收"
    assert out["n_valid"] == 0


def test_run_member_window_fail_maps_to_insufficient_not_500(db: Session):
    def pf(req):
        return FakeBt()

    def loader():
        return []  # 无交易日 → run_walk_forward HTTP 400/404

    out = acc.run_member(
        db, {"registry_id": 1, "expr": "roc(c_m, 20)", "name": "m"},
        "2023-01-01", "2025-06-30",
        portfolio_runner=pf,
        load_bar_dates=loader,
    )
    assert out["verdict"] == "样本不足"


def test_service_does_not_import_forbidden_strategies():
    assert "EnhancedFactorStrategy" not in SERVICE_SRC
    assert "Csi800EnhancedStrategy" not in SERVICE_SRC
    assert "enhanced_factor_hs300" not in SERVICE_SRC
    assert 'get_strategy("enhanced_factor")' not in SERVICE_SRC
    assert 'get_strategy("csi800_enhanced")' not in SERVICE_SRC
    assert "bump_n_tested" not in SERVICE_SRC
    assert "admit(" not in SERVICE_SRC
    assert "remove_member" not in SERVICE_SRC
    assert "check_enable_gate" not in SERVICE_SRC
    assert "_do_backtest" not in SERVICE_SRC
    assert "_persist" not in SERVICE_SRC


def test_build_member_meta_cls_and_not_in_registry():
    meta, params = acc.build_member_meta("roc(c_m, 20)")
    assert meta["cls"] is WorksetMemberLongTopN
    assert meta["index_code"] == "000906"
    assert meta["index_symbol"] == "sh000906"
    assert params["expr"] == "roc(c_m, 20)"
    assert params["top_n"] == 50
    assert params["rebalance_period"] == 21
    assert params["max_position_pct"] == 0.05
    assert params["momentum_lookback"] == 126
    from app.core.strategies.registry import STRATEGY_REGISTRY
    classes = [v.get("cls") for v in STRATEGY_REGISTRY.values()]
    assert WorksetMemberLongTopN not in classes
    keys = set(STRATEGY_REGISTRY)
    assert "workset_member_long_topn" not in keys
    assert "_workset_member_long_topn" not in keys


def test_migrate_creates_table(db: Session):
    # create_all 已建表；再跑 migrate 应幂等
    acc.migrate_factor_workset_accept(db.connection())
    db.commit()
    n = db.execute(select(func.count()).select_from(FactorWorksetAcceptRun)).scalar()
    assert int(n or 0) == 0


def test_new_post_appends_get_latest(db: Session):
    members = _ws_members(3)
    with _patch_ws(members):
        a = acc.start_accept(db, sync=True, run_member_fn=_fake_member("通过"))
        b = acc.start_accept(db, sync=True, run_member_fn=_fake_member("拒绝"))
    assert b["run_id"] != a["run_id"]
    got = acc.get_latest_accept(db)
    assert got["run"]["run_id"] == b["run_id"]
    assert got["run"]["collection"]["verdict"] == "拒绝"
    n = db.execute(select(func.count()).select_from(FactorWorksetAcceptRun)).scalar()
    assert int(n) == 2


def test_invalid_range():
    with pytest.raises(acc.AcceptError) as ei:
        acc._parse_dates("2025-01-01", "2023-01-01")
    assert ei.value.error_code == "invalid_range"
    with pytest.raises(acc.AcceptError) as ei:
        acc._parse_dates("bad", "2023-01-01")
    assert ei.value.error_code == "invalid_range"
