#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
看门狗逻辑自检 · 跨平台（Mac 上就能跑）

不碰真实进程、不真重启、不真发网络告警：把探测、时钟、重启、告警通道全部换成假的，
只验证状态机 —— 什么时候该重启、什么时候该闭嘴、什么时候该升级。

跑法：
    cd qmt/watchdog && python3 selftest.py
    python3 watchdog.py --selftest
"""

import os
import sys
import json
import tempfile
import shutil
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watchdog as wd  # noqa: E402

PASS, FAIL = [], []
DAY = "2026-11-02"                       # 周一
DAYS = [DAY, "2026-11-03", "2026-11-04"]


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         "" if cond else "  → " + str(detail)[:200]))


class FakeClock(object):
    def __init__(self, start):
        self.dt = start

    def now(self):
        return self.dt

    def ts(self):
        return self.dt.timestamp()

    def tick(self, minutes):
        self.dt = self.dt + timedelta(minutes=minutes)


class FakeProbe(wd.Probe):
    """三级心跳全部脚本化。"""

    def __init__(self, cfg, clock, calendar):
        wd.Probe.__init__(self, cfg, now_fn=clock.now)
        self.calendar = calendar
        self.l1 = True
        self.l2 = {"ok": True, "reason": "", "hb_age_min": 3}
        self.l3 = {"done": True, "alerts": []}
        self.l1_calls = 0

    def l1_process(self):
        self.l1_calls += 1
        return self.l1, ("XtItClient.exe" if self.l1 else None)

    def l2_alive(self):
        return dict(self.l2)

    def l3_result(self):
        return dict(self.l3)

    def snapshot(self):
        alive, name = self.l1_process()
        return {"l1": {"ok": alive, "proc": name},
                "l2": self.l2_alive(),
                "l3": self.l3_result()}


class FakeAlerter(object):
    def __init__(self):
        self.sent = []

    def send(self, title, lines, escalate=False):
        self.sent.append({"title": ("【人工介入】" if escalate else "") + title,
                          "lines": lines})
        return True


def fresh(clock, restart_result=None, overrides=None):
    """每个场景独立目录 + 独立状态，避免跨场景污染与时钟回拨。"""
    tmp = tempfile.mkdtemp(prefix="wd_st_")
    cal_path = os.path.join(tmp, "calendar.json")
    with open(cal_path, "w", encoding="utf-8") as f:
        json.dump({"trade_dates": DAYS}, f)
    cfg = wd.deep_merge(wd.DEFAULT_CONFIG, {
        "qmt": {"process_names": ["XtItClient.exe"],
                "exe_path": os.path.join(tmp, "fake_qmt.exe")},
        "action": {"fail_threshold": 3, "cooldown_sec": 600,
                   "restart_wait_sec": 0, "max_restart_per_day": 3},
        "alert": {"dedup_sec": 1800, "escalate_after": 3, "channel": "log"},
        "calendar_file": "calendar.json",
    })
    if overrides:
        cfg = wd.deep_merge(cfg, overrides)
    cal = wd.Calendar(cal_path)
    state = wd.State(os.path.join(tmp, "state.json"),
                     now_fn=clock.ts, today_fn=lambda: clock.now().date())
    alerter = FakeAlerter()
    probe = FakeProbe(cfg, clock, cal)
    if restart_result is not None:
        wd.restart_qmt = lambda c, log_fn=None: restart_result
    else:
        wd.restart_qmt = lambda c, log_fn=None: (True, "模拟重启成功")
    return tmp, (cfg, cal, state, alerter, probe)


def run(ctx, dry_run=False):
    cfg, cal, state, alerter, probe = ctx
    wd.time.sleep = lambda *_a, **_k: None     # 自检不真睡
    return wd.run_once(cfg, state, alerter, probe,
                       log_fn=lambda s: None, dry_run=dry_run)


def titles(alerter):
    return [a["title"] for a in alerter.sent]


def at(hm, day=DAY):
    return FakeClock(datetime.strptime(day + " " + hm, "%Y-%m-%d %H:%M"))


def main():
    try:
        print("\n[1] 非交易日 / 值守时段外 → 彻底闭嘴")
        clock = at("10:00", "2026-11-01")      # 周日
        tmp, ctx = fresh(clock)
        out = run(ctx)
        check("周日跳过", out["skipped"] == "非交易日", out.get("skipped"))
        shutil.rmtree(tmp, ignore_errors=True)

        clock = at("08:00")
        tmp, ctx = fresh(clock)
        out = run(ctx)
        check("开盘前跳过", bool(out["skipped"]), out.get("skipped"))
        shutil.rmtree(tmp, ignore_errors=True)

        clock = at("16:00")
        tmp, ctx = fresh(clock)
        out = run(ctx)
        check("收盘后跳过", bool(out["skipped"]), out.get("skipped"))
        shutil.rmtree(tmp, ignore_errors=True)

        print("\n[2] 一切正常 → 零动作零告警")
        clock = at("09:30")
        tmp, ctx = fresh(clock)
        out = run(ctx)
        check("无跳过", out["skipped"] is None, out.get("skipped"))
        check("无动作", out["actions"] == [], out["actions"])
        check("无告警", out["alerts"] == [], out["alerts"])
        check("未发任何通知", ctx[3].sent == [], ctx[3].sent)
        shutil.rmtree(tmp, ignore_errors=True)

        print("\n[3] 进程挂了 → 连续 3 次才动手（防抖，别被一次抖动骗去重启）")
        clock = at("09:30")
        tmp, ctx = fresh(clock)
        cfg, cal, state, alerter, probe = ctx
        probe.l1 = False
        outs = []
        for _ in range(3):
            outs.append(run(ctx))
            clock.tick(2)
        check("前 2 次不动手", all("重启" not in " ".join(o["actions"]) for o in outs[:2]),
              outs[:2])
        check("第 3 次动手", any("重启" in " ".join(o["actions"]) for o in outs[2:]),
              outs[2]["actions"])
        check("失败计数清零", state.data["consec_fail"] == 0, state.data["consec_fail"])
        check("重启计数 +1", state.restarts_today() == 1, state.data["restarts"])
        check("重启后等待回报", state.data["awaiting_recovery"] is True,
              state.data.get("awaiting_recovery"))

        print("\n[4] 重启后冷却期 → 不再重复动手（重启完要给客户端加载的时间）")
        outs = []
        for _ in range(3):
            outs.append(run(ctx))
            clock.tick(2)
        check("冷却期内重启次数不变", state.restarts_today() == 1, state.restarts_today())
        check("动作里写明冷却期",
              any("冷却期" in a for o in outs for a in o["actions"]),
              [o["actions"] for o in outs])
        shutil.rmtree(tmp, ignore_errors=True)

        print("\n[5] 重启额度耗尽 → 升级告警，且不会无限重启")
        clock = at("09:30")
        tmp, ctx = fresh(clock, overrides={
            "action": {"fail_threshold": 1, "cooldown_sec": 600,
                       "restart_wait_sec": 0, "max_restart_per_day": 3}})
        cfg, cal, state, alerter, probe = ctx
        probe.l1 = False
        for _ in range(6):
            run(ctx)
            clock.tick(11)               # 刚好越过 10 分钟冷却
        check("重启不超过上限 3", state.restarts_today() <= 3, state.restarts_today())
        check("发出额度耗尽告警", any("重启额度耗尽" in t for t in titles(alerter)), titles(alerter))
        check("额度耗尽带人工介入标记",
              any("人工介入" in t and "额度" in t for t in titles(alerter)), titles(alerter))
        shutil.rmtree(tmp, ignore_errors=True)

        print("\n[6] 重启一直失败 → 累计到阈值后升级")
        clock = at("09:30")
        tmp, ctx = fresh(clock, restart_result=(False, "启动失败：模拟"))
        cfg, cal, state, alerter, probe = ctx
        probe.l1 = False
        for _ in range(10):
            run(ctx)
            clock.tick(45)
        t2 = titles(alerter)
        check("发出重启失败告警", any("重启失败" in t for t in t2), t2)
        check("失败累计后升级", any("人工介入" in t and "重启失败" in t for t in t2), t2)
        shutil.rmtree(tmp, ignore_errors=True)

        print("\n[7] L2 心跳过期 → 告警一次，去重不发第二遍，且绝不自动重启")
        clock = at("09:30")
        tmp, ctx = fresh(clock)
        cfg, cal, state, alerter, probe = ctx
        probe.l2 = {"ok": False, "reason": "心跳过期 90 分钟", "hb_age_min": 90}
        run(ctx)
        n1 = len(alerter.sent)
        clock.tick(3)
        run(ctx)
        check("心跳异常告警一次", n1 == 1, titles(alerter))
        check("去重复不再发", len(alerter.sent) == 1, titles(alerter))
        check("L2 不触发重启", all("重启" not in t for t in titles(alerter)), titles(alerter))
        shutil.rmtree(tmp, ignore_errors=True)

        print("\n[8] 找不到日志文件 → 报「心跳源缺失」，而不是假装一切正常")
        clock = at("09:30")
        tmp, ctx = fresh(clock)
        cfg, cal, state, alerter, probe = ctx
        probe.l2 = {"ok": None, "reason": "未找到策略日志文件", "hb_age_min": None}
        run(ctx)
        check("心跳源缺失告警", any("心跳源缺失" in t for t in titles(alerter)), titles(alerter))
        shutil.rmtree(tmp, ignore_errors=True)

        print("\n[9] 收盘后无 [done] → 告警；策略 [alert] → 单独告警")
        clock = at("15:10")
        tmp, ctx = fresh(clock)
        cfg, cal, state, alerter, probe = ctx
        probe.l3 = {"done": False, "alerts": ["[alert] ANOMALY 门控开启但无买单"]}
        run(ctx)
        t3 = titles(alerter)
        check("当日未执行告警", any("当日未执行" in t for t in t3), t3)
        check("策略异常告警", any("策略报异常" in t for t in t3), t3)
        shutil.rmtree(tmp, ignore_errors=True)

        print("\n[10] 15:05 之前不催 [done]（盘中别瞎叫）")
        clock = at("14:00")
        tmp, ctx = fresh(clock)
        cfg, cal, state, alerter, probe = ctx
        probe.l3 = {"done": False, "alerts": []}
        run(ctx)
        check("盘中不报未执行", all("当日未执行" not in t for t in titles(alerter)),
              titles(alerter))
        shutil.rmtree(tmp, ignore_errors=True)

        print("\n[11] 进程恢复 → 发恢复通知，且只发一次")
        clock = at("09:30")
        tmp, ctx = fresh(clock)
        cfg, cal, state, alerter, probe = ctx
        probe.l1 = False
        for _ in range(3):
            run(ctx)
            clock.tick(2)
        clock.tick(45)
        probe.l1 = True
        run(ctx)
        before = len(alerter.sent)
        clock.tick(3)
        run(ctx)
        check("发出恢复通知", any("已恢复" in t for t in titles(alerter)), titles(alerter))
        check("恢复通知不重复", len(alerter.sent) == before, len(alerter.sent))
        shutil.rmtree(tmp, ignore_errors=True)

        print("\n[12] 状态跨日重置（重启额度不该跨日累计）")
        clock = at("09:30")
        tmp, ctx = fresh(clock)
        cfg, cal, state, alerter, probe = ctx
        probe.l1 = False
        for _ in range(4):
            run(ctx)
            clock.tick(45)
        n_today = state.restarts_today()
        clock.dt = datetime.strptime("2026-11-03 09:30", "%Y-%m-%d %H:%M")
        st2 = wd.State(os.path.join(tmp, "state.json"),
                       now_fn=clock.ts, today_fn=lambda: clock.now().date())
        check("新的一天额度清零", st2.restarts_today() == 0,
              "昨日 %d → 今日 %d" % (n_today, st2.restarts_today()))
        shutil.rmtree(tmp, ignore_errors=True)

        print("\n[13] dry-run 演练：不真重启、不真发告警")
        clock = at("09:30")
        tmp, ctx = fresh(clock)
        cfg, cal, state, alerter, probe = ctx
        probe.l1 = False
        for _ in range(3):
            run(ctx, dry_run=True)
            clock.tick(2)
        check("dry-run 未调真重启", state.restarts_today() == 0, state.data["restarts"])
        check("dry-run 未发告警", alerter.sent == [], alerter.sent)
        shutil.rmtree(tmp, ignore_errors=True)

        print("\n[14] 单实例锁（防止计划任务把看门狗堆成一堆）")
        tmp = tempfile.mkdtemp(prefix="wd_lock_")
        lock_path = os.path.join(tmp, "w.lock")
        a = wd.SingleInstance(lock_path, alive_fn=lambda pid: True)
        check("第一个实例拿到锁", a.acquire() is True)
        b = wd.SingleInstance(lock_path, alive_fn=lambda pid: True)
        check("第二个实例被拒", b.acquire() is False)
        c = wd.SingleInstance(lock_path, alive_fn=lambda pid: False)
        check("持锁进程已死可接管", c.acquire() is True)
        shutil.rmtree(tmp, ignore_errors=True)

        print("\n[15] 日历解析与兜底")
        tmp = tempfile.mkdtemp(prefix="wd_cal_")
        cal_path = os.path.join(tmp, "calendar.json")
        with open(cal_path, "w", encoding="utf-8") as f:
            json.dump({"trade_dates": DAYS}, f)
        c_ok = wd.Calendar(cal_path)
        check("日历命中交易日", c_ok.is_trading_day(datetime.strptime(DAY, "%Y-%m-%d").date()))
        check("日历排除周末", not c_ok.is_trading_day(
            datetime.strptime("2026-11-01", "%Y-%m-%d").date()))
        c_bad = wd.Calendar(os.path.join(tmp, "nope.json"))
        check("缺失时退化为工作日", c_bad.fallback and c_bad.is_trading_day(
            datetime.strptime(DAY, "%Y-%m-%d").date()))
        shutil.rmtree(tmp, ignore_errors=True)

        print("\n[16] 真实日志文件扫描（hb / done / alert 三标记解析）")
        tmp = tempfile.mkdtemp(prefix="wd_log_")
        logf = os.path.join(tmp, "fake_qmt.log")
        with open(logf, "w", encoding="utf-8") as f:
            f.write("[hb] %s 09:35 alive\n" % DAY)
            f.write("[hb] %s 13:50 alive\n" % DAY)
            f.write("[done] %s · 门控=开 · 候选 12 · 买 2 · 卖 1\n" % DAY)
            f.write("[alert] ANOMALY 测试异常行\n")
        info = wd.scan_logs([logf], wd.DEFAULT_CONFIG, today=DAY)
        check("解析到最新心跳", info["hb_last"] ==
              datetime.strptime(DAY + " 13:50", "%Y-%m-%d %H:%M"), info["hb_last"])
        check("解析到 done", info["done_today"] is True, info)
        check("解析到 alert", len(info["alerts"]) == 1, info["alerts"])
        shutil.rmtree(tmp, ignore_errors=True)

        print("\n[17] 单文件策略心跳确实存在且节流生效（看门狗 L2 的抓手）")
        import io
        import contextlib
        strat_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "fresh_ma_qmt.py")
        check("单文件策略存在", os.path.exists(strat_path), strat_path)
        with open(strat_path, encoding="utf-8") as f:
            src = f.read()
        check("handlebar 里调用了 heartbeat", "heartbeat(today)" in src)
        check("心跳标记是 [hb]", '[hb]' in src)

        import importlib.util
        import time as _real_time
        spec = importlib.util.spec_from_file_location("fm_strat", strat_path)
        fm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fm)

        class FakeTime(object):
            def __init__(self):
                self.hm = "09:00"

            def strftime(self, fmt):
                return self.hm

        fm.time = FakeTime()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fm.heartbeat("2026-11-02")
            n1 = buf.getvalue().count("[hb]")
            fm.time.hm = "09:05"
            fm.heartbeat("2026-11-02")
            n2 = buf.getvalue().count("[hb]")
            fm.time.hm = "09:20"
            fm.heartbeat("2026-11-02")
            n3 = buf.getvalue().count("[hb]")
        check("首次调用打心跳", n1 == 1, n1)
        check("15 分钟内被节流", n2 == 1, n2)
        check("超过间隔再打一次", n3 == 2, n3)
        check("心跳行格式可被解析", "[hb] 2026-11-02 09:00 alive" in buf.getvalue(),
              buf.getvalue().strip()[:80])
        fm.time = _real_time      # 还原，别影响后续场景

        print("\n[18] 客户端被重启后，防超单不能失效（看门狗自己引入的风险）")

        class FakeOrder(object):
            def __init__(self, remark, code, status, vol=100, px=10.0):
                self.m_strRemark = remark
                self.m_strInstrumentID = code
                self.m_nOrderStatus = status
                self.m_nVolumeTotalOriginal = vol
                self.m_dLimitPrice = px

        fm.PENDING.clear()                      # 模拟「重启后内存清空」
        fm.get_trade_detail_data = lambda acct, t, k: [
            FakeOrder("fm_600148_BUY", "600148.SH", 50),    # 已报未终结 → 应恢复
            FakeOrder("fm_600519_SELL", "600519.SH", 53),   # 已成（终态）→ 不恢复
            FakeOrder("other_strategy", "000001.SZ", 50),   # 别家策略的单 → 不认
        ]
        n = fm.sync_pending_from_broker(force=True)
        check("恢复 1 笔未终结委托", n == 1, fm.PENDING)
        check("终态委托不恢复", "fm_600519_SELL" not in fm.PENDING, list(fm.PENDING))
        check("只认本策略的 uid", all(k.startswith("fm_") for k in fm.PENDING),
              list(fm.PENDING))
        check("has_open_order 命中已挂单标的",
              fm.has_open_order("600148.SH") is True, fm.PENDING)
        check("别家标的的挂单不误伤",
              fm.has_open_order("000001.SZ") is False, fm.PENDING)
        check("恢复项带来源标记",
              fm.PENDING.get("fm_600148_BUY", {}).get("from_broker") is True,
              fm.PENDING.get("fm_600148_BUY"))

    finally:
        pass

    print("\n%s  通过 %d · 失败 %d" % ("=" * 46, len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAIL: " + f)
        return 1
    print("全部通过")
    return 0


def run_selftest():
    return main()


if __name__ == "__main__":
    sys.exit(main())
