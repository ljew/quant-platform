"""数据管道调度器（设计 v1.0：定时数据拉取/更新）。

每交易日 19:00 后执行 datahub 管道（部分失败自动降级 etl_daily）。
由 main.startup 启动（env QUANT_DATA_SCHEDULE=1 启用，默认关——避免沙箱频繁
在线拉取；生产服务器建议开启）。
"""
from __future__ import annotations

import logging
import os
import subprocess

from app.core.subproc import run_with_timeout
import sys
import threading
import time
from datetime import datetime

logger = logging.getLogger("data_scheduler")

# 每个交易日 19:00 后执行一次数据管道（tushare 收盘数据就绪 + 因子计算）
RUN_HOUR, RUN_MIN = 19, 0  # tushare 日线晚间就绪，19:00 保证当日数据可得
_CHECK_INTERVAL = 60  # 秒
# 失败重试策略：不要把「瞬时故障」升级成「当天不再跑」。
# 2026-09~11 这个模式在采集/编排/验证上各踩过：记账写在成功之前 = 一次抖动吃掉一整天。
_MAX_RETRY = 3        # 同一任务每天最多重试 3 次
_RETRY_GAP = 900      # 重试最小间隔 15 分钟（ETL 要打 tushare，不能每 60s 重来）
# 当日日线数据的发布时点（A 股 15:00 收盘，各源通常 16:00 后可得）
_DATA_READY_HOUR = 16
# 管道单次整体超时（秒）：数据源在发布临界/网络黑洞时可能长时间无进展，
# 超时即杀子进程、调度线程永不阻塞（否则 16:00 卡死会连带 19:00 定时瘫痪）。
_PIPELINE_TIMEOUT = 1500

# —— 策略验证（移植/JK 系列一条流水线）——
# 每天数据管道跑完后自动复跑一遍策略验证，与聚宽实测数字带容差对照，
# 结果写入 data/reports/strategy_verify_<key>.html|json，指标漂移第一时间可见。
# 策略验证时刻。原 20:30 与 20:47 的自动挖矿只隔 17 分钟：
# 验证子进程要 4.5G，挖矿（factor_gp）也要几个 G，两者叠在 5.5G 的容器上限上
# 就是必然 OOM（2026-11-02 的连续 OOM 里就有这一层）。提前到 20:05，
# 与 ETL(19:00 起，通常 20~40 分钟)、挖矿(20:47)、采集(20:40) 都错开。
VERIFY_HOUR, VERIFY_MIN = 20, 5
_VERIFY_TIMEOUT = 1800
# 验证子进程的地址空间上限（KB）。容器整体 mem_limit=5.5g，后端自身常驻约 0.5g。
# 实测 jk001 全段回测在 3g 下会 MemoryError（13 秒就退出），4.5g 正常跑完
# （收益 +99.53% / 夏普 0.550 / 回撤 -34.55%）。超限时子进程抛 MemoryError 退出，
# 不再让内核 SIGKILL 把整个容器拖死。
_VERIFY_MEM_LIMIT_KB = int(os.getenv("QUANT_VERIFY_MEM_KB", str(4608 * 1024)))
_VERIFY_STRATEGIES = os.getenv("QUANT_VERIFY_STRATEGIES", "jk001")
# 默认只验 jk001：jk002（全A池 5200 只）在 4.5g 地址空间下仍会 MemoryError，
# 而它 2026-09-16 已判定「扩池失败、建议停用」（样本外 -21.17pp），
# 每晚为它吃掉几个 G 不值得。要验就显式设 QUANT_VERIFY_STRATEGIES=jk001,jk002，
# 同时调大 mem_limit 与 QUANT_VERIFY_MEM_KB。

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
    "verify": {               # 策略验证子任务
        "enabled": True,
        "strategies": _VERIFY_STRATEGIES,
        "run_hour": f"{VERIFY_HOUR:02d}:{VERIFY_MIN:02d}",
        "last_run_at": None,
        "last_success": None,
        "last_error": None,
        "reports": [],
    },
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
        proc = run_with_timeout(
            [sys.executable, "-c", code],
            env=env, cwd=backend,
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
        proc = run_with_timeout(
            [sys.executable, script],
            env=env,
            cwd=backend,
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


_VAL_LAST_TRY = 0.0


def _refresh_valuation_if_due(now: datetime) -> bool:
    """全市场估值快照补齐（源表 `stock_valuation_daily` + 聚合表 `market_valuation_daily`）。

    背景：这两张表之前只有 2026-10-08 的一次手工回填，之后无人维护，到 11-06
    已落后 21 个交易日。ETF 资金流看板的「估值分位」依赖它们，不补就一直显示
    旧分位 —— 而且**不会报任何错**，静默失真最难发现。

    幂等：源表与聚合表都够新时直接返回。19:30 之后才动，避开 ETL 抢 tushare 配额
    （估值是 daily_basic 按日全市场，一天一次调用，成本很低）。
    失败只记日志、按 _RETRY_GAP 重试，不影响主流程。
    """
    global _VAL_LAST_TRY
    if (now.hour, now.minute) < (19, 30):
        return False
    if now.timestamp() - _VAL_LAST_TRY < _RETRY_GAP:
        return False

    from sqlalchemy import func, select

    from app.database import SessionLocal
    from app.models import MarketValuationDaily, StockValuationDaily
    from app.services import market_valuation as mv

    try:
        with SessionLocal() as db:
            src = db.scalar(select(func.max(StockValuationDaily.trade_date)))
            agg = db.scalar(select(func.max(MarketValuationDaily.trade_date)))
            if src is not None and agg is not None and agg >= src and not mv.missing_days(db):
                return False          # 都齐了，什么都不做
            _VAL_LAST_TRY = now.timestamp()
            added = mv.update(db)
            days = mv.rebuild(db)
    except Exception as e:  # noqa: BLE001
        _VAL_LAST_TRY = now.timestamp()
        logger.warning("估值快照刷新失败: %s", str(e)[:200])
        return False

    logger.info("估值快照已刷新：源表 +%d 行，聚合 %d 个交易日", added, days)
    return True


_FIN_LAST_TRY = 0.0
_FIN_GAP = 6 * 3600        # 财报接口 6 小时冷却（一个季度实际只需 1 次调用）


def _refresh_financials_if_due(now: datetime) -> bool:
    """财务三表按报告期增量更新。

    背景：`financials_raw` 原本只有 2026-10-08 那次从旧研究缓存的一次性导入，
    **没有任何增量更新路径**。结果是 11-06 时最新只到 2026-06-30 期，
    2026Q3（9-30 期）一行没有 —— 三季报 10-31 就该披露完。

    与估值表那次静默滞后同一类问题：不报错，只有人肉翻日期才会发现。
    逻辑见 `services/financials.py`：按法定披露截止日算「本应已有」的期次，
    落后就拉，截止日后 45 天内允许重拉以捞晚披露与更正。
    """
    global _FIN_LAST_TRY
    if now.timestamp() - _FIN_LAST_TRY < _FIN_GAP:
        return False

    from app.database import SessionLocal
    from app.services import financials as fin

    try:
        with SessionLocal() as db:
            todo = fin.periods_to_fetch(db)
            if not todo:
                return False          # 已是最新且不在重拉窗口内
            _FIN_LAST_TRY = now.timestamp()
            n = fin.update(db)
    except Exception as e:  # noqa: BLE001
        _FIN_LAST_TRY = now.timestamp()
        logger.warning("财报快照刷新失败: %s", str(e)[:200])
        return False

    logger.info("财报已更新：%s，共 %d 行", [str(p) for p in todo], n)
    return True


def _run_verify(strategies: str) -> tuple[bool, str]:
    """自动策略验证（子进程隔离 + 硬超时 + 内存上限）。

    必须在子进程里跑：回测是一次几十秒～几分钟的全市场计算，放在调度线程内
    会让整个调度循环停摆；且失败不影响日更结果，独立记录。

    内存上限用 `ulimit -v` 包一层，而不是 preexec_fn —— 调度跑在后台线程里，
    preexec_fn 在 fork 后、exec 前执行，此时若其他线程持有锁（logging 等）会死锁。
    超过上限时子进程自己抛 MemoryError 退出，父进程只看到 rc!=0；
    不再让内核 SIGKILL（rc=-9）把整个容器/VM 拖死（2026-11-02 实测过）。
    """
    backend = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = dict(os.environ)
    env["PYTHONPATH"] = backend
    inner = " ".join([sys.executable, "scripts/verify_strategy.py",
                      "--strategy", strategies, "--quick"])
    cmd = ["/bin/sh", "-c", f"ulimit -v {_VERIFY_MEM_LIMIT_KB}; exec {inner}"]
    try:
        proc = run_with_timeout(cmd, env=env, cwd=backend, timeout=_VERIFY_TIMEOUT)
    except subprocess.TimeoutExpired:
        msg = f"验证超时（>{_VERIFY_TIMEOUT}s）已终止"
        logger.warning(msg)
        return False, msg
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "")[-300:]
        logger.warning("策略验证失败 rc=%s: %s", proc.returncode, msg)
        return False, msg
    return True, (proc.stdout or "")[-300:]


def _orchestrator_ran_today() -> bool:
    """研究流水线（Orchestrator）今天是否已成功跑过（P4 去重判据）。"""
    try:
        from datetime import date as _date

        from sqlalchemy import select

        from app.database import SessionLocal
        from app.models import ResearchRun

        with SessionLocal() as db:
            row = db.execute(
                select(ResearchRun).where(
                    ResearchRun.status == "SUCCESS",
                    ResearchRun.finished_at >= datetime.combine(
                        _date.today(), datetime.min.time()),
                ).order_by(ResearchRun.id.desc()).limit(1)
            ).scalars().first()
            return row is not None
    except Exception as e:  # noqa: BLE001
        logger.warning("查询 Orchestrator 运行记录失败（按未跑处理）: %s", e)
        return False


def _loop(enabled: bool) -> None:
    last_run_date = ""
    # 当日失败重试记账。为什么必须有：last_run_date 只要写在"成功之前"，
    # 一次瞬时故障（数据源抖动、瞬时 DNS）就会吃掉当天全部重试机会 ——
    # 这个模式在公众号采集、编排器、验证上各踩过一次（2026-09~11 共四次）。
    retry = {"date": "", "etl_n": 0, "etl_ts": 0.0, "ver_n": 0, "ver_ts": 0.0}
    last_beat = 0.0
    while True:
        try:
            now = datetime.now()
            # 每 30 分钟一条心跳：2026-11-04/05 连续两晚没跑策略验证，事后翻日志
            # 既没有「跳过」也没有「完成」，无法判断是循环没转、还是条件没满足。
            if now.timestamp() - last_beat >= 1800:
                last_beat = now.timestamp()
                logger.info("data-scheduler 心跳：enabled=%s 上次 ETL=%s 上次验证=%s",
                            enabled, last_run_date or "-",
                            _status["verify"].get("last_run_date") or "-")
            if retry["date"] != _today_str():
                retry = {"date": _today_str(), "etl_n": 0, "etl_ts": 0.0,
                         "ver_n": 0, "ver_ts": 0.0}
            if enabled:
                # 全市场估值：独立于 ETL —— ETL 分支一天只进一次（跑完即锁 last_run_date），
                # 挂在里面就永远等不到 19:30 之后的重试窗口。本函数自带 15 分钟冷却，
                # 每 60 秒评估一次的成本只是一条 MAX 查询。
                try:
                    _refresh_valuation_if_due(now)
                except Exception as e:  # noqa: BLE001
                    logger.warning("估值快照巡检异常: %s", e)

                # 财务三表：报告期落后就补（Q3 曾静默空了近一周）
                try:
                    _refresh_financials_if_due(now)
                except Exception as e:  # noqa: BLE001
                    logger.warning("财报快照巡检异常: %s", e)

                # 定时触发（交易日 19:00 后）或断供自愈（只要该有的数据没到就补跑）
                due = (_is_trading_day(now)
                       and (now.hour, now.minute) >= (RUN_HOUR, RUN_MIN))
                stale = _is_stale(now)
                etl_ready = (
                    retry["etl_n"] < _MAX_RETRY
                    and (not retry["etl_ts"] or now.timestamp() - retry["etl_ts"] >= _RETRY_GAP)
                )
                if (due or stale) and last_run_date != _today_str() and etl_ready:
                    # P4 去重（2026-09-30）：研究流水线（Orchestrator，17:30）已包含
                    # 完整数据管道；若其今天已 SUCCESS 则 19:00 这轮数据日更跳过
                    # （策略验证仍照常），避免同一份数据一天跑两遍。
                    if _orchestrator_ran_today():
                        last_run_date = _today_str()
                        _status["last_run_at"] = now.isoformat(timespec="seconds")
                        logger.info("研究流水线今日已跑，跳过 19:00 数据日更（策略验证照常）")
                    else:
                        _status["catch_up"] = bool(stale and not due)
                        logger.info("触发每日数据更新 %s（%s）", _today_str(),
                                    "断供自愈" if (stale and not due) else "定时")
                        _status["last_run_at"] = now.isoformat(timespec="seconds")
                        _status["runs_total"] += 1
                        ok = _run_update()
                        if ok:
                            last_run_date = _today_str()
                            retry.update(etl_n=0, etl_ts=0.0)
                        else:
                            retry["etl_n"] += 1
                            retry["etl_ts"] = now.timestamp()
                            left = _MAX_RETRY - retry["etl_n"]
                            logger.warning("数据日更失败（第 %d/%d 次），%s",
                                           retry["etl_n"], _MAX_RETRY,
                                           f"{_RETRY_GAP // 60} 分钟后重试" if left > 0
                                           else "今日不再重试")
                            if left <= 0:
                                last_run_date = _today_str()  # 放弃今日，避免每 60s 空转
                    # 指数成分(PIT)月度快照：当日更成功后巡检当月是否缺快照（幂等，月频）
                    try:
                        _refresh_membership_if_due(now)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("成分快照巡检异常: %s", e)


                    # 策略自动验证：数据更新之后、每晚一次（20:30 后）。
                    # 成功才记 last_run_date；失败按 _RETRY_GAP 重试，最多 _MAX_RETRY 次。
                    v = _status["verify"]
                    ver_ready = (
                        retry["ver_n"] < _MAX_RETRY
                        and (not retry["ver_ts"]
                             or now.timestamp() - retry["ver_ts"] >= _RETRY_GAP)
                    )
                    if (now.hour, now.minute) >= (VERIFY_HOUR, VERIFY_MIN) \
                            and v.get("last_run_date") != _today_str() and ver_ready:
                        v["last_run_at"] = now.isoformat(timespec="seconds")
                        ok, msg = _run_verify(_VERIFY_STRATEGIES)
                        if ok:
                            v["last_run_date"] = _today_str()
                            v["last_success"] = now.isoformat(timespec="seconds")
                            v["last_error"] = None
                            v["reports"] = [
                                f"data/reports/strategy_verify_{k.strip()}.html"
                                for k in _VERIFY_STRATEGIES.split(",") if k.strip()
                            ]
                            retry.update(ver_n=0, ver_ts=0.0)
                            logger.info("策略验证完成：%s", _VERIFY_STRATEGIES)
                        else:
                            v["last_error"] = msg
                            retry["ver_n"] += 1
                            retry["ver_ts"] = now.timestamp()
                            if retry["ver_n"] >= _MAX_RETRY:
                                v["last_run_date"] = _today_str()
                                logger.warning("策略验证连续失败 %d 次，今日不再重试",
                                               retry["ver_n"])
                            else:
                                logger.warning("策略验证失败（第 %d/%d 次），%s 分钟后重试",
                                               retry["ver_n"], _MAX_RETRY, _RETRY_GAP // 60)
        except Exception as e:  # noqa: BLE001
            logger.error("数据调度循环异常: %s", e)
        time.sleep(_CHECK_INTERVAL)


_sched_thread: threading.Thread | None = None


def start_data_scheduler(enabled: bool | None = None) -> threading.Thread:
    """启动调度 daemon 线程。enabled 缺省读 env QUANT_DATA_SCHEDULE。"""
    global _sched_thread
    if _sched_thread is not None and _sched_thread.is_alive():
        logger.info("调度器已在运行，跳过重复启动（start_data_scheduler）")
        return _sched_thread
    if enabled is None:
        enabled = os.getenv("QUANT_DATA_SCHEDULE", "0") == "1"
    # 启动时先清理上次遗留的 RUNNING 僵尸。
    # 原逻辑只在「下次 ETL 超时」的异常分支里清，成功路径不清 →
    # 一旦某次管道卡死、之后每次都成功，那条 RUNNING 会永远挂着，监控一直误判。
    # 服务刚启动时不可能有真正在跑的管道（子进程已随旧进程退出），此处清理安全。
    _mark_zombie_runs_failed()
    _status["enabled"] = enabled
    t = threading.Thread(target=_loop, args=(enabled,), daemon=True, name="data-scheduler")
    t.start()
    _sched_thread = t
    logger.info("数据调度器已启动（enabled=%s，每交易日 %02d:%02d 后自动日更）", enabled, RUN_HOUR, RUN_MIN)
    return t
