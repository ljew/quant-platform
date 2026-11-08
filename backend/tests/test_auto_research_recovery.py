"""自动研究调度键、重启判重与失败重试。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import AutoResearchRun
from app.services.auto_research import (
    _claim_scheduled_run,
    _finish_run,
    _new_run,
)


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_schedule_key_survives_session_restart(session_factory):
    with session_factory() as db:
        first = _claim_scheduled_run(db, "mining", "2026-11-08")
        assert first is not None
        run_id = first.id

    # 模拟进程/Session 重启：同一调度键不得再次运行。
    with session_factory() as db:
        assert _claim_scheduled_run(db, "mining", "2026-11-08") is None
        assert db.get(AutoResearchRun, run_id).status == "RUNNING"


def test_manual_run_does_not_consume_schedule_key(session_factory):
    with session_factory() as db:
        manual = _new_run(db, "review", "manual")
        scheduled = _claim_scheduled_run(db, "review", "2026-11-08")
        assert scheduled is not None
        assert scheduled.id != manual.id
        assert scheduled.schedule_key == "2026-11-08"


def test_success_never_retries(session_factory):
    with session_factory() as db:
        run = _claim_scheduled_run(db, "mining", "2026-11-08")
        _finish_run(db, run.id, True, {"ok": True})
        assert _claim_scheduled_run(db, "mining", "2026-11-08", retry_minutes=0) is None


def test_failed_run_retries_after_backoff_with_attempt(session_factory):
    with session_factory() as db:
        run = _claim_scheduled_run(db, "mining", "2026-11-08")
        _finish_run(db, run.id, False, {}, error="boom")
        assert _claim_scheduled_run(db, "mining", "2026-11-08") is None

        run = db.get(AutoResearchRun, run.id)
        run.finished_at = datetime.now() - timedelta(minutes=16)
        db.commit()
        retried = _claim_scheduled_run(db, "mining", "2026-11-08")
        assert retried is not None
        assert retried.id == run.id
        assert retried.attempt == 2
        assert retried.status == "RUNNING"
        assert retried.error is None
        count = db.scalar(select(func.count()).select_from(AutoResearchRun))
        assert count == 1
