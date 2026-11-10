"""在独立子进程里跑一个模拟盘任务。

为什么必须隔离：xq 系策略的池子是 `signal_union`（全部历史命中标的的并集），
模拟盘要一次性加载全市场面板。实测在**主进程**跑会把 5.5G 的容器直接 OOM
（exit 137，整个后端被杀，所有页面 502）。回测能跑是因为它本来就走子进程。

隔离后行为变化：
  · 子进程 OOM → 容器活着，任务记 failed，下次重试
  · 子进程有自己的内存上限（默认 4.5G），超了先死自己
  · 主进程只负责调度与记账，不参与重算

用法（由 paper_runner 调用，不要手动跑）：
    PYTHONPATH=backend python3 scripts/paper_job.py <task_id>
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time


def _limit_memory() -> None:
    """给子进程设地址空间上限。容器 5.5G，留 1G 给主进程与 SQLite 缓存。"""
    try:
        gb = float(os.getenv("PAPER_MEM_LIMIT_GB", "4.5"))
    except ValueError:
        gb = 4.5
    limit = int(gb * 1024 ** 3)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        pass  # 平台不支持就跳过，别让任务因为设限失败而起不来


def main(task_id: int) -> int:
    _limit_memory()

    from app.database import SessionLocal
    from app.models import PaperTask
    from app.core.engine.paper_engine import run_paper_task

    db = SessionLocal()
    t0 = time.time()
    try:
        task = db.get(PaperTask, task_id)
        if task is None:
            print(json.dumps({"ok": False, "task_id": task_id,
                              "error": f"任务 {task_id} 不存在"}))
            return 1
        run_paper_task(db, task)
        db.refresh(task)
        print(json.dumps({
            "ok": True, "task_id": task_id,
            "secs": round(time.time() - t0, 1),
            "strategy_key": task.strategy_key,
            "last_bar_date": str(task.last_bar_date or ""),
            "last_run_at": str(task.last_run_at or "")[:19],
            "error_msg": (task.error_msg or "")[:200],
        }, ensure_ascii=False))
        return 0
    except MemoryError:
        print(json.dumps({"ok": False, "task_id": task_id,
                          "error": "内存超限（MemoryError）—— 这个策略的池子太大"}))
        return 2
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "task_id": task_id,
                          "error": f"{type(e).__name__}: {str(e)[:300]}"},
                         ensure_ascii=False))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print(json.dumps({"ok": False, "error": "用法：paper_job.py <task_id>"}))
        sys.exit(2)
    sys.exit(main(int(sys.argv[1])))
