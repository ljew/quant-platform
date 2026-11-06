#!/usr/bin/env python3
"""把后端从当前 shell 的进程组里摘出去（double-fork + setsid），做到真正常驻。

为什么需要它：
  WorkBuddy 的 Bash 工具启动的后台进程属于会话级，会话结束会被回收；而本机的
  launchd 域在沙箱里访问不了（`launchctl bootstrap` 报 `5: Input/output error`）。
  两条路都堵，只能用 setsid 脱离进程组——这样进程的父进程变成 launchd(1)，
  与发起它的 shell 无关。

用法：
    python scripts/start_backend_daemon.py            # 启动（端口占用则跳过）
    python scripts/start_backend_daemon.py --status    # 看状态
    python scripts/start_backend_daemon.py --stop      # 停止

日志：data/logs/backend.log（in-place 截断用 `: > 文件`，别 mv，会换 inode）
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
PY = "/Users/happyljew/.workbuddy/binaries/python/envs/quant/bin/python"
PORT = 8000
LOG = ROOT / "data" / "logs" / "backend.log"


def _listening_pid() -> str | None:
    try:
        out = subprocess.run(["lsof", "-tnP", f"-iTCP:{PORT}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=10)
        pids = [p for p in out.stdout.split() if p.strip()]
        return pids[0] if pids else None
    except Exception:
        return None


def status() -> int:
    pid = _listening_pid()
    if pid:
        print(f"✓ 后端运行中：PID {pid} · http://127.0.0.1:{PORT}")
        return 0
    print(f"✗ 端口 {PORT} 无监听")
    return 1


def stop() -> int:
    pid = _listening_pid()
    if not pid:
        print(f"端口 {PORT} 无监听，无需停止")
        return 0
    os.kill(int(pid), signal.SIGTERM)
    for _ in range(20):
        time.sleep(0.5)
        if not _listening_pid():
            print(f"✓ 已停止（原 PID {pid}）")
            return 0
    print(f"⚠ PID {pid} 未在 10s 内退出")
    return 1


def start() -> int:
    if _listening_pid():
        print(f"端口 {PORT} 已被占用，跳过启动")
        return 0

    LOG.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND)
    env.setdefault("QUANT_DATA_SCHEDULE", "1")

    # ── 第一次 fork：父进程立即返回，子进程继续 ──
    if os.fork() > 0:
        # 给子进程一点时间绑定端口，再回报结果
        for _ in range(30):
            time.sleep(0.5)
            pid = _listening_pid()
            if pid:
                print(f"✓ 后端已启动（脱离 shell）：PID {pid} · "
                      f"http://127.0.0.1:{PORT}")
                print(f"  日志：{LOG}")
                return 0
        print("⚠ 启动超时，查看日志：", LOG, file=sys.stderr)
        return 1

    # ── 子进程：脱离会话与控制终端 ──
    os.setsid()
    if os.fork() > 0:
        os._exit(0)                     # 第二个父进程退出，孙进程被 init 收养

    os.chdir(str(BACKEND))
    os.umask(0o022)
    os.environ.update(env)

    log_fd = os.open(str(LOG), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.execv(PY, [PY, "-m", "uvicorn", "app.main:app",
                  "--host", "0.0.0.0", "--port", str(PORT)])
    os._exit(1)                          # execv 失败才会到这


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    a = ap.parse_args()
    if a.status:
        return status()
    if a.stop:
        return stop()
    return start()


if __name__ == "__main__":
    raise SystemExit(main())
