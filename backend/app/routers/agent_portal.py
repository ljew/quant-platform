"""智能体门户：把 dsh web（宿主 :3080）反代到平台同域 /agent/ 下。

为什么必须同域，而不是前端直接 <a href=":3080">：
1. dsh 的登录态是 HttpOnly cookie；:8000 → :3080 是跨**端口**，浏览器里
   是两个 origin，直接跳过去要么吃 401，要么得每次带 ?token=（设计文档
   第 05 节「跨端口认证」）。
2. dsh 的 /api 还有一道 browser-trust fence，校验 Host：反代时必须让 dsh
   看到自己认识的那个 authority。所以 dsh 侧要带
   `--public-url http://localhost:8000/agent/ --trusted-host localhost:8000`。

路由分两组：
- proxy_router（根级，无前缀）：/agent/* → dsh，含 401 自动引导登录
- api_router（/api/v1/portal）：状态与入口地址，给左栏「智能体」用

反代不做任何解析改写，只做 header 过滤 + 流式转发 —— dsh 前端是自包含
bundle，路径都相对 <base href="./">，前缀剥掉后天然对得上。
"""

from __future__ import annotations

import asyncio
import logging
import re

import aiohttp
import httpx
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from app.config import BASE_DIR

logger = logging.getLogger(__name__)

# dsh 跑在宿主上，容器里访问宿主要用 host.docker.internal
DSH_UPSTREAM = "http://host.docker.internal:3080"
# dsh 每次启动都会打印带 token 的 URL，由 dsh-web.sh 落到这个文件
TOKEN_LOG = BASE_DIR / "data" / "agent_portal.log"

_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

proxy_router = APIRouter(tags=["agent-portal"])
api_router = APIRouter(prefix="/portal", tags=["agent-portal"])


def _token() -> str | None:
    """从 dsh 启动日志里读最后一次的访问令牌。"""
    try:
        text = TOKEN_LOG.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    hits = re.findall(r"\?token=([A-Za-z0-9_\-]{10,})", text)
    return hits[-1] if hits else None


def _offline_page() -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html><meta charset="utf-8">
<body style="font-family:-apple-system,system-ui,sans-serif;background:#0f1115;color:#e6e8ee;
padding:56px;line-height:1.75;max-width:720px;margin:0 auto">
<div style="font-size:13px;letter-spacing:.08em;color:#8b93a7">智能体 · 未连接</div>
<h2 style="margin:10px 0 18px;font-weight:600">dsh 没在跑</h2>
<p style="color:#a8b0c2">平台把智能体当成同一入口的一个页签，但 dsh 是独立进程，
它没起来时这里自然打不开。回终端执行：</p>
<pre style="background:#171a21;border:1px solid #262b36;border-radius:8px;padding:14px 16px;
overflow:auto;color:#c9d1e2">~/quant-platform/scripts/dsh-web.sh start</pre>
<p style="color:#8b93a7;font-size:13px">起来之后刷新这一页即可；dsh 的登录 cookie
会由本页自动种下，不用手工复制 token。</p>
</body>"""
    )


async def _proxy(request: Request, path: str) -> Response:
    # URL 必须按原始字节重建，不能走 Starlette 的 path 参数 + httpx 的 params：
    # dsh 的插件是 `plugins/??a/client.js&rev=x` 这种 combo 写法，第二个 `?`
    # 是 query 的一部分。走 params 会被转义成 %3F，dsh 找不到资源，前端只显示
    # 「Failed to load plugins: HTML did not preload client.js」（实测过）。
    raw = request.scope.get("raw_path", b"").decode("latin-1")
    sub = raw[len("/agent"):] or "/"
    if not sub.startswith("/"):
        sub = "/" + sub
    url = DSH_UPSTREAM + sub
    raw_qs = request.scope.get("query_string", b"").decode("latin-1")
    if raw_qs:
        url = f"{url}?{raw_qs}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
    # Host 必须改写成 dsh --public-url 里的 authority：
    # dsh 用它给登录 cookie 签名（payload 里带 authority），也用它做 /api 的
    # browser-trust 校验。留 host.docker.internal 也能跑，但 cookie 就绑到了
    # 容器视角的地址上，换台机器/改 upstream 就全废 —— 绑 localhost:8000 才对得上
    # 浏览器实际访问的 origin。
    headers["host"] = "localhost:8000"

    client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(30.0, read=None))
    try:
        req = client.build_request(
            request.method,
            url,
            headers=headers,
            content=request.stream(),
        )
        resp = await client.send(req, stream=True)
    except httpx.ConnectError:
        await client.aclose()
        return _offline_page()
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("dsh 反代失败：%s", exc)
        return JSONResponse({"detail": f"dsh upstream error: {exc}"}, status_code=502)

    # 401 且手上有 token：307 回同一路径带 token，让浏览器自己种 cookie。
    # 加 __booted=1 防循环（token 失效时不要无限跳）。
    if resp.status_code in (401, 403) and "token=" not in raw_qs:
        await resp.aclose()
        await client.aclose()
        token = _token()
        if token:
            sep = "&" if raw_qs else ""
            # ⚠️ 前缀要带回来：sub 是剥掉 /agent 之后的路径，直接拿它拼
            # Location 会把 /agent/ 变成 /，浏览器一跳就落到平台首页去了
            # （第一版就这么错的，表面上「反代成功」，实际打开的是自家 SPA）。
            return RedirectResponse(
                f"/agent{sub}?{raw_qs}{sep}token={token}&__booted=1", status_code=307
            )
        return _offline_page()

    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_HEADERS}

    async def body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body(), status_code=resp.status_code, headers=out_headers,
        background=None,
    )


@proxy_router.api_route("/agent", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def agent_root(request: Request) -> Response:
    return await _proxy(request, "")


@proxy_router.api_route("/agent/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def agent_any(request: Request, path: str) -> Response:
    return await _proxy(request, path)


@proxy_router.websocket("/agent/{path:path}")
@proxy_router.websocket("/agent")
async def agent_ws(websocket: WebSocket, path: str = "") -> None:
    """WebSocket 反代。dsh 的实时通道全走 WS —— 不接这个，页面能开但不会动。

    2026-11-11 实测发现的：HTTP 反代做完后，容器日志里持续出现
    `StaticFiles ... AssertionError`，那是 WS 握手落到了 `/` 的静态挂载上。
    dsh 前端确实用 WS（24 条失败记录），只补 HTTP 等于半个入口。

    Host 必须跟着 HTTP 那条一起改写成 localhost:8000：dsh 用 authority
    给 cookie 签名，WS 握手也要过同一道校验。
    """
    raw = websocket.scope.get("raw_path", b"").decode("latin-1")
    sub = raw[len("/agent"):] or "/"
    if not sub.startswith("/"):
        sub = "/" + sub
    qs = websocket.scope.get("query_string", b"").decode("latin-1")
    url = f"ws://host.docker.internal:3080{sub}" + (f"?{qs}" if qs else "")

    hdrs = {"Host": "localhost:8000"}
    cookie = websocket.headers.get("cookie")
    if cookie:
        hdrs["Cookie"] = cookie
    proto = websocket.headers.get("sec-websocket-protocol")
    if proto:
        hdrs["Sec-WebSocket-Protocol"] = proto
    sub_proto = proto.split(",")[0].strip() if proto else None

    await websocket.accept(subprotocol=sub_proto)
    session = aiohttp.ClientSession(trust_env=False)
    try:
        async with session.ws_connect(
            url, headers=hdrs, timeout=aiohttp.ClientWSTimeout(ws_close=10),
            max_msg_size=0, autoping=True,
        ) as up:
            async def c2s() -> None:
                while True:
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        return
                    if msg.get("text") is not None:
                        await up.send_str(msg["text"])
                    elif msg.get("bytes") is not None:
                        await up.send_bytes(msg["bytes"])

            async def s2c() -> None:
                async for m in up:
                    if m.type == aiohttp.WSMsgType.TEXT:
                        await websocket.send_text(m.data)
                    elif m.type == aiohttp.WSMsgType.BINARY:
                        await websocket.send_bytes(m.data)
                    else:
                        return

            _, pending = await asyncio.wait(
                {asyncio.create_task(c2s()), asyncio.create_task(s2c())},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except aiohttp.ClientError as exc:
        logger.warning("dsh WS 连不上（dsh 没起？）：%s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("dsh WS 反代中断：%s", exc)
    finally:
        await session.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass


@api_router.get("/status")
async def portal_status() -> dict:
    """左栏「智能体」的可用性探测 —— 前端每 60s 拉一次。"""
    token = _token()
    alive, detail = False, ""
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=2.5) as cli:
            r = await cli.get(f"{DSH_UPSTREAM}/")
        alive = r.status_code in (200, 303, 401)
        detail = f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        detail = str(exc) or exc.__class__.__name__
    return {
        "alive": alive,
        "detail": detail,
        "has_token": bool(token),
        "entry": "/agent/",
        "upstream": DSH_UPSTREAM,
        "hint": "" if alive else "~/quant-platform/scripts/dsh-web.sh start",
    }


@api_router.get("/boot")
async def portal_boot() -> Response:
    """首次进入的引导：带上 token 跳一次，把 HttpOnly cookie 种下。"""
    token = _token()
    if not token:
        return RedirectResponse("/agent/", status_code=303)
    return RedirectResponse(f"/agent/?token={token}&__booted=1", status_code=303)
