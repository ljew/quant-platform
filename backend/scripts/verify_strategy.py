#!/usr/bin/env python3
"""策略验证流水线：一键跑样本内/样本外分段回测，出对照报告（JSON + HTML）。

用途
----
把「移植到平台的策略」与「原版（聚宽等）实测数字」放在同一张表里对照，
每次改代码后跑一遍，就能立刻看出是改好了、还是改坏了。

用法（backend/ 目录下）
----------------------
    PYTHONPATH=$(pwd) python scripts/verify_strategy.py --strategy jk001
    PYTHONPATH=$(pwd) python scripts/verify_strategy.py --strategy jk001,jk002
    PYTHONPATH=$(pwd) python scripts/verify_strategy.py --strategy jk001 --start 2025-01-02 --end 2026-09-11

输出
----
    data/reports/strategy_verify_<key>.html   人看的报告
    data/reports/strategy_verify_<key>.json   机器读的指标（供定时任务/门禁消费）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, ".")

from app.database import SessionLocal, init_db  # noqa: E402
from app.core.strategies.registry import STRATEGY_REGISTRY  # noqa: E402
from app.routers.strategy import _run_portfolio  # noqa: E402
from app.schemas import BacktestRequest  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "reports"

# 原版（聚宽 jk001_live_v1.py 文件头）实测数字，作为对照基准。
# 只有标注了参考值的区间才会做「达标/偏离」判定，没有则只出平台自身结果。
REFERENCE = {
    "jk001": [
        {
            "name": "样本内 2019-01-02~2024-12-13",
            "start": "2019-01-02", "end": "2024-12-13",
            "ref": {"total_return": 1.1483, "sharpe": 0.836, "max_drawdown": -0.2284,
                    "benchmark": 0.3245},
            "note": "平台日K从 2019-01-02 起，策略还需 260 日预热 → 实际从 2020 年初才建仓，"
                    "本段不可直接比较（缺 2019 年数据）。",
            "comparable": False,
        },
        {
            "name": "样本外 2025-01-02~2026-09-11",
            "start": "2025-01-02", "end": "2026-09-11",
            "ref": {"total_return": 0.1740, "sharpe": 0.674, "max_drawdown": -0.1103,
                    "benchmark": 0.1805},
            "note": "原版最重要的一段：年化 α +4.10%（t=0.58，不显著），跑输沪深300 0.65pp。",
            "comparable": True,
        },
    ],
}

# 指标允许偏离带宽（超过即判「偏离」，提示需要排查而不一定是 bug）
TOLERANCE = {"total_return": 0.15, "sharpe": 0.25, "max_drawdown": 0.08, "benchmark": 0.05}


def _run_one(db, key: str, start: str, end: str, params: dict | None = None):
    meta = STRATEGY_REGISTRY[key]
    p = dict(meta["default_params"])
    p.update(params or {})
    req = BacktestRequest(symbol="sh000300", start=start, end=end, strategy=key,
                          params=p, initial_cash=1_000_000, commission=0.0003,
                          slippage=0.002, adj="qfq")
    return _run_portfolio(db, req, meta, p)


def _verdict(seg: dict, got: dict) -> tuple[str, str]:
    """与参考值对照，给出 (判定, 说明)。"""
    ref = seg.get("ref")
    if not ref or not seg.get("comparable", True):
        return "参考", seg.get("note", "")
    diffs = []
    for k, label in (("total_return", "收益"), ("sharpe", "夏普"),
                     ("max_drawdown", "回撤"), ("benchmark", "基准")):
        if k not in ref or got.get(k) is None:
            continue
        d = got[k] - ref[k]
        tol = TOLERANCE[k]
        if abs(d) > tol:
            diffs.append(f"{label}偏离 {d:+.3f}（容差 ±{tol}）")
    if not diffs:
        return "一致", "各指标都在容差内"
    return "偏离", "；".join(diffs)


def _html(key: str, rows: list[dict]) -> str:
    def cell(v, fmt="{:+.2%}"):
        return "—" if v is None else fmt.format(v)

    trs = []
    for r in rows:
        color = {"一致": "#1a8a48", "偏离": "#d92c2c", "参考": "#7a8299"}.get(r["verdict"], "#333")
        trs.append(f"""<tr>
      <td>{r['name']}</td>
      <td class="num">{cell(r['got'].get('total_return'))}</td>
      <td class="num">{cell(r['ref'].get('total_return') if r['ref'] else None)}</td>
      <td class="num">{r['got'].get('sharpe', 0):.3f}</td>
      <td class="num">{r['ref'].get('sharpe') if r['ref'] else '—'}</td>
      <td class="num">{cell(r['got'].get('max_drawdown'))}</td>
      <td class="num">{cell(r['ref'].get('max_drawdown') if r['ref'] else None)}</td>
      <td class="num">{cell(r['got'].get('benchmark'))}</td>
      <td class="num">{r['got'].get('trade_count', 0)}</td>
      <td style="color:{color};font-weight:600">{r['verdict']}</td>
    </tr>
    <tr><td colspan="10" class="note">{r['detail']}</td></tr>""")
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{key} 验证报告</title><style>
body{{font-family:-apple-system,"PingFang SC",sans-serif;margin:32px;color:#1f2533;background:#fff}}
h1{{font-size:20px}} table{{border-collapse:collapse;width:100%;margin-top:16px;font-size:13px}}
th,td{{border:1px solid #e6eaf2;padding:8px 10px;text-align:left}}
th{{background:#f4f6fa;color:#7a8299;font-weight:600}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.note{{color:#7a8299;font-size:12px;background:#fafbfd}}
.meta{{color:#7a8299;font-size:12px;margin-top:6px}}
</style></head><body>
<h1>策略验证报告 · {key}</h1>
<div class="meta">生成时间 {date.today().isoformat()} · 平台回测（收盘价成交，qfq，佣金 0.03%，滑点 0.2%）</div>
<table>
<tr><th>区间</th><th>平台收益</th><th>参考收益</th><th>平台夏普</th><th>参考夏普</th>
<th>平台回撤</th><th>参考回撤</th><th>基准</th><th>成交</th><th>判定</th></tr>
{''.join(trs)}
</table>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="jk001", help="策略 key，逗号分隔多个")
    ap.add_argument("--start", default="", help="自定义区间起点（覆盖内置分段）")
    ap.add_argument("--end", default="", help="自定义区间终点")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for key in [k.strip() for k in args.strategy.split(",") if k.strip()]:
        if key not in STRATEGY_REGISTRY:
            print(f"[skip] 未注册策略 {key}")
            continue
        if args.start and args.end:
            segs = [{"name": f"{args.start}~{args.end}", "start": args.start,
                     "end": args.end, "ref": None, "comparable": False}]
        else:
            segs = REFERENCE.get(key, [{"name": "默认区间", "start": "2021-01-01",
                                        "end": date.today().isoformat(),
                                        "ref": None, "comparable": False}])
        rows = []
        for seg in segs:
            try:
                r = _run_one(db, key, seg["start"], seg["end"])
                got = {"total_return": r.total_return, "sharpe": r.sharpe,
                       "max_drawdown": r.max_drawdown,
                       "benchmark": r.benchmark_total_return,
                       "trade_count": r.trade_count}
                v, detail = _verdict(seg, got)
                rows.append({"name": seg["name"], "got": got, "ref": seg.get("ref"),
                             "verdict": v, "detail": detail or seg.get("note", "")})
                print(f"[{key}] {seg['name']}: 收益 {got['total_return']:+.2%} "
                      f"夏普 {got['sharpe']:.3f} 回撤 {got['max_drawdown']:.2%} → {v}")
            except Exception as e:  # noqa: BLE001
                rows.append({"name": seg["name"], "got": {}, "ref": seg.get("ref"),
                             "verdict": "失败", "detail": str(e)})
                print(f"[{key}] {seg['name']}: 失败 {e}")

        (OUT_DIR / f"strategy_verify_{key}.json").write_text(
            json.dumps({"strategy": key, "date": date.today().isoformat(), "rows": rows},
                       ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        (OUT_DIR / f"strategy_verify_{key}.html").write_text(_html(key, rows), encoding="utf-8")
        print(f"[saved] data/reports/strategy_verify_{key}.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
