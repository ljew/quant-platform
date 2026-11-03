"""国家队介入强度指数（NSI）每日信号调度器。

**为什么需要它**

`national_team` 策略读的是 `/data/research/nsi_signal.csv`。这个文件由
`nsi_daily.py` 生成，如果不定时刷新，策略在平台上就是一张静态快照 ——
回测永远对，但当日读数会停在部署那天。

**时点选次日 08:30（不是当晚 19:30）**

2026-11-03 实测发现：ETF 份额并非「T 日盘后立刻可得」。前一晚 22:30 运行时，
11-02 的份额还没落地，增量更新只拿到指数数据，算出的 score 是 48.9；
次日 08:57 再跑，份额补齐后同样是 11-02，score 变成 **77.3**。
差了一倍 —— 用当晚的数据等于系统性低估托底强度。

所以改成**次日 08:30**：数据已完整，且仍赶在 09:30 开盘前，与策略
「用 T-1 信号、T 日开盘执行」的 PIT 口径正好对上。

**幂等**

脚本本身是增量更新（只补缺失日期），重复跑不会产生脏数据；
调度器再用 `_done` 集合按「日期#slot」去重，服务中途重启也不会重复执行。
服务在 08:30 之后才启动也会补跑（见 `_loop` 的比较逻辑）。

**不做严格交易日判断**

平台没有独立的交易日历服务，这里按「周一至周五」放行。节假日会白跑一次，
但增量更新发现没新数据就直接退出，成本可忽略；反过来漏跑会断信号，代价更大。
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("nsi_scheduler")

SLOTS = [(8, 30)]
_CHECK_INTERVAL = 60          # 秒
_TIMEOUT = 600                # 单次上限：正常 30–60s，留足 tushare 重试余量

_status = {
    "enabled": False,
    "slots": [f"{h:02d}:{m:02d}" for h, m in SLOTS],
    "last_run_at": None,
    "last_success": None,
    "last_error": None,
    "last_summary": None,
    "last_score": None,
    "last_position": None,
    "runs_total": 0,
}
_done: set[str] = set()
_lock = threading.Lock()


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_status() -> dict:
    with _lock:
        return {**_status, "done_today": sorted(k for k in _done if k.startswith(_today()))}


def _script() -> Path | None:
    """定位 nsi_daily.py：容器在 /data/research，本机在 <项目根>/scripts/research。"""
    here = Path(__file__).resolve()
    roots = [
        Path("/data/research/nsi_daily.py"),                          # 容器（bind 挂载）
        here.parents[2] / "data" / "research" / "nsi_daily.py",       # 本机（项目根/data）
        here.parents[3] / "scripts" / "research" / "nsi_daily.py",    # 本机（项目根/scripts）
    ]
    for p in roots:
        if p.exists():
            return p
    return None


def _run_update() -> tuple[bool, str]:
    script = _script()
    if script is None:
        return False, "找不到 nsi_daily.py"
    cmd = [sys.executable, str(script)]
    logger.info("NSI 信号更新开始：%s", " ".join(cmd))
    try:
        # 脚本会从环境变量读 QUANT_TUSHARE_TOKEN；容器已注入，无需额外传
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT,
                           cwd=str(script.parent))
    except subprocess.TimeoutExpired:
        return False, f"更新超时（>{_TIMEOUT}s）"
    except Exception as e:  # noqa: BLE001
        return False, f"启动失败：{e}"

    out = (r.stdout or "") + (r.stderr or "")
    summary, score, pos = "", None, None
    for line in (r.stdout or "").splitlines():
        if "[NSI]" in line and "score=" in line:
            summary = line.strip()
            # 形如：[NSI] 2026-11-02  score=48.9（NSI 48.9 + 位置惩罚 +0.0×4）
            try:
                score = float(line.split("score=")[1].split("（")[0])
            except (IndexError, ValueError):
                pass
        if "仓位=" in line:
            try:
                pos = float(line.split("仓位=")[1].split("%")[0]) / 100.0
            except (IndexError, ValueError):
                pass
    with _lock:
        if score is not None:
            _status["last_score"] = score
        if pos is not None:
            _status["last_position"] = pos

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
                    if (now.hour, now.minute) >= (h, m) and key not in _done:
                        with _lock:
                            _done.add(key)
                            _status["last_run_at"] = now.isoformat(timespec="seconds")
                            _status["runs_total"] += 1
                        ok, msg = _run_update()
                        with _lock:
                            if ok:
                                _status["last_success"] = datetime.now().isoformat(timespec="seconds")
                                _status["last_summary"] = msg
                                _status["last_error"] = None
                                logger.info("NSI 信号更新完成：%s", msg)
                            else:
                                _status["last_error"] = msg
                                logger.warning("NSI 信号更新失败：%s", msg)
                        break
        except Exception as e:  # noqa: BLE001
            logger.warning("NSI 调度循环异常：%s", e)
        threading.Event().wait(_CHECK_INTERVAL)


def run_now() -> tuple[bool, str]:
    """立即执行一次更新（供 API 手动触发 / 运维排查）。"""
    with _lock:
        _status["last_run_at"] = datetime.now().isoformat(timespec="seconds")
        _status["runs_total"] += 1
    ok, msg = _run_update()
    with _lock:
        if ok:
            _status["last_success"] = datetime.now().isoformat(timespec="seconds")
            _status["last_summary"] = msg
            _status["last_error"] = None
        else:
            _status["last_error"] = msg
    return ok, msg


def start_nsi_scheduler(enabled: bool = True) -> threading.Thread:
    """启动 NSI 信号更新 daemon 线程。"""
    with _lock:
        _status["enabled"] = enabled
    t = threading.Thread(target=_loop, args=(enabled,), daemon=True, name="nsi-scheduler")
    t.start()
    logger.info("NSI 调度器已启动（enabled=%s，每交易日 %s 更新信号）",
                enabled, " / ".join(f"{h:02d}:{m:02d}" for h, m in SLOTS))
    return t
