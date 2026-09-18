#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""四池因子有效性检验 → HTML 报告。

回答：把股票池从沪深300 扩到全A，FCF/OE 与低波动还剩多少预测力？

运行：
  cd backend && PYTHONPATH=. python scripts/run_factor_check.py
"""
from __future__ import annotations

import os
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from app.services.eval import factor_ic, report  # noqa: E402

SD, ED = "20220101", "20260915"      # 主口径：四池共同区间（沪深300/中证500 快照自 2021-11 起）
OUT = os.path.join(ROOT, "data", "reports", "factor_alla_check.html")


def main():
    t0 = time.time()
    print("=" * 78, flush=True)
    print("四池因子有效性检验  %s ~ %s" % (SD, ED), flush=True)
    print("=" * 78, flush=True)

    res = factor_ic.run(SD, ED)

    print("\n" + "=" * 78, flush=True)
    print("【表】四池 IC 对照", flush=True)
    print("=" * 78, flush=True)
    s = res["summary"]
    for fac in ["FCF/OE", "低波动", "ROE", "最终打分"]:
        sub = s[s["因子"] == fac]
        if sub.empty:
            continue
        print("\n  ▸ %s" % fac, flush=True)
        print("    %-12s %10s %8s %8s %7s %6s"
              % ("池", "IC均值", "IC_IR", "t值", "胜率", "样本"), flush=True)
        for _, r in sub.iterrows():
            print("    %-12s %+10.4f %+8.3f %+8.2f %6.0f%% %6d"
                  % (r["池"], r["ic"], r["ir"], r["t"], r["win"] * 100, r["n"]),
                  flush=True)

    if not res["layered"].empty:
        print("\n" + "=" * 78, flush=True)
        print("【表】分层月均收益（G1 最低 → G5 最高，多空 = G5−G1）", flush=True)
        print("=" * 78, flush=True)
        lay = res["layered"]
        pv = lay.pivot_table(index=["池", "因子"], columns="组", values="月均收益")
        order = [c for c in ["G1", "G2", "G3", "G4", "G5", "多空G5-G1"]
                 if c in pv.columns]
        print("    %-22s" % "" + "".join("%11s" % c for c in order), flush=True)
        for idx, row in pv.iterrows():
            line = "    %-22s" % f"{idx[0]}·{idx[1]}"
            for c in order:
                v = row[c]
                line += "%11s" % (f"{v*100:+.3f}%" if v == v else "-")
            print(line, flush=True)

    out = report.render_factor_report(res, OUT, "2022-01", "2026-09")
    print("\n" + "=" * 78, flush=True)
    print("【结论】%s" % out["verdict"].replace("<b>", "").replace("</b>", ""), flush=True)
    print("【报告】%s" % out["path"], flush=True)
    print("[耗时] %.0fs" % (time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
