"""基金数据管道调度器：把「基金」当成一条独立、可监控、可应急重跑的数据管道。

为什么单独一条管道
------------------
基金链路和主行情链路的数据节奏完全不同，混在 data_scheduler 里会互相拖累：

  * 行情每交易日一次（19:00 ETL），基金净值也是每日，但基金**持仓/监管仓位/
    FOF 报告**由监管披露日历决定 —— 季报 4 月下旬、中报 8 月下旬、三季报 10 月
    下旬、年报次年 3 月下旬。平时一个月不动一次，披露期一周内要补几千只。
  * 基金链路的落点是独立库 data/fund_research.db（5.7GB 主库曾因容器与本机
    双写损坏过，2026-09-10「file is not a database」、2026-11-08 malformed），
    把它和主库耦合会让一次基金重算的风险扩散到全平台。
  * 派生计算有严格依赖（穿透依赖持仓+监管仓位，回归依赖净值+穿透），
    必须按 DAG 串，不能像 ETL 那样平铺。

调度模型：数据驱动，不是时间驱动
--------------------------------
采集环节（kind=collect）到点就跑；派生环节（kind=derived）只在**上游有新成功**
时重算 —— 否则每天白跑几十分钟回归，既耗 CPU 又让日志失去信息量。

幂等键 = (stage, run_date)
--------------------------
同一环节同一天成功过就跳过。容器重启 / 崩溃恢复后自动补跑当天未完成的环节，
不会因为重启就把当天的活重做一遍，也不会因为重启就漏掉。

应急能力
--------
每个环节都能单独触发（页面「立即运行」/ POST /fund/pipeline/run），
失败可原地重试，运行输出（脚本最后若干行）落库可查 —— 出问题时不用再
docker exec 进容器翻日志。

四个应急出口，对应四类真实故障：

  * 运行   单个环节立刻重跑（force 忽略「今天已成功」）
  * 补跑   把今天还没成功的环节按依赖顺序补齐
  * 重置   清掉卡住的 running 记录。容器重启/进程被杀后，库里那条 running
           还在，_claim 会拒绝启动，最长要等 3 小时才自动接管 —— 应急不能等
  * 暂停   停自动巡检、只保留手动。手工补历史数据时，派生环节会在旁边
           自动触发抢同一个库的写锁

监控为什么要主动推送
--------------------
只落日志的监控等于没监控。2026-11-04「策略验证连续两晚没跑」，日志里连
「跳过」两个字都没有，人只有打开页面才会发现。所以：

  * 失败 → 立刻邮件（_alert_fail），dedup 按 (环节, 日期)，重试失败不刷屏
  * 该跑却没跑（连启动都没有，prev is None）→ 过点 90 分钟邮件（_alert_missed）

窗口外自愈
----------
披露环节原先只在窗口内触发。容器若在窗口期停机，或窗口过了才发现没抓，
欠账要等下一个窗口，最长 5 个月（2026-11-08 正是如此：Q3 窗口 10/31 已过
8 天，持仓还停在 2025-12-31）。现在 _behind_schedule 允许「本期文件没落地」
时窗口外也补抓，已抓过的期次照旧跳过。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("fund_scheduler")

DB_PATH = Path(os.getenv("QUANT_FUND_DB", "/data/fund_research.db"))
SCRIPTS = Path(os.getenv("QUANT_SCRIPTS_DIR", "/app/scripts"))

_CHECK_INTERVAL = 120          # 2 分钟扫一次
_STARTUP_GRACE = 180           # 启动宽限：避免容器重启瞬间把所有环节一起拉起来
_RUNNING_STALE_SEC = 3 * 3600  # running 超过这个时长视为僵尸，允许接管
_DETAIL_LINES = 40             # 落库保留的脚本输出行数

# 告警：失败 / 该跑没跑，都主动推邮件。只落日志的监控等于没监控 ——
# 2026-11-04 策略验证连续两晚没跑，日志里连「跳过」都没有，人只有打开页面
# 才会发现。dedup_key 按 (环节, 日期) 去重，重试失败不会刷屏。
_ALERT_ON = os.getenv("QUANT_FUND_ALERT", "1") == "1"
_OVERDUE_GRACE_MIN = 90        # 过点后多久还没开始，判定「该跑没跑」
_paused = False                # 暂停巡检：手工补数时避免和调度器抢库

# 披露窗口：季报/中报/年报的法定披露截止日附近
# 只在窗口内才去抓持仓/监管仓位/FOF 报告，平时不做无谓的网络请求。
#   Q1：4/15–4/30   H1：8/15–8/31   Q3：10/15–10/31   FY：次年 3/15–3/31
_DISCLOSURE_WINDOWS = (
    ((4, 15), (4, 30)),
    ((8, 15), (8, 31)),
    ((10, 15), (10, 31)),
    ((3, 15), (3, 31)),
)

STAGES: list[dict] = [
    # ---------------- 采集层 ----------------
    {
        "key": "nav", "label": "基金净值", "kind": "collect",
        "script": "fetch_fund_nav_daily.py",
        # 日更模式：起点自动取「库内最新净值次日」，不用人工维护窗口参数。
        "args": ["--incremental"],
        "hour": 19, "minute": 40, "timeout": 3600,
        "desc": "逐基金日净值增量 → nav_raw.csv（起点=库内最新净值次日）",
        "on": "daily",
    },
    {
        "key": "nav_import", "label": "净值入库", "kind": "derived",
        "script": "import_fund_nav.py", "args": [],
        "hour": 20, "minute": 15, "timeout": 2400,
        "depends": ["nav"],
        "desc": "nav_raw.csv → fund_nav_daily（INSERT OR REPLACE，幂等可重跑）",
    },
    {
        "key": "holdings", "label": "基金持仓", "kind": "collect",
        "script": "fetch_fund_holdings.py", "args": [],   # 参数由 _holdings_args 动态生成
        "hour": 20, "minute": 10, "timeout": 7200,
        "desc": "Tushare 逐基金持仓明细 → holdings_{报告期}.csv（按披露期分文件，新期次才全量）",
        "on": "disclosure",
    },
    {
        "key": "csrc", "label": "监管行业配置", "kind": "collect",
        "script": "fetch_csrc_industry.py", "args": [],   # 参数由 _csrc_args 动态生成
        "hour": 20, "minute": 45, "timeout": 5400,
        "desc": "天天基金 HYPZ 证监会行业配置 → fund_csrc_industry（仓位约束的权威锚）",
        "on": "disclosure",
    },
    {
        "key": "fof", "label": "FOF 定期报告", "kind": "collect",
        "script": "fetch_fof_reports.py", "args": [],    # 参数由 _fof_args 动态生成
        "hour": 21, "minute": 15, "timeout": 7200,
        "desc": "FOF 中报/年报 PDF → 基金投资明细（FOF 层的唯一合法来源）",
        "on": "disclosure",
    },
    # ---------------- 计算层 ----------------
    {
        "key": "penetration", "label": "持仓穿透", "kind": "derived",
        "script": "build_fund_penetration.py", "args": [],
        "hour": 22, "minute": 0, "timeout": 5400,
        # 显式日更：结构可信度随锚点年龄逐日衰减（structure_decay），
        # 所以即使披露数据没变，隔天重算的行业结构也应该略有不同。
        # 只在「上游变了」才跑会让估计停在披露日、越放越旧。
        "on": "daily",
        "depends": ["holdings", "csrc"],
        "desc": "双轨补齐 + 行业穿透 + 监管仓位约束 → fund_hold_coverage / fund_industry_exposure",
    },
    {
        "key": "style", "label": "风格穿透", "kind": "derived",
        "script": "build_fund_style.py", "args": ["--periods", "6"],
        "hour": 22, "minute": 25, "timeout": 2400,
        "depends": ["penetration"],
        "desc": "持仓 → 个股 → 五因子风格坐标（size/value/momentum/volatility/quality）",
    },
    {
        "key": "regression", "label": "净值回归校验", "kind": "derived",
        "script": "build_fund_regression.py", "args": [],
        "hour": 22, "minute": 40, "timeout": 7200,
        "depends": ["nav_import", "penetration"],
        "desc": "约束 WLS 净值回归，独立校验穿透结果（R²/相关/一致率）",
    },
    {
        "key": "fof_penetration", "label": "FOF 行业穿透", "kind": "derived",
        "script": "build_fof_penetration.py", "args": [],
        "hour": 23, "minute": 10, "timeout": 1800,
        "depends": ["fof", "penetration"],
        "desc": "FOF → 底层基金 → 行业，只用披露值，未穿透部分单列不估算",
    },
    {
        "key": "fof_check", "label": "FOF 两层校验", "kind": "derived",
        "script": "audit_fof_holdings.py", "args": ["--write"],
        "hour": 23, "minute": 20, "timeout": 900,
        "depends": ["fof_penetration"],
        "desc": "两路口径交叉校验 → fund_fof_layer_check（硬约束：穿透+直接权益 ≤ 100%）",
    },
    {
        "key": "fof_diagnosis", "label": "FOF 可穿透诊断", "kind": "derived",
        "script": "build_fof_diagnosis.py", "args": [],
        "hour": 23, "minute": 30, "timeout": 1200,
        "depends": ["penetration"],
        "desc": "未穿透上界与处置建议 → fund_fof_diagnosis",
    },
]

STAGE_MAP = {s["key"]: s for s in STAGES}

_status = {"enabled": False, "started_at": None, "current": None, "last_tick": None}
_lock = threading.Lock()
_running: set[str] = set()
_thread: threading.Thread | None = None
_boot_at: float = 0.0


# ------------------------------------------------------------------ 存储
def _conn(timeout: float = 60) -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), timeout=timeout)
    con.execute("PRAGMA busy_timeout=60000")
    return con


def init_db() -> None:
    con = _conn()
    try:
        con.executescript(
            """
CREATE TABLE IF NOT EXISTS fund_pipeline_runs (
  stage        TEXT NOT NULL,
  run_date     TEXT NOT NULL,     -- YYYY-MM-DD（幂等键）
  status       TEXT NOT NULL,     -- running / ok / fail / skip
  trigger      TEXT,              -- schedule / manual / retry
  started_at   TEXT,
  finished_at  TEXT,
  duration_sec REAL,
  rows         INTEGER,           -- 脚本自报的影响行数（能解析到时）
  detail       TEXT,              -- 脚本最后若干行输出（排障用，不用进容器）
  error        TEXT,
  PRIMARY KEY (stage, run_date));

CREATE INDEX IF NOT EXISTS ix_fpr_stage ON fund_pipeline_runs(stage, started_at);

CREATE TABLE IF NOT EXISTS fund_pipeline_state (
  stage       TEXT PRIMARY KEY,
  last_ok_at  TEXT,
  last_fail_at TEXT,
  last_error  TEXT,
  runs_total  INTEGER DEFAULT 0,
  fails_total INTEGER DEFAULT 0);
"""
        )
        con.commit()
    finally:
        con.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def _load_states() -> dict[str, dict]:
    con = _conn()
    try:
        rows = con.execute(
            "SELECT stage,last_ok_at,last_fail_at,last_error,runs_total,fails_total "
            "FROM fund_pipeline_state"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    finally:
        con.close()
    return {r[0]: {"last_ok_at": r[1], "last_fail_at": r[2], "last_error": r[3],
                   "runs_total": r[4] or 0, "fails_total": r[5] or 0} for r in rows}


def _today_run(stage: str) -> dict | None:
    con = _conn()
    try:
        row = con.execute(
            "SELECT stage,run_date,status,trigger,started_at,finished_at,duration_sec,"
            "rows,detail,error FROM fund_pipeline_runs WHERE stage=? AND run_date=?",
            (stage, _today()),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    finally:
        con.close()
    if not row:
        return None
    keys = ("stage", "run_date", "status", "trigger", "started_at", "finished_at",
            "duration_sec", "rows", "detail", "error")
    return dict(zip(keys, row))


def _claim(stage: str, trigger: str) -> bool:
    """抢占式写入 running，返回是否抢到。跨进程用 SQLite 主键兜住。"""
    with _lock:
        if stage in _running:
            return False
        _running.add(stage)
    d = _today()
    con = _conn()
    try:
        cur = con.execute(
            "INSERT INTO fund_pipeline_runs(stage,run_date,status,trigger,started_at) "
            "VALUES (?,?,'running',?,?) "
            "ON CONFLICT(stage,run_date) DO UPDATE SET status='running',trigger=?,"
            "started_at=?,finished_at=NULL,error=NULL,duration_sec=NULL "
            "WHERE fund_pipeline_runs.status!='running' "
            "   OR fund_pipeline_runs.started_at IS NULL "
            "   OR julianday('now') - julianday(fund_pipeline_runs.started_at) > ?",
            (stage, d, trigger, _now(), trigger, _now(), _RUNNING_STALE_SEC / 86400.0),
        )
        con.commit()
        return cur.rowcount > 0
    except Exception as e:  # noqa: BLE001
        logger.warning("抢占 %s 失败：%s", stage, e)
        return False
    finally:
        con.close()


def _release(stage: str) -> None:
    with _lock:
        _running.discard(stage)


def _finish(stage: str, ok: bool, detail: str, rows: int | None,
            error: str, t0: float) -> None:
    dur = round(time.time() - t0, 1)
    d = _today()
    con = _conn()
    try:
        con.execute(
            "UPDATE fund_pipeline_runs SET status=?,finished_at=?,duration_sec=?,"
            "rows=?,detail=?,error=? WHERE stage=? AND run_date=?",
            ("ok" if ok else "fail", _now(), dur, rows,
             (detail or "")[-8000:], (error or "")[:2000], stage, d),
        )
        con.execute(
            "INSERT INTO fund_pipeline_state(stage,last_ok_at,last_fail_at,last_error,"
            "runs_total,fails_total) VALUES (?,?,?,?,1,?) "
            "ON CONFLICT(stage) DO UPDATE SET "
            "  last_ok_at = COALESCE(excluded.last_ok_at, fund_pipeline_state.last_ok_at),"
            "  last_fail_at = COALESCE(excluded.last_fail_at, fund_pipeline_state.last_fail_at),"
            "  last_error = excluded.last_error,"
            "  runs_total = fund_pipeline_state.runs_total + 1,"
            "  fails_total = fund_pipeline_state.fails_total + excluded.fails_total",
            (stage, _now() if ok else None, None if ok else _now(),
             None if ok else (error or "")[:2000], 0 if ok else 1),
        )
        con.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("写运行结果失败 %s：%s", stage, e)
    finally:
        con.close()
    _release(stage)
    if not ok:
        _alert_fail(stage, (STAGE_MAP.get(stage) or {}).get("label", stage),
                    error, detail)


# ------------------------------------------------------------------ 告警
def _send_alert(subject: str, body_lines: list[str], dedup_key: str) -> dict:
    """走平台已有的邮件通道（data/notify_channels.json）；未配置就静默返回。"""
    if not _ALERT_ON:
        return {"ok": False, "skipped": True, "reason": "告警开关关闭"}
    try:
        from app.database import SessionLocal
        from app.services.mailer import send_email_html

        rows = "".join(
            f"<tr><td style='padding:4px 10px 4px 0;color:#666;white-space:nowrap;"
            f"vertical-align:top'>{k}</td>"
            f"<td style='padding:4px 0;font-family:ui-monospace,Menlo,monospace'>{v}</td></tr>"
            for k, v in (ln.split("｜", 1) if "｜" in ln else ("", ln) for ln in body_lines)
        )
        html = (
            "<div style='font:13px/1.7 -apple-system,BlinkMacSystemFont,\"PingFang SC\","
            "sans-serif;color:#222'>"
            f"<div style='font-size:15px;font-weight:600;margin-bottom:10px'>{subject}</div>"
            f"<table style='border-collapse:collapse'>{rows}</table>"
            "<div style='margin-top:14px;color:#888;font-size:12px'>"
            "基金数据管道 · 独立库 fund_research.db</div></div>"
        )
        db = SessionLocal()
        try:
            return send_email_html(db, subject, html, dedup_key=dedup_key)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("告警发送失败（%s）：%r", dedup_key, e)
        return {"ok": False, "error": repr(e)[:200]}


def _alert_fail(stage: str, label: str, error: str, detail: str) -> None:
    """环节失败 → 立刻告警。同一环节同一天只发一次。"""
    d = _today()
    tail = "\n".join((detail or "").strip().splitlines()[-12:]) or "（无输出）"
    _send_alert(
        f"[基金管道] {label} 执行失败 · {d}",
        [
            f"环节｜{label}（{stage}）",
            f"日期｜{d}",
            f"错误｜{error or '未返回错误信息'}",
            f"输出｜\n<pre style='margin:4px 0;padding:8px;background:#f6f6f6;"
            f"border-radius:4px;font-size:12px;white-space:pre-wrap'>{tail}</pre>",
            "处置｜数据中心 → 数据管道 → 基金数据管道，点该环节「运行」重试；"
            "若显示运行中卡住，点「重置」再重试",
        ],
        f"fund_pipeline_fail:{stage}:{d}",
    )
    logger.warning("基金管道 %s 失败告警已发出", stage)


def _alert_missed(stage: str, label: str, run_at: str) -> None:
    """该跑却没跑（连启动都没有）→ 告警。这是调度器最危险的失效模式：
    不报错、不写失败记录，页面上只是「未跑」，只有人工看才发现。"""
    d = _today()
    _send_alert(
        f"[基金管道] {label} 到点未触发 · {d}",
        [
            f"环节｜{label}（{stage}）",
            f"应跑时点｜{run_at}",
            f"当前状态｜过点 {_OVERDUE_GRACE_MIN} 分钟仍未启动，且无失败记录",
            "可能原因｜后端容器未运行 / 调度线程已停 / 依赖上游一直没成功",
            "处置｜数据中心 → 数据管道，确认「调度已启用」与「巡检」时间在走；"
            "再点该环节「运行」手动补跑",
        ],
        f"fund_pipeline_missed:{stage}:{d}",
    )
    logger.warning("基金管道 %s 未触发告警已发出", stage)



# ------------------------------------------------------------------ 执行
def _parse_rows(out: str) -> int | None:
    """从脚本输出里捞「本次 N 行 / 明细 N 行 / 落库 N 行」这类自报数。"""
    import re
    pats = (
        r"明细\s*(\d[\d,]*)\s*行",
        r"本次\s*\d+\s*只\s*/\s*(\d[\d,]*)\s*行",
        r"落库\s*(\d[\d,]*)\s*行",
        (r"(\d[\d,]*)\s*行行业明细"),
    )
    for p in pats:
        m = re.findall(p, out or "")
        if m:
            try:
                return int(m[-1].replace(",", ""))
            except Exception:  # noqa: BLE001
                continue
    return None


def _latest_period(d: date | None = None) -> str:
    """按今天推算「理应已披露完的最新报告期」。

    监管截止日：一季报 4/30、中报 8/31、三季报 10/31、年报次年 4/30。
    这里取已过截止日的最近一期，用于给增量抓取命名（holdings_20260930.csv）。
    """
    d = d or date.today()
    cands = [
        (date(d.year, 4, 30), f"{d.year}0331"),
        (date(d.year, 8, 31), f"{d.year}0630"),
        (date(d.year, 10, 31), f"{d.year}0930"),
        (date(d.year - 1, 4, 30), f"{d.year - 1}1231"),
    ]
    ok = [p for dl, p in cands if dl <= d]
    return max(ok) if ok else f"{d.year - 1}1231"


def _holdings_args() -> list[str] | None:
    """持仓增量抓取参数：每个报告期一个独立 CSV + 独立进度文件。

    为什么不复用 holdings_raw.csv：progress.json 记录的是「这只基金抓过」，
    不清空就永远抓不到新一期。按报告期分文件后，新一期天然是空进度 → 全量跑
    一遍；老期次的文件已经存在就直接跳过，不会每天重跑 1.2 万只。
    """
    period = _latest_period()
    out = f"holdings_{period}.csv"
    path = Path(os.getenv("QUANT_HOLDINGS_DIR", "/data/holdings")) / out
    if path.exists() and path.stat().st_size > 1_000_000:
        return None      # 该期已抓过，本环节今天无事可做
    return ["--since", f"{period[:4]}0101", "--until", "29991231",
            "--out", out, "--reset-done"]


def _csrc_args() -> list[str]:
    return ["--workers", "5", "--years", f"2015-{date.today().year}"]


def _fof_args() -> list[str]:
    return ["--kinds", "H1,FY", "--workers", "3", "--retry-fail",
            "--years", f"2019-{date.today().year}"]


_ARGS_FN = {
    "holdings": _holdings_args,
    "csrc": _csrc_args,
    "fof": _fof_args,
}


def _build_args(st: dict) -> list[str] | None:
    """返回脚本参数；返回 None 表示本环节今天无需运行。"""
    fn = _ARGS_FN.get(st["key"])
    if fn:
        return fn()
    return [a for a in (st.get("args") or []) if a != ""]


def run_stage(key: str, force: bool = False, trigger: str = "manual",
              wait: bool = False) -> dict:
    """执行单个环节。wait=True 同步返回结果（应急接口用），否则丢线程。"""
    st = STAGE_MAP.get(key)
    if not st:
        return {"ok": False, "error": f"未知环节 {key}"}

    if not force:
        prev = _today_run(key)
        if prev and prev["status"] == "ok":
            return {"ok": True, "skipped": True, "reason": "今天已成功，跳过"}

    if not _claim(key, trigger):
        return {"ok": False, "error": "该环节正在运行（或抢占失败）"}

    def _job():
        t0 = time.time()
        script = SCRIPTS / st["script"]
        if not script.exists():
            _finish(key, False, "", None, f"脚本不存在：{script}", t0)
            return
        try:
            extra = _build_args(st)
        except Exception as e:  # noqa: BLE001
            _finish(key, False, "", None, f"构建参数失败：{e!r}", t0)
            return
        if extra is None:
            _finish(key, True, "本报告期已抓取，跳过", 0, "", t0)
            logger.info("基金管道 %s 跳过：本报告期数据已存在", key)
            return
        args = [sys.executable, str(script)] + list(extra)
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONPATH", "/app")
        logger.info("基金管道启动 %s（%s）：%s", key, st["label"], " ".join(args[1:]))
        try:
            p = subprocess.run(
                args, cwd=str(SCRIPTS.parent), env=env,
                capture_output=True, text=True, timeout=st["timeout"],
            )
            out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
            tail = "\n".join(out.strip().splitlines()[-_DETAIL_LINES:])
            ok = p.returncode == 0
            _finish(key, ok, tail, _parse_rows(out),
                    "" if ok else f"exit={p.returncode}", t0)
            logger.info("基金管道 %s %s，%.1fs", key, "完成" if ok else "失败", time.time() - t0)
        except subprocess.TimeoutExpired:
            _finish(key, False, "", None, f"超时 {st['timeout']}s", t0)
            logger.warning("基金管道 %s 超时", key)
        except Exception as e:  # noqa: BLE001
            _finish(key, False, "", None, repr(e)[:800], t0)
            logger.warning("基金管道 %s 异常：%r", key, e)

    if wait:
        _job()
        return {"ok": True, "stage": key, "done": True, "result": _today_run(key)}
    threading.Thread(target=_job, name=f"fund-{key}", daemon=True).start()
    return {"ok": True, "stage": key, "started": True}


def reset_stage(key: str, reason: str = "人工重置") -> dict:
    """把今天卡在 running 的记录作废，让「运行」能立刻重新触发。

    为什么需要：容器重启 / 进程被杀后，库里那条 running 记录还在（而内存里的
    _running 已经空了）。_claim 会以「正在运行」为由拒绝启动，最长要等
    _RUNNING_STALE_SEC（3 小时）才自动接管 —— 应急时不能等这么久。

    安全边界：本进程内真有活线程时**拒绝重置**，避免同一环节跑两份、
    双双写同一个库。
    """
    st = STAGE_MAP.get(key)
    if not st:
        return {"ok": False, "error": f"未知环节 {key}"}
    with _lock:
        if key in _running:
            return {"ok": False,
                    "error": "该环节本进程内仍在运行，不能重置（等它结束或等超时）"}
    d = _today()
    con = _conn()
    try:
        cur = con.execute(
            "UPDATE fund_pipeline_runs SET status='fail',finished_at=?,error=?,detail=? "
            "WHERE stage=? AND run_date=? AND status='running'",
            (_now(), reason, "人工重置：原 running 记录作废（未产生有效输出）", key, d),
        )
        con.commit()
        n = cur.rowcount
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"重置失败：{e!r}"[:300]}
    finally:
        con.close()
    if n:
        logger.warning("基金管道 %s 已人工重置", key)
    return {"ok": True, "stage": key, "reset": bool(n),
            "message": "已重置，可重新运行" if n else "今日没有卡住的运行记录"}


# ------------------------------------------------------------------ 触发判定
def _in_disclosure_window(d: date | None = None) -> bool:
    d = d or date.today()
    for (m1, d1), (m2, d2) in _DISCLOSURE_WINDOWS:
        if (d.month, d.day) >= (m1, d1) and (d.month, d.day) <= (m2, d2):
            return True
    return False


def _is_trading_day(d: date | None = None) -> bool:
    try:
        from app.core.trading_calendar import is_trading_day
        return is_trading_day(d or date.today())
    except Exception:  # noqa: BLE001
        d = d or date.today()
        return d.weekday() < 5


def _passed_today(hour: int, minute: int) -> bool:
    now = datetime.now()
    return (now.hour, now.minute) >= (hour, minute)


def _derived_due(st: dict, states: dict[str, dict]) -> bool:
    """派生环节：上游有比本环节更新的成功记录才跑。

    这样净值日更后回归会重算，但周末没有新数据时不会白跑一遍全量回归。
    """
    me = states.get(st["key"], {}).get("last_ok_at")
    for up in st.get("depends") or []:
        up_ok = states.get(up, {}).get("last_ok_at")
        if not up_ok:
            continue
        if not me or up_ok > me:
            return True
    return False


def _behind_schedule(st: dict) -> bool:
    """窗口外判断「是否欠账」：本期该拿的数据还没落地。

    为什么需要：披露环节原先只在窗口内触发（4/15–4/30、8/15–8/31、
    10/15–10/31、3/15–3/31）。一旦容器在窗口期停机，或窗口过了才发现没抓，
    这次欠账就要等到下一个窗口才可能被补 —— 最长 5 个月。2026-11-08 就是
    这个情况：Q3 窗口 10/31 已过 8 天，持仓停在 2025-12-31，只能人工补。

    现在改成「窗口外也自愈」：只要本期该有的文件还没落地（_holdings_args
    返回参数而非 None），就允许触发。已经抓过的期次文件存在 → 返回 None →
    照旧不跑，不会因为这条放宽而每天重抓一万两千只。
    """
    if st["key"] == "holdings":
        try:
            return _holdings_args() is not None
        except Exception:  # noqa: BLE001
            return False
    return False


def _should_run_today(st: dict, states: dict[str, dict]) -> bool:
    """今天是否该跑这个环节（只看日历/上游，不看是否已跑）。"""
    if st["kind"] == "collect":
        on = st.get("on", "daily")
        if on == "disclosure" and not _in_disclosure_window():
            return _behind_schedule(st)
        if on == "daily" and not _is_trading_day():
            return False
        return True
    # 派生环节默认「上游变了才跑」；但显式声明 on=daily 的（如持仓穿透，
    # 其结构可信度按天衰减）必须每天重算，否则估计值会僵在披露日。
    if st.get("on") == "daily":
        return _is_trading_day()
    return _derived_due(st, states)


def _overdue(st: dict) -> bool:
    """过点超过宽限期 —— 用来区分「还没到点」和「到点了没动」。"""
    now = datetime.now()
    due = now.replace(hour=st["hour"], minute=st["minute"],
                      second=0, microsecond=0)
    return (now - due).total_seconds() > _OVERDUE_GRACE_MIN * 60


def tick() -> list[str]:
    """扫一遍，返回本轮触发的环节。"""
    started: list[str] = []
    if time.time() - _boot_at < _STARTUP_GRACE:
        return started
    states = _load_states()
    for st in STAGES:
        key = st["key"]
        if not _passed_today(st["hour"], st["minute"]):
            continue
        prev = _today_run(key)
        if prev and prev["status"] in ("ok", "running"):
            continue
        if not _should_run_today(st, states):
            continue
        # 到点了、该跑、却连一条运行记录都没有 → 主动告警（每天每环节一次）。
        # 注意只看 prev is None：prev.status == fail 的情况 _finish 已经报过了，
        # 这里再报一次就是重复轰炸。
        if prev is None and _overdue(st):
            _alert_missed(key, st["label"], f"{st['hour']:02d}:{st['minute']:02d}")
        try:
            r = run_stage(key, force=False, trigger="schedule")
            if r.get("started"):
                started.append(key)
        except Exception as e:  # noqa: BLE001
            logger.warning("触发 %s 失败：%r", key, e)
    return started


def _loop() -> None:
    while True:
        try:
            with _lock:
                _status["last_tick"] = _now()
            if _paused:
                with _lock:
                    _status["current"] = None
            else:
                got = tick()
                if got:
                    logger.info("本轮触发：%s", "、".join(got))
                with _lock:
                    _status["current"] = None
        except Exception as e:  # noqa: BLE001
            logger.warning("基金管道巡检异常：%r", e)
        time.sleep(_CHECK_INTERVAL)


def set_paused(paused: bool) -> dict:
    """暂停/恢复自动巡检。

    用途：手工补历史数据、或排查某个环节时，不希望调度器在旁边自动触发
    派生环节抢同一个库的写锁。暂停只停「自动触发」，手动「运行」照常可用 ——
    应急时人永远优先于钟。
    """
    global _paused
    _paused = bool(paused)
    logger.warning("基金管道巡检已%s", "暂停" if _paused else "恢复")
    return {"ok": True, "paused": _paused}


# ------------------------------------------------------------------ 对外
def get_status() -> dict:
    """给监控页用：每个环节的定义 + 今天状态 + 历史累计 + 库内数据新鲜度。"""
    states = _load_states()
    out = []
    for st in STAGES:
        run = _today_run(st["key"])
        s = states.get(st["key"], {})
        out.append({
            "key": st["key"], "label": st["label"], "kind": st["kind"],
            "desc": st.get("desc", ""), "on": st.get("on", ""),
            "depends": st.get("depends") or [],
            "run_at": f"{st['hour']:02d}:{st['minute']:02d}",
            "timeout": st["timeout"],
            "today_status": (run or {}).get("status"),
            "today_started": (run or {}).get("started_at"),
            "today_finished": (run or {}).get("finished_at"),
            "today_duration": (run or {}).get("duration_sec"),
            "today_rows": (run or {}).get("rows"),
            "today_error": (run or {}).get("error"),
            "today_detail": (run or {}).get("detail"),
            "last_ok_at": s.get("last_ok_at"),
            "last_fail_at": s.get("last_fail_at"),
            "last_error": s.get("last_error"),
            "runs_total": s.get("runs_total", 0),
            "fails_total": s.get("fails_total", 0),
            "running": st["key"] in _running,
        })
    with _lock:
        base = dict(_status)
    base.update({
        "stages": out,
        "paused": _paused,
        "in_disclosure_window": _in_disclosure_window(),
        "is_trading_day": _is_trading_day(),
        "data": _data_freshness(),
    })
    return base


def _data_freshness() -> dict:
    """独立库各产出的最新日期与行数 —— 页面判断「数据到底新不新」的依据。"""
    probes = {
        "fund_nav_daily": ("fund_nav_daily", "nav_date"),
        "fund_hold_coverage": ("fund_hold_coverage", "end_date"),
        "fund_industry_exposure": ("fund_industry_exposure", "end_date"),
        "fund_csrc_industry": ("fund_csrc_industry", "end_date"),
        "fund_fof_holdings": ("fund_fof_holdings", "end_date"),
        "fund_fof_penetration": ("fund_fof_penetration", "end_date"),
        "fund_industry_regression": ("fund_industry_regression", "end_date"),
    }
    con = _conn(timeout=15)
    out: dict[str, dict] = {}
    try:
        for name, (table, col) in probes.items():
            try:
                r = con.execute(
                    f"SELECT count(*), MAX({col}) FROM {table}").fetchone()
                out[name] = {"rows": int(r[0] or 0), "latest": r[1]}
            except Exception:  # noqa: BLE001
                out[name] = {"rows": 0, "latest": None}
    finally:
        con.close()
    return out


def run_all_missing(trigger: str = "manual") -> dict:
    """应急：把今天还没成功的环节按依赖顺序跑一遍（同步，供接口用）。"""
    res = []
    for st in STAGES:
        prev = _today_run(st["key"])
        if prev and prev["status"] == "ok":
            res.append({"stage": st["key"], "skipped": True})
            continue
        # 上游未成功就跳过，避免用旧数据算出错结果
        bad = [u for u in (st.get("depends") or [])
               if not (_today_run(u) or {}).get("status") == "ok"]
        if bad:
            res.append({"stage": st["key"], "skipped": True,
                        "reason": f"上游未完成：{','.join(bad)}"})
            continue
        run_stage(st["key"], force=True, trigger=trigger)
        res.append({"stage": st["key"], "started": True})
    return {"ok": True, "results": res}


def start_fund_scheduler(enabled: bool = True) -> None:
    global _thread, _boot_at
    init_db()
    _boot_at = time.time()
    with _lock:
        _status["enabled"] = enabled
        _status["started_at"] = _now()
    if not enabled:
        logger.info("基金管道调度器未启用（QUANT_FUND_SCHEDULE!=1）")
        return
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="fund-scheduler", daemon=True)
    _thread.start()
    logger.info("基金管道调度器已启动：%d 个环节，每 %ds 巡检，披露窗口=%s",
                len(STAGES), _CHECK_INTERVAL, _in_disclosure_window())
