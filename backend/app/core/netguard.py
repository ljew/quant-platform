"""网络就绪探测：断网时不要消耗重试机会。

## 为什么需要

Mac 进入维护睡眠（`standby` / `powernap`）后网络会断，唤醒瞬间 DNS 也往往还没就绪。
此时任何联网任务都会以 `Name or service not known` 失败。

2026-11-07 早上的教训：08:24 和 08:53 两次 DNS 失败就耗光了公众号采集当天的 3 次机会，
09:04 第三次撞上 Cookie 过期，当天 0 篇入库 —— 而实际上 09:10 网络早就好了。

所以联网任务启动前先探一下：
- 未就绪 → **不计入重试次数**，等下一轮探测（网络恢复后立刻跑）
- 就绪   → 正常执行，失败才计数

探测只做 DNS 解析 + TCP 建连（不发 HTTP），不会被对方计入请求/触发限流。
"""
from __future__ import annotations

import logging
import socket

logger = logging.getLogger("netguard")

# 常用探测目标。传自己实际要访问的域名，才能真实反映「这个任务现在能不能跑」。
WEREAD = ("weread.qq.com", 443)
TUSHARE = ("api.tushare.pro", 443)


def network_ready(
    host: str = WEREAD[0],
    port: int = WEREAD[1],
    timeout: float = 6.0,
) -> bool:
    """DNS 能解析且 TCP 能建连 → True。任何异常都当「没准备好」。"""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        if not infos:
            return False
        family, socktype, proto, _canon, sockaddr = infos[0]
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
            return True
        finally:
            sock.close()
    except Exception:  # noqa: BLE001
        return False


def explain(host: str = WEREAD[0], port: int = WEREAD[1]) -> str:
    """给日志用的一句话说明当前卡在哪一步（DNS 还是连不通）。"""
    try:
        socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception as e:  # noqa: BLE001
        return f"DNS 解析失败（{type(e).__name__}）"
    if not network_ready(host, port):
        return "DNS 正常但连不上目标端口（可能未完全唤醒）"
    return "网络正常"
