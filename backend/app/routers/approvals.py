"""审批中心 API：反代到宿主侧 approvals_api（127.0.0.1:8770）。

审批库 `~/.dsh/agent_actions.db` **不能挂进容器**：MCP（dsh 的 stdio 子进程）
在宿主直接读写它，容器再挂 bind mount 上去写就是双写者 ——
knowledge.db 那次真损坏正是这么来的（gRPC-FUSE 锁不可靠）。

所以：库只由宿主进程碰，平台这边只转发。
宿主服务是 `agent/approvals_api.py`，launchd 常驻（com.quant.approvals）。

令牌不经这一层：approvals_api 在返回前就把 token 字段剥掉了，
这里再透传也不会带出来。
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Body, HTTPException

logger = logging.getLogger(__name__)

UPSTREAM = "http://host.docker.internal:8770"

router = APIRouter(prefix="/approvals", tags=["approvals"])

_HINT = ("审批服务没起。在项目根执行 "
         "`python3 agent/approvals_api.py`（或 `launchctl kickstart -k gui/$(id -u)/com.quant.approvals`）")


async def _call(method: str, path: str, **kw):
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=8.0) as cli:
            r = await cli.request(method, f"{UPSTREAM}{path}", **kw)
    except httpx.HTTPError as exc:
        logger.warning("approvals 上游不可达：%s", exc)
        raise HTTPException(503, _HINT) from exc
    try:
        body = r.json()
    except ValueError:
        raise HTTPException(502, f"审批服务返回了非 JSON（HTTP {r.status_code}）")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, body.get("error") or f"HTTP {r.status_code}")
    return body


@router.get("")
async def list_approvals(status: str = "", limit: int = 50):
    """审批列表。status 留空 = 全部；常用 pending（待人批）。"""
    q = f"?limit={limit}" + (f"&status={status}" if status else "")
    return await _call("GET", f"/actions{q}")


@router.get("/{action_id}")
async def get_approval(action_id: int):
    return await _call("GET", f"/actions/{action_id}")


@router.post("/{action_id}/approve")
async def approve(action_id: int, payload: dict = Body(default={})):
    """通过 → 签发一次性令牌（令牌只写进库，不回给这里）。"""
    return await _call("POST", f"/actions/{action_id}/approve",
                       json={"by": payload.get("by") or "user"})


@router.post("/{action_id}/reject")
async def reject(action_id: int, payload: dict = Body(default={})):
    """否决。理由 ≥10 字是状态机的硬要求，会进记忆。"""
    return await _call("POST", f"/actions/{action_id}/reject",
                       json={"reason": payload.get("reason") or "",
                             "by": payload.get("by") or "user"})
