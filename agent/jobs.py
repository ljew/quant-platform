"""三条定时任务：盘前体检 / 盘后总结 / 研究卡复核。

为什么不挂 dsh 自己的 schedule：`dsh-schedule` 的 README 写死了
「无法在 headless 或仅 SDK 组合中单独挂载」—— 投递需要 Web 的 Session
controller 和持久化后端。所以用系统级定时（launchd）跑一次 headless，
结果落到 ~/.dsh/jobs/，一份一天。

用法：
    python3 agent/jobs.py list
    python3 agent/jobs.py morning          # 手动跑一次（调试用）
    python3 agent/jobs.py review --dry-run # 只打印会执行什么
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DSH = Path.home() / ".dsh" / "runtime" / "node_modules" / ".bin" / "dsh"
OUT_DIR = Path.home() / ".dsh" / "jobs"
WEB_PATCH = Path.home() / ".dsh" / "profiles" / "web" / "cordis.patch.yml"

# preset 与要问的话。每条都要能独立回答「今天该不该动手」。
JOBS: dict[str, dict] = {
    "morning": {
        "time": "08:45",
        "preset": "data-eng",
        "title": "盘前体检",
        "prompt": (
            "盘前体检。只做三件事，按顺序：\n"
            "1) 用 get_framework_health 看五层健康，指出任何非 OK 的层与具体问题；\n"
            "2) 用 list_paper_tasks 看模拟盘净值算到哪一天，落后超过 2 个交易日就标红；\n"
            "3) 用 get_latest_digest 看今天的当日全景是否已生成（没有就说没有）。\n"
            "最后给一句结论：今天的数据能不能用来做决策。不要给投资建议。"
        ),
    },
    "close": {
        "time": "17:30",
        "preset": "ops",
        "title": "盘后总结",
        "prompt": (
            "盘后总结。只做三件事：\n"
            "1) 用 get_signal_actions 看今天出了多少买卖/观察信号，各多少条；\n"
            "2) 用 list_paper_tasks 报告每个模拟盘的净值、收益、持仓数、净值日期；\n"
            "3) 用 get_framework_health 确认今天的数据链路有没有 FAIL。\n"
            "结论一句话：今天有没有异常需要人处理。不要预测明天。"
        ),
    },
    "review": {
        "time": "20:30",
        "preset": "strategy-researcher",
        "title": "研究卡复核",
        "prompt": (
            "研究卡复核。只做两件事：\n"
            "1) 用 list_research_cards 取全部卡，挑出「证据最弱」的三张"
            "（标准：hypothesis 是占位符、evidence 为空、或从没跑过回测）；\n"
            "2) 对这三张各说一句该怎么处理（补证据 / 降级 / 清理），并说明依据。\n"
            "不要改任何东西；要改就只提议，等人批。"
        ),
    },
}


def _api_key() -> str:
    """从 web profile 的 patch 里取模型 key。headless 不继承凭据服务。"""
    if os.getenv("KIMI_CODE_API_KEY"):
        return os.environ["KIMI_CODE_API_KEY"]
    try:
        text = WEB_PATCH.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"sk-kimi-[A-Za-z0-9]+", text)
    return m.group(0) if m else ""


def run(job: str, dry_run: bool = False) -> int:
    spec = JOBS.get(job)
    if not spec:
        print(f"未知任务 {job}，可选：{', '.join(JOBS)}")
        return 2

    stamp = datetime.now().strftime("%Y-%m-%d")
    out_file = OUT_DIR / f"{stamp}-{job}.md"

    # 用一次性 patch 把默认 preset 指到该任务要的角色
    patch = Path(f"/tmp/quant-job-{job}.yml")
    patch.write_text(
        "- id: agent-preset-registry\n  config:\n"
        f"    default: {spec['preset']}\n", encoding="utf-8")

    cmd = [str(DSH), "--profile", "headless", "--patch", str(patch), spec["prompt"]]
    if dry_run:
        print("preset:", spec["preset"])
        print("输出:", out_file)
        print("prompt:", spec["prompt"][:120], "…")
        return 0

    env = {**os.environ}
    key = _api_key()
    if key:
        env["KIMI_CODE_API_KEY"] = key
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = datetime.now()
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                          cwd=str(Path.home() / "quant-platform"), timeout=900)
    body = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    cost = (datetime.now() - t0).total_seconds()

    out_file.write_text(
        f"# {spec['title']} · {stamp}\n\n"
        f"preset: `{spec['preset']}`　耗时 {cost:.0f}s　exit {proc.returncode}\n\n"
        f"{body}\n\n" + (f"---\nstderr:\n```\n{err[-2000:]}\n```\n" if err else ""),
        encoding="utf-8")
    print(f"{spec['title']} 完成，{cost:.0f}s → {out_file}")
    if proc.returncode != 0:
        print(err[-600:], file=sys.stderr)
    return proc.returncode


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="jobs")
    p.add_argument("job", nargs="?", help="morning / close / review / list")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    if not a.job or a.job == "list":
        for k, v in JOBS.items():
            print(f"{k:<8} {v['time']}  {v['title']:<8} preset={v['preset']}")
        return 0
    return run(a.job, a.dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
