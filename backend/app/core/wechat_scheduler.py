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

logger = logging.getLogger("wechat_scheduler")

SLOTS = [(8, 20), (20, 40)]     # 每天两次
_CHECK_INTERVAL = 60            # 秒
_TIMEOUT = 3600                 # 单次抓取上限（历史上全量补抓约 40 分钟）
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

_done: set[str] = set()          # 已完成的时间点（形如 2026-11-02#08:20）
_lock = threading.Lock()


def get_status() -> dict:
    with _lock:
        return {**_status, "done_today": sorted(k for k in _done if k.startswith(_today()))}


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _crawl_script() -> Path | None:
    """定位 crawl_content.py。容器内是 /app/scripts，本机是 <项目根>/backend/scripts。"""
    here = Path(__file__).resolve()
    cands = [
        Path("/app/scripts/wechat/crawl_content.py"),                  # 容器
        here.parents[3] / "scripts" / "wechat" / "crawl_content.py",   # 容器（按层级推）
        here.parents[3] / "backend" / "scripts" / "wechat" / "crawl_content.py",  # 本机
    ]
    for c in cands:
        if c.exists():
            return c
    return None


def _run_crawl() -> tuple[bool, str]:
    script = _crawl_script()
    if script is None:
        return False, "找不到 crawl_content.py"
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
    if r.returncode != 0:
        return False, f"退出码 {r.returncode}：{out.strip()[-300:]}"
    return True, summary or "完成"


def _loop(enabled: bool) -> None:
    while True:
        try:
            if enabled:
                now = datetime.now()
                for h, m in SLOTS:
                    key = f"{_today()}#{h:02d}:{m:02d}"
                    # 到点且今天这个 slot 还没跑 → 跑（服务在时间点之后才启动也会补跑）
                    if (now.hour, now.minute) >= (h, m) and key not in _done:
                        with _lock:
                            _done.add(key)
                            _status["last_run_at"] = now.isoformat(timespec="seconds")
                            _status["runs_total"] += 1
                        ok, msg = _run_crawl()
                        with _lock:
                            if ok:
                                _status["last_success"] = datetime.now().isoformat(timespec="seconds")
                                _status["last_summary"] = msg
                                _status["last_error"] = None
                                logger.info("公众号抓取完成：%s", msg)
                            else:
                                _status["last_error"] = msg
                                logger.warning("公众号抓取失败：%s", msg)
                        break  # 一轮只处理一个 slot
        except Exception as e:  # noqa: BLE001
            logger.warning("公众号调度循环异常：%s", e)
        threading.Event().wait(_CHECK_INTERVAL)


def start_wechat_scheduler(enabled: bool | None = None) -> threading.Thread:
    """启动抓取 daemon 线程。enabled 缺省读 QUANT_WECHAT_SCHEDULE（默认开）。"""
    if enabled is None:
        enabled = settings.wechat_schedule
    with _lock:
        _status["enabled"] = enabled
    t = threading.Thread(target=_loop, args=(enabled,), daemon=True, name="wechat-scheduler")
    t.start()
    logger.info("公众号调度器已启动（enabled=%s，每天 %s 抓增量）",
                enabled, " / ".join(f"{h:02d}:{m:02d}" for h, m in SLOTS))
    return t
