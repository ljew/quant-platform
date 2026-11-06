"""子进程执行工具：带硬超时，超时杀**整个进程组**（含孙进程）。

为什么不能直接用 `subprocess.run(timeout=...)`
--------------------------------------------
它只对**直接子进程**发 kill。若子进程又派生了孙进程（datahub 各步骤、
akshare 内部请求、`/bin/sh -c "ulimit ...; exec python ..."` 里的 python 等），
且孙进程继承了 stdout/stderr 管道，CPython 的 `communicate()` 会一直读管道
直到 EOF —— 即使直接子进程已经被杀，只要有孙进程还持有写端，调用方就永久阻塞。

2026-11-05 实证：编排 #305 的 data 步骤 `DATA_STEP_TIMEOUT=1800`，
事件里却是「数据管道完成（72699s）」= 20.2 小时，超时形同虚设；
当晚 19:00 的 ETL、20:05 的策略验证全部被拖住，第二天才发现。

做法：`start_new_session=True` 让子进程自成一个进程组，超时后
`os.killpg(..., SIGKILL)` 一次性带走整组；随后最多再等 10 秒收尾输出，
仍拿不到就放弃（绝不允许二次阻塞）。
"""
from __future__ import annotations

import os
import signal
import subprocess


def run_with_timeout(cmd, timeout, cwd=None, env=None) -> subprocess.CompletedProcess:
    """执行 cmd（list 或 shell 字符串），超过 timeout 秒杀掉整个进程组。

    返回 CompletedProcess（stdout/stderr 已解码为 str）。
    超时抛 subprocess.TimeoutExpired，异常里带上已读到的输出，便于排障。
    """
    if isinstance(cmd, str):
        cmd = ["/bin/sh", "-c", cmd]

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,   # 独立进程组：killpg 才能带走孙进程
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            # 仍有进程持有管道：放弃等待，绝不二次阻塞
            out, err = "", ""
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL 整个进程组；进程已退出或拿不到 pgid 时退回单进程 kill。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except ProcessLookupError:
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
