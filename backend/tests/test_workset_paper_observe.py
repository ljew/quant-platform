"""W48-3 工作集短名单模拟盘观察：建盘、过期、幂等、expr 回读、钩子、不 bump。"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.cost_defaults import COMMISSION, SLIPPAGE
from app.core.engine import paper_engine as pe
from app.core.strategies.registry import STRATEGY_REGISTRY, get_strategy, list_strategies
from app.core.strategies.workset_long_topn import WorksetMemberLongTopN
from app.database import Base
from app.models import (
    FactorRegistry,
    FactorWorkset,
    FactorWorksetAcceptRun,
    IndexKlineDaily,
    PaperTask,
)
from app.services import workset_backtest_accept as acc
from app.services import workset_paper_observe as obs
from app.services.factor_mt import bump_n_tested, get_n_tested


BACKEND = Path(__file__).resolve().parents[1]
SERVICE_SRC = (BACKEND / "app/services/workset_paper_observe.py").read_text(encoding="utf-8")
ENGINE_SRC = (BACKEND / "app/core/engine/paper_engine.py").read_text(encoding="utf-8")


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
    obs.migrate_paper_workset_observe(s.connection())
    s.commit()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed_index(db: Session, day: str = "2026-11-12"):
    db.add(IndexKlineDaily(
        symbol="sh000906",
        trade_date=date.fromisoformat(day),
        open=1, high=1, low=1, close=1,
    ))
    db.commit()
    return day


def _ws_members(n: int, *, expr0: str | None = None) -> list[dict]:
    out = []
    for i in range(n):
        expr = expr0 if (i == 0 and expr0 is not None) else f"roc(c_m, {10 + i})"
        out.append({
            "registry_id": i + 1,
            "expr": expr,
            "name": f"因子{i+1}",
            "rating": "可用",
            "t_stat": 2.1,
            "added_at": "2026-11-11T04:01:00",
        })
    return out


def _accept_row(
    db: Session,
    members: list[dict],
    verdicts: list[str] | None = None,
    *,
    status: str = "done",
    collection: str | None = None,
    start: str = "2023-01-01",
    end: str = "2025-06-30",
    adj: str = "qfq",
    ver: str | None = None,
    commission: float | None = None,
    slippage: float | None = None,
):
    snap = acc.snapshot_fingerprint(members)
    if verdicts is None:
        verdicts = ["通过"] * len(members)
    results = []
    for m, v in zip(members, verdicts):
        results.append({
            "registry_id": m["registry_id"],
            "name": m.get("name"),
            "expr": m.get("expr"),
            "verdict": v,
        })
    if collection is None:
        collection = acc.aggregate_collection(len(members), results)
    r = FactorWorksetAcceptRun(
        created_at=datetime.utcnow(),
        finished_at=datetime.utcnow() if status == "done" else None,
        status=status,
        start=start, end=end, adj=adj,
        initial_cash=1_000_000,
        rules_version=ver or acc.ACCEPT_RULES_VERSION,
        index_code="000906",
        index_symbol="sh000906",
        top_n=50, rebalance_period=21, max_weight=0.05,
        cost_commission=COMMISSION if commission is None else commission,
        cost_slippage=SLIPPAGE if slippage is None else slippage,
        snapshot_json=json.dumps(snap, ensure_ascii=False),
        members_json=json.dumps(results, ensure_ascii=False),
        collection_verdict=collection,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _seed_live_workset(db: Session, members: list[dict]):
    for m in members:
        reg = FactorRegistry(
            id=m["registry_id"],
            name=m.get("name") or f"f{m['registry_id']}",
            expr=m["expr"],
            status="candidate",
        )
        db.merge(reg)
        existing = db.execute(
            select(FactorWorkset).where(FactorWorkset.registry_id == m["registry_id"])
        ).scalars().first()
        if existing is None:
            db.add(FactorWorkset(registry_id=m["registry_id"], added_at=datetime.utcnow(), note=""))
    db.commit()


def _ready(db: Session, n: int = 3, verdicts: list[str] | None = None, **kw):
    members = kw.pop("members", None) or _ws_members(n)
    _seed_live_workset(db, members)
    start_day = _seed_index(db, kw.pop("index_day", "2026-11-12"))
    row = _accept_row(db, members, verdicts, **kw)
    return members, row, start_day


# ---------------------------------------------------------------------------
# 建盘主路径
# ---------------------------------------------------------------------------
def test_create_passed_members_portfolio_defaults(db: Session):
    members, row, day = _ready(db, 3)
    out = obs.create_workset_observe(db, run_id=row.id)
    assert out["ok"] is True
    assert out["run_id"] == row.id
    assert out["stale"] is False
    assert out["start_date"] == day
    assert out["start_date"] != acc.ACCEPT_DEFAULT_START
    assert out["rules_version"] == acc.ACCEPT_RULES_VERSION
    assert out["collection"]["verdict"] == "通过"
    assert out["collection"]["n_pass"] == 3
    assert len(out["tasks"]) == 3
    assert all(t["created"] and t["expr_complete"] for t in out["tasks"])
    tasks = db.execute(select(PaperTask)).scalars().all()
    assert len(tasks) == 3
    for t in tasks:
        assert t.kind == "portfolio"
        assert t.index_code == "000906"
        assert t.adj == "qfq"
        assert t.commission == COMMISSION
        assert t.slippage == SLIPPAGE
        assert t.initial_cash == 1_000_000
        assert t.start_date == day
        assert t.strategy_key == obs.WORKSET_OBSERVE_KEY
        assert t.enabled is True
        assert t.workset_observe_run_id == row.id
        assert t.workset_observe_expr
        assert "expr" not in json.loads(t.params_json or "{}")
        assert t.name.startswith("工作集观察 · ")
        assert f"run#{row.id}" in t.name


def test_collection_not_pass_still_creates_passed_only(db: Session):
    verdicts = ["通过", "拒绝", "无法按回测口径验收"]
    members, row, _ = _ready(db, 3, verdicts=verdicts, collection="拒绝")
    out = obs.create_workset_observe(db, run_id=row.id)
    assert out["collection"]["verdict"] == "拒绝"
    assert out["collection"]["n_pass"] == 1
    assert len([t for t in out["tasks"] if t.get("expr_complete")]) == 1
    assert out["tasks"][0]["registry_id"] == 1
    ids = [t.workset_observe_registry_id for t in db.execute(select(PaperTask)).scalars().all()]
    assert ids == [1]


def test_idempotent_second_post_opens_existing(db: Session):
    members, row, day = _ready(db, 2, collection="未齐")
    first = obs.create_workset_observe(db, run_id=row.id)
    # 改账本水位，二次不得重置
    t0 = db.get(PaperTask, first["tasks"][0]["task_id"])
    t0.last_bar_date = "2026-11-13"
    t0.state_json = '{"cash": 1}'
    db.commit()
    second = obs.create_workset_observe(db, run_id=row.id)
    assert len(db.execute(select(PaperTask)).scalars().all()) == 2
    by_reg = {t["registry_id"]: t for t in second["tasks"]}
    for item in first["tasks"]:
        got = by_reg[item["registry_id"]]
        assert got["task_id"] == item["task_id"]
        assert got["created"] is False
        assert got["existing"] is True
    t0 = db.get(PaperTask, first["tasks"][0]["task_id"])
    assert t0.last_bar_date == "2026-11-13"
    assert t0.start_date == day
    assert t0.state_json == '{"cash": 1}'


def test_recreate_after_hard_delete(db: Session):
    members, row, _ = _ready(db, 1, collection="未齐")
    first = obs.create_workset_observe(db, run_id=row.id)
    tid = first["tasks"][0]["task_id"]
    t = db.get(PaperTask, tid)
    db.delete(t)
    db.commit()
    second = obs.create_workset_observe(db, run_id=row.id)
    assert second["tasks"][0]["created"] is True
    assert db.execute(select(func.count(PaperTask.id))).scalar() == 1
    t2 = db.get(PaperTask, second["tasks"][0]["task_id"])
    assert t2 is not None
    assert t2.workset_observe_run_id == row.id


# ---------------------------------------------------------------------------
# 拒绝码
# ---------------------------------------------------------------------------
def test_stale_snapshot_rejected_zero_created(db: Session):
    members, row, _ = _ready(db, 3)
    # 变更工作集表达式 → is_stale
    live = [{**m} for m in members]
    live[0] = {**live[0], "expr": "roc(c_m, 99)"}
    # 同步注册表/工作集
    reg = db.get(FactorRegistry, 1)
    reg.expr = "roc(c_m, 99)"
    db.commit()
    with pytest.raises(obs.ObserveError) as ei:
        obs.create_workset_observe(db, run_id=row.id)
    assert ei.value.error_code == "snapshot_stale"
    assert "过期" in ei.value.message
    assert db.execute(select(func.count(PaperTask.id))).scalar() == 0


def test_no_passed_members(db: Session):
    members, row, _ = _ready(db, 2, verdicts=["拒绝", "样本不足"], collection="拒绝")
    with pytest.raises(obs.ObserveError) as ei:
        obs.create_workset_observe(db, run_id=row.id)
    assert ei.value.error_code == "no_passed_members"
    assert db.execute(select(func.count(PaperTask.id))).scalar() == 0


def test_accept_not_ready_running_or_error_or_missing(db: Session):
    members = _ws_members(2)
    _seed_live_workset(db, members)
    _seed_index(db)
    running = _accept_row(db, members, status="running")
    with pytest.raises(obs.ObserveError) as ei:
        obs.create_workset_observe(db, run_id=running.id)
    assert ei.value.error_code == "accept_not_ready"

    # 最新一次是 error、没有 done
    running.status = "error"
    db.commit()
    with pytest.raises(obs.ObserveError) as ei:
        obs.create_workset_observe(db)
    assert ei.value.error_code == "accept_not_ready"

    # 清空
    db.execute(FactorWorksetAcceptRun.__table__.delete())
    db.commit()
    with pytest.raises(obs.ObserveError) as ei:
        obs.create_workset_observe(db)
    assert ei.value.error_code == "accept_not_ready"


def test_index_no_bars(db: Session):
    members = _ws_members(1)
    _seed_live_workset(db, members)
    row = _accept_row(db, members, collection="未齐")
    with pytest.raises(obs.ObserveError) as ei:
        obs.create_workset_observe(db, run_id=row.id)
    assert ei.value.error_code == "index_no_bars"
    assert "ETL" in ei.value.message


def test_rules_param_forbidden(db: Session):
    members, row, _ = _ready(db, 1, collection="未齐")
    forbidden = [
        {"start_date": "2023-01-01"},
        {"adj": "hfq"},
        {"commission": 0.0001},
        {"slippage": 0.0015},
        {"top_n": 30},
        {"index_code": "000300"},
        {"strategy_key": "momentum"},
        {"expr": "roc(c_m, 20)"},
        {"initial_cash": 500000},
        {"short": True},
        {"neutralize_industry": True},
        {"members": [1]},
    ]
    for payload in forbidden:
        with pytest.raises(obs.ObserveError) as ei:
            obs.create_workset_observe(db, run_id=row.id, payload=payload)
        assert ei.value.error_code == "rules_param_forbidden", payload
    # 锁死值显式传入应放行
    out = obs.create_workset_observe(
        db, run_id=row.id,
        payload={"adj": "qfq", "commission": COMMISSION, "top_n": 50, "initial_cash": 1_000_000},
    )
    assert out["ok"] is True


def test_does_not_fallback_to_earlier_run(db: Session):
    members, row1, _ = _ready(db, 3)
    # 再写一次已完成但 stale 的「最新」——工作集不变时先建最新；然后改工作集让最新 stale
    row2 = _accept_row(db, members)  # 更新的 done，同快照暂未过期
    # 让最新 stale：改现场工作集
    reg = db.get(FactorRegistry, 1)
    reg.expr = "roc(c_m, 99)"
    db.commit()
    with pytest.raises(obs.ObserveError) as ei:
        obs.create_workset_observe(db)  # 省略 run_id = 最新
    assert ei.value.error_code == "snapshot_stale"
    # 指定更早一次（看起来未过期：快照仍是旧 expr，但不得回退）
    # 注意：is_stale(row1, current) 也会是 True，因为 current expr 已变。
    # 再造：最新 stale 是因为 rules_version，更早一次 version 正常且 current 匹配更早快照？
    # 按契约：最新一次 stale 时即使更早一次「看起来未过期」也不建。
    reg.expr = members[0]["expr"]
    db.commit()
    row2.rules_version = "old-rules"
    db.commit()
    # 现在 is_stale(row2)=True（version），is_stale(row1)=False（成员/expr 对得上）
    stale2, _ = acc.is_stale(row2, members)
    stale1, _ = acc.is_stale(row1, members)
    assert stale2 is True
    assert stale1 is False
    with pytest.raises(obs.ObserveError) as ei:
        obs.create_workset_observe(db)
    assert ei.value.error_code == "snapshot_stale"
    with pytest.raises(obs.ObserveError) as ei:
        obs.create_workset_observe(db, run_id=row1.id)
    assert ei.value.error_code == "snapshot_stale"
    assert db.execute(select(func.count(PaperTask.id))).scalar() == 0


# ---------------------------------------------------------------------------
# 超长 expr / 回读 / 截断防护
# ---------------------------------------------------------------------------
def test_long_expr_stored_in_text_and_reread(db: Session):
    long_expr = "roc(c_m, 20) + " + ("close/delay(close,1) + " * 80) + "vol"
    assert len(long_expr) > 1024
    members = _ws_members(1, expr0=long_expr)
    _seed_live_workset(db, members)
    _seed_index(db)
    row = _accept_row(db, members, collection="未齐")
    out = obs.create_workset_observe(db, run_id=row.id)
    assert out["tasks"][0]["expr_complete"] is True
    t = db.get(PaperTask, out["tasks"][0]["task_id"])
    assert t.workset_observe_expr == long_expr
    assert len(t.workset_observe_expr) > 1024
    # 即使有人把截断 expr 塞进 params_json，解析仍以 TEXT 列为准
    t.params_json = json.dumps({"expr": long_expr[:1024], "top_n": 50})
    db.commit()
    meta, params = pe._resolve_paper_meta(t)
    assert params["expr"] == long_expr
    assert params["expr"] != json.loads(t.params_json)["expr"]


def test_reread_mismatch_cannot_observe_no_truncated_task(db: Session):
    members, row, _ = _ready(db, 1, collection="未齐")

    real_refresh = Session.refresh

    def _tamper(self, instance, *a, **k):
        real_refresh(self, instance, *a, **k)
        if isinstance(instance, PaperTask) and instance.workset_observe_expr:
            instance.workset_observe_expr = instance.workset_observe_expr[:3]

    with patch.object(Session, "refresh", _tamper):
        with pytest.raises(obs.ObserveError) as ei:
            obs.create_workset_observe(db, run_id=row.id)
    assert ei.value.error_code == "cannot_observe"
    assert db.execute(select(func.count(PaperTask.id))).scalar() == 0


# ---------------------------------------------------------------------------
# 副作用
# ---------------------------------------------------------------------------
def test_no_bump_no_enable_no_remove(db: Session):
    members, row, _ = _ready(db, 3)
    n0 = get_n_tested(db)
    statuses = {r.id: r.status for r in db.execute(select(FactorRegistry)).scalars().all()}
    ws_n = db.execute(select(func.count(FactorWorkset.id))).scalar()
    with patch("app.services.factor_mt.bump_n_tested", wraps=bump_n_tested) as spy, \
         patch("app.services.factor_workset.admit", create=True) as spy_admit, \
         patch("app.services.factor_workset.remove_member", create=True) as spy_rm:
        obs.create_workset_observe(db, run_id=row.id)
        assert spy.call_count == 0
        assert spy_admit.call_count == 0
        assert spy_rm.call_count == 0
    assert get_n_tested(db) == n0
    db.expire_all()
    for rid, st in statuses.items():
        assert db.get(FactorRegistry, rid).status == st
    assert db.execute(select(func.count(FactorWorkset.id))).scalar() == ws_n
    # 不自动删停：再造一个旧任务，第二次建盘不得把它 enabled=False / 删除
    old = PaperTask(
        name="旧观察",
        strategy_key=obs.WORKSET_OBSERVE_KEY,
        kind="portfolio",
        index_code="000906",
        params_json="{}",
        commission=COMMISSION, slippage=SLIPPAGE, adj="qfq",
        enabled=True,
        workset_observe_run_id=999,
        workset_observe_registry_id=1,
        workset_observe_expr="x",
    )
    db.add(old)
    db.commit()
    oid = old.id
    obs.create_workset_observe(db, run_id=row.id)  # 幂等
    leftover = db.get(PaperTask, oid)
    assert leftover is not None and leftover.enabled is True


def test_stale_flag_on_old_task_not_deleted(db: Session):
    members, row, _ = _ready(db, 1, collection="未齐")
    out = obs.create_workset_observe(db, run_id=row.id)
    t = db.get(PaperTask, out["tasks"][0]["task_id"])
    fields = obs.observe_public_fields(db, t)
    assert fields["workset_observe"] is True
    assert fields["workset_observe_stale"] is False
    assert fields["strategy_label"] == "工作集观察"
    # 新完成 run 让旧任务过期
    _accept_row(db, members)
    fields = obs.observe_public_fields(db, t)
    assert fields["workset_observe_stale"] is True
    assert fields["workset_observe_stale_reason"] == "快照已过期"
    assert db.get(PaperTask, t.id) is not None
    assert db.get(PaperTask, t.id).enabled is True


# ---------------------------------------------------------------------------
# 引擎钩子
# ---------------------------------------------------------------------------
def test_resolve_paper_meta_observe_does_not_call_get_strategy(db: Session):
    members, row, _ = _ready(db, 1, collection="未齐")
    out = obs.create_workset_observe(db, run_id=row.id)
    t = db.get(PaperTask, out["tasks"][0]["task_id"])
    with patch.object(pe, "get_strategy", wraps=get_strategy) as spy:
        meta, params = pe._resolve_paper_meta(t)
    assert spy.call_count == 0
    assert meta["cls"] is WorksetMemberLongTopN
    assert params["expr"] == members[0]["expr"]
    assert params["top_n"] == 50
    assert params["rebalance_period"] == 21
    assert params["max_position_pct"] == 0.05
    assert params["warmup_days"] == 126


def test_resolve_paper_meta_ordinary_still_get_strategy():
    from app.core.strategies.registry import DISABLED_STRATEGIES
    key = next(k for k in STRATEGY_REGISTRY if k not in DISABLED_STRATEGIES)
    task = PaperTask(
        name="ordinary", strategy_key=key, kind="single",
        symbols="sh600519", params_json="{}",
    )
    with patch.object(pe, "get_strategy", wraps=get_strategy) as spy:
        meta, params = pe._resolve_paper_meta(task)
    assert spy.call_count == 1
    assert meta is STRATEGY_REGISTRY[key] or meta["cls"] is STRATEGY_REGISTRY[key]["cls"]


def test_resolve_missing_expr_raises_and_does_not_get_strategy():
    task = PaperTask(
        name="broken", strategy_key=obs.WORKSET_OBSERVE_KEY, kind="portfolio",
        index_code="000906", params_json=json.dumps({"expr": "truncated"}),
        workset_observe_run_id=1,
        workset_observe_registry_id=2,
        workset_observe_expr="",
    )
    with patch.object(pe, "get_strategy", wraps=get_strategy) as spy:
        with pytest.raises(ValueError, match="完整表达式"):
            pe._resolve_paper_meta(task)
    assert spy.call_count == 0


def test_run_paper_task_observe_key_does_not_write_error_msg(db: Session):
    members, row, _ = _ready(db, 1, collection="未齐")
    out = obs.create_workset_observe(db, run_id=row.id)
    t = db.get(PaperTask, out["tasks"][0]["task_id"])
    dummy = {
        "symbol": "",
        "last_bar_date": "2026-11-12",
        "account_start": "2026-11-12",
        "equity": 1_000_000,
        "cash": 1_000_000,
        "positions": {},
        "new_trades": [],
        "curve": [{"date": "2026-11-12", "equity": 1_000_000}],
    }
    with patch.object(pe, "get_strategy", wraps=get_strategy) as spy, \
         patch.object(pe, "_load_portfolio", return_value=dummy):
        result = pe.run_paper_task(db, t)
    assert spy.call_count == 0
    assert result.get("ok") is True
    db.refresh(t)
    assert t.error_msg is None


def test_run_paper_task_hooks_cover_three_sites():
    # 源码：三处不再直接 get_strategy，都走 _resolve_paper_meta
    assert ENGINE_SRC.count("get_strategy(") == 1  # 仅 _resolve_paper_meta 内部
    assert "_resolve_paper_meta(task)" in ENGINE_SRC
    assert ENGINE_SRC.count("_resolve_paper_meta(task)") >= 3


# ---------------------------------------------------------------------------
# 注册表 / 禁止顶替 / 命名
# ---------------------------------------------------------------------------
def test_registry_assertions_still_green():
    from app.core.strategies.registry import STRATEGY_REGISTRY as REG
    classes = [v.get("cls") for v in REG.values()]
    assert WorksetMemberLongTopN not in classes
    keys = set(REG)
    assert "workset_member_long_topn" not in keys
    assert "_workset_member_long_topn" not in keys
    listed = list_strategies() if callable(list_strategies) else []
    # list_strategies 可能返回 dict 或 list
    if isinstance(listed, dict):
        listed_keys = set(listed)
    else:
        listed_keys = {x["key"] if isinstance(x, dict) else getattr(x, "key", x) for x in listed}
    assert "_workset_member_long_topn" not in listed_keys
    assert "WorksetMemberLongTopN" not in str(listed)


def test_service_does_not_import_forbidden_strategies():
    assert "EnhancedFactorStrategy" not in SERVICE_SRC
    assert "Csi800EnhancedStrategy" not in SERVICE_SRC
    assert "enhanced_factor_hs300" not in SERVICE_SRC
    assert "hs300_enhanced" not in SERVICE_SRC
    assert 'get_strategy("enhanced_factor")' not in SERVICE_SRC
    assert 'get_strategy("csi800_enhanced")' not in SERVICE_SRC
    assert "bump_n_tested" not in SERVICE_SRC
    assert "admit(" not in SERVICE_SRC
    assert "remove_member" not in SERVICE_SRC
    assert "_do_backtest" not in SERVICE_SRC
    assert "_persist" not in SERVICE_SRC
    assert "STRATEGY_REGISTRY[" not in SERVICE_SRC


def test_generic_create_rejects_internal_key(db: Session):
    from fastapi import HTTPException
    from app.routers.paper import PaperTaskCreate, create_task

    class _Req(PaperTaskCreate):
        pass

    req = PaperTaskCreate(
        name="should-fail",
        strategy_key=obs.WORKSET_OBSERVE_KEY,
        kind="portfolio",
        index_code="000906",
    )
    with pytest.raises(HTTPException) as ei:
        create_task(req, db)
    assert ei.value.status_code == 400
    assert "未知策略" in str(ei.value.detail)


def test_task_name_truncation_keeps_ids():
    long_name = "超长成员名称" * 20
    n = obs.build_observe_task_name(long_name, 12345, 678)
    assert len(n) <= 64
    assert "12345" in n
    assert "r678" in n or "678" in n
    # 短名保持产品公式
    short = obs.build_observe_task_name("动量20", 7, 12)
    assert short == "工作集观察 · 动量20 · run#7"


def test_frontend_factorlab_has_no_observe_exit():
    web = BACKEND.parent / "web/src"
    card = (web / "pages").rglob("*")
    hits = []
    for p in web.rglob("*"):
        if p.suffix not in {".tsx", ".ts", ".jsx", ".js"}:
            continue
        # 本切片允许的入口
        if p.name in {"WorksetAcceptPanel.tsx", "PaperPage.tsx", "client.ts", "App.tsx"}:
            continue
        txt = p.read_text(encoding="utf-8")
        if "去模拟盘观察" in txt or "workset-observe" in txt:
            hits.append(str(p.relative_to(web)))
    assert hits == []


def test_migrate_idempotent(db: Session):
    obs.migrate_paper_workset_observe(db.connection())
    obs.migrate_paper_workset_observe(db.connection())
    db.commit()
    # unique index exists
    rows = list(db.connection().exec_driver_sql("PRAGMA index_list(paper_tasks)"))
    names = {r[1] for r in rows}
    assert "uq_paper_workset_observe" in names
