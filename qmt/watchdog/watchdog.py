#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
QMT 看门狗 · Windows 端常驻守护（纯标准库，Python 3.6+）

为什么需要它：
    策略跑在 QMT 封闭沙箱里 —— 不能写文件、不能联网、不能推告警。
    策略一旦卡死（客户端崩了、策略没被加载、handlebar 不再触发），
    外面完全看不见。这个脚本跑在 QMT 外面，替它盯着。

三级心跳：
    L1 进程   QMT 客户端进程在不在
    L2 活性   策略 handlebar 还在被触发吗（靠 [hb] 心跳行，节流 15 分钟一行）
    L3 结果   今天真的执行完了吗（[done]）、有没有异常（[alert]）

处置策略：
    L1 连续失败 N 次 → 杀进程 → 重启客户端 → 冷却 → 复查
    重启当日超过上限 → 不再重启，只告警（防止重启风暴）
    L2/L3 失败       → 只告警，不重启（多数是行情/候选问题，重启没用）
    恢复             → 发一条恢复通知，避免"告警了但不知道好了没"

用法：
    python watchdog.py                 # 常驻循环
    python watchdog.py --once          # 只跑一轮（计划任务保活用）
    python watchdog.py --probe-logs    # 列出候选日志路径命中情况（首次部署必跑）
    python watchdog.py --status        # 打印当前状态与三级心跳结果
    python watchdog.py --selftest      # 逻辑自检（注入假探测，不碰真实进程）

依赖：无第三方。日历文件 calendar.json（由平台导出）缺失时退化为周一至周五。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import glob as globmod
from datetime import datetime, date, timedelta

IS_WIN = sys.platform.startswith("win")

# ══════════════════════ 默认配置 ══════════════════════

DEFAULT_CONFIG = {
    "qmt": {
        # 客户端进程名（tasklist 匹配），按顺序找，命中任一即认为进程在
        "process_names": ["XtItClient.exe", "XtTradeClient.exe", "QMT.exe"],
        # 客户端主程序绝对路径，重启用。--probe-logs 会提示常见位置
        "exe_path": "",
        # 启动参数（有些券商版本需要带账号/静默参数）
        "exe_args": [],
        # 工作目录（一般设成 exe 所在目录）
        "exe_cwd": "",
    },
    "logs": {
        # 策略 print 输出落到哪里，各券商版本不一样。支持 glob 与 %VAR%
        # 首次部署先跑 --probe-logs 看哪个命中，把命中的留下
        "globs": [
            r"%USERPROFILE%\\.xtquant\\log\\*.log",
            r"D:\\QMT\\bin.x64\\userdata\\log\\*.log",
            r"D:\\*QMT*\\bin.x64\\userdata\\log\\*.log",
            r"%APPDATA%\\QMT\\log\\*.log",
        ],
        # 命中的日志里按修改时间取最新的几个
        "max_files": 3,
        "tail_bytes": 131072,
    },
    "probe": {
        "hb_tag": "[hb]",        # 策略心跳行前缀
        "done_tag": "[done]",    # 当日执行完成
        "alert_tag": "[alert]",  # 异常
        # 交易时段内心跳最大允许间隔（分钟）。策略 15 分钟打一次，留一倍余量
        "hb_max_age_min": 35,
    },
    "schedule": {
        "interval_sec": 120,     # 常驻模式轮询间隔
        "start_time": "08:50",   # 此前不检查（人还没开机）
        "alive_check_from": "09:20",  # L1/L2 检查窗口起点
        "alive_check_to": "15:00",
        "done_check_after": "15:05",  # 此后检查 L3
        "stop_time": "15:45",    # 之后收工，不再打扰
    },
    "action": {
        "fail_threshold": 3,     # 连续失败几次才动手
        "cooldown_sec": 600,     # 重启后冷静期，期内不判定失败
        "restart_wait_sec": 90,  # 启动后等待多久再复查
        "max_restart_per_day": 3,
    },
    "alert": {
        "channel": "wecom",      # wecom / feishu / log
        "webhook": "",           # 也可走环境变量 WATCHDOG_WEBHOOK
        "dedup_sec": 1800,       # 同类告警最小间隔
        "escalate_after": 3,     # 重启失败几次后升级（标题加【人工介入】）
    },
    "calendar_file": "calendar.json",
}


def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = deep_merge(cfg, json.load(f))
    return cfg


# ══════════════════════ 交易日历 ══════════════════════

class Calendar(object):
    """读平台导出的 calendar.json（{"trade_dates": ["2026-01-05", ...]}）。
    文件缺失时退化为周一至周五，并在日志里明确提示口径不可靠。"""

    def __init__(self, path):
        self.path = path
        self.dates = set()
        self.fallback = False
        self._load()

    def _load(self):
        try:
            if not os.path.exists(self.path):
                raise IOError("missing")
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("trade_dates") or data.get("dates") or []
            self.dates = set(str(d)[:10] for d in raw)
            if not self.dates:
                raise ValueError("empty")
        except Exception:
            self.fallback = True

    def is_trading_day(self, d=None):
        d = d or date.today()
        key = d.strftime("%Y-%m-%d")
        if self.fallback:
            return d.weekday() < 5
        return key in self.dates

    def note(self):
        return ("日历缺失，按周一至周五兜底（节假日会误报）"
                if self.fallback else "日历已载入 %d 个交易日" % len(self.dates))


# ══════════════════════ 进程 / 日志探测 ══════════════════════

def _expand(p):
    return os.path.expandvars(p) if IS_WIN else p


def _decode_out(raw):
    """tasklist 在中文 Windows 上输出 GBK，失败时退 UTF-8。只解一次。"""
    raw = raw or b""
    for enc in ("gbk", "utf-8"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("gbk", "ignore")


def proc_alive_by_name(name):
    """tasklist 按进程名查存活。非 Windows 直接返回 False（自检时会被替换）。"""
    if not IS_WIN:
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s" % name, "/NH"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        return name.lower() in _decode_out(out.stdout).lower()
    except Exception:
        return False


def pid_alive(pid):
    if not IS_WIN:
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "PID eq %d" % int(pid), "/NH"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        return str(pid) in _decode_out(out.stdout)
    except Exception:
        return False


def find_log_files(cfg):
    """按 globs 找日志文件，按修改时间倒序取前 max_files 个。"""
    hits = []
    for pattern in cfg["logs"]["globs"]:
        try:
            for p in globmod.glob(_expand(pattern)):
                if os.path.isfile(p):
                    hits.append(p)
        except Exception:
            continue
    hits = sorted(set(hits), key=lambda p: os.path.getmtime(p), reverse=True)
    return hits[:int(cfg["logs"]["max_files"])]


def tail_lines(path, tail_bytes):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            raw = f.read()
        return raw.decode("utf-8", "ignore").splitlines()
    except Exception:
        return []


HB_RE = re.compile(r"\[hb\]\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})")


def scan_logs(files, cfg, today=None):
    """返回 dict: {hb_last: datetime|None, done_today: bool, alerts: [str]}
    today 可注入（自检用），默认系统日期。"""
    tags = cfg["probe"]
    today = today or date.today().strftime("%Y-%m-%d")
    hb_last = None
    done = False
    alerts = []
    for path in files:
        for line in tail_lines(path, int(cfg["logs"]["tail_bytes"])):
            if tags["hb_tag"] in line:
                m = HB_RE.search(line)
                if m:
                    try:
                        dt = datetime.strptime(m.group(1) + " " + m.group(2),
                                               "%Y-%m-%d %H:%M")
                        if hb_last is None or dt > hb_last:
                            hb_last = dt
                    except Exception:
                        pass
            if tags["done_tag"] in line and today in line:
                done = True
            if tags["alert_tag"] in line:
                alerts.append(line.strip()[-160:])
    return {"hb_last": hb_last, "done_today": done, "alerts": alerts[-5:]}


class Probe(object):
    """把三级心跳包成一次探测，便于自检时整体替换。"""

    def __init__(self, cfg, now_fn=None):
        self.cfg = cfg
        self.now_fn = now_fn or datetime.now
        self.log_files = []

    def l1_process(self):
        for name in self.cfg["qmt"]["process_names"]:
            if proc_alive_by_name(name):
                return True, name
        return False, None

    def l2_alive(self):
        self.log_files = find_log_files(self.cfg)
        if not self.log_files:
            return {"ok": None, "reason": "未找到策略日志文件", "hb_age_min": None}
        info = scan_logs(self.log_files, self.cfg, self.now_fn().date())
        if info["hb_last"] is None:
            return {"ok": False, "reason": "当日无 [hb] 心跳",
                    "hb_age_min": None, "info": info}
        age = (self.now_fn() - info["hb_last"]).total_seconds() / 60.0
        ok = age <= float(self.cfg["probe"]["hb_max_age_min"])
        return {"ok": ok, "reason": "" if ok else "心跳过期 %.0f 分钟" % age,
                "hb_age_min": round(age, 1), "info": info}

    def l3_result(self):
        self.log_files = find_log_files(self.cfg)
        info = scan_logs(self.log_files, self.cfg, self.now_fn().date())
        return {"done": info["done_today"], "alerts": info["alerts"], "info": info}

    def snapshot(self):
        alive, name = self.l1_process()
        return {"l1": {"ok": alive, "proc": name},
                "l2": self.l2_alive(),
                "l3": self.l3_result()}


# ══════════════════════ 状态持久化 ══════════════════════

class State(object):
    """重启计数、冷却、告警去重都落盘 —— 看门狗自己也可能被重启，不能只靠内存。"""

    def __init__(self, path, now_fn=None, today_fn=None):
        self.path = path
        self.now_fn = now_fn or time.time
        self.today_fn = today_fn or (lambda: date.today())
        self.data = {"consec_fail": 0, "restarts": {}, "last_restart_ts": 0,
                     "last_restart_ok": None, "alerts": {}, "day": "",
                     "awaiting_recovery": False}
        self._load()
        self._roll_day()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
        except Exception:
            pass

    def _roll_day(self):
        today = self.today_fn().strftime("%Y-%m-%d")
        if self.data.get("day") != today:
            self.data["day"] = today
            self.data["consec_fail"] = 0
            self.data["restarts"] = {}
            self.data["last_restart_ts"] = 0
            self.data["last_restart_ok"] = None
            self.data["alerts"] = {}

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            print("[wd] 状态写盘失败：%s" % e)

    def in_cooldown(self, cooldown_sec):
        return (self.now_fn() - float(self.data.get("last_restart_ts", 0))) < float(cooldown_sec)

    def restarts_today(self):
        return len(self.data.get("restarts", {}))

    def should_alert(self, key, dedup_sec):
        last = float(self.data.get("alerts", {}).get(key, 0))
        return (self.now_fn() - last) >= float(dedup_sec)

    def mark_alert(self, key):
        self.data.setdefault("alerts", {})[key] = self.now_fn()

    def mark_restart(self, ok):
        ts = self.now_fn()
        self.data["restarts"][str(ts)] = "ok" if ok else "fail"
        self.data["last_restart_ts"] = ts
        self.data["last_restart_ok"] = bool(ok)
        self.data["consec_fail"] = 0
        # 重启完必须回报一句「好了 / 还没好」，否则外面只知道出过事，不知道结果
        self.data["awaiting_recovery"] = True


# ══════════════════════ 告警 ══════════════════════

def send_wecom(webhook, title, lines):
    import urllib.request
    body = "\n".join(lines)
    payload = {"msgtype": "text",
               "text": {"content": "%s\n%s" % (title, body)}}
    req = urllib.request.Request(
        webhook, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def send_feishu(webhook, title, lines):
    import urllib.request
    payload = {"msg_type": "text",
               "content": {"text": "%s\n%s" % (title, "\n".join(lines))}}
    req = urllib.request.Request(
        webhook, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


class Alerter(object):
    def __init__(self, cfg, log_fn=None, now_fn=None):
        self.cfg = cfg["alert"]
        self.log_fn = log_fn or (lambda s: None)
        self.now_fn = now_fn or time.time
        self.webhook = self.cfg.get("webhook") or os.environ.get("WATCHDOG_WEBHOOK", "")

    def send(self, title, lines, escalate=False):
        if escalate:
            title = "【人工介入】" + title
        text = title + "\n" + "\n".join(lines)
        self.log_fn("[alert] " + text.replace("\n", " | "))
        ch = self.cfg.get("channel", "log")
        if ch == "log" or not self.webhook:
            self.log_fn("[alert] 通道=%s webhook=%s → 仅本地记录"
                        % (ch, "有" if self.webhook else "空"))
            return False
        try:
            if ch == "wecom":
                send_wecom(self.webhook, title, lines)
            elif ch == "feishu":
                send_feishu(self.webhook, title, lines)
            else:
                self.log_fn("[alert] 未知通道 %s" % ch)
                return False
            return True
        except Exception as e:
            self.log_fn("[alert] 发送失败：%s" % e)
            return False


# ══════════════════════ 重启 ══════════════════════

def restart_qmt(cfg, log_fn=None):
    """杀进程 → 启动客户端。返回 (ok, 说明)。"""
    log_fn = log_fn or (lambda s: None)
    exe = cfg["qmt"].get("exe_path", "")
    if not exe or not os.path.exists(exe):
        return False, "exe_path 未配置或不存在：%r" % exe
    killed = []
    for name in cfg["qmt"]["process_names"]:
        if proc_alive_by_name(name):
            try:
                subprocess.run(["taskkill", "/F", "/IM", name, "/T"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=30)
                killed.append(name)
            except Exception as e:
                log_fn("[wd] 结束 %s 失败：%s" % (name, e))
    time.sleep(5)
    try:
        cwd = cfg["qmt"].get("exe_cwd") or os.path.dirname(exe)
        args = [exe] + list(cfg["qmt"].get("exe_args") or [])
        subprocess.Popen(args, cwd=cwd, close_fds=True)
        return True, "已重启（结束 %s，启动 %s）" % (",".join(killed) or "无存活进程",
                                                    os.path.basename(exe))
    except Exception as e:
        return False, "启动失败：%s" % e


# ══════════════════════ 时段门控 ══════════════════════

def hm_now(now_fn=None):
    return (now_fn or datetime.now)().strftime("%H:%M")


def in_window(hm, start, end):
    return start <= hm <= end


# ══════════════════════ 单轮检查 ══════════════════════

def run_once(cfg, state, alerter, probe, log_fn=None, dry_run=False):
    """返回 dict 描述本轮做了什么。dry_run=True 时不真重启、不真发告警。"""
    log_fn = log_fn or (lambda s: None)
    now = probe.now_fn()
    hm = now.strftime("%H:%M")
    sched = cfg["schedule"]
    act = cfg["action"]
    out = {"skipped": None, "actions": [], "alerts": []}

    if not probe.calendar.is_trading_day(now.date()):
        out["skipped"] = "非交易日"
        return out
    if hm < sched["start_time"] or hm > sched["stop_time"]:
        out["skipped"] = "不在值守时段 %s-%s（当前 %s）" % (
            sched["start_time"], sched["stop_time"], hm)
        return out

    snap = probe.snapshot()
    out["snapshot"] = snap

    # ── L1 进程 ──
    if in_window(hm, sched["alive_check_from"], sched["stop_time"]):
        if not snap["l1"]["ok"]:
            state.data["consec_fail"] = int(state.data.get("consec_fail", 0)) + 1
            n = state.data["consec_fail"]
            log_fn("[wd] L1 进程缺失（连续 %d/%d）" % (n, act["fail_threshold"]))
            if n >= int(act["fail_threshold"]):
                if state.in_cooldown(act["cooldown_sec"]):
                    out["actions"].append("冷却期内，跳过重启")
                elif state.restarts_today() >= int(act["max_restart_per_day"]):
                    out["actions"].append("当日重启已达上限，不再重启")
                    key = "restart_exhausted"
                    if state.should_alert(key, cfg["alert"]["dedup_sec"]):
                        if dry_run:
                            out["alerts"].append("(dry-run) 重启额度耗尽")
                        else:
                            alerter.send("QMT 看门狗：当日重启额度耗尽",
                                         ["机器：%s" % _host(),
                                          "时间：%s" % now.strftime("%Y-%m-%d %H:%M:%S"),
                                          "已重启 %d 次仍未恢复，需要人工介入"
                                          % state.restarts_today()],
                                         escalate=True)
                        state.mark_alert(key)
                else:
                    if dry_run:
                        # 演练模式：只说打算做什么，不碰状态、不真重启、不告警
                        out["actions"].append("(dry-run) 将重启 QMT")
                    else:
                        ok, msg = restart_qmt(cfg, log_fn)
                        time.sleep(int(act["restart_wait_sec"]))
                        ok = probe.l1_process()[0]
                        msg = msg + " · 复查进程=%s" % ("在" if ok else "不在")
                        state.mark_restart(ok)
                        out["actions"].append("重启：%s" % msg)
                        key = "restart_%s" % ("fail" if not ok else "ok")
                        if not ok and state.should_alert(key, cfg["alert"]["dedup_sec"]):
                            fails = len([v for v in state.data["restarts"].values()
                                         if v == "fail"])
                            alerter.send("QMT 看门狗：重启失败",
                                         ["机器：%s" % _host(),
                                          "时间：%s" % now.strftime("%Y-%m-%d %H:%M:%S"),
                                          "当日失败 %d 次：%s" % (fails, msg)],
                                         escalate=fails >= int(cfg["alert"]["escalate_after"]))
                            state.mark_alert(key)
        else:
            # 恢复判定：要么连续失败计数没清零（没到阈值自己好了），
            # 要么刚重启过、正等着确认（重启后必须回报一句，否则不知道好了没）
            if state.data.get("consec_fail") or state.data.get("awaiting_recovery"):
                log_fn("[wd] L1 恢复")
                key = "recover"
                if state.should_alert(key, cfg["alert"]["dedup_sec"]):
                    extra = "（重启后确认）" if state.data.get("awaiting_recovery") else ""
                    if dry_run:
                        out["alerts"].append("(dry-run) 恢复通知")
                    else:
                        alerter.send("QMT 看门狗：已恢复",
                                     ["机器：%s" % _host(),
                                      "时间：%s" % now.strftime("%Y-%m-%d %H:%M:%S"),
                                      "进程 %s 重新在线%s" % (snap["l1"]["proc"], extra)])
                    state.mark_alert(key)
            state.data["consec_fail"] = 0
            state.data["awaiting_recovery"] = False

    # ── L2 活性 ──
    if in_window(hm, sched["alive_check_from"], sched["alive_check_to"]):
        l2 = snap["l2"]
        if l2["ok"] is False:
            key = "l2_stale"
            if state.should_alert(key, cfg["alert"]["dedup_sec"]):
                if dry_run:
                    out["alerts"].append("(dry-run) 策略心跳异常：%s" % l2["reason"])
                else:
                    alerter.send("QMT 看门狗：策略心跳异常",
                                 ["机器：%s" % _host(),
                                  "时间：%s" % now.strftime("%Y-%m-%d %H:%M:%S"),
                                  "现象：%s" % l2["reason"],
                                  "处置：不自动重启（多数是行情/候选问题），请人工看 QMT 界面"])
                state.mark_alert(key)
        elif l2["ok"] is None:
            key = "l2_nolog"
            if state.should_alert(key, cfg["alert"]["dedup_sec"]):
                msg = "找不到日志文件，L2/L3 全部失效。请跑 python watchdog.py --probe-logs 定位路径"
                log_fn("[wd] " + msg)
                if dry_run:
                    out["alerts"].append("(dry-run) " + msg)
                else:
                    alerter.send("QMT 看门狗：心跳源缺失",
                                 ["机器：%s" % _host(), "时间：%s" % now.strftime("%Y-%m-%d %H:%M:%S"),
                                  msg])
                state.mark_alert(key)

    # ── L3 结果 ──
    if hm >= sched["done_check_after"]:
        l3 = snap["l3"]
        if not l3["done"]:
            key = "l3_nodone"
            if state.should_alert(key, cfg["alert"]["dedup_sec"]):
                if dry_run:
                    out["alerts"].append("(dry-run) 当日未执行完成")
                else:
                    alerter.send("QMT 看门狗：当日未执行",
                                 ["机器：%s" % _host(),
                                  "时间：%s" % now.strftime("%Y-%m-%d %H:%M:%S"),
                                  "收盘前未见到 [done]，今天这笔调仓可能没跑成",
                                  "请人工核对 QMT 委托列表"])
                state.mark_alert(key)
        for a in l3["alerts"]:
            key = "l3_alert_" + a[:40]
            if state.should_alert(key, cfg["alert"]["dedup_sec"]):
                if dry_run:
                    out["alerts"].append("(dry-run) 策略异常：%s" % a[:60])
                else:
                    alerter.send("QMT 看门狗：策略报异常",
                                 ["机器：%s" % _host(),
                                  "时间：%s" % now.strftime("%Y-%m-%d %H:%M:%S"),
                                  a])
                state.mark_alert(key)

    state.save()
    return out


def _host():
    try:
        import socket
        return socket.gethostname()
    except Exception:
        return "unknown"


# ══════════════════════ 单实例锁 ══════════════════════

class SingleInstance(object):
    def __init__(self, path, alive_fn=None):
        self.path = path
        self.alive_fn = alive_fn or pid_alive
        self.acquired = False

    def acquire(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    pid = int(f.read().strip() or 0)
                if pid and self.alive_fn(pid):
                    return False
        except Exception:
            pass
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            self.acquired = True
            return True
        except Exception:
            return False

    def release(self):
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except Exception:
            pass


# ══════════════════════ 入口 ══════════════════════

def make_logger(path):
    def log(msg):
        line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
        try:
            print(line)
        except Exception:
            pass
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    return log


def build_context(cfg, base_dir, log_fn=None, now_fn=None):
    cal = Calendar(os.path.join(base_dir, cfg["calendar_file"]))
    st_path = os.path.join(base_dir, "watchdog_state.json")
    today_fn = (lambda: datetime.fromtimestamp(now_fn()).date()) if now_fn else None
    state = State(st_path, now_fn=now_fn, today_fn=today_fn)
    alerter = Alerter(cfg, log_fn=log_fn, now_fn=now_fn)
    probe = Probe(cfg, now_fn=(lambda: datetime.fromtimestamp(now_fn())) if now_fn else None)
    probe.calendar = cal
    return cal, state, alerter, probe


def _bootstrap(base_dir, cfg_path=None):
    """自检与 CLI 共用的装配。"""
    cfg = load_config(cfg_path or os.path.join(base_dir, "watchdog_config.json"))
    log = make_logger(os.path.join(base_dir, "watchdog.log"))
    cal, state, alerter, probe = build_context(cfg, base_dir, log_fn=log)
    return cfg, log, cal, state, alerter, probe


def main(argv=None):
    ap = argparse.ArgumentParser(description="QMT 看门狗")
    ap.add_argument("--config", default="")
    ap.add_argument("--dir", default="", help="工作目录，默认脚本所在目录")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--probe-logs", action="store_true", help="探测日志路径")
    ap.add_argument("--status", action="store_true", help="打印当前三级心跳")
    ap.add_argument("--dry-run", action="store_true", help="不真重启、不真发告警")
    ap.add_argument("--selftest", action="store_true", help="逻辑自检（不碰真实进程）")
    args = ap.parse_args(argv)

    base_dir = args.dir or os.path.dirname(os.path.abspath(__file__))
    cfg_path = args.config or os.path.join(base_dir, "watchdog_config.json")
    cfg = load_config(cfg_path)
    log = make_logger(os.path.join(base_dir, "watchdog.log"))

    if args.probe_logs:
        files = find_log_files(cfg)
        alive, name = Probe(cfg).l1_process()
        print("进程：%s（%s）" % ("在" if alive else "不在", name or "无"))
        print("日历：%s" % Calendar(os.path.join(base_dir, cfg["calendar_file"])).note())
        print("候选日志命中 %d 个：" % len(files))
        for p in files:
            print("  %s  %d bytes  %s" % (
                p, os.path.getsize(p),
                datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S")))
        if not files:
            print("  未命中。请在 QMT 里随便 print 一行，然后在资源管理器搜索该文本所在文件，")
            print("  把路径写进 watchdog_config.json 的 logs.globs")
        return 0

    cal, state, alerter, probe = build_context(cfg, base_dir, log_fn=log)
    log("[wd] 启动 · 配置=%s · %s" % (cfg_path, cal.note()))

    if args.selftest:
        from selftest import run_selftest
        return run_selftest()

    if args.status:
        snap = probe.snapshot()
        print(json.dumps(snap, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.once:
        out = run_once(cfg, state, alerter, probe, log_fn=log, dry_run=args.dry_run)
        log("[wd] 本轮：%s" % json.dumps(out, ensure_ascii=False, default=str))
        return 0

    lock = SingleInstance(os.path.join(base_dir, "watchdog.lock"))
    if not lock.acquire():
        log("[wd] 已有实例在跑，退出")
        return 0
    log("[wd] 常驻循环启动，间隔 %s 秒" % cfg["schedule"]["interval_sec"])
    try:
        while True:
            try:
                out = run_once(cfg, state, alerter, probe, log_fn=log, dry_run=args.dry_run)
                if out.get("skipped"):
                    log("[wd] 跳过：%s" % out["skipped"])
                else:
                    log("[wd] 完成：%s" % json.dumps(out, ensure_ascii=False, default=str))
            except Exception as e:
                log("[wd] 本轮异常：%s" % e)
            time.sleep(int(cfg["schedule"]["interval_sec"]))
    except KeyboardInterrupt:
        pass
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
