"""ETF 资金流调度器（盘中采样 + 收盘补数 + 日级更新）。

## 为什么单独一个调度器

看板有三条时间线，节奏完全不同：

| 时点 | 动作 | 理由 |
|---|---|---|
| 交易时段每分钟 | `realtime_quote` 采一次 | tushare 盘中不给 1min 线，只能用累计成交额差分 |
| 15:05 | 拉当日完整 1min 线覆盖采样 | 份额和分钟线都是 T 日盘后才能取到 |
| 19:30 | 日级更新（日线/份额/净值/分类矩阵） | 晚于 19:00 数据管道，避免抢 tushare 配额 |

**注意 15:05 那一档不是可选项**：盘中采到的是「累计成交额差分」，精度受采样间隔
和网络抖动影响；收盘后用官方 1min 线覆盖，才和 `fund_daily.amount` 对得上
（实测误差 0.0%）。不补的话，看板上今天的分钟曲线会跟日线打架。

## 与 nsi_scheduler 的关系

两者互不依赖。NSI 是信号（08:30 刷新，T+1 执行），本调度器是观察数据（盘中+盘后）。
**本调度器产出的任何数字都不得进入策略**，见 services/etf_flow.py 开头的纪律说明。

## 幂等

- 采样：`sample_time` 取自行情时间，重复采同一时刻命中唯一约束覆盖，不会造脏点
- 补数/日更：用 `_done` 按「日期#slot」去重，服务重启后同一 slot 不会重复跑
- 首次初始化：只在库为空（universe=0）时触发，已有数据不重拉
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, time as dtime

from app.database import SessionLocal
from app.services import etf_flow

logger = logging.getLogger("etf_flow_scheduler")

IDLE_INTERVAL = 60          # 非交易时段的检查间隔（秒）

# 各时点都被实测校准过（不是拍的）：
#
# 1) 当日 1min 线：15:05 起每 10 分钟试一次，直到 20:00 接上日级更新。
#    **不能挑一个固定时点**：2026-11-06 实测 15:05 / 15:09 / 16:21 三次都返回 0 行，
#    tushare 什么时候落地完全不可预测。以前定死在 16:30，一旦那个点还没数据，
#    看板上今天的分时就会一直停在实时采样拼出来的碎片上，只能等人工点。
# 2) 19:30 日级更新（日线/成交额/12 类矩阵）。晚于 19:00 数据管道，避免抢配额。
# 3) 次日 08:30 补份额并重算净流入。ETF 份额 T 日盘后并不立即可得，
#    与 NSI 规格书 §6.5 记录的现象一致（当晚算出的 score 只有次日早上的 2/3）。
#    这也是为什么看板上的「当日净流入」通常要等第二天早上才出现。
MINUTES_WINDOW = (dtime(15, 5), dtime(20, 0))
MIN_RETRY_SEC = 600         # 当日分钟线的重试间隔
DAILY_SLOT = dtime(19, 30)
SHARE_SLOT = dtime(8, 30)

_status = {
    "enabled": False,
    "last_sample": None,
    "last_sample_points": 0,
    "last_update": None,
    "last_error": None,
    "bootstrapped": False,
    "samples_total": 0,
}
_done: set[str] = set()
_lock = threading.Lock()
_last_min_try: float = 0.0     # 上次尝试拉当日分钟线的时间戳


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_status() -> dict:
    with _lock:
        return {**_status, "done_today": sorted(k for k in _done if k.startswith(_today()))}


# ── 三个动作 ──────────────────────────────────────────────────────────
def _sample() -> None:
    db = SessionLocal()
    try:
        n = etf_flow.sample_intraday(db)
        with _lock:
            _status["last_sample"] = datetime.now().isoformat(timespec="seconds")
            _status["last_sample_points"] = n
            if n:
                _status["samples_total"] += 1
                _status["last_error"] = None
    except Exception as e:  # noqa: BLE001
        with _lock:
            _status["last_error"] = f"采样失败：{e}"
        logger.warning("ETF 盘中采样失败：%s", e)
    finally:
        db.close()


def _has_today_minutes(db) -> bool:
    from sqlalchemy import func, select
    from app.models import EtfMinuteBar
    n = db.scalar(select(func.count()).select_from(EtfMinuteBar)
                  .where(EtfMinuteBar.trade_time >= datetime.combine(date.today(), dtime(9, 30))))
    return bool(n)


def _maybe_pull_today_minutes() -> None:
    """当日官方 1min 线：还没入库就每 10 分钟试一次，落地后不再试。

    为什么要「反复试探」而不是挑一个固定时点：tushare 的当日分钟线何时落地
    不可预测（2026-11-06 实测 15:05 / 15:09 / 16:21 三次都是 0 行）。
    每 10 分钟试一次，代价是一天最多几十次调用，收益是数据一落地看板就自动
    从「实时采样碎片」切成「官方完整线」，不需要人守着点按钮。
    """
    global _last_min_try
    if time.time() - _last_min_try < MIN_RETRY_SEC:
        return
    _last_min_try = time.time()          # 无论成功失败都等下一轮，避免刷爆配额
    db = SessionLocal()
    try:
        if _has_today_minutes(db):
            return
        codes = etf_flow.ensure_universe(db)
        n = etf_flow.update_minutes(db, codes, force=True, day=_today())
        if n:
            with _lock:
                _status["last_update"] = datetime.now().isoformat(timespec="seconds")
            logger.info("当日官方分钟线已入库（%d 只），看板切换到完整线", n)
        else:
            logger.info("当日分钟线仍未落地，%d 秒后重试", MIN_RETRY_SEC)
    except Exception as e:  # noqa: BLE001
        logger.warning("拉取当日分钟线失败：%s", e)
    finally:
        db.close()


def _refresh() -> None:
    """19:30 日级更新；顺带兜一次当日 1min 线（万一 15:05–20:00 那轮重试都没撞上）。"""
    db = SessionLocal()
    try:
        codes = etf_flow.ensure_universe(db)
        if not _has_today_minutes(db):
            n = etf_flow.update_minutes(db, codes, day=_today())
            logger.info("ETF 当日分钟线已补齐：%d 只", n)
        res = etf_flow.update(db)
        with _lock:
            _status["last_update"] = datetime.now().isoformat(timespec="seconds")
            _status["last_error"] = None
        logger.info("ETF 资金流更新完成：%s", res)
    except Exception as e:  # noqa: BLE001
        with _lock:
            _status["last_error"] = f"更新失败：{e}"
        logger.warning("ETF 资金流更新失败：%s", e)
    finally:
        db.close()


def _refresh_share() -> None:
    """次日盘前补份额并重算净流入（T+1 盘前才可见）。"""
    db = SessionLocal()
    try:
        codes = etf_flow.ensure_universe(db)
        n = etf_flow.update_daily(db, codes)
        etf_flow.rebuild_flow(db)
        with _lock:
            _status["last_update"] = datetime.now().isoformat(timespec="seconds")
            _status["last_error"] = None
        logger.info("ETF 份额已补齐（%d 只），净流入已重算", n)
    except Exception as e:  # noqa: BLE001
        with _lock:
            _status["last_error"] = f"份额更新失败：{e}"
        logger.warning("ETF 份额更新失败：%s", e)
    finally:
        db.close()


def _boot() -> None:
    """库为空时做一次全量初始化（约 84 次调用、40 秒）。"""
    db = SessionLocal()
    try:
        s = etf_flow.status(db)
        if s["universe"] > 0:
            with _lock:
                _status["bootstrapped"] = True
            logger.info("ETF 资金流库已存在：%s 只样本 / %s 个交易日",
                        s["universe"], s["flow_days"])
            return
        logger.info("ETF 资金流库为空，开始首次初始化（约 40 秒）...")
        etf_flow.bootstrap(db)
        with _lock:
            _status["bootstrapped"] = True
    except Exception as e:  # noqa: BLE001
        with _lock:
            _status["last_error"] = f"初始化失败：{e}"
        logger.warning("ETF 资金流首次初始化失败：%s", e)
    finally:
        db.close()


# ── 主循环 ────────────────────────────────────────────────────────────
def _loop(enabled: bool) -> None:
    if enabled:
        _boot()
    while True:
        try:
            if enabled:
                now = datetime.now()
                # 周末不跑（节假日会白跑一次采样，成本可忽略，漏跑代价更大）
                if now.weekday() < 5:
                    if etf_flow.in_session(now.time()):
                        _sample()
                    # 当日 1min 线：15:05–20:00 之间每 10 分钟试一次，落地即停
                    if MINUTES_WINDOW[0] <= now.time() <= MINUTES_WINDOW[1]:
                        _maybe_pull_today_minutes()
                    # 日级更新：19:30 之后触发一次（含补跑）
                    k2 = f"{_today()}#daily"
                    if now.time() >= DAILY_SLOT and k2 not in _done:
                        with _lock:
                            _done.add(k2)
                        logger.info("触发日级更新")
                        _refresh()
                    # 份额：次日 08:30–09:10 之间补一次（T+1 盘前才落地）
                    k3 = f"{_today()}#share"
                    if SHARE_SLOT <= now.time() <= dtime(9, 10) and k3 not in _done:
                        with _lock:
                            _done.add(k3)
                        logger.info("触发份额补数")
                        _refresh_share()
        except Exception as e:  # noqa: BLE001
            logger.warning("ETF 资金流调度循环异常：%s", e)
        threading.Event().wait(IDLE_INTERVAL)


def start_etf_flow_scheduler(enabled: bool = True) -> threading.Thread:
    """启动 ETF 资金流 daemon 线程。"""
    with _lock:
        _status["enabled"] = enabled
    t = threading.Thread(target=_loop, args=(enabled,), daemon=True,
                         name="etf-flow-scheduler")
    t.start()
    logger.info("ETF 资金流调度器已启动（enabled=%s）", enabled)
    return t
