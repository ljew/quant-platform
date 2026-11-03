"""NSI 策略回测验证 · 三段同向 + 对照
按 strategy-backtest-validation 协议：
  ① 样本内（参数挑选段） 滑点 0
  ② 样本外（唯一能证伪的段） 滑点 0
  ③ 全段 滑点 0.002
对照：恒定 70% 仓位（boost=0.7 → 永远 70%）
"""
import json
import subprocess
import sys

API = "http://127.0.0.1:8000/api/v1/strategy/backtest"


def run(symbol, start, end, params, slippage, tag):
    body = {
        "symbol": symbol, "start": start, "end": end,
        "strategy": "national_team", "params": params,
        "initial_cash": 1000000, "commission": 0.00025, "slippage": slippage,
    }
    r = subprocess.run(
        ["curl", "-s", "--noproxy", "*", "--max-time", "180", "-X", "POST", API,
         "-H", "Content-Type: application/json", "-d", json.dumps(body)],
        capture_output=True, text=True, timeout=200,
    )
    try:
        d = json.loads(r.stdout)
    except Exception:
        return {"tag": tag, "err": r.stdout[:120]}
    if "detail" in d:
        return {"tag": tag, "err": str(d["detail"])[:120]}
    eq = d.get("equity_curve") or []
    bench = eq[-1].get("benchmark", 0) if eq else 0
    return {
        "tag": tag,
        "ann": d.get("annual_return", 0) * 100,
        "mdd": d.get("max_drawdown", 0) * 100,
        "sharpe": d.get("sharpe", 0),
        "total": d.get("total_return", 0) * 100,
        "bench": (bench / 1e6 - 1) * 100 if bench else 0,
        "trades": len(d.get("trades") or []),
    }


P_SIG = {"base_position": 0.7, "boost_position": 0.98, "hold_days": 20, "threshold": 0.0}
P_FLAT = {"base_position": 0.7, "boost_position": 0.70, "hold_days": 20, "threshold": 0.0}

SEGS = [
    ("IS   样本内", "2019-01-02", "2024-12-31", 0.0),
    ("OOS  样本外", "2025-01-02", "2026-11-02", 0.0),
    ("ALL  全段含成本", "2019-01-02", "2026-11-02", 0.002),
]

rows = []
print("=" * 88)
print("NSI 策略：三段验证 vs 恒定70%仓位")
print("=" * 88)
print(f"{'区间':<18}{'方案':<10}{'年化':>8}{'回撤':>9}{'夏普':>7}{'总收益':>9}{'基准':>9}{'成交':>6}")
for tag, s, e, slip in SEGS:
    a = run("sh000300", s, e, P_SIG, slip, tag + " NSI")
    b = run("sh000300", s, e, P_FLAT, slip, tag + " 恒70")
    rows += [a, b]
    for x in (a, b):
        if "err" in x:
            print(f"{x['tag']:<18}ERR {x['err']}")
            continue
        name = "NSI" if "NSI" in x["tag"] else "恒70%"
        print(f"{tag.split()[0]:<18}{name:<10}{x['ann']:>7.2f}%{x['mdd']:>8.2f}%"
              f"{x['sharpe']:>7.2f}{x['total']:>8.2f}%{x['bench']:>8.2f}%{x['trades']:>6}")
    if not any("err" in x for x in (a, b)):
        print(f"{'':<18}{'超额':<10}{a['ann']-b['ann']:>+7.2f}pp"
              f"{a['mdd']-b['mdd']:>+8.2f}pp{a['sharpe']-b['sharpe']:>+7.2f}")
    print("-" * 88)

json.dump(rows, open("/tmp/nsi_3seg.json", "w"), ensure_ascii=False, indent=1)
print("\n结果 -> /tmp/nsi_3seg.json")
