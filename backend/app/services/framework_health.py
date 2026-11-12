"""框架体检：五层健康状态的统一数据源。

被两处消费：
  - scripts/daily_health.py   命令行体检（退出码可接 cron）
  - routers/monitor.py        /monitor/framework-health 供驾驶舱消费

设计原则：
  - 只读，不改任何数据
  - 不自己发 HTTP 请求（避免自调用超时），直接读 DB / 内存 STATE
  - 每项检查独立 try/except，单项失败不影响其余
  - 耗时一律用 agent_events 首末时间算（ResearchRun.started_at 曾有 UTC 时区 bug）
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

logger = logging.getLogger(__name__)

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_RANK = {OK: 0, WARN: 1, FAIL: 2}


def _worst(statuses: list[str]) -> str:
    if not statuses:
        return OK
    return max(statuses, key=lambda s: _RANK.get(s, 0))


def _trading_gap(last: date, today: date) -> int:
    """last 到 today 之间的工作日数（粗算，不含节假日表）。"""
    n, d = 0, last
    while d < today:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _item(label: str, status: str, detail: str) -> dict[str, str]:
    return {"label": label, "status": status, "detail": detail}


def _etl_deadline(now: datetime, hour: int = 19, minute: int = 0) -> datetime | None:
    """最近一个「数据本该已经更新完」的交易日 hour:minute。

    默认 19:00 —— ETL 只在交易日 19:00 跑，所以不能用「距今多少小时」这种绝对阈值：
    周日上午看到「46 小时前」是**完全正常**的 —— 下一次本来就该是周一 19:00。
    旧逻辑写死 36 小时，导致每个周末「调度层」都挂 WARN。

    2026-11-08 老刘就是盯着这个 WARN 问「什么意思」才发现的。

    编排器（17:30）跑的是同一套 12 步管道，且当日 19:00 的 ETL 会因它成功而跳过，
    所以用编排器作基准时必须传 17:30，否则永远比不过 19:00 那条线。
    """
    from datetime import time as _time

    from app.core.trading_calendar import is_trading_day

    d = now.date()
    for _ in range(10):
        try:
            ok = is_trading_day(d)
        except Exception:  # noqa: BLE001
            ok = d.weekday() < 5
        if ok:
            # 该交易日 hour:minute 起跑，次日再给 12 小时宽限（含降级重试）
            base = datetime.combine(d, _time(hour, minute))
            if base + timedelta(hours=12) <= now:
                return base
        d = d - timedelta(days=1)
    return None


def _etl_lag_status(fin: datetime | None, now: datetime,
                    hour: int = 19, minute: int = 0) -> tuple[str, str]:
    """ETL 是否落后：跟「最近该跑的那个交易日 hour:minute」比，而不是跟小时数比。

    提前一天之后仍无记录才算落后 —— 中间有 12 小时宽限给降级重试。
    """
    if fin is None:
        return OK, ""
    exp = _etl_deadline(now, hour, minute)
    if exp is None:
        return OK, ""
    # ⚠️ 时区：fin（PipelineRun.finished_at）是 **UTC naive**（模型默认 utcnow），
    # 这里的 exp 是**本地 naive**。不换算直接比会凭空差 8 小时，明明跑过还报 WARN。
    exp_utc = exp.astimezone().astimezone(timezone.utc).replace(tzinfo=None)
    if fin >= exp_utc:
        return OK, ""
    return WARN, f"（应于 {exp:%m-%d %H:%M} 后完成，仍停在更早）"


# ── 1. 调度层 ──────────────────────────────────────────────
def _check_scheduler(db) -> dict[str, Any]:
    """调度是否在跑，看两样东西：开关配置 + 数据库里的实际运行记录。

    注意：不能读 data_scheduler.get_status() —— 那是进程内内存状态，
    从 CLI 或另一个进程调用时恒为「未启用」，会误报。必须查 DB。
    """
    import os

    items = []
    env = os.getenv("QUANT_DATA_SCHEDULE", "")
    # 仅作展示：CLI 进程没有 launchd 注入的环境变量，不能据此判 FAIL。
    # 真正判断"调度是否在跑"看下面的最近 ETL 记录。
    items.append(_item("调度开关", OK,
                       f"QUANT_DATA_SCHEDULE={env}" if env else "服务进程内注入（CLI 不可见）"))

    try:
        from app.models import PipelineRun, ResearchRun

        run = db.execute(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(1)).scalar_one_or_none()
        # 19:00 的 data_scheduler 在「今日编排已 SUCCESS」时会**按设计跳过**数据日更
        # （避免同一套 12 步管道一天跑两遍），此时 pipeline_runs 当天不再新增记录。
        # 只看 pipeline_runs 就会天天误报「ETL 停在更早」—— 2026-11-12 早上就是这条
        # 把整个调度层判成 WARN。编排器跑的是同一套管道，所以取两者较新的那个。
        orch = db.execute(
            select(ResearchRun).where(ResearchRun.status == "SUCCESS")
            .order_by(ResearchRun.id.desc()).limit(1)
        ).scalar_one_or_none()
        orch_newer = bool(
            orch and orch.finished_at and
            (run is None or run.finished_at is None or orch.finished_at > run.finished_at)
        )
        if run:
            now = datetime.now()
            started = run.started_at
            age_min = (now - started).total_seconds() / 60 if started else None

            if run.status == "RUNNING":
                # 正常 ETL 约 8-14 分钟。超过 60 分钟说明子进程已退出但记录没收尾
                # （_mark_zombie_runs_failed 只在下次 ETL 超时时才触发，中间会一直挂着）
                if age_min is not None and age_min > 60:
                    items.append(_item("最近 ETL", FAIL,
                                       f"#{run.id} 僵尸 · RUNNING 已 {age_min / 60:.1f} 小时，进程可能已退出"))
                else:
                    items.append(_item("最近 ETL", OK,
                                       f"#{run.id} 运行中 {age_min:.0f} 分钟" if age_min is not None
                                       else f"#{run.id} 运行中"))
            elif run.status == "SUCCESS":
                fin = run.finished_at or started
                lag_h = (now - fin).total_seconds() / 3600 if fin else None
                st, extra = _etl_lag_status(fin, now)
                items.append(_item("最近 ETL", st,
                                   (f"#{run.id} SUCCESS · {lag_h:.1f} 小时前{extra}"
                                    if lag_h is not None else f"#{run.id} SUCCESS")))
            else:
                items.append(_item("最近 ETL", WARN, f"#{run.id} {run.status} · 需排查"))
        else:
            items.append(_item("最近 ETL", FAIL, "无运行记录"))

        if orch_newer:
            now = datetime.now()
            # ⚠️ 时区地雷：ResearchRun.finished_at 是 `datetime.now()`（**本地 naive**），
            # 而 PipelineRun.started_at/finished_at 是 utcnow（**UTC naive**），
            # _etl_lag_status 内部按 UTC 比。直接把本地时间喂进去会凭空多 8 小时、
            # 让滞后判定永远 OK —— 等于把这个检查废掉。这里显式换算。
            fin_local = orch.finished_at
            fin_utc = fin_local.astimezone().astimezone(timezone.utc).replace(tzinfo=None)
            lag_h = (now - fin_local).total_seconds() / 3600
            # 编排器的截止线是它自己的开跑时刻（默认 17:30，与 orchestrator 的
            # QUANT_ORCHESTRATOR_HOUR/MINUTE 同一来源），不是 19:00
            oh = int(os.getenv("QUANT_ORCHESTRATOR_HOUR", "17"))
            om = int(os.getenv("QUANT_ORCHESTRATOR_MINUTE", "30"))
            st, extra = _etl_lag_status(fin_utc, now, oh, om)
            # 编排器比 ETL 更新 → 用它覆盖上面那条判定（同一套 12 步管道）
            items = [it for it in items if it.get("label") != "最近 ETL"]
            items.append(_item("最近 ETL", st,
                               f"#{orch.id} 编排器完成 · {lag_h:.1f} 小时前{extra}"
                               f"（当日 19:00 数据日更按设计跳过）"))
    except Exception as e:
        items.append(_item("最近 ETL", FAIL, f"读取失败 {type(e).__name__}"))

    # 策略验证报告新鲜度：每晚 20:05（含周末）跑一次，漏跑或连续失败会静默停更。
    # 2026-11-03 实测：20:05 那轮没产出报告，文件停在 11-02，而 verify.last_error
    # 只是进程内存值、重启即清零 —— 等于没人看得见。这里用报告文件 mtime 兜底。
    try:
        from app.config import DATA_DIR

        reps = list((Path(DATA_DIR) / "reports").glob("strategy_verify_*.json"))
        if not reps:
            items.append(_item("策略验证", WARN, "尚无验证报告（每晚 20:05 应产出）"))
        else:
            newest = max(reps, key=lambda p: p.stat().st_mtime)
            age_h = (datetime.now().timestamp() - newest.stat().st_mtime) / 3600
            # 40h ≈ 漏掉一晚；72h ≈ 连漏两晚
            st = OK if age_h <= 40 else (WARN if age_h <= 72 else FAIL)
            items.append(_item("策略验证", st,
                               f"{newest.name} · {age_h:.1f} 小时前更新"))
    except Exception as e:  # noqa: BLE001
        items.append(_item("策略验证", WARN, f"读取失败 {type(e).__name__}"))

    return {"key": "scheduler", "name": "调度层", "status": _worst([i["status"] for i in items]), "items": items}


# ── 2. 编排层 ──────────────────────────────────────────────
def _check_orchestrator(db) -> dict[str, Any]:
    items = []
    try:
        from app.models import AgentEvent, ResearchRun
        from app.services import orchestrator as orch

        running = bool((orch.STATE or {}).get("running"))
        items.append(_item("流水线状态", OK if not running else WARN,
                           "空闲" if not running else f"运行中 · {orch.STATE.get('step')}"))

        # 排除 CANCELLED：容器重复启动会导致同一时点被触发多次，多余的那些已人工取消。
        # 若不排除，体检会长期显示「最近 run #309 CANCELLED/FAILED」，看着像出事（2026-11-07）。
        run = db.execute(
            select(ResearchRun)
            .where(ResearchRun.status != "CANCELLED")
            .order_by(ResearchRun.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if run:
            bad = run.status not in ("SUCCESS", "FAILED")
            items.append(_item("最近 run", WARN if bad else OK,
                               f"#{run.id} {run.status} · 数据日 {run.data_date or '—'}"))
            # 耗时用事件时间（不受历史 started_at 时区 bug 影响）
            span = db.execute(
                select(func.min(AgentEvent.created_at), func.max(AgentEvent.created_at))
                .where(AgentEvent.run_id == run.id)
            ).one_or_none()
            mins = None
            if span and span[0] and span[1]:
                mins = (span[1] - span[0]).total_seconds() / 60
            if mins is None and run.started_at and run.finished_at:
                mins = (run.finished_at - run.started_at).total_seconds() / 60
            if mins is not None:
                items.append(_item("最近 run 耗时", OK if mins < 90 else WARN, f"{mins:.1f} 分钟"))
        else:
            items.append(_item("最近 run", WARN, "无运行记录"))
    except Exception as e:
        items.append(_item("编排器", FAIL, f"读取失败 {type(e).__name__}"))
    return {"key": "orchestrator", "name": "编排层", "status": _worst([i["status"] for i in items]), "items": items}


# ── 3. 数据层 ──────────────────────────────────────────────
def _check_data() -> dict[str, Any]:
    items = []
    try:
        import duckdb

        from app.config import DATA_DIR

        duck = Path(DATA_DIR) / "quant.duckdb"
        con = duckdb.connect(str(duck), read_only=True)
        last = con.execute("SELECT max(trade_date) FROM kline_daily").fetchone()[0]
        n = con.execute("SELECT count(*) FROM kline_daily").fetchone()[0]
        con.close()
        gap = _trading_gap(last, date.today())
        st = OK if gap <= 1 else (WARN if gap <= 3 else FAIL)
        items.append(_item("日线最新日期", st, f"{last} · 落后 {gap} 个工作日"))
        items.append(_item("日线总量", OK, f"{n:,} 行"))
    except Exception as e:
        items.append(_item("日线数据", FAIL, f"{type(e).__name__}: {str(e)[:40]}"))

    try:
        from app.config import DATA_DIR

        total = sum(f.stat().st_size for f in Path(DATA_DIR).glob("*.db*"))
        gb = total / 1024 ** 3
        items.append(_item("库体积", OK if gb < 20 else WARN, f"{gb:.1f} GB"))
    except Exception:
        pass
    return {"key": "data", "name": "数据层", "status": _worst([i["status"] for i in items]), "items": items}


# ── 4. 研究层 ──────────────────────────────────────────────
def _check_research(db) -> dict[str, Any]:
    items = []
    try:
        from app.models import ResearchCard

        rows = db.execute(
            select(ResearchCard.state, func.count()).group_by(ResearchCard.state)
        ).all()
        dist = ", ".join(f"{s}={c}" for s, c in rows) or "无卡片"
        items.append(_item("卡片分布", OK, dist))

        since = datetime.now() - timedelta(days=7)
        recent = db.execute(
            select(func.count()).select_from(ResearchCard).where(ResearchCard.updated_at >= since)
        ).scalar() or 0
        items.append(_item("近 7 天变动", OK if recent > 0 else WARN,
                           f"{recent} 张" if recent else "0 张 · 研究可能停滞"))
    except Exception as e:
        items.append(_item("研究卡", FAIL, f"读取失败 {type(e).__name__}"))
    return {"key": "research", "name": "研究层", "status": _worst([i["status"] for i in items]), "items": items}


# ── 5. 运行时（磁盘 / 库可用性 / 采集新鲜度）──────────────
def _check_runtime(db) -> dict[str, Any]:
    """补上最容易「悄悄坏掉」的三项：磁盘满、SQLite 打不开、采集静默停摆。

    为什么不查 quick_check：主库 5GB，全表校验要几十秒且每次体检都跑，
    会把看门狗拖成负担。这里只做「能打开 + 能读 schema」，真正坏掉的库
    连 sqlite_master 都读不了，够用。
    """
    items = []
    # 1) 磁盘水位
    try:
        from app.config import DATA_DIR

        st = os.statvfs(str(DATA_DIR))
        free_pct = st.f_bavail / max(st.f_blocks, 1) * 100
        free_gb = st.f_bavail * st.f_frsize / 1024 ** 3
        s = OK if free_pct >= 15 else (WARN if free_pct >= 8 else FAIL)
        items.append(_item("磁盘剩余", s, f"{free_gb:.0f} GB · {free_pct:.0f}%"))
    except Exception as e:  # noqa: BLE001
        items.append(_item("磁盘剩余", WARN, f"读取失败 {type(e).__name__}"))

    # 2) 两个库能不能打开
    for label, name in (("SQLite 主库", "quant_dev.db"), ("公众号库", "wechat/knowledge.db")):
        try:
            from app.config import DATA_DIR

            p = Path(DATA_DIR) / name
            if not p.exists():
                items.append(_item(label, WARN, "文件不存在"))
                continue
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
            con.close()
            items.append(_item(label, OK, f"{p.stat().st_size / 1024 ** 3:.1f} GB"))
        except Exception as e:  # noqa: BLE001
            items.append(_item(label, FAIL, f"{type(e).__name__}: {str(e)[:40]}"))

    # 3) 采集新鲜度：公众号连续两天无新增 → 大概率凭据失效或调度挂了
    try:
        from app.config import DATA_DIR

        p = Path(DATA_DIR) / "wechat" / "knowledge.db"
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        last = con.execute("SELECT max(fetched_at) FROM articles").fetchone()[0]
        con.close()
        if last:
            hours = (datetime.now().timestamp() - last) / 3600
            s = OK if hours <= 30 else (WARN if hours <= 54 else FAIL)
            items.append(_item("采集新鲜度", s, f"{hours:.0f} 小时前写入"))
    except Exception:  # noqa: BLE001
        pass

    return {"key": "runtime", "name": "运行时", "status": _worst([i["status"] for i in items]), "items": items}


def framework_health(db) -> dict[str, Any]:
    """五层体检总入口。返回 {checked_at, worst, layers:[...]}"""
    layers = [
        _check_scheduler(db),
        _check_orchestrator(db),
        _check_data(),
        _check_research(db),
        _check_runtime(db),
    ]
    return {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "worst": _worst([l["status"] for l in layers]),
        "layers": layers,
    }
