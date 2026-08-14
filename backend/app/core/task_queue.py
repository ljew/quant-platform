"""轻量任务队列（完整版架构：设计 v1.0 目标 Celery + Redis）。

单机/单进程阶段用线程池实现，提供与 Celery 等价的语义：
    submit() 提交后台任务 → 立即返回 task_id
    get()    查询状态（running/done/error + 进度 + 结果 id）

生产如需多进程/持久化，可替换为 Celery+Redis，接口层不变。
"""
from __future__ import annotations

import inspect
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="taskq")
_tasks: dict[str, dict] = {}
_lock = threading.RLock()

# 任务历史保留上限（防内存无限增长）
_MAX_TASKS = 200


def submit(name: str, fn: Callable, *args, **kwargs) -> str:
    """提交后台任务，立即返回 task_id。fn 返回值存入 result_id。

    若 fn 声明了参数 `_task_id`，将自动注入本次任务的 task_id（供进度上报）。
    """
    tid = uuid.uuid4().hex[:12]
    with _lock:
        _tasks[tid] = {
            "id": tid,
            "name": name,
            "status": "running",
            "progress": 0.0,
            "message": "",
            "result_id": None,
            "error": None,
        }
        # 清理最旧任务
        if len(_tasks) > _MAX_TASKS:
            for k in list(_tasks.keys())[: len(_tasks) - _MAX_TASKS]:
                _tasks.pop(k, None)

    def _run() -> None:
        try:
            kw = dict(kwargs)
            if "_task_id" in inspect.signature(fn).parameters:
                kw["_task_id"] = tid
            rid = fn(*args, **kw)
            with _lock:
                t = _tasks[tid]
                t["status"] = "done"
                t["progress"] = 1.0
                t["result_id"] = rid
        except Exception as e:  # noqa: BLE001
            with _lock:
                t = _tasks[tid]
                t["status"] = "error"
                t["error"] = str(e)
                t["traceback"] = traceback.format_exc()[-1500:]

    _executor.submit(_run)
    return tid


def get(tid: str) -> Optional[dict]:
    with _lock:
        t = _tasks.get(tid)
        return dict(t) if t else None


def update_progress(tid: str, progress: float, message: str = "") -> None:
    with _lock:
        t = _tasks.get(tid)
        if t:
            t["progress"] = round(progress, 4)
            if message:
                t["message"] = message


def list_tasks(limit: int = 20) -> list[dict]:
    with _lock:
        items = sorted(_tasks.values(), key=lambda x: x["id"], reverse=True)
        return [dict(t) for t in items[:limit]]
