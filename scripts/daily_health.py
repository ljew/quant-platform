#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""投研框架每日体检 —— 一条命令看清四层是否正常。

用法：
    python scripts/daily_health.py            # 人读表格
    python scripts/daily_health.py --json     # 机器读，供驾驶舱/告警用

退出码：0=全部正常，1=有 WARN，2=有 FAIL（可直接用于 cron/告警判断）

本脚本是 app.services.framework_health 的薄封装 —— 与页面
/api/v1/monitor/framework-health 用同一份逻辑，结论必然一致。
不要在脚本里另写一套检查，否则迟早和页面打架。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_ICON = {OK: "●", WARN: "▲", FAIL: "✕"}


def render(result: dict) -> str:
    w = 72
    out = ["=" * w, f"投研框架体检  {datetime.now():%Y-%m-%d %H:%M}", "=" * w]
    for layer in result["layers"]:
        out.append(f"\n[{layer['name']}]  {layer['status']}")
        for it in layer["items"]:
            out.append(f"  {_ICON[it['status']]} {it['label']:<16} {it['detail']}")
    tally = {s: 0 for s in (OK, WARN, FAIL)}
    for layer in result["layers"]:
        for it in layer["items"]:
            tally[it["status"]] = tally.get(it["status"], 0) + 1
    out.append("\n" + "-" * w)
    out.append(f"结论：{result['worst']}   (OK {tally[OK]} / WARN {tally[WARN]} / FAIL {tally[FAIL]})")
    out.append("-" * w)
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        from app.database import SessionLocal
        from app.services.framework_health import framework_health
    except Exception as e:
        print(f"无法加载体检模块：{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        result = framework_health(db)
    finally:
        db.close()

    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return {OK: 0, WARN: 1, FAIL: 2}[result["worst"]]


if __name__ == "__main__":
    sys.exit(main())
