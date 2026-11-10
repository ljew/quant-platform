"""模拟盘任务的子进程入口（调度器与手动触发共用）。

`run_in_subprocess` 是唯一推荐入口：它负责起进程、等结果、超时杀、把
stdout 的一行 JSON 解析回来。调用方只关心 (ok, info)。

⚠️ 不要把 `run_paper_task` 直接在主进程调用 —— xq 系策略会 OOM 拖死容器
（详见 scripts/paper_job.py 的说明）。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("quant.paper")

# 容器化铁律：后端根用 parents[2]（services → app → backend）
BACKEND_DIR = Path(__file__).resolve().parents[2]

# 组合要重算多长区间，单标的只跑当日，给不同的额度
TIMEOUT_PORTFOLIO = 900
TIMEOUT_SINGLE = 180


def run_in_subprocess(task_id: int, kind: str = "portfolio") -> tuple[bool, dict]:
    """在子进程里跑一次任务。返回 (ok, info)，**不抛异常**。"""
    timeout = TIMEOUT_PORTFOLIO if kind == "portfolio" else TIMEOUT_SINGLE
    env = {**os.environ}
    env["PYTHONPATH"] = str(BACKEND_DIR)
    # 子进程不要继承代理：它只连本机 SQLite / DuckDB，走代理反而报错
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)

    try:
        proc = subprocess.Popen(
            [sys.executable, "scripts/paper_job.py", str(task_id)],
            cwd=str(BACKEND_DIR), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except Exception as e:  # noqa: BLE001
        return False, {"error": f"起子进程失败：{e}"}

    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        logger.warning("模拟盘 #%s 子进程超时 %ss，已终止", task_id, timeout)
        return False, {"error": f"超过 {timeout}s 未完成，已终止"}

    line = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
    info: dict = {}
    if line:
        try:
            info = json.loads(line)
        except json.JSONDecodeError:
            info = {"raw": line[:300]}

    if proc.returncode != 0:
        info.setdefault("error", (err or "")[-400:] or f"exit {proc.returncode}")
        return False, info
    return True, info
