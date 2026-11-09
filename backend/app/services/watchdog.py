"""看门狗：定期体检框架四层，异常发邮件 + 事件留痕，必要时自动补跑。

为什么放在进程内跑
------------------
本机 launchd 受 macOS TCC 限制（项目曾长期在 ~/Desktop 下），历史上反复出现
「服务没起来但没人知道」。进程内自检能覆盖最常见的**静默故障**：
数据滞后、流水线漏跑、研究层异常 —— 这些都是以前发生过、且没有任何告警的。
进程本身挂掉由外部守护负责（launchd 每 10 分钟探活，见 plist）。

防打扰
------
同一组异常（指纹）在 COOLDOWN 内只发一次邮件；恢复正常时补一封「已恢复」。
否则每 10 分钟一封会把邮箱淹没，最后你就把告警当噪音忽略了。

自愈（保守）
------------
只做一件有把握的事：交易日 17:35 之后仍没有当日成功的 research_run → 自动触发
一次编排（start_run 幂等）。**不改数据、不删记录**，其它情况只告警。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from app.config import DATA_DIR
from app.database import SessionLocal
from app.services.framework_health import OK

CHECK_INTERVAL_SEC = 600          # 体检间隔：10 分钟
COOLDOWN_SEC = 6 * 3600           # 同指纹告警冷却：6 小时
HEAL_AFTER_HOUR, HEAL_AFTER_MIN = 17, 35

_state: dict = {
    "runs": 0,
    "last_check": None,
    "last_fp": "",
    "last_sent_at": 0.0,
    "last_status": "",
    "last_heal": None,
    "last_error": None,
}

# 告警记账持久化。为什么必须落盘：_state 在内存里，容器一重启 last_fp/last_sent_at
# 就归零 → 同一个故障会重新发一轮告警（2026-11-02 夜间 OOM 反复重启，
# 同一指纹连发 5 封，是典型的「狼来了」制造机）。
_STATE_FILE = Path(DATA_DIR) / "state" / "watchdog.json"


def _load_state() -> None:
    try:
        if _STATE_FILE.exists():
            saved = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            for k in ("last_fp", "last_sent_at", "last_status", "last_heal"):
                if k in saved:
                    _state[k] = saved[k]
    except Exception as e:  # noqa: BLE001
        logger.warning("看门狗状态读取失败（按全新处理）：%s", e)


def _save_state() -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(
            json.dumps({k: _state[k] for k in
                        ("last_fp", "last_sent_at", "last_status", "last_heal")},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("看门狗状态写入失败：%s", e)

# 用 uvicorn 的 logger：应用自建的 logger 在容器里没有 handler，日志会石沉大海
# （`docker logs` 看不到，等于没法确认看门狗到底跑没跑）。
logger = logging.getLogger("uvicorn.error")


def _fp(health: dict) -> str:
    # ⚠️ framework_health 的状态常量是**大写** OK/WARN/FAIL；用小写比较会让
    #    所有检查项都落进「异常」，告警邮件全是噪音（2026-11-02 踩过）。
    bad = sorted(f"{x['key']}:{x['status']}" for x in health["layers"]
                 if x["status"] != OK)
    return hashlib.md5("|".join(bad).encode()).hexdigest()[:10] if bad else ""


def _bad_items(health: dict) -> list[tuple[str, str, str]]:
    """返回 [(层名, 项名, 详情)]，只取非 OK 的。"""
    out = []
    for layer in health["layers"]:
        for it in layer["items"]:
            if it["status"] != OK:
                out.append((layer["name"], it["label"], it["detail"]))
    return out


def _render(health: dict, recovered: bool = False) -> tuple[str, str]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if recovered:
        return (f"量化平台已恢复 · {now}",
                "<div style='font-family:-apple-system;font-size:14px;line-height:1.7'>"
                "<p><b>四层框架体检已恢复正常</b>，无需处理。</p>"
                f"<p style='color:#888;font-size:12px'>{now} 自动检测</p></div>")

    rows = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'>{layer}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{label}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{detail}</td></tr>"
        for layer, label, detail in _bad_items(health)
    )
    html = (
        "<div style='font-family:-apple-system;font-size:14px;line-height:1.7'>"
        f"<h3 style='color:#c0392b;margin:0 0 12px'>量化平台异常 · {now}</h3>"
        f"<p>体检结论：<b>{health['worst']}</b></p>"
        "<table style='border-collapse:collapse;font-size:13px;width:100%'>"
        "<tr style='background:#f7f7f7'><th align='left' style='padding:6px 10px'>层</th>"
        "<th align='left' style='padding:6px 10px'>检查项</th>"
        "<th align='left' style='padding:6px 10px'>现状</th></tr>"
        f"{rows}</table>"
        "<p style='margin-top:14px;color:#555;font-size:13px'>"
        "自愈动作：若为「今日编排未跑」，17:35 后会自动补跑一次；其余需人工确认。</p>"
        f"<p style='color:#888;font-size:12px'>{now} 自动检测 · 同类告警 6 小时内不重复</p>"
        "</div>"
    )
    return f"量化平台异常 · {now}（{health['worst']}）", html


def _maybe_heal(db, health: dict) -> str | None:
    """保守自愈：交易日 17:35 后当日仍无成功 run → 触发一次编排。"""
    now = datetime.now()
    if (now.hour, now.minute) < (HEAL_AFTER_HOUR, HEAL_AFTER_MIN):
        return None
    try:
        from app.core.trading_calendar import is_trading_day
        if not is_trading_day(now.date()):
            return None
        # 顺带补模拟盘：启用但还没建任务的策略，达标就自动建（幂等）
        try:
            from app.services.paper_auto import ensure_paper_for_enabled
            r = ensure_paper_for_enabled(db)
            if r["created"]:
                logger.info("自动补建模拟盘：%s",
                            ", ".join(f"{c['strategy_key']}#{c['task_id']}"
                                      for c in r["created"]))
        except Exception as e:  # noqa: BLE001
            logger.info("自动补建模拟盘失败：%s", str(e)[:120])

        from app.services import orchestrator
        if orchestrator.STATE.get("running"):
            return None
        if orchestrator._daily_state.get("last_run_date") == now.date().isoformat():
            return None
        rid = orchestrator.start_run(db, trigger="watchdog")
        _state["last_heal"] = f"{now:%H:%M} 补跑 run#{rid}"
        logger.warning("watchdog 触发补跑：run#%s", rid)
        return f"已自动补跑编排 run#{rid}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("watchdog 自愈失败：%s", exc)
        return None


def check_once(send_mail: bool = True) -> dict:
    """跑一次体检（可被 /monitor 或脚本直接调用）。返回摘要 dict。"""
    from app.services.framework_health import framework_health
    from app.services import mailer

    db = SessionLocal()
    try:
        health = framework_health(db)
        fp = _fp(health)
        now_ts = datetime.now().timestamp()
        summary = {
            "worst": health["worst"],
            "fingerprint": fp,
            "bad": len(_bad_items(health)),
            "mail": "skip",
            "heal": None,
        }

        if not fp:
            # 已恢复：此前发过告警才补一封恢复通知
            if _state["last_fp"] and send_mail:
                subject, html = _render(health, recovered=True)
                r = mailer.send_email_html(db, subject, html,
                                           dedup_key=f"watchdog-recover|{health['checked_at'][:10]}")
                summary["mail"] = "recovered" if r.get("ok") and not r.get("skipped") else "skip"
            _state.update(last_fp="", last_status=OK)
            _save_state()
            return summary

        summary["heal"] = _maybe_heal(db, health)
        cooling = (fp == _state["last_fp"]
                   and now_ts - _state["last_sent_at"] < COOLDOWN_SEC)
        if send_mail and not cooling:
            subject, html = _render(health)
            r = mailer.send_email_html(
                db, subject, html,
                dedup_key=f"watchdog|{fp}|{health['checked_at'][:10]}",  # 同指纹每天最多一封
            )
            ok = bool(r.get("ok")) and not r.get("skipped")
            summary["mail"] = "sent" if ok else ("skipped" if r.get("skipped") else "fail")
            if ok:
                _state["last_sent_at"] = now_ts
        _state.update(last_fp=fp, last_status=health["worst"])
        _save_state()
        return summary
    finally:
        db.close()


_sched_started = False


def start_watchdog(interval_sec: int | None = None) -> None:
    """启动看门狗线程（daemon，随进程退出）。"""
    global _sched_started
    if _sched_started:
        logger.info("调度器已在运行，跳过重复启动（start_watchdog）")
        return
    interval = int(interval_sec or CHECK_INTERVAL_SEC)

    _load_state()

    def _loop() -> None:
        # 启动后先等 2 分钟：避免与 init_db/调度器启动抢资源，也避开冷启动噪音
        threading.Event().wait(120)
        while True:
            try:
                r = check_once()
                _state.update(runs=_state["runs"] + 1,
                              last_check=datetime.now().isoformat(timespec="seconds"),
                              last_error=None)
                if r["fingerprint"]:
                    logger.warning("watchdog: %s（%d 项异常，mail=%s，heal=%s）",
                                   r["worst"], r["bad"], r["mail"], r["heal"])
                else:
                    logger.info("watchdog: ok")
                _ = r
            except Exception as exc:  # noqa: BLE001
                _state["last_error"] = str(exc)[:300]
                logger.warning("watchdog 体检失败：%s", exc)
            threading.Event().wait(interval)

    threading.Thread(target=_loop, daemon=True, name="watchdog").start()
    _sched_started = True
    logger.info("看门狗已启动（每 %ss 体检一次）", interval)


def status() -> dict:
    return dict(_state)
