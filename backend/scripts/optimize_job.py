"""参数寻优子进程 worker（由 POST /strategy/optimize/async 拉起）。

用法：
    python scripts/optimize_job.py <job_dir>

    job_dir/payload.json    输入：OptimizeRequest 的 dict
    job_dir/progress.json   输出：{"done": n, "total": m, "elapsed": 秒}，每跑完一组覆盖写一次
    job_dir/result.json     输出：{"ok": bool, "results": [...], "error": str|None}

为什么要单独起进程：大网格要跑几百上千次回测，放在 API 进程里（哪怕丢进后台线程）
一旦某组参数触发死循环或外部行情请求卡住，整个后端会被拖死且杀不掉那个线程。
子进程 + 硬超时是本项目的标准做法（数据调度、一键修复同款）。
"""
from __future__ import annotations

import json
import os
import sys
import time

# backend 根目录入 path，保证 `app.*` 可导入（PYTHONPATH 已设置时也不影响）
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def _atomic_write(path: str, obj: dict) -> None:
    """先写 .tmp 再原子替换：读端永远看不到写了一半的文件。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: optimize_job.py <job_dir>", file=sys.stderr)
        return 2
    job_dir = sys.argv[1]
    with open(os.path.join(job_dir, "payload.json"), encoding="utf-8") as f:
        payload = json.load(f)
    progress_path = os.path.join(job_dir, "progress.json")
    result_path = os.path.join(job_dir, "result.json")

    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.routers.strategy import _MAX_ASYNC_COMBOS, _optimize_core
    from app.schemas import OptimizeRequest

    req = OptimizeRequest(**payload)
    t0 = time.time()
    db = SessionLocal()
    out: dict
    try:

        def _progress(done: int, total: int) -> None:
            _atomic_write(progress_path,
                          {"done": done, "total": total, "elapsed": round(time.time() - t0, 1)})

        trials = _optimize_core(db, req, _MAX_ASYNC_COMBOS, progress=_progress)
        out = {"ok": True, "results": [t.model_dump() for t in trials], "error": None}
    except HTTPException as e:
        out = {"ok": False, "results": [], "error": str(e.detail)}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "results": [], "error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

    out["elapsed"] = round(time.time() - t0, 1)
    _atomic_write(result_path, out)
    print(f"OPTIMIZE_JOB done ok={out['ok']} n={len(out['results'])} {out['elapsed']}s")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
