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

from sqlalchemy import func, select  # noqa: E402
from app.models import IndexKlineDaily, IndexMembership  # noqa: E402

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
            "ref": {"total_return": 0.5729, "sharpe": 0.5956, "max_drawdown": -0.1893,
                    "benchmark": 0.3227},
            # ⚠️ 旧参考值 +114.83%/-22.84%/夏普0.836 是错的（来源不明）。
            #    2026-09-15 找到聚宽回测存档 jq2019/metrics.json（同区间 1449 交易日），
            #    实测为 +57.29% / 夏普 0.596 / 回撤 -18.93% / 基准 +32.27%，以此为准。
            # ⚠️ 聚宽该段「平均仓位仅 51.6%」（中性参数明明是 0.9）—— 回撤小是半仓的
            #    副产品，不是策略设计的风控；平台忠实满仓 89%，故回撤天然更大。
            "slippage": 0.0,          # 聚宽回测无滑点，对比需同口径
            "note": "基准已对齐（+32.27% vs 平台 +32.45%）。\n"
                    "⚠️ 平台默认已开启 trend_bear_mode='ma'（修原版 bearish 几乎不触发的缺陷），\n"
                    "   故平台结果高于原版口径：同成本口径下 +101.79%/回撤-34.29%/夏普0.762\n"
                    "   （原版口径 origin 为 +77.44%/-42.84%/0.644）。要与聚宽逐条对齐时\n"
                    "   请用 trend_bear_mode='origin' 跑对照。\n"
                    "剩余差异主因：平台满仓 89% vs 聚宽实际 51.6% —— 聚宽回撤小是半仓的\n"
                    "副产品，不是策略设计的风控。",
            "comparable": True,
        },
        {
            "name": "样本外 2025-01-02~2026-09-11",
            "start": "2025-01-02", "end": "2026-09-11",
            "ref": {"total_return": 0.1740, "sharpe": 0.674, "max_drawdown": -0.1103,
                    "benchmark": 0.1805},
            "note": "⚠️ 该参考值与已被证伪的样本内 +114.83% 同源，可靠性存疑，"
                    "暂按『仅作方向性参考』处理（comparable=False）。"
                    "待老刘提供 2025-2026 的聚宽实盘/回测存档后再校正。"
                    "基准 +18.05% 已确认与聚宽一致，可作为数据侧的校验锚点。",
            "comparable": False,
        },
    ],
    "jk002": [
        {
            "name": "全区间 2019-01-02~2024-12-13",
            "start": "2019-01-02", "end": "2024-12-13",
            "ref": None,
            "note": "jk002 是平台新增的扩池版本：池子=平台全A（pool_mode=all，含科创板），"
                    "基准=国证A指 sz399317（近似全市场）。聚宽侧无对应实盘，仅做平台内部留档。"
                    "注：中证全指 000985 的历史 PIT 成分与行情均取不到（tushare index_weight 受权限"
                    "限制、新浪指数源停在 2016 年），故改用全A池 + 国证A指近似。",
            "comparable": False,
        },
        {
            "name": "样本外 2025-01-02~2026-09-11",
            "start": "2025-01-02", "end": "2026-09-11",
            "ref": None,
            "note": "同上，平台内部留档。",
            "comparable": False,
        },
    ],
}

# 指标允许偏离带宽（超过即判「偏离」，提示需要排查而不一定是 bug）
TOLERANCE = {"total_return": 0.15, "sharpe": 0.25, "max_drawdown": 0.08, "benchmark": 0.05}


def _run_one(db, key: str, start: str, end: str, params: dict | None = None,
              seg: dict | None = None):
    meta = STRATEGY_REGISTRY[key]
    p = dict(meta["default_params"])
    p.update(params or {})
    req = BacktestRequest(symbol=meta.get("index_symbol", "sh000300"),
                          start=start, end=end, strategy=key,
                          params=p, initial_cash=1_000_000, commission=0.0003,
                          slippage=seg.get("slippage", 0.002) if seg else 0.002,
                          adj="qfq")
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


def _data_ready(db, key: str) -> tuple[bool, str]:
    """前置检查：该策略依赖的指数成分快照与指数日K是否就绪。

    没有这一步，定时任务会每天在一个「数据还没准备好的」策略上失败并刷满错误日志；
    明确跳过并说明原因，缺什么一目了然（例如 jk002 的中证全指需要单独 seed）。
    """
    meta = STRATEGY_REGISTRY[key]
    idx_code = meta.get("index_code")
    idx_symbol = meta.get("index_symbol")
    # pool_mode=all（如 jk002）不依赖指数 PIT 成分快照，只要求指数日K（做基准）在库
    if str(meta.get("default_params", {}).get("pool_mode") or "index") == "all":
        n_k = db.execute(
            select(func.count()).select_from(IndexKlineDaily)
            .where(IndexKlineDaily.symbol == idx_symbol)
        ).scalar() or 0
        if not n_k:
            return False, (f"缺基准指数 {idx_symbol} 的日K"
                           f"（需先跑 scripts/seed_index_kline.py --symbol {idx_symbol}）")
        return True, f"全A池（不依赖成分快照）/ 基准K线 {n_k} 根"
    n_mem = db.execute(
        select(func.count()).select_from(IndexMembership)
        .where(IndexMembership.index_code == idx_code)
    ).scalar() or 0
    n_k = db.execute(
        select(func.count()).select_from(IndexKlineDaily)
        .where(IndexKlineDaily.symbol == idx_symbol)
    ).scalar() or 0
    if not n_mem:
        return False, f"缺指数 {idx_code} 的 PIT 成分快照（需先跑 scripts/seed_membership.py --index {idx_code}）"
    if not n_k:
        return False, f"缺指数 {idx_symbol} 的日K（需先跑 scripts/seed_index_kline.py --symbol {idx_symbol}）"
    return True, f"成分 {n_mem} 条 / 指数K线 {n_k} 根"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="jk001", help="策略 key，逗号分隔多个")
    ap.add_argument("--start", default="", help="自定义区间起点（覆盖内置分段）")
    ap.add_argument("--end", default="", help="自定义区间终点")
    ap.add_argument("--quick", action="store_true",
                    help="只跑每个策略的最后一段（近段），供每晚定时任务使用。"
                         "全A池策略（jk002）跑 6 年要 20 分钟以上，每晚全量跑不现实")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for key in [k.strip() for k in args.strategy.split(",") if k.strip()]:
        if key not in STRATEGY_REGISTRY:
            print(f"[skip] 未注册策略 {key}")
            continue
        ok, why = _data_ready(db, key)
        if not ok:
            print(f"[{key}] 数据未就绪，跳过：{why}")
            rows = [{"name": "数据前置检查", "got": {}, "ref": None,
                     "verdict": "跳过", "detail": why}]
            (OUT_DIR / f"strategy_verify_{key}.json").write_text(
                json.dumps({"strategy": key, "date": date.today().isoformat(),
                            "rows": rows}, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
            (OUT_DIR / f"strategy_verify_{key}.html").write_text(
                _html(key, rows), encoding="utf-8")
            continue
        if args.start and args.end:
            segs = [{"name": f"{args.start}~{args.end}", "start": args.start,
                     "end": args.end, "ref": None, "comparable": False}]
        else:
            segs = REFERENCE.get(key, [{"name": "默认区间", "start": "2021-01-01",
                                        "end": date.today().isoformat(),
                                        "ref": None, "comparable": False}])
            if args.quick:
                segs = segs[-1:]
        rows = []
        for seg in segs:
            try:
                r = _run_one(db, key, seg["start"], seg["end"], seg=seg)
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
