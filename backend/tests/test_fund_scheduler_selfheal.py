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


# ------------------------------------------------- 跨生命周期残留（2026-11-10）
def _put_run_at(stage: str, run_date: str, status="running", beat=None):
    beat = beat or datetime.now().isoformat(timespec="seconds")
    con = sqlite3.connect(str(fs.DB_PATH))
    con.execute(
        "INSERT INTO fund_pipeline_runs(stage,run_date,status,trigger,started_at,"
        "heartbeat_at) VALUES (?,?,?,'schedule',?,?) "
        "ON CONFLICT(stage,run_date) DO UPDATE SET status=excluded.status,"
        "started_at=excluded.started_at,heartbeat_at=excluded.heartbeat_at",
        (stage, run_date, status, beat, beat),
    )
    con.commit()
    con.close()


def test_startup_sweep_clears_yesterday_running(db):
    """跨天残留的 running 必须在启动时作废。

    2026-11-09 净值脚本 00:03 已跑完 6668 行，容器 00:25 重启把 _finish 吃掉，
    记录永远 running，nav_import 于是整天不跑。
    """
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    _put_run_at("nav", yesterday, status="running", beat=f"{yesterday}T23:30:38")

    assert fs._sweep_orphan_runs() == 1
    con = sqlite3.connect(str(fs.DB_PATH))
    row = con.execute(
        "SELECT status,error FROM fund_pipeline_runs WHERE stage='nav' AND run_date=?",
        (yesterday,)).fetchone()
    con.close()
    assert row[0] == "fail"
    assert "跨天残留" in row[1]


def test_startup_sweep_leaves_today_running_alone(db):
    """今天的 running 不能盲清：可能真有本进程的线程在跑，交给心跳判定。"""
    _put_run(status="running")
    assert fs._sweep_orphan_runs() == 0
    con = sqlite3.connect(str(fs.DB_PATH))
    st = con.execute("SELECT status FROM fund_pipeline_runs WHERE stage='penetration'").fetchone()[0]
    con.close()
    assert st == "running"


def test_derived_due_uses_last_run_not_last_ok(db):
    """上游被打断（只有 fail、没有 ok）也要让下游跑。

    只看 last_ok_at 的话，上游被中断等于下游永远等不到 —— 这正是
    净值入库被卡一整天的直接原因。
    """
    con = sqlite3.connect(str(fs.DB_PATH))
    con.execute("INSERT INTO fund_pipeline_state(stage,last_ok_at,last_fail_at) "
                "VALUES ('nav','2026-11-08T23:00:00','2026-11-09T23:30:38')")
    con.execute("INSERT INTO fund_pipeline_state(stage,last_ok_at) "
                "VALUES ('nav_import','2026-11-08T23:14:45')")
    con.execute("INSERT INTO fund_pipeline_runs(stage,run_date,status,started_at) "
                "VALUES ('nav','2026-11-09','fail','2026-11-09T23:30:38')")
    con.commit()
    con.close()

    states = fs._load_states()
    assert states["nav"]["last_ok_at"] == "2026-11-08T23:00:00"   # ok 没更新
    assert states["nav"]["last_run_at"] == "2026-11-09T23:30:38"  # 但跑过
    assert fs._derived_due({"key": "nav_import", "depends": ["nav"]}, states) is True


def test_derived_not_due_when_downstream_newer(db):
    """下游已经比上游新时不要白跑（保持幂等，不乱刷）。"""
    con = sqlite3.connect(str(fs.DB_PATH))
    con.execute("INSERT INTO fund_pipeline_state(stage,last_ok_at) VALUES ('nav','2026-11-09T23:00:00')")
    con.execute("INSERT INTO fund_pipeline_state(stage,last_ok_at) VALUES ('nav_import','2026-11-09T23:20:00')")
    con.commit()
    con.close()
    assert fs._derived_due({"key": "nav_import", "depends": ["nav"]}, fs._load_states()) is False
