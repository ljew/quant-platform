"""ETF 资金流 API（国家队 ETF 资金看板 · 观察口径）。

与 `strategy.py` 里 `/nsi/*` 的分工：
- `/nsi/*` 是**信号**（5 只核心 ETF 份额，T+1 执行，national_team 策略消费）
- `/etf-flow/*` 是**观察**（20 只 / 12 类宽基 / 分钟量能，仅供人看）

两者数据源都是 tushare 的份额接口，但口径不同，**不可互相替代**。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.services import etf_flow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/etf-flow", tags=["etf-flow"])

# 看板数据组装要扫 13 万行分钟线，约 200–400ms。前端会轮询，
# 60 秒内直接复用，避免每次请求都重算。
_CACHE: dict = {"at": 0.0, "data": None}
_CACHE_TTL = 60.0
_LOCK = threading.Lock()

# 初始化/更新任务的状态（后端只跑一个实例，进程内变量足够）
_TASK: dict = {"running": False, "kind": None, "message": "", "started_at": None}


def _run_bg(kind: str, force: bool = False):
    """后台跑初始化/更新，用独立 session（请求的 session 会随响应关闭）。"""
    global _TASK
    with _LOCK:
        if _TASK["running"]:
            return
        _TASK = {"running": True, "kind": kind, "message": "已启动",
                 "started_at": time.time()}
    db = SessionLocal()
    try:
        if kind == "bootstrap":
            res = etf_flow.bootstrap(db, force=force)
        else:
            res = etf_flow.update(db)
        _CACHE.update({"at": 0.0, "data": None})     # 让下一次请求重算
        _TASK.update({"running": False, "message": res.get("message") or "完成"})
        logger.info("[etf-flow] %s 完成：%s", kind, _TASK["message"])
    except Exception as e:  # noqa: BLE001
        logger.exception("[etf-flow] %s 失败", kind)
        _TASK.update({"running": False, "message": f"失败：{e}"})
    finally:
        db.close()


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    """看板全量数据。前端 60 秒轮询一次即可，服务端有缓存。"""
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["at"] < _CACHE_TTL:
        return _CACHE["data"]
    try:
        data = etf_flow.build_dashboard(db)
    except Exception as e:  # noqa: BLE001
        logger.exception("[etf-flow] dashboard 构建失败")
        raise HTTPException(status_code=500, detail=f"看板数据构建失败：{e}")
    _CACHE.update({"at": now, "data": data})
    return data


@router.get("/status")
def status(db: Session = Depends(get_db)):
    """数据新鲜度 + 后台任务状态。用于排查「为什么页面是空的」。"""
    s = etf_flow.status(db)
    s["task"] = {k: v for k, v in _TASK.items() if k != "started_at"}
    s["task_elapsed"] = (round(time.time() - _TASK["started_at"], 1)
                         if _TASK.get("started_at") and _TASK["running"] else None)
    return s


@router.post("/bootstrap")
def bootstrap(background: BackgroundTasks, force: bool = False):
    """首次全量初始化（约 84 次接口调用、40 秒）。后台执行，立即返回。"""
    if _TASK.get("running"):
        return {"ok": False, "message": f"已有任务在跑（{_TASK['kind']}）"}
    background.add_task(_run_bg, "bootstrap", force)
    return {"ok": True, "message": "初始化已在后台启动，约 40 秒后刷新状态"}


@router.post("/update")
def update(background: BackgroundTasks):
    """日级增量更新（日线/份额/净值/分钟/分类矩阵）。"""
    if _TASK.get("running"):
        return {"ok": False, "message": f"已有任务在跑（{_TASK['kind']}）"}
    background.add_task(_run_bg, "update")
    return {"ok": True, "message": "更新已在后台启动"}


@router.post("/sample")
def sample(db: Session = Depends(get_db)):
    """手动采一次实时快照（盘中排查用）。"""
    try:
        n = etf_flow.sample_intraday(db)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"采样失败：{e}")
    _CACHE.update({"at": 0.0, "data": None})
    return {"ok": n > 0, "points": n}


@router.post("/minutes")
def pull_today_minutes(db: Session = Depends(get_db)):
    """强制拉当日官方 1min 线。

    用途：收盘后 tushare 的当日分钟线还没落地时，前端显示的是实时采样拼出来的
    碎片（只有服务启动后的那一段）。这个接口让人可以不等调度器重试、手动催一次。
    返回 0 表示源站还没发布，不是出错 —— 前端据此提示「稍后再试」即可。
    """
    try:
        codes = etf_flow.ensure_universe(db)
        n = etf_flow.update_minutes(db, codes, force=True,
                                    day=date.today().strftime("%Y-%m-%d"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"拉取分钟线失败：{e}")
    _CACHE.update({"at": 0.0, "data": None})
    return {
        "ok": n > 0,
        "symbols": n,
        "message": "官方分钟线已入库" if n else "tushare 尚未发布当日分钟线，稍后再试",
    }
