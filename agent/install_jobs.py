"""把三条定时任务装成 launchd 服务（每工作日跑一次）。

为什么用 launchd 而不是 dsh 的 schedule：`dsh-schedule` 依赖 Web 的
Session controller，headless 组合里挂不上（见 jobs.py 的注释）。

用法：
    python3 agent/install_jobs.py --print   # 只打印，不落盘
    python3 agent/install_jobs.py           # 写 plist 并尝试加载

⚠️ 沙箱里 `launchctl` 常常连不上用户域（报 Input/output error）。
写进 ~/Library/LaunchAgents/ 后，下次登录会自动加载；想立刻生效，
在「终端」里跑一次：
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.quant.agent-<job>.plist
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from jobs import JOBS

PYTHON = os.path.realpath(os.sys.executable)
AGENT_DIR = Path(__file__).resolve().parent
LA_DIR = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = Path.home() / ".dsh" / "jobs"


def plist(job: str, spec: dict) -> str:
    hh, mm = (int(x) for x in spec["time"].split(":"))
    weekdays = "".join(
        f"""    <dict>
      <key>Weekday</key><integer>{d}</integer>
      <key>Hour</key><integer>{hh}</integer>
      <key>Minute</key><integer>{mm}</integer>
    </dict>
"""
        for d in (1, 2, 3, 4, 5)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.quant.agent-{job}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{PYTHON}</string>
    <string>{AGENT_DIR / "jobs.py"}</string>
    <string>{job}</string>
  </array>
  <key>WorkingDirectory</key><string>{AGENT_DIR.parent}</string>
  <key>StartCalendarInterval</key>
  <array>
{weekdays}  </array>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>{LOG_DIR}/launchd-{job}.log</string>
  <key>StandardErrorPath</key><string>{LOG_DIR}/launchd-{job}.err.log</string>
  <!-- 任务本身有 15 分钟超时；这里只防卡死 -->
  <key>ExitTimeOut</key><integer>120</integer>
</dict>
</plist>
"""


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="install_jobs")
    p.add_argument("--print", dest="dry", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    a = p.parse_args(argv)

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if a.uninstall:
        for job in JOBS:
            f = LA_DIR / f"com.quant.agent-{job}.plist"
            if f.exists():
                f.unlink()
                print("removed", f)
        return 0

    if a.dry:
        print(plist("morning", JOBS["morning"]))
        return 0

    LA_DIR.mkdir(parents=True, exist_ok=True)
    loaded, failed = [], []
    for job, spec in JOBS.items():
        f = LA_DIR / f"com.quant.agent-{job}.plist"
        f.write_text(plist(job, spec), encoding="utf-8")
        print(f"写入 {f}（每工作日 {spec['time']} · {spec['title']}）")
        r = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(f)],
            capture_output=True, text=True)
        (loaded if r.returncode == 0 else failed).append(job)
        if r.returncode != 0 and r.stderr.strip():
            print(f"  ⚠️ 立即加载失败：{r.stderr.strip()[:120]}")

    if failed:
        print("\nplist 已写好，下次登录会自动生效。想现在生效，在「终端」里执行：")
        for job in failed:
            print(f"  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.quant.agent-{job}.plist")
    if loaded:
        print(f"\n已立即加载：{', '.join(loaded)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(os.sys.argv[1:]))
