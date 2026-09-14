"""数据管道调度器（设计 v1.0：定时数据拉取/更新）。

每交易日 19:00 后执行 datahub 管道（部分失败自动降级 etl_daily）。
由 main.startup 启动（env QUANT_DATA_SCHEDULE=1 启用，默认关——避免沙箱频繁
在线拉取；生产服务器建议开启）。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

logger = logging.getLogger("data_scheduler")

# 每个交易日 19:00 后执行一次数据管道（tushare 收盘数据就绪 + 因子计算）
RUN_HOUR, RUN_MIN = 19, 0  # tushare 日线晚间就绪，19:00 保证当日数据可得
_CHECK_INTERVAL = 60  # 秒
# 当日日线数据的发布时点（A 股 15:00 收盘，各源通常 16:00 后可得）
_DATA_READY_HOUR = 16
# 管道单次整体超时（秒）：数据源在发布临界/网络黑洞时可能长时间无进展，
# 超时即杀子进程、调度线程永不阻塞（否则 16:00 卡死会连带 19:00 定时瘫痪）。
_PIPELINE_TIMEOUT = 1500

# 运行状态（供监控页查询）
_status = {
    "enabled": False,
    "run_hour": f"{RUN_HOUR:02d}:{RUN_MIN:02d}",
    "last_run_at": None,      # 最近一次触发时间
    "last_success": None,     # 最近一次成功时间
    "last_error": None,       # 最近一次失败信息
    "runs_total": 0,
    "catch_up": False,        # 最近一次是否由「断供自愈」触发（而非 19:00 定时）
    "next_run": f"每交易日 {RUN_HOUR:02d}:{RUN_MIN:02d}，或检测到数据滞后时立即补跑",
}


def get_status() -> dict:
    """调度器状态快照（监控页用）。"""
    import copy

    return copy.deepcopy(_status)


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _is_trading_day(d: datetime) -> bool:
    """是否为 A 股交易日（周末 + 法定节假日休市）。"""
    from app.core.trading_calendar import is_trading_day as _td

    return _td(d)


def _run_update() -> bool:
    """每日数据任务。

    P5 起：优先执行 datahub 管道（extract→clean→score→mined→factor，运行记录
    入 pipeline_runs 供监控），任一步骤失败自动降级 etl_daily。

    2026-09-03 修复：管道以**子进程隔离执行 + 整体超时**。此前在主调度线程内
    同步跑 run_pipeline，数据源在发布临界（16:00 前后）可能长时间无进展（东财
    兜底链逐只重试），单步卡死 = 调度线程永久阻塞，连带当天 19:00 定时也瘫痪
    （实测卡 5.7 小时、数据断供一天）。子进程 + 超时 kill 后，无论管道怎么卡，
    调度循环都能继续，下个时点（19:00 / 次日）自动重试。
    """
    from app.database import SessionLocal

    # 本文件 …/backend/app/core/data_scheduler.py → dirname×3 = backend/
    backend = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = dict(os.environ)
    env["PYTHONPATH"] = backend
    # —— 新管道优先（子进程隔离）——
    code = (
        "from app.datahub.runner import run_pipeline;"
        "import sys;"
        "sys.stdout.flush();"
        "rid = run_pipeline('scheduler');"
        "print('PIPELINE_RID', rid, flush=True)"
    )
    rid = None
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env, cwd=backend, capture_output=True, text=True,
            timeout=_PIPELINE_TIMEOUT,
        )
        import re

        m = re.search(r"PIPELINE_RID\s+(\d+)", proc.stdout or "")
        rid = int(m.group(1)) if m else None
    except subprocess.TimeoutExpired:
        logger.error("datahub 管道超过 %ss 未完成，已终止子进程（调度线程不受影响）",
                     _PIPELINE_TIMEOUT)
        _status["last_error"] = f"pipeline timeout >{_PIPELINE_TIMEOUT}s（子进程已终止）"
        _mark_zombie_runs_failed()
        return False
    except Exception as e:  # noqa: BLE001
        logger.error("datahub 管道启动异常(%s)，降级 etl_daily: %s", type(e).__name__, e)
        _status["last_error"] = f"pipeline launch: {str(e)[:200]}"
        rid = None

    if rid is not None:
        from app.models import PipelineRun

        with SessionLocal() as db:
            st = db.get(PipelineRun, rid)
        if st is None:
            logger.warning("datahub 管道 run_id=%s 未查到记录（子进程可能异常退出）", rid)
            _status["last_error"] = f"pipeline run {rid} 无记录"
        elif st.status == "SUCCESS":
            logger.info("datahub 管道完成 run_id=%s", rid)
            _status["last_success"] = datetime.now().isoformat(timespec="seconds")
            return True
        else:
            err = st.error or f"pipeline status={st.status}"
            logger.warning("datahub 管道未完全成功(run_id=%s)，降级 etl_daily: %s",
                           rid, str(err)[:200])
            _status["last_error"] = f"pipeline fallback: {str(err)[:200]}"
    # 子进程整体失败/无记录 → 降级 etl_daily 兜底
    script = os.path.join(backend, "scripts", "etl_daily.py")
    try:
        proc = subprocess.run(
            [sys.executable, script],
            env=env,
            cwd=backend,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        if proc.returncode == 0:
            logger.info("ETL 日更成功:\n%s", proc.stdout[-1200:])
            _status["last_success"] = datetime.now().isoformat(timespec="seconds")
            return True
        logger.error("ETL 日更失败 rc=%s:\n%s", proc.returncode, proc.stderr[-1200:])
        _status["last_error"] = f"rc={proc.returncode}: {proc.stderr[-300:]}"
        return False
    except Exception as e:  # noqa: BLE001
        logger.error("ETL 日更异常: %s", e)
        _status["last_error"] = str(e)[:300]
        return False


def _mark_zombie_runs_failed() -> None:
    """把超时遗留的 RUNNING 管道 run 标为 FAILED（子进程已被杀，不会自行收尾）。"""
    try:
        from sqlalchemy import text

        from app.database import SessionLocal

        with SessionLocal() as db:
            db.execute(text(
                "UPDATE pipeline_runs SET status='FAILED',"
                " error=COALESCE(error||' | ','')||'管道超时子进程终止',"
                " finished_at=COALESCE(finished_at, started_at)"
                " WHERE status='RUNNING'"
            ))
            db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("超时僵尸 run 清理失败: %s", e)


def _expected_complete_day(now: datetime):
    """数据理应更新到的最后一个「完整」交易日（16:00 前视为上一交易日）。"""
    from datetime import timedelta

    from app.core.trading_calendar import is_trading_day

    d = now.date() if now.hour >= _DATA_READY_HOUR else now.date() - timedelta(days=1)
    for _ in range(10):
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    return None


def _is_stale(now: datetime) -> bool:
    """最后一个完整交易日的个股日K是否缺失 —— 断供自愈的判据。

    只按「19:00 定时触发」有个致命缺陷：调度器一旦没开、或当晚跑挂了，
    数据就静默地一天天落后，而监控页要等老刘自己打开才看得见。
    因此这里额外做滞后探测：只要发现该有的数据没到，就立刻补跑一次（每天最多一次）。
    """
    from sqlalchemy import func, select

    exp = _expected_complete_day(now)
    if exp is None:
        return False
    try:
        from app.database import SessionLocal
        from app.models import KlineDaily

        with SessionLocal() as db:
            last = db.execute(select(func.max(KlineDaily.trade_date))).scalar()
    except Exception as e:  # noqa: BLE001
        logger.warning("滞后探测失败: %s", e)
        return False
    return last is None or last < exp


def _refresh_membership_if_due(now: datetime) -> bool:
    """指数成分（PIT）月度快照自动刷新 —— 快照滞后于「上月最后一天」才拉。

    背景：组合回测的 point-in-time 选股池依赖 `index_membership` 月度快照，但它
    之前只在「回测时按需补拉」（tushare index_weight 现已无权限，靠中证官网 csindex
    免费源兜底，只能取最新一期）。若不主动维护，平时不跑回测就一直停在旧月份，
    等到回测时才发现成分已滞后一个月。

    为什么用「上月最后一天」作基准（而不是当月 1 日）：月末调仓快照（如 10-31）
    是在**次月**才从 csindex 可拉（且发布日若逢周末还要顺延）。若只查「当月是否有
    快照」，11 月初巡检时 10-31 快照尚未发布会误判为不缺，等它发布后快照日期又
    落在上个月、永远进不了「当月窗口」→ 形成 10 月快照永久缺失的空窗。

    本函数挂在每日数据任务成功之后：幂等（快照够新即跳过）、低频（6 个核心指数，
    每月实际 ~6 次在线调用）、失败不影响主流程。拉取成功后同步 DuckDB 该表。
    """
    from datetime import timedelta

    from sqlalchemy import func, select

    from app.database import SessionLocal
    from app.models import IndexMembership

    try:
        from app.services.etl import CORE_INDEXES
        from app.services.membership_store import get_membership
    except Exception as e:  # noqa: BLE001
        logger.debug("成分刷新依赖不可用: %s", e)
        return False

    expected = (now.date().replace(day=1) - timedelta(days=1))  # 上月最后一天
    # 巡检区间从上上月首日起，保证上月末/本月的快照都落得进 csindex 的区间校验
    start = (now.date() - timedelta(days=75)).replace(day=1)
    try:
        with SessionLocal() as db:
            rows = db.execute(
                select(IndexMembership.index_code, func.max(IndexMembership.trade_date))
                .where(IndexMembership.index_code.in_(CORE_INDEXES))
                .group_by(IndexMembership.index_code)
            ).all()
            latest = {code: d for code, d in rows}
            todo = [c for c in CORE_INDEXES
                    if latest.get(c) is None or latest[c] < expected]
            if not todo:
                return False
            refreshed: list[str] = []
            for code in todo:
                try:
                    got = get_membership(db, code, start, now.date())
                    if any(d >= expected.isoformat() for d, _ in got):
                        refreshed.append(code)
                except Exception as e:  # noqa: BLE001
                    db.rollback()
                    logger.warning("成分刷新失败 %s: %s", code, str(e)[:150])
            if not refreshed:
                return False
    except Exception as e:  # noqa: BLE001
        logger.warning("成分快照巡检失败: %s", e)
        return False

    logger.info("指数成分(PIT)月度快照已刷新: %s", refreshed)
    try:
        from app.services import duckdb_sync

        duckdb_sync.sync_all(only=["index_membership"], verbose=False)
    except Exception as e:  # noqa: BLE001
        logger.warning("成分快照同步 DuckDB 失败: %s", e)
    return True


def _loop(enabled: bool) -> None:
    last_run_date = ""
    while True:
        try:
            now = datetime.now()
            if enabled:
                # 定时触发（交易日 19:00 后）或断供自愈（只要该有的数据没到就补跑）
                due = (_is_trading_day(now)
                       and (now.hour, now.minute) >= (RUN_HOUR, RUN_MIN))
                stale = _is_stale(now)
                if (due or stale) and last_run_date != _today_str():
                    _status["catch_up"] = bool(stale and not due)
                    logger.info("触发每日数据更新 %s（%s）", _today_str(),
                                "断供自愈" if (stale and not due) else "定时")
                    _status["last_run_at"] = now.isoformat(timespec="seconds")
                    _status["runs_total"] += 1
                    _run_update()  # 数据日更（失败已内部降级并记录）
                    # 指数成分(PIT)月度快照：当日更成功后巡检当月是否缺快照（幂等，月频）
                    try:
                        _refresh_membership_if_due(now)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("成分快照巡检异常: %s", e)
                    last_run_date = _today_str()
        except Exception as e:  # noqa: BLE001
            logger.error("数据调度循环异常: %s", e)
        time.sleep(_CHECK_INTERVAL)


def start_data_scheduler(enabled: bool | None = None) -> threading.Thread:
    """启动调度 daemon 线程。enabled 缺省读 env QUANT_DATA_SCHEDULE。"""
    if enabled is None:
        enabled = os.getenv("QUANT_DATA_SCHEDULE", "0") == "1"
    _status["enabled"] = enabled
    t = threading.Thread(target=_loop, args=(enabled,), daemon=True, name="data-scheduler")
    t.start()
    logger.info("数据调度器已启动（enabled=%s，每交易日 %02d:%02d 后自动日更）", enabled, RUN_HOUR, RUN_MIN)
    return t
