"""审批动作表：智能体唯一能碰的写入口。

设计要点（对应《智能体架构设计》第 06 / 13 节）：

  · **提议与执行分离**。agent 只能 propose，写 pending；真正执行要人等一步。
  · **令牌不经模型**。令牌只在 approve 时生成，MCP 按 action_id 内部取用；
    工具入参里没有 token —— 写成入参就可能被注入文本骗走。
  · **一次性 + 30 分钟过期**。consume 时标记 used，重复调用直接失败（幂等）。
  · **库在 ~/.dsh/**。平台容器绝不读写这个文件（knowledge.db 的教训：
    宿主进程 + 容器共写同一个 SQLite 会真损坏）。

状态机：pending → approved → done
                ↘ rejected
                ↘ expired        （pending 超时）
                ↘ failed         （执行报错，可重放）
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(os.getenv("QUANT_AGENT_DB", Path.home() / ".dsh" / "agent_actions.db"))

TERMINAL = ("done", "rejected", "expired", "failed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def now_iso() -> str:
    """给响应包用的时间戳（UTC，秒级）。"""
    return _iso(_now())


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma busy_timeout=10000")
    conn.execute(
        """
        create table if not exists agent_actions (
          id          integer primary key autoincrement,
          action      text not null,
          payload     text not null,
          reason      text,
          proposed_by text,
          status      text not null default 'pending',
          token       text,
          dedupe_key  text,
          created_at  text not null,
          expires_at  text not null,
          decided_at  text,
          decided_by  text,
          executed_at text,
          result      text,
          note        text
        )
        """
    )
    conn.execute("create index if not exists idx_status on agent_actions(status)")
    conn.execute("create index if not exists idx_dedupe on agent_actions(dedupe_key, status)")
    return conn


def _dedupe(action: str, payload: dict) -> str:
    raw = json.dumps({"a": action, "p": payload}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def propose(action: str, payload: dict, *, reason: str = "",
            proposed_by: str = "", ttl_min: int = 30) -> dict:
    """写一条待审批动作。同 action + payload 已有 pending 时直接返回它（幂等）。"""
    key = _dedupe(action, payload)
    now = _now()
    with _conn() as conn:
        row = conn.execute(
            "select * from agent_actions where dedupe_key=? and status='pending' "
            "and expires_at > ? order by id desc limit 1", (key, _iso(now))
        ).fetchone()
        if row:
            return {"action_id": row["id"], "status": "pending", "reused": True,
                    "expires_at": row["expires_at"]}
        cur = conn.execute(
            "insert into agent_actions(action,payload,reason,proposed_by,status,"
            "dedupe_key,created_at,expires_at) values(?,?,?,?,?,?,?,?)",
            (action, json.dumps(payload, ensure_ascii=False), reason, proposed_by,
             "pending", key, _iso(now), _iso(now + timedelta(minutes=ttl_min))),
        )
        aid = cur.lastrowid
    return {"action_id": aid, "status": "pending", "reused": False,
            "expires_at": _iso(now + timedelta(minutes=ttl_min))}


def get(action_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("select * from agent_actions where id=?", (action_id,)).fetchone()
    return dict(row) if row else None


def list_actions(status: str | None = None, limit: int = 50) -> list[dict]:
    sql = "select * from agent_actions"
    args: list[Any] = []
    if status:
        sql += " where status=?"
        args.append(status)
    sql += " order by id desc limit ?"
    args.append(limit)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def approve(action_id: int, by: str = "user") -> dict:
    """通过审批并签发一次性令牌。令牌只存在库里，不返回给调用方界面以外的模型。"""
    expire_stale()
    now = _now()
    with _conn() as conn:
        row = conn.execute("select * from agent_actions where id=?", (action_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"没有 id={action_id} 的动作"}
        if row["status"] != "pending":
            return {"ok": False, "error": f"状态是 {row['status']}，不能通过"}
        token = secrets.token_urlsafe(24)
        conn.execute(
            "update agent_actions set status='approved', token=?, decided_at=?, decided_by=? "
            "where id=?", (token, _iso(now), by, action_id),
        )
    return {"ok": True, "action_id": action_id, "status": "approved",
            "token_issued": True, "expires_at": row["expires_at"]}


def reject(action_id: int, reason: str, by: str = "user") -> dict:
    if len((reason or "").strip()) < 10:
        return {"ok": False, "error": "否决必须填理由（≥10 字），理由会进记忆"}
    now = _now()
    with _conn() as conn:
        row = conn.execute("select status from agent_actions where id=?", (action_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"没有 id={action_id} 的动作"}
        if row["status"] != "pending":
            return {"ok": False, "error": f"状态是 {row['status']}，不能否决"}
        conn.execute(
            "update agent_actions set status='rejected', decided_at=?, decided_by=?, note=? "
            "where id=?", (_iso(now), by, reason, action_id),
        )
    return {"ok": True, "action_id": action_id, "status": "rejected"}


def consume(action_id: int) -> dict:
    """取用令牌：必须是 approved、未过期、未使用。成功后标记 done（一次性）。"""
    expire_stale()
    with _conn() as conn:
        row = conn.execute("select * from agent_actions where id=?", (action_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"没有 id={action_id} 的动作"}
        if row["status"] != "approved":
            return {"ok": False, "error": f"状态是 {row['status']}，需要先通过审批"}
        if not row["token"]:
            return {"ok": False, "error": "没有令牌，审批可能没走正常流程"}
        conn.execute(
            "update agent_actions set status='done', executed_at=?, token=null where id=?",
            (_iso(_now()), action_id),
        )
    return {"ok": True, "action_id": action_id, "action": row["action"],
            "payload": json.loads(row["payload"])}


def mark_failed(action_id: int, error: str) -> None:
    with _conn() as conn:
        conn.execute(
            "update agent_actions set status='failed', note=?, executed_at=? where id=?",
            (str(error)[:500], _iso(_now()), action_id),
        )


def mark_done(action_id: int, result: dict) -> None:
    with _conn() as conn:
        conn.execute(
            "update agent_actions set status='done', executed_at=?, result=? where id=?",
            (_iso(_now()), json.dumps(result, ensure_ascii=False), action_id),
        )


def expire_stale() -> int:
    """pending 超时 → expired。不补跑，不重置（文档写死的）。"""
    with _conn() as conn:
        cur = conn.execute(
            "update agent_actions set status='expired' "
            "where status='pending' and expires_at <= ?", (_iso(_now()),)
        )
        return cur.rowcount
