"""NSI 策略回测验证 · 六道检验中可做的四项
5 权重阶梯扫描 + 单调性      -> 阈值 60..80 扫描
6 换股票池（最强的一条）      -> 沪深300 / 中证500 / 上证50 / 中证1000
1 收益集中度                -> 剔除最佳年份后的超额
3 时间切片                  -> 已由三段验证覆盖（IS/OOS）
"""
import json
import subprocess

API = "http://127.0.0.1:8000/api/v1/strategy/backtest"
START, END = "2019-01-02", "2026-11-02"


def run(symbol, params, slippage=0.002, strat="national_team"):
    body = {"symbol": symbol, "start": START, "end": END, "strategy": strat,
            "params": params, "initial_cash": 1000000,
            "commission": 0.00025, "slippage": slippage}
    r = subprocess.run(["curl", "-s", "--noproxy", "*", "--max-time", "180",
                        "-X", "POST", API, "-H", "Content-Type: application/json",
                        "-d", json.dumps(body)], capture_output=True, text=True, timeout=200)
    try:
        d = json.loads(r.stdout)
    except Exception:
        return None
    if "detail" in d:
        return None
    return {
        "ann": d.get("annual_return", 0) * 100,
        "mdd": d.get("max_drawdown", 0) * 100,
        "sharpe": d.get("sharpe", 0),
        "total": d.get("total_return", 0) * 100,
        "trades": len(d.get("trades") or []),
    }


BASE = {"base_position": 0.7, "boost_position": 0.98, "hold_days": 20}

print("=" * 92)
print("检验 5：阈值阶梯扫描（全段含 0.002 滑点，标的 sh000300）")
print("=" * 92)
print(f"{'阈值':>8}{'年化':>9}{'回撤':>10}{'夏普':>8}{'总收益':>10}{'成交':>6}")
ths = [60, 65, 68, 71.3, 75, 80]
scan = {}
for th in ths:
    p = dict(BASE, threshold=th)
    r = run("sh000300", p)
    if not r:
        print(f"{th:>8} ERR")
        continue
    scan[th] = r
    print(f"{th:>8}{r['ann']:>8.2f}%{r['mdd']:>9.2f}%{r['sharpe']:>8.2f}"
          f"{r['total']:>9.2f}%{r['trades']:>6}")
if scan:
    a = [v["ann"] for v in scan.values()]
    print(f"\n年化极差 = {max(a)-min(a):.2f}pp  （判据：>3pp 说明是拟合出来的尖峰）")
    mono = all(scan[ths[i]]["ann"] >= scan[ths[i+1]]["ann"] - 0.6
               for i in range(len(ths)-1) if ths[i] in scan and ths[i+1] in scan)
    print(f"单调性（阈值越高越严 → 年化不应大跳）: {'大致单调' if mono else '非单调，有跳变'}")

print("\n" + "=" * 92)
print("检验 6：换标的（最强的一条）—— alpha 是 universe 特有还是通用？")
print("=" * 92)
print(f"{'标的':<16}{'年化':>9}{'回撤':>10}{'夏普':>8}{'总收益':>10}{'成交':>6}")
targets = [("sh000300", "沪深300(原始)"), ("sh000905", "中证500"),
           ("sh000016", "上证50"), ("sh000852", "中证1000")]
pool = {}
for code, name in targets:
    r = run(code, dict(BASE, threshold=0.0))
    if not r:
        print(f"{name:<16} 无数据/失败")
        continue
    pool[code] = r
    print(f"{name:<16}{r['ann']:>8.2f}%{r['mdd']:>9.2f}%{r['sharpe']:>8.2f}"
          f"{r['total']:>9.2f}%{r['trades']:>6}")
if "sh000300" in pool and len(pool) > 1:
    others = [v["ann"] for k, v in pool.items() if k != "sh000300"]
    print(f"\n沪深300 年化 {pool['sh000300']['ann']:.2f}%  |  其他三个均值 "
          f"{sum(others)/len(others):.2f}%  |  衰减 {pool['sh000300']['ann']-sum(others)/len(others):+.2f}pp")

print("\n" + "=" * 92)
print("检验 1：收益集中度 —— 去掉最好的一年还剩多少")
print("=" * 92)
print("（由 nsi_validate_3seg 的分年度数据另行核算，见报告）")

json.dump({"threshold_scan": scan, "pool": pool}, open("/tmp/nsi_checks.json", "w"),
          ensure_ascii=False, indent=1)
print("\n-> /tmp/nsi_checks.json")
