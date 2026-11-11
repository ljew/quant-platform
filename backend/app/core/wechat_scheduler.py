"""公众号每日抓取调度器。

为什么放在后端进程里跑、而不是宿主机 launchd：
crawl_content.py 只读 wewe-rss.db（bind 挂载的 /data/wechat/wewe-rss.db）去取待抓
文章列表，全程不调 wewe-rss 的 HTTP 接口。所以容器内完全可以跑，跟着后端一起
restart: unless-stopped，少一个需要手工 bootstrap 的 launchd 任务。

只有 wewe-rss 本身（连微信读书拉列表）必须留在宿主机 —— 它要维持扫码登录态。

时间点选 08:20 / 20:40：wewe-rss 每 3 小时（:42）刷一次列表，这两个点能覆盖
当天绝大多数发文，且早上那次赶在开盘前。
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.core.netguard import WEREAD, explain as net_explain, network_ready

logger = logging.getLogger("wechat_scheduler")

# 主采集放在 6:20：用户 7 点前都在家、网络稳定，跑完（约 9 分钟）也在 7 点前。
# 原 8:20 那轮正好赶上出门/唤醒，2026-11-07 早上两次 DNS 未就绪就是这么来的。
SLOTS = [(6, 20), (20, 40)]     # 每天两次
# 工作日 9:00 补跑：上班时间一定有网。周末不补 —— 用户明确「不上班就不用补，
# 等他自己打开网络再手动补」，免得白白消耗请求、还可能撞上限流。
RETRY_SLOT = (9, 0)
_CHECK_INTERVAL = 60            # 秒
_TIMEOUT = 5400                 # 单次抓取上限。2026-11-02 实测：interval=6s 时
                                # 111 个号基础 666s，再加每个命中冷却的号 60s 退避，
                                # 一轮实测 2513s（42 分钟）。原 3600s 余量太薄，
                                # 一旦超时会被 kill 且当天该 slot 已记为 done，不再重试。
_SINCE_DAYS = 7                 # 只抓近 7 天，留补抓窗口又不至于触发风控

_status = {
    "enabled": False,
    "slots": [f"{h:02d}:{m:02d}" for h, m in SLOTS],
    "last_run_at": None,
    "last_success": None,
    "last_error": None,
    "last_summary": None,
    "runs_total": 0,
}

_done: set[str] = set()          # 已完成（成功）的时间点（形如 2026-11-02#08:20）
_retry: dict[str, dict] = {}     # 失败重试记账：key -> {attempts, last_ts}

_COOL_FILE = Path(settings.wechat_data_dir) / "state" / "collect_cooldown.json"
_DONE_FILE = Path(settings.wechat_data_dir) / "state" / "collect_done.json"
_REPORT_FILE = Path(settings.wechat_data_dir) / "state" / "collect_last.json"


def _load_done() -> set[str]:
    """读「今天哪些 slot 已经成功跑过」的落盘记录。

    为什么必须落盘：_done 原来是纯内存，容器一重启就清空，而 slot 只要还在
    补跑窗口内（_CATCHUP_HOURS）就会被重新触发。2026-11-12 凌晨连着重启三次，
    06:20 那轮采集跟着重复跑了三次 —— 每次约 10 分钟，还白撞一轮限流。
    """
    import json
    import time

    try:
        items = json.loads(_DONE_FILE.read_text(encoding="utf-8")).get("done") or []
        today = time.strftime("%Y-%m-%d")
        return {str(k) for k in items if str(k).startswith(today)}
    except Exception:  # noqa: BLE001
        return set()


def _save_done() -> None:
    import json
    import time

    try:
        _DONE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DONE_FILE.write_text(
            json.dumps({"done": sorted(_done), "saved_at": time.time()},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def _load_cooldown() -> float:
    """读上次落盘的限流冷却截止时间（epoch 秒）。读不到就当没有。"""
    import json

    try:
        d = json.loads(_COOL_FILE.read_text(encoding="utf-8"))
        return float(d.get("until") or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def _save_cooldown(until: float, reason: str) -> None:
    import json
    import time

    try:
        _COOL_FILE.parent.mkdir(parents=True, exist_ok=True)
        _COOL_FILE.write_text(
            json.dumps({"until": until, "reason": reason[:160], "saved_at": time.time()},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("写入限流冷却失败：%s", e)


_cool_until = _load_cooldown()
_MAX_ATTEMPTS = 2                # 同一 slot 每天最多试 2 次（6:20 + 6:41 仍在 7 点前）
_RETRY_GAP = 600                 # 失败后至少隔 10 分钟再试（避开唤醒瞬间网络未就绪）
# 限流专属退避：微信读书返回 HTTP 499 时，10 分钟一次的「立刻重打」只会加重风控。
# 2026-11-06 实测：21:40 起 cover/content 全部 499，而调度仍按 10 分钟重试 3 次。
_THROTTLE_GAP = 1800             # 疑似限流（退出码 3 / 499）时隔 30 分钟再试
_THROTTLE_COOLDOWN = 3600        # 限流后的**全局**冷却（跨 slot、跨重启，落盘）
_CATCHUP_HOURS = 3               # 错过 slot 后最多补 3 小时（6:20 最晚 9:20 补，
                                 # 正好接上工作日 9:00 的补跑窗口；不再拖到晚上）
# 为什么需要 catch-up 窗口：原判定只有 `now < slot` 才跳过，于是晚上 22:49 仍会去补
# 早上 08:20 的 slot，与 20:40 的 slot 叠加 → 限流期间一晚上打了 4 次。
# 为什么需要全局冷却：冷却/重试记账原本只在内存，容器一重启就清零。2026-11-06 晚
# 排查时重启了 5 次，每次重启都立刻重打一遍，把 499 窗口越拉越长（实测 ≥70 分钟）。
_lock = threading.Lock()


def _load_report() -> dict:
    """最近一次采集的失败明细（_run_crawl 落盘）。读不到就返回空。"""
    import json

    try:
        d = json.loads(_REPORT_FILE.read_text(encoding="utf-8"))
        return {"at": d.get("at"), "summary": d.get("summary"),
                "failures": (d.get("failures") or [])[:30]}
    except Exception:  # noqa: BLE001
        return {}


def get_status() -> dict:
    with _lock:
        return {
            **_status,
            "done_today": sorted(k for k in _done if k.startswith(_today())),
            "retry_today": {k: v["attempts"] for k, v in _retry.items()
                            if k.startswith(_today())},
            "max_attempts": _MAX_ATTEMPTS,
            "last_report": _load_report(),
        }


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _crawl_script() -> Path | None:
    """定位采集脚本（容器内 /app/scripts，本机 <项目根>/backend/scripts）。

    优先 `collect_weread.py`（微信读书直连，2026-11-02 起的主力）：
    wewe-rss 已停摆（中转站 502），它那套读 wewe-rss.db 待抓列表的
    `crawl_content.py` 只剩兜底价值（历史遗留，没有新文章可抓）。
    """
    here = Path(__file__).resolve()
    roots = [
        Path("/app/scripts/wechat"),                                    # 容器
        here.parents[3] / "scripts" / "wechat",                         # 容器（按层级推）
        here.parents[3] / "backend" / "scripts" / "wechat",             # 本机
    ]
    for d in roots:
        for name in ("collect_weread.py", "crawl_content.py"):
            c = d / name
            if c.exists():
                return c
    return None


def _run_crawl() -> tuple[bool, str]:
    script = _crawl_script()
    if script is None:
        return False, "找不到采集脚本（collect_weread.py / crawl_content.py）"
    if script.name == "collect_weread.py":
        # 微信读书渠道：只取每个号最新一篇，幂等跳过；失败退出码 2 = 凭证失效、3 = 疑似限流
        # ⚠️ 间隔必须 6s：2026-11-02 实测 3s 跑到第 22 个号就被限流，剩下 89 个全失败，
        # 而失败分支原本不 sleep，259s 就"跑完"并报成功 —— 静默失败。
        cmd = [sys.executable, str(script), "--interval", "6"]
    else:
        cmd = [sys.executable, str(script), "--since-days", str(_SINCE_DAYS)]
    logger.info("公众号抓取开始：%s", " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT,
                           cwd=str(script.parent))
    except subprocess.TimeoutExpired:
        return False, f"抓取超时（>{_TIMEOUT}s）"
    except Exception as e:  # noqa: BLE001
        return False, f"启动失败：{e}"

    out = (r.stdout or "") + (r.stderr or "")
    # 从输出里摘一行统计，方便前端/日志看抓了多少
    summary = ""
    for line in reversed(out.splitlines()):
        if "已抓正文" in line or "完成：" in line:
            summary = line.strip()
            break
    # 汇总只有「失败 N」这个数字，谁失败、为什么失败全被丢掉了 ——
    # 2026-11-12 连着两轮都是「失败 15」，只能靠猜。把明细留一份：
    # 日志里打前 5 条（方便 docker logs 直接看），完整清单落盘给页面/排查用。
    # ⚠️ 必须在退出码判断**之前**写：凭证失效（退出码 2）恰恰是最需要明细的场景。
    fails = [ln.strip() for ln in out.splitlines()
             if ("失败" in ln or "❌" in ln) and not ln.strip().startswith("完成：")]
    if fails or r.returncode != 0:
        if fails:
            logger.warning("公众号抓取失败明细 %d 条，前 5 条：%s",
                           len(fails), " ｜ ".join(fails[:5]))
        try:
            import json as _json
            import time as _time

            _REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
            _REPORT_FILE.write_text(_json.dumps(
                {"at": _time.strftime("%Y-%m-%d %H:%M:%S"), "rc": r.returncode,
                 "summary": summary, "failures": fails},
                ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    if r.returncode != 0:
        return False, f"退出码 {r.returncode}：{out.strip()[-300:]}"
    return True, summary or "完成"


def _slots_for(now: datetime) -> list[tuple[int, int]]:
    """当天实际要处理的 slot：主采集 + （仅工作日）9:00 补跑。

    周末不追加补跑位 —— 用户明确「不上班就不用补，等他自己打开网络再手动补」。
    """
    slots = list(SLOTS)
    if now.weekday() < 5:            # 0=周一 … 4=周五
        slots.append(RETRY_SLOT)
    return slots


def _loop(enabled: bool) -> None:
    global _cool_until          # 限流时会重设并落盘；global 必须在本函数任何读取之前
    while True:
        try:
            if enabled:
                now = datetime.now()
                for h, m in _slots_for(now):
                    slot_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
                    if now < slot_dt:
                        continue                      # 还没到点
                    if (now - slot_dt).total_seconds() > _CATCHUP_HOURS * 3600:
                        continue                      # 错过太久，当天放弃该 slot
                    if now.timestamp() < _cool_until:
                        break                         # 限流全局冷却中（重启也生效）
                    key = f"{_today()}#{h:02d}:{m:02d}"
                    if key in _done:
                        continue
                    # 9:00 是「补跑位」：只有当天 6:20 那轮没成功才跑，成功就不重复采
                    if (h, m) == RETRY_SLOT:
                        main_key = f"{_today()}#{SLOTS[0][0]:02d}:{SLOTS[0][1]:02d}"
                        if main_key in _done:
                            continue
                    # 失败不再当天一票否决：2026-11-03 Mac 睡眠唤醒瞬间 DNS 未就绪，
                    # 08:20 那轮 5 个号就熔断退出，而 slot 已被记 done → 整个上午不再重试，
                    # 当天 0 篇。改成「成功才记 done，失败最多重试 3 次、间隔 10 分钟」。
                    st = _retry.get(key, {"attempts": 0, "last_ts": 0.0})
                    if st["attempts"] >= _MAX_ATTEMPTS:
                        continue
                    gap = st.get("gap") or _RETRY_GAP
                    if st["last_ts"] and now.timestamp() - st["last_ts"] < gap:
                        continue
                    # ⚠️ 网络未就绪时**不消耗重试次数**，只把下次探测推后 2 分钟。
                    # 2026-11-07 教训：08:24 / 08:53 两次 DNS 失败就把当天 3 次机会耗光，
                    # 09:04 第三次又撞上 Cookie 过期 → 当天 0 篇。而 09:10 网络早就好了。
                    # 失败的不是任务，是「探测时机」。真正断网时应等网络回来，不该记账。
                    if not network_ready(*WEREAD):
                        st["last_ts"] = now.timestamp() - gap + 120
                        _retry[key] = st
                        reason = net_explain(*WEREAD)
                        _status["last_error"] = f"网络未就绪（{reason}），不消耗重试次数，等恢复"
                        logger.info("网络未就绪，暂缓公众号采集：%s", reason)
                        break
                    st["attempts"] += 1
                    _retry[key] = st
                    with _lock:
                        _status["last_run_at"] = now.isoformat(timespec="seconds")
                        _status["runs_total"] += 1
                    ok, msg = _run_crawl()
                    with _lock:
                        if ok:
                            _done.add(key)
                            _save_done()
                            _retry.pop(key, None)
                            _status["last_success"] = datetime.now().isoformat(timespec="seconds")
                            _status["last_summary"] = msg
                            _status["last_error"] = None
                            logger.info("公众号抓取完成：%s", msg)
                        else:
                            st["last_ts"] = now.timestamp()
                            # 限流要有专属退避：499 属于风控，密集重试只会把窗口拉长
                            throttled = ("退出码 3" in msg) or ("499" in msg)
                            st["gap"] = _THROTTLE_GAP if throttled else _RETRY_GAP
                            if throttled:
                                _cool_until = now.timestamp() + _THROTTLE_COOLDOWN
                                _save_cooldown(_cool_until, msg)
                                logger.warning(
                                    "命中限流，全局冷却 %s 分钟（落盘，重启不清零）",
                                    _THROTTLE_COOLDOWN // 60)
                            _status["last_error"] = msg
                            logger.warning("公众号抓取失败（第 %s/%s 次，%s退避 %ss）：%s",
                                           st["attempts"], _MAX_ATTEMPTS,
                                           "限流" if throttled else "普通",
                                           st["gap"], msg)
                    break  # 一轮只处理一个 slot
        except Exception as e:  # noqa: BLE001
            logger.warning("公众号调度循环异常：%s", e)
        threading.Event().wait(_CHECK_INTERVAL)


_sched_thread: threading.Thread | None = None


def start_wechat_scheduler(enabled: bool | None = None) -> threading.Thread:
    """启动抓取 daemon 线程。enabled 缺省读 QUANT_WECHAT_SCHEDULE（默认开）。"""
    global _sched_thread
    if _sched_thread is not None and _sched_thread.is_alive():
        logger.info("调度器已在运行，跳过重复启动（start_wechat_scheduler）")
        return _sched_thread
    if enabled is None:
        enabled = settings.wechat_schedule
    with _lock:
        _status["enabled"] = enabled
        # 重启不丢「今天已跑过」——否则只要还在补跑窗口内就会重跑一遍采集
        # 用 update 而不是 |= ：函数里对模块级 set 用 |= 会被当成局部变量重新绑定
        _done.update(_load_done())
    t = threading.Thread(target=_loop, args=(enabled,), daemon=True, name="wechat-scheduler")
    t.start()
    _sched_thread = t
    logger.info("公众号调度器已启动（enabled=%s，每天 %s 抓增量）",
                enabled, " / ".join(f"{h:02d}:{m:02d}" for h, m in SLOTS))
    return t
