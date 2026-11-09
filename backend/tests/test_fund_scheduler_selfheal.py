"""基金管道的自愈能力（2026-11-09 重构）—— 心跳、僵尸接管、自动重试、每日穿透。

背景：容器 20:08 重启把净值子进程杀了，库里那条 running 记录没人清理，
22:00 的巡检看到 running 直接跳过，当天该环节永久停摆且不报警。这组用例
锁住修复后的行为，避免以后再退回去。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from app.core import fund_scheduler as fs


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "DB_PATH", tmp_path / "fund.db")
    monkeypatch.setattr(fs, "_running", set())
    monkeypatch.setattr(fs, "_boot_at", 0.0)
    monkeypatch.setattr(fs, "_alert_missed", lambda *a, **k: None)
    fs.init_db()
    return tmp_path / "fund.db"


def _stage(key="penetration", **kw):
    st = {"key": key, "label": key, "kind": "derived", "script": "x.py",
          "args": [], "hour": 0, "minute": 0, "timeout": 60, "depends": [],
          "every_day": True}
    st.update(kw)
    return st


def _put_run(status="running", started=None, heartbeat=None, pid=None,
             attempts=1, trigger="schedule", finished=None):
    started = started or datetime.now().isoformat(timespec="seconds")
    con = sqlite3.connect(str(fs.DB_PATH))
    con.execute(
        "INSERT INTO fund_pipeline_runs(stage,run_date,status,trigger,started_at,"
        "finished_at,attempts,pid,heartbeat_at) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(stage,run_date) DO UPDATE SET status=excluded.status,"
        "started_at=excluded.started_at,finished_at=excluded.finished_at,"
        "attempts=excluded.attempts,pid=excluded.pid,"
        "heartbeat_at=excluded.heartbeat_at",
        ("penetration", fs._today(), status, trigger, started, finished,
         attempts, pid, heartbeat),
    )
    con.commit()
    con.close()


def _patch_single_stage(monkeypatch, stage):
    monkeypatch.setattr(fs, "STAGES", [stage])
    monkeypatch.setattr(fs, "STAGE_MAP", {stage["key"]: stage})


def _patch_run_stage(monkeypatch, called):
    def fake(key, force=False, trigger="manual", wait=False):
        called.append((key, trigger))
        return {"ok": True, "started": True}
    monkeypatch.setattr(fs, "run_stage", fake)


# ------------------------------------------------------------------ 心跳 / 僵尸
def test_init_db_adds_heartbeat_columns(db):
    con = sqlite3.connect(str(db))
    cols = {r[1] for r in con.execute("PRAGMA table_info(fund_pipeline_runs)")}
    con.close()
    assert {"pid", "heartbeat_at", "attempts"} <= cols


def test_run_is_dead_when_heartbeat_stale():
    old = (datetime.now() - timedelta(seconds=fs._HEARTBEAT_STALE_SEC + 60)).isoformat()
    assert fs._run_is_dead({"heartbeat_at": old, "pid": None}) is True


def test_run_is_dead_when_pid_gone():
    fresh = datetime.now().isoformat(timespec="seconds")
    # PID 999999 在容器里几乎必然不存在
    assert fs._run_is_dead({"heartbeat_at": fresh, "pid": 999999}) is True


def test_run_alive_when_pid_live_and_heartbeat_fresh():
    import os as _os
    fresh = datetime.now().isoformat(timespec="seconds")
    assert fs._run_is_dead({"heartbeat_at": fresh, "pid": _os.getpid()}) is False


def test_tick_takes_over_dead_running(db, monkeypatch):
    """核心回归：被杀掉的 running 必须被自动作废并重跑，而不是被跳过。"""
    stage = _stage()
    _patch_single_stage(monkeypatch, stage)
    called: list = []
    _patch_run_stage(monkeypatch, called)
    monkeypatch.setattr(fs, "_is_trading_day", lambda d=None: True)

    old = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
    _put_run(status="running", started=old, heartbeat=old, pid=999999)

    started = fs.tick()
    assert started == ["penetration"]
    run = fs._today_run("penetration")
    assert run["status"] == "fail"                     # 僵尸记录已作废
    assert "心跳" in (run["error"] or "")              # 且写清了接管原因
    assert called and called[0][1] == "retry"          # 接管即刻重跑（不走 10 分钟冷却）


def test_claim_increments_attempts(db):
    fs._claim("penetration", "schedule")
    fs._release("penetration")
    fs._claim("penetration", "retry")
    fs._release("penetration")
    assert fs._today_run("penetration")["attempts"] == 2


def test_tick_skips_live_running(db, monkeypatch):
    """本进程里真在跑的，绝不能被当成僵尸再拉一份。"""
    stage = _stage()
    _patch_single_stage(monkeypatch, stage)
    called: list = []
    _patch_run_stage(monkeypatch, called)
    monkeypatch.setattr(fs, "_is_trading_day", lambda d=None: True)

    fresh = datetime.now().isoformat(timespec="seconds")
    _put_run(status="running", started=fresh, heartbeat=fresh, pid=1)
    fs._running.add("penetration")

    assert fs.tick() == []
    assert called == []


def test_heartbeat_updates_timestamp(db):
    _put_run(status="running", heartbeat="2000-01-01T00:00:00")
    fs._heartbeat("penetration", 1234)
    run = fs._today_run("penetration")
    assert run["heartbeat_at"] != "2000-01-01T00:00:00"
    assert run["pid"] == 1234


# ------------------------------------------------------------------ 重试策略
def test_retry_after_cooldown(db, monkeypatch):
    stage = _stage()
    _patch_single_stage(monkeypatch, stage)
    called: list = []
    _patch_run_stage(monkeypatch, called)
    monkeypatch.setattr(fs, "_is_trading_day", lambda d=None: True)

    old = (datetime.now() - timedelta(minutes=20)).isoformat(timespec="seconds")
    _put_run(status="fail", started=old, finished=old, attempts=1)

    assert fs.tick() == ["penetration"]
    assert called[0][1] == "retry"


def test_no_retry_during_cooldown(db, monkeypatch):
    stage = _stage()
    _patch_single_stage(monkeypatch, stage)
    called: list = []
    _patch_run_stage(monkeypatch, called)
    monkeypatch.setattr(fs, "_is_trading_day", lambda d=None: True)

    just = (datetime.now() - timedelta(seconds=30)).isoformat(timespec="seconds")
    _put_run(status="fail", started=just, finished=just, attempts=1)

    assert fs.tick() == []
    assert called == []


def test_retry_gives_up_after_max_attempts(db, monkeypatch):
    stage = _stage()
    _patch_single_stage(monkeypatch, stage)
    called: list = []
    _patch_run_stage(monkeypatch, called)
    monkeypatch.setattr(fs, "_is_trading_day", lambda d=None: True)

    old = (datetime.now() - timedelta(minutes=60)).isoformat(timespec="seconds")
    _put_run(status="fail", started=old, finished=old, attempts=fs._MAX_ATTEMPTS)

    assert fs.tick() == []
    assert called == []


# ------------------------------------------------------------------ 节律 / 依赖
def test_penetration_runs_every_day_including_weekend(monkeypatch):
    monkeypatch.setattr(fs, "_is_trading_day", lambda d=None: False)
    st = fs.STAGE_MAP["penetration"]
    assert fs._should_run_today(st, {}) is True


def test_nav_still_skips_non_trading_day(monkeypatch):
    monkeypatch.setattr(fs, "_is_trading_day", lambda d=None: False)
    st = fs.STAGE_MAP["nav"]
    assert fs._should_run_today(st, {}) is False


def test_upstream_busy_blocks(db, monkeypatch):
    stage = _stage(depends=["holdings"])
    _patch_single_stage(monkeypatch, stage)
    called: list = []
    _patch_run_stage(monkeypatch, called)
    monkeypatch.setattr(fs, "_is_trading_day", lambda d=None: True)
    fs._running.add("holdings")

    assert fs.tick() == []
    assert called == []
