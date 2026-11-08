"""热点简报调度：按交易时段触发，不是每小时。

2026-11-07 老刘定的时刻表：

- 开盘前：07:00、09:00
- 盘中：  10:00、11:00、13:00、14:00（每小时一次，12 点午休不跑）
- 收盘后：16:00、21:00
- 非交易日：只跑 09:00、21:00 —— 周末消息面稀，逐小时跑纯烧钱。
  21 点收工而不是 16 点：周末也讲「早/晚」两场，且 21:02 → 次日 07:02
  正好 10 小时，落在 720 分钟上限内 —— 若收在 16 点，到周一早 7 点隔 15
  小时，会被上限截掉 3 小时（周日傍晚的消息就漏了）

为什么独立成模块而不是塞进 data_scheduler：那位的循环里已经压了 ETL、策略验证、
成分快照、估值快照、财报补更五件事，再加一件会让「谁卡住了」更难定位。

幂等：按「日期#小时」去重 —— 每个 slot 独占一个整点，这个键仍然够用；
失败不记 done，同一 slot 最多试 2 次（:02 / :05）。
⚠️ 错过不补：简报有时效，补上个小时的热点没有意义。容器 22 点启动时，
21 点那次不会补跑。

⚠️ 窗口不是固定 60 分钟：21:00 → 次日 07:00 隔了 10 小时，写死 60 会把隔夜
消息全漏掉。`_window_minutes` 算「距上一个 slot 的实际分钟数」，并据此放大
财联社的翻页数与条数上限（见 hot_brief.collect_flash 的 pages 参数）。
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, timedelta

from sqlalchemy import select

logger = logging.getLogger("hot_brief_scheduler")

# 交易日触发时刻（整点；实际在 :SLOT_MINUTE 触发，避开整点其他任务）
SLOTS_TRADING = (7, 9, 10, 11, 13, 14, 16, 21)
# 非交易日：早/晚各一次（21 点收工，见文件头注释）
SLOTS_OFFDAY = (9, 21)
# 当日全景综述（brief_digest）的生成时刻：当天最后一轮简报（21:02）之后再跑，
# 这样 16:00-21:00 的傍晚消息也能进综述。生成一次约 100 秒（v4-pro 是推理模型），
# 放在独立线程里，不能阻塞本循环 —— 否则 60 秒一次的 slot 检查会被它卡住。
DIGEST_HOUR, DIGEST_MINUTE = 21, 45
_digest_done: set[str] = set()

SLOT_MINUTE = 2                 # 每个 slot 的 :02 触发
_CHECK_INTERVAL = 60            # 秒
_MAX_ATTEMPTS = 2               # 同一 slot 最多试 2 次（约 :02 / :05）
_WINDOW_CAP = 720               # 单轮最多回看 12 小时（再长快讯也翻不完）

_done: set[str] = set()
_attempts: dict[str, int] = {}
_status = {"enabled": False, "last_run_at": None, "last_summary": None,
           "last_error": None, "runs_total": 0, "last_window_min": None}
_lock = threading.Lock()
_sched_thread: threading.Thread | None = None


def _slots_for(day: date | None = None) -> tuple[int, ...]:
    """当天的触发时刻。日历查不到时按交易日处理（宁可多跑，不要漏盘中）。"""
    from app.core.trading_calendar import is_trading_day

    d = day or date.today()
    try:
        return SLOTS_TRADING if is_trading_day(d) else SLOTS_OFFDAY
    except Exception:  # noqa: BLE001
        return SLOTS_TRADING


def _window_minutes(now: datetime, slots: tuple[int, ...]) -> int:
    """距上一个 slot 的分钟数，clip 到 [30, _WINDOW_CAP]。

    21:00 收工到次日 07:00 开工，中间 10 小时的消息都属于「隔夜要闻」，
    必须一起给模型；反过来连续两个整点之间只要 60 分钟。
    """
    prev = None
    for h in sorted(slots, reverse=True):
        if h < now.hour:
            prev = now.replace(hour=h, minute=SLOT_MINUTE, second=0, microsecond=0)
            break
    if prev is None:
        # 当天第一个 slot 之前 → 回溯到**昨天实际跑过的最后一个 slot**。
        # 不能直接用 max(slots)：周一 07:02 若按今天表取 21:02，等于假设
        # 周日 21 点跑过，而周日（非交易日）只跑 9/16 —— 会凭空漏掉
        # 周日 16:02 之后的消息。所以必须查昨天自己的时刻表。
        yday = (now - timedelta(days=1)).date()
        prev = (now - timedelta(days=1)).replace(
            hour=max(_slots_for(yday)), minute=SLOT_MINUTE, second=0, microsecond=0)
    return int(min(_WINDOW_CAP, max(30, (now - prev).total_seconds() / 60)))


def _next_slot(now: datetime, slots: tuple[int, ...]) -> str:
    for h in slots:
        if h > now.hour or (h == now.hour and now.minute < SLOT_MINUTE):
            return f"{h:02d}:{SLOT_MINUTE:02d}"
    # 今天没了 → 报明天第一个（周日 16:02 之后应该是「明天 07:02」而不是「明天」）
    nxt = _slots_for((now + timedelta(days=1)).date())
    return f"明天 {min(nxt):02d}:{SLOT_MINUTE:02d}"


def get_status() -> dict:
    now = datetime.now()
    slots = _slots_for(now.date())
    today = now.strftime("%Y-%m-%d")
    return {**_status,
            "slot_minute": SLOT_MINUTE,
            "slots_today": list(slots),
            "is_trading_day": slots == SLOTS_TRADING,
            "next_slot": _next_slot(now, slots),
            "done_today": sorted(k for k in _done if k.startswith(today))}


def _seed_done_from_db() -> set[str]:
    """用库里已有的简报补齐 _done，避免「容器一重启就重跑当前 slot」。

    2026-11-07 实测：11:07~11:10 容器被外部反复重启（RestartCount=0，是手动
    restart 事件），每次重启 `_done` 清空 → 同一个小时连续生成 3 份简报，
    白烧 3 次 LLM + DeepSeek 搜索配额。改成从 DB 恢复后，重启不再重复。
    """
    from app.database import SessionLocal
    from app.models import HotBrief

    db = SessionLocal()
    try:
        since = datetime.now() - timedelta(days=1)
        rows = db.execute(
            select(HotBrief.created_at).where(HotBrief.created_at >= since)
        ).scalars().all()
    except Exception:  # noqa: BLE001
        return set()
    finally:
        db.close()
    return {f"{r:%Y-%m-%d}#{r.hour:02d}" for r in rows if r}


def _run_once(minutes: int = 60) -> tuple[bool, str]:
    from app.database import SessionLocal
    from app.services.hot_brief import run_brief

    db = SessionLocal()
    try:
        r = run_brief(db, minutes=minutes)
    finally:
        db.close()
    if r.get("ok"):
        head = f"回看 {minutes} 分钟 · 输入 {r.get('n_input')} 条"
        if not r.get("n_items"):
            return True, f"{head} → 本轮无实质新增"
        return True, (f"{head}（快讯 {r.get('n_flash')} + 境外 {r.get('n_foreign', 0)} + 搜索 {r.get('n_search')}）"
                      f" → 保留 {r.get('n_items')} 条")
    return False, str(r.get("error") or "未知错误")


def _digest_done_today() -> bool:
    """库里当天已有综述就不再重复生成（重启后 _digest_done 会丢）。"""
    from app.database import SessionLocal
    from app.models import BriefDigest

    db = SessionLocal()
    try:
        day = datetime.now().date().isoformat()
        return db.query(BriefDigest).filter(BriefDigest.day == day,
                                            BriefDigest.error == "").first() is not None
    except Exception:  # noqa: BLE001
        return False
    finally:
        db.close()


def _run_digest() -> None:
    from app.database import SessionLocal
    from app.services.brief_digest import run as run_digest

    db = SessionLocal()
    try:
        r = run_digest(db)
        if r.get("ok"):
            logger.info("当日综述完成：%s · %d 字 · %s 秒（要点 %s 条 / 资讯 %s 条）",
                        r.get("day"), r.get("chars"), r.get("seconds"),
                        r.get("n_items"), r.get("n_news"))
        else:
            logger.warning("当日综述失败：%s", r.get("error"))
    except Exception as e:  # noqa: BLE001
        logger.warning("当日综述异常：%s", str(e)[:160])
    finally:
        db.close()


def _loop(enabled: bool) -> None:
    while True:
        try:
            if enabled:
                now = datetime.now()
                slots = _slots_for(now.date())
                key = f"{now:%Y-%m-%d}#{now.hour:02d}"
                # ⚠️ 三个条件缺一不可：当前小时在时刻表里、过了 :02、这个 slot 没跑成过。
                # 少了第一个，就退回「每小时都跑」的老行为。
                if (now.hour in slots and now.minute >= SLOT_MINUTE
                        and key not in _done
                        and _attempts.get(key, 0) < _MAX_ATTEMPTS):
                    _attempts[key] = _attempts.get(key, 0) + 1
                    minutes = _window_minutes(now, slots)
                    with _lock:
                        _status["last_run_at"] = now.isoformat(timespec="seconds")
                        _status["runs_total"] += 1
                        _status["last_window_min"] = minutes
                    ok, msg = _run_once(minutes)
                    with _lock:
                        if ok:
                            _done.add(key)
                            _status["last_summary"] = msg
                            _status["last_error"] = None
                            logger.info("热点简报完成：%s", msg)
                        else:
                            _status["last_error"] = msg
                            logger.warning("热点简报失败（第 %s/%s 次）：%s",
                                           _attempts[key], _MAX_ATTEMPTS, msg)
                # 当日综述：每天 21:45 之后一次，独立线程跑（v4-pro 要 100 秒，
                # 同步跑会把下一轮 slot 检查拖过去）。
                dkey = f"{now:%Y-%m-%d}#digest"
                if (now.hour == DIGEST_HOUR and now.minute >= DIGEST_MINUTE
                        and dkey not in _digest_done):
                    _digest_done.add(dkey)
                    if _digest_done_today():
                        logger.info("当日综述已存在，跳过重复生成")
                    else:
                        threading.Thread(target=_run_digest, daemon=True,
                                         name="brief-digest").start()
        except Exception as e:  # noqa: BLE001
            logger.warning("热点简报调度循环异常：%s", e)
        threading.Event().wait(_CHECK_INTERVAL)


def start_hot_brief_scheduler(enabled: bool | None = None) -> threading.Thread:
    """启动守护线程。enabled 缺省读 QUANT_HOT_BRIEF（默认开）。"""
    global _sched_thread
    if _sched_thread is not None and _sched_thread.is_alive():
        logger.info("调度器已在运行，跳过重复启动（hot_brief）")
        return _sched_thread
    if enabled is None:
        enabled = os.getenv("QUANT_HOT_BRIEF", "1") not in ("0", "", "false", "False")
    with _lock:
        _status["enabled"] = enabled
    seeded = _seed_done_from_db()
    if seeded:
        with _lock:
            _done.update(seeded)
        logger.info("沿用库中已生成的简报，跳过 %d 个已完成时段", len(seeded))

    t = threading.Thread(target=_loop, args=(enabled,), daemon=True, name="hot-brief")
    t.start()
    _sched_thread = t
    logger.info("热点简报调度器已启动（enabled=%s）：交易日 %s，非交易日 %s（均 :%02d 触发）",
                enabled, list(SLOTS_TRADING), list(SLOTS_OFFDAY), SLOT_MINUTE)
    return t
