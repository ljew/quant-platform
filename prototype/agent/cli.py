"""命令行入口：`python3 -m agent.cli "你的问题"`

    python3 -m agent.cli "缠论还值不值得跟"
    python3 -m agent.cli --list            # 看 8 个智能体和它们的工具
    python3 -m agent.cli --history         # 看最近几次提问
    python3 -m agent.cli --trace 3         # 看第 3 次实际调了哪些工具
    python3 -m agent.cli --no-llm "..."    # 不调 Planner，用规则模板拆任务
"""

from __future__ import annotations

import argparse
import sys

from . import audit, personas
from .tools import REGISTRY


def _hr(ch: str = "─", n: int = 68) -> str:
    return ch * n


def _pad(text: str, width: int) -> str:
    """按显示宽度左对齐（中文算 2 格）。中英混排时 str.ljust 会歪。"""
    w = sum(2 if ord(c) > 0x2E80 else 1 for c in text)
    return text + " " * max(0, width - w)


def cmd_list() -> None:
    print("\n八个数字人\n" + _hr("═"))
    lv = {"A": "A 全自主", "B": "B 半自主", "C": "C 只答不问"}
    for p in personas.describe():
        print(f"\n  {_pad(p['name'], 12)} {_pad(lv[p['level']], 12)} {p['key']}")
        print(f"    工具：{', '.join(p['tools'])}")
    print(f"\n工具注册表（{len(REGISTRY)} 个，全部只读）\n" + _hr())
    for name, spec in REGISTRY.items():
        print(f"  {name:22s} {spec['desc'][:44]}")
    print()


def cmd_history(limit: int = 10) -> None:
    rows = audit.recent_runs(limit)
    if not rows:
        print("还没有记录。先问一个问题试试。")
        return
    print(f"\n最近 {len(rows)} 次\n" + _hr())
    print(f"  {'#':>3}  {'时间':16s} {'agent':>5} {'调用':>4} {'耗时':>7}  问题")
    for r in rows:
        print(f"  {r['id']:>3}  {r['created_at'][5:16]:16s} {r['n_agents'] or 0:>5} "
              f"{r['n_calls'] or 0:>4} {(r['total_ms'] or 0)/1000:>6.1f}s  {r['question'][:34]}")
    print()


def cmd_trace(run_id: int) -> None:
    acts = audit.actions_of(run_id)
    if not acts:
        print(f"#{run_id} 没有工具调用记录")
        return
    print(f"\n#{run_id} 的工具调用链（这是审计的核心）\n" + _hr())
    for a in acts:
        flag = "✓" if a["ok"] else "✗"
        print(f"  {flag} {a['ts'][11:]} {a['agent']:9s} {a['tool']:22s} "
              f"{a['ms']:>4}ms  {str(a['summary'])[:60]}")
    print()


def cmd_ask(question: str, use_llm: bool = True) -> int:
    from .orchestrator import run

    try:
        out = run(question, use_llm=use_llm, verbose=True)
    except Exception as e:  # noqa: BLE001
        print(f"\n执行失败：{type(e).__name__}: {e}\n", file=sys.stderr)
        return 1

    print("\n" + _hr("═"))
    print("回答\n" + _hr())
    print(out["answer"])
    print("\n" + _hr())
    u = out["usage"]
    print(f"#{out['run_id']} · {len(out['agents'])} 个智能体 · {out['n_calls']} 次工具调用 · "
          f"{out['total_ms']/1000:.1f}s · "
          f"token {u.get('prompt_tokens', 0)}+{u.get('completion_tokens', 0)}")
    print(f"追溯：python3 -m agent.cli --trace {out['run_id']}\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="agent", description="量化投研平台 · 多智能体原型（只读）")
    ap.add_argument("question", nargs="*", help="你要问的问题")
    ap.add_argument("--list", action="store_true", help="列出智能体与工具")
    ap.add_argument("--history", action="store_true", help="最近几次提问")
    ap.add_argument("--trace", type=int, metavar="RUN_ID", help="查看某次运行的调用链")
    ap.add_argument("--no-llm", action="store_true", help="不调 Planner，用规则模板拆任务")
    args = ap.parse_args()

    if args.list:
        cmd_list()
        return 0
    if args.history:
        cmd_history()
        return 0
    if args.trace:
        cmd_trace(args.trace)
        return 0
    if not args.question:
        ap.print_help()
        return 0
    return cmd_ask(" ".join(args.question), use_llm=not args.no_llm)


if __name__ == "__main__":
    raise SystemExit(main())
