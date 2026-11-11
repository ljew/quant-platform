"""审批中心的宿主侧 HTTP 口 —— 把 agent_actions.db 暴露给平台后端。

为什么要有这个进程，而不是让容器直接读那个库：
`~/.dsh/agent_actions.db` 在宿主本地磁盘上，MCP（dsh 的 stdio 子进程）直接读写它。
如果容器也挂 bind mount 上去写，就是「宿主 + 容器共写同一个 SQLite」——
knowledge.db 那次真损坏就是这么来的（gRPC-FUSE 的锁不可靠）。
所以收敛成单一真相：库只由宿主进程碰，平台通过 HTTP 说话。

MCP 和这个服务都在宿主的本地磁盘上，SQLite 的 WAL + busy_timeout(10s)
足以串行化两个写者，不会再出现容器那一层的问题。

安全：
· 只绑 127.0.0.1，不对外；平台后端在容器里，经 host.docker.internal 进来。
· **token 一律不出现**：列表和详情都剥掉 `token` 字段。令牌只在 approve 时
  写进库，由 MCP 内部按 action_id 取用（设计文档「令牌不经模型」）。

用法：
    python3 agent/approvals_api.py            # 前台跑，默认 127.0.0.1:8770
    python3 agent/approvals_api.py --selftest # 自检，不绑端口
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import actions  # noqa: E402

HOST = "127.0.0.1"
PORT = 8770

# 绝不外流的字段。token 是「动作级授权」，一旦能被界面读到，
# 就等于把「人批」这道闸门的钥匙挂在了门上。
_SECRET_FIELDS = {"token"}


def _sanitize(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {k: v for k, v in row.items() if k not in _SECRET_FIELDS}


def _shape(row: dict) -> dict:
    """给前端看的形状：payload 解析成对象，状态与时间原样。"""
    try:
        payload = json.loads(row.get("payload") or "{}")
    except ValueError:
        payload = {}
    return {
        "id": row["id"],
        "action": row["action"],
        "payload": payload,
        "reason": row.get("reason") or "",
        "proposed_by": row.get("proposed_by") or "",
        "status": row["status"],
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
        "decided_at": row.get("decided_at"),
        "decided_by": row.get("decided_by"),
        "executed_at": row.get("executed_at"),
        "note": row.get("note") or "",
        "has_token": bool(row.get("token")),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 不刷屏：只在出错时留痕
        if str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # —— 工具 ——
    def _json(self, code: int, body) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    # —— 路由 ——
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        if u.path == "/health":
            return self._json(200, {"ok": True, "db": str(actions.DB_PATH)})
        if parts[:1] == ["actions"]:
            if len(parts) == 1:
                q = parse_qs(u.query)
                status = (q.get("status") or [""])[0] or None
                limit = int((q.get("limit") or ["50"])[0])
                try:
                    rows = actions.list_actions(status=status, limit=max(1, min(limit, 200)))
                except Exception as exc:  # noqa: BLE001
                    return self._json(500, {"ok": False, "error": str(exc)})
                return self._json(200, {
                    "ok": True,
                    "count": len(rows),
                    "items": [_shape(_sanitize(r)) for r in rows],
                })
            if len(parts) == 2 and parts[1].isdigit():
                row = _sanitize(actions.get(int(parts[1])))
                if not row:
                    return self._json(404, {"ok": False, "error": "没有这个动作"})
                return self._json(200, {"ok": True, "item": _shape(row)})
        return self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        if len(parts) != 3 or parts[0] != "actions" or not parts[1].isdigit():
            return self._json(404, {"ok": False, "error": "not found"})
        aid, verb = int(parts[1]), parts[2]
        body = self._body()
        if verb == "approve":
            r = actions.approve(aid, by=body.get("by") or "user")
        elif verb == "reject":
            r = actions.reject(aid, body.get("reason") or "", by=body.get("by") or "user")
        else:
            return self._json(404, {"ok": False, "error": "not found"})
        return self._json(200 if r.get("ok") else 400, r)

    def do_DELETE(self):  # noqa: N802
        return self._json(405, {"ok": False, "error": "审批记录不可删除，只能否决"})


def selftest() -> int:
    """不绑端口，直接跑一遍状态机 —— 部署前确认库能开、表齐全。"""
    actions.expire_stale()
    rows = actions.list_actions(limit=5)
    leaks = [r["id"] for r in rows if "token" in _sanitize(r)]
    print(f"db={actions.DB_PATH}")
    print(f"可读：{len(rows)} 条；token 泄漏：{leaks or '无'}")
    return 0 if not leaks else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    srv = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"approvals api on http://{HOST}:{args.port}  db={actions.DB_PATH}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
