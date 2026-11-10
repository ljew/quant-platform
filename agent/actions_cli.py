"""审批命令行：给人用的那一侧。

agent 只能 propose，真正放行在这里。设计文档第 10 节的三个按钮对应：

    approve  → 通过（生成一次性令牌；卡状态类立即执行，建盘类等 agent 再调）
    reject   → 否决（必须写理由，理由进记忆）
    list     → 待确认 / 已处理

为什么不做成网页：审批中心迟早要接 dsh 的审批 UI，但**先把状态机和令牌跑对**
比先做界面重要。这个 CLI 就是第 3 步的验收工具，也是页面做好前的过渡入口。
"""

from __future__ import annotations

import argparse
import json
import sys

import actions
from platform_api import post


def _fmt(row: dict) -> str:
    payload = json.loads(row["payload"])
    brief = payload.get("name") or f"card#{payload.get('card_id')}→{payload.get('to_state')}"
    return (f"#{row['id']:<4} {row['status']:<9} {row['action']:<20} {brief}\n"
            f"      提议 {row['proposed_by'] or '-'} · {row['created_at'][:16]} · "
            f"截止 {row['expires_at'][:16]}\n"
            f"      理由 {row['reason'] or '-'}")


def cmd_list(args) -> int:
    actions.expire_stale()
    rows = actions.list_actions(args.status, args.limit)
    if not rows:
        print("没有记录")
        return 0
    for r in rows:
        print(_fmt(r))
        print()
    return 0


def cmd_approve(args) -> int:
    row = actions.get(args.id)
    if not row:
        print(f"没有 id={args.id} 的动作")
        return 1
    res = actions.approve(args.id, by=args.by)
    if not res.get("ok"):
        print(f"不能通过：{res.get('error')}")
        return 1
    print(f"已通过 #{args.id}（令牌已签发，一次性，{row['expires_at'][:16]} 前有效）")

    if row["action"] == "card_transition":
        payload = json.loads(row["payload"])
        try:
            data, url = post(f"/research/cards/{payload['card_id']}/transition",
                             {"to_state": payload["to_state"], "reason": payload["reason"]})
        except Exception as e:  # noqa: BLE001
            actions.mark_failed(args.id, str(e))
            print(f"执行失败：{e}（状态已记 failed，可重放）")
            return 1
        actions.mark_done(args.id, {"response": data})
        print(f"卡状态已改：#{payload['card_id']} → {payload['to_state']}（POST {url}）")
    else:
        print("建盘类动作：令牌已就绪，等 agent 带这个 action_id 再调一次才建。")
    return 0


def cmd_reject(args) -> int:
    res = actions.reject(args.id, args.reason, by=args.by)
    if not res.get("ok"):
        print(f"不能否决：{res.get('error')}")
        return 1
    print(f"已否决 #{args.id}，理由已记录")
    return 0


def cmd_expire(args) -> int:
    n = actions.expire_stale()
    print(f"过期 {n} 条")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="actions_cli")
    p.add_argument("--db", help="覆盖审批库路径（默认 ~/.dsh/agent_actions.db）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="列出动作")
    s.add_argument("--status", default=None)
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("approve", help="通过")
    s.add_argument("id", type=int)
    s.add_argument("--by", default="user")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("reject", help="否决")
    s.add_argument("id", type=int)
    s.add_argument("--reason", required=True)
    s.add_argument("--by", default="user")
    s.set_defaults(fn=cmd_reject)

    s = sub.add_parser("expire", help="清理超时")
    s.set_defaults(fn=cmd_expire)

    args = p.parse_args(argv)
    if args.db:
        from pathlib import Path
        actions.DB_PATH = Path(args.db)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
