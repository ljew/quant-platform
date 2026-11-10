"""审计：每次智能体的工具调用都落库，出问题能倒查。

为什么这个不能省：
  智能体的结论错了，你要能回答「它当时看到了什么、调了哪个接口、返回了什么」。
  没有这张表，只能靠猜 —— 而"看起来很对但其实是幻觉"正是多智能体最大的失败模式。

表结构刻意做得扁平，方便直接 SQL 查：
  runs    一次提问一行（问题、任务单、耗时、token）
  actions 一次工具调用一行（谁、调了什么、参数、结果、耗时）
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from .config import AUDIT_DB

SCHEMA = """
create table if not exists runs (
    id          integer primary key autoincrement,
    created_at  text not null,
    question    text not null,
    plan_json   text,
    answer      text,
    n_agents    integer,
    n_calls     integer,
    total_ms    integer,
    prompt_tok  integer,
    completion_tok integer,
    status      text,
    error       text
);

create table if not exists actions (
    id          integer primary key autoincrement,
    run_id      integer not null,
    ts          text not null,
    agent       text not null,
    tool        text not null,
    params_json text,
    ok          integer,
    ms          integer,
    summary     text
);

create index if not exists idx_actions_run on actions(run_id);
create index if not exists idx_runs_created on runs(created_at desc);
"""


@contextmanager
def _conn():
    AUDIT_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(AUDIT_DB, timeout=10)
    try:
        c.executescript(SCHEMA)
        yield c
        c.commit()
    finally:
        c.close()


def start_run(question: str, plan_dict: dict) -> int:
    with _conn() as c:
        cur = c.execute(
            "insert into runs(created_at, question, plan_json, status) values (?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), question,
             json.dumps(plan_dict, ensure_ascii=False), "running"),
        )
        return int(cur.lastrowid)


def log_action(run_id: int, agent: str, tool: str, params: dict,
               result: dict, ms: int) -> None:
    ok = 1 if result.get("ok") else 0
    # 摘要只留 300 字，够判断"它看到了什么"，又不至于把库撑爆
    data = result.get("data")
    summary = result.get("error") or json.dumps(data, ensure_ascii=False)[:300]
    with _conn() as c:
        c.execute(
            "insert into actions(run_id, ts, agent, tool, params_json, ok, ms, summary) "
            "values (?,?,?,?,?,?,?,?)",
            (run_id, datetime.now().isoformat(timespec="seconds"), agent, tool,
             json.dumps(params, ensure_ascii=False), ok, ms, summary),
        )


def finish_run(run_id: int, *, answer: str, n_agents: int, n_calls: int,
               total_ms: int, usage: dict, status: str = "ok",
               error: str = "") -> None:
    with _conn() as c:
        c.execute(
            "update runs set answer=?, n_agents=?, n_calls=?, total_ms=?, "
            "prompt_tok=?, completion_tok=?, status=?, error=? where id=?",
            (answer, n_agents, n_calls, total_ms,
             usage.get("prompt_tokens"), usage.get("completion_tokens"),
             status, error[:400], run_id),
        )


def recent_runs(limit: int = 10) -> list[dict]:
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "select id, created_at, question, n_agents, n_calls, total_ms, status "
            "from runs order by id desc limit ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def actions_of(run_id: int) -> list[dict]:
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "select ts, agent, tool, ok, ms, summary from actions "
            "where run_id=? order by id", (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]
