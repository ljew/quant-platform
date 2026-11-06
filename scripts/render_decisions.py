#!/usr/bin/env python3
"""重新生成架构文档的设计决策表（数据源 docs/decisions.json）。

用法：
  python scripts/render_decisions.py                       # 仅按现有 JSON 重渲染
  python scripts/render_decisions.py --add "决策" --reason "理由" [--date 2026-09-30]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "backend"))

from app.services import decisions_log  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--add", help="追加一条决策")
    ap.add_argument("--reason", default="", help="决策理由")
    ap.add_argument("--date", default=None, help="日期（默认今天）")
    args = ap.parse_args()

    if args.add:
        entry = decisions_log.append(args.add, args.reason, args.date)
        print(f"已追加并渲染：{entry['date']} · {entry['decision']}")
    else:
        changed = decisions_log.render()
        print("已重新渲染架构文档决策表" if changed else "内容无变化")


if __name__ == "__main__":
    main()
