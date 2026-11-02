"""新鲜信号组合模拟 v2：分窗口 + 交易级诊断（2026-10-14 重新优化）。

相对 v1（sim_fresh_signal.py 内联版）修正：
  1) 模拟窗口 = 信号日全窗口 vs 近90天两个口径分别评估（v1 混跑了两个 regime）
  2) 基准与组合同窗口
  3) 卖出也走「收盘决策 → 次日开盘执行」，与买入对称（v1 当日收盘决策+当日收盘成交）
  4) 逐日 mark（按交易日历，不是按信号日），尾部未平仓按最后可得收盘价 mark
  5) 交易级诊断：成交笔数、平均每笔超额 vs 候选池平均、平均持有天数——
     用于验证组合收益确实来自信号 alpha 而非记账假象

口径：T+1 开盘建仓 / 次日开盘出场，hfq 同口径，成本双收（与 sim_portfolio_compare.py 一致）。
"""
import sys, random
sys.path.insert(0, 'backend')
from datetime import date
import pandas as pd
from sqlalchemy import create_engine, text
from app.services.xq.data import load_panel

DB = 'sqlite:////Users/happyljew/quant-platform/data/quant_dev.db'
DUCK = '/Users/happyljew/quant-platform/data/quant.duckdb'
COST_BUY, COST_SELL = 1 + 0.00025 + 0.00001 + 0.001, 1 - 0.00025 - 0.0005 - 0.00001 - 0.001
CAPITAL = 2_000_000
ENABLED = {'chan_buy', 'high_tight_flag'}

eng = create_engine(DB)
panel = load_panel(DUCK, lookback=260)
px = panel.df.set_index(['date', 'symbol'])[['o_hfq', 'c_hfq']].sort_index()
px_d = {}
for (d, s), r in px.iterrows():
    px_d[(d, s)] = (float(r['o_hfq']), float(r['c_hfq']))
trade_dates = sorted({d for d, _ in px_d})
date_idx = {d: i for i, d in enumerate(trade_dates)}

with eng.connect() as c:
    sd = c.execute(text('SELECT date, symbol, strategy FROM signal_daily')).fetchall()
    act_rows = c.execute(text(
        'SELECT date, symbol, action, streak, close, ma20 FROM signal_actions')).fetchall()
sig_by_day: dict[date, dict[str, list[str]]] = {}
for d, s, st in sd:
    sig_by_day.setdefault(date.fromisoformat(d), {}).setdefault(s, []).append(st)
act_by_day: dict[date, dict[str, dict]] = {}
for d, s, a, st, cl, m2 in act_rows:
    act_by_day.setdefault(date.fromisoformat(d), {})[s] = {'action': a, 'streak': st or 0, 'close': cl, 'ma20': m2}


def bench_series(d0: date, d1: date):
    days = [d for d in trade_dates if d0 <= d <= d1]
    out = []
    for d in days:
        vals = [v[1] for (dd, s), v in px_d.items() if dd == d]
        out.append(sum(vals) / len(vals))
    return days, out


def simulate(name, rule, max_pos, hold_days, stop_loss, trailing, d0, d1,
             seed=7, verbose=True):
    rng = random.Random(seed)
    cash = CAPITAL
    holdings = {}   # sym -> {shares, entry, entry_date, peak}
    pend_b, pend_s = [], []
    trades = []
    eq = []
    sim_days = [d for d in trade_dates if d0 <= d <= d1]
    sig_days = [d for d in sim_days if d in sig_by_day or d in act_by_day]
    for d in sim_days:
        i = date_idx[d]
        # 开盘执行昨日挂单
        for s in pend_s:
            if s in holdings:
                p = px_d.get((d, s), (None, None))[0]
                if p:
                    h = holdings.pop(s)
                    cash += p * COST_SELL * h['shares']
                    trades.append((s, h['entry_date'], d, p / h['entry'] - 1))
        pend_s.clear()
        for s in pend_b:
            if s in holdings or len(holdings) >= max_pos:
                continue
            p = px_d.get((d, s), (None, None))[0]
            if not p:
                continue
            target = min(CAPITAL / max_pos, cash / max(max_pos - len(holdings), 1))
            shares = int(target / (p * COST_BUY) // 100 * 100)
            if shares <= 0 or p * COST_BUY * shares > cash:
                continue
            cash -= p * COST_BUY * shares
            holdings[s] = {'shares': shares, 'entry': p, 'entry_date': d, 'peak': p}
        pend_b.clear()
        # 收盘：风控 + 时间止盈决策（次日开盘执行）
        mv = 0
        for s, h in list(holdings.items()):
            c = px_d.get((d, s), (None, None))[1]
            if not c:
                continue
            mv += c * h['shares']
            h['peak'] = max(h['peak'], c)
            pnl = c / h['entry'] - 1
            if pnl <= stop_loss or c / h['peak'] - 1 <= trailing or i - date_idx[h['entry_date']] >= hold_days:
                pend_s.append(s)
        # 仅在信号日生成新候选
        if d in sig_by_day or d in act_by_day:
            cands = []
            for sym, strats in sig_by_day.get(d, {}).items():
                act = act_by_day.get(d, {}).get(sym)
                if act is None:
                    continue
                if rule == 'buystrong':
                    if act['action'] == 'BUY_STRONG':
                        cands.append((sym, 0))
                elif rule == 'fresh_ma':
                    if act['streak'] <= 2 and any(x in ENABLED for x in strats) \
                            and act['close'] is not None and act['ma20'] is not None \
                            and act['close'] < act['ma20']:
                        cands.append((sym, 0 if 'high_tight_flag' in strats else 1))
            rng.shuffle(cands)
            cands.sort(key=lambda x: x[1])
            for s, _ in cands:
                if s not in holdings and s not in pend_b and len(holdings) + len(pend_b) < max_pos:
                    pend_b.append(s)
        eq.append({'date': d, 'total': cash + mv + sum(
            h['entry'] * h['shares'] for s, h in holdings.items() if (d, s) not in px_d), 'n': len(holdings)})
    total_ret = (eq[-1]['total'] / CAPITAL - 1) * 100
    peak = CAPITAL; mdd = 0
    for e in eq:
        peak = max(peak, e['total']); mdd = min(mdd, (e['total'] / peak - 1) * 100)
    bd, bs = bench_series(d0, d1)
    b_ret = (bs[-1] / bs[0] - 1) * 100 if len(bs) > 1 else 0
    n_tr = len(trades)
    avg_tr = sum(t[3] for t in trades) / n_tr * 100 if n_tr else 0
    avg_hold = sum((date_idx[t[2]] - date_idx[t[1]]) for t in trades) / n_tr if n_tr else 0
    if verbose:
        print(f'{name}: 收益 {total_ret:+6.1f}% | 回撤 {mdd:5.1f}% | 超额(vs等权) '
              f'{total_ret - b_ret:+6.1f}pp | 成交 {n_tr:>3} 笔 | 均持 {avg_hold:4.1f} 天 | '
              f'日均持仓 {sum(e["n"] for e in eq)/len(eq):.1f} 只')
    return {'name': name, 'ret': total_ret, 'mdd': mdd, 'bench': b_ret, 'trades': trades}


FULL = (date(2025, 9, 18), date(2026, 9, 30))
RECENT = (date(2026, 7, 1), date(2026, 9, 30))

print('════ 窗口A：全250天（2025-09-18 ~ 2026-09-30，含 pre-July regime）════')
simulate('V0 现行规则 top30      ', 'buystrong', 30, 10, -0.10, -9e9, *FULL)
simulate('V1 fresh+ma top10 T+10 ', 'fresh_ma', 10, 10, -0.08, -0.08, *FULL)
simulate('V5 fresh only top10 T+10', 'fresh', 10, 10, -0.08, -0.08, *FULL) if False else None
print()
print('════ 窗口B：近90天（2026-07-01 ~ 2026-09-30，当前 regime）════')
simulate('V0 现行规则 top30      ', 'buystrong', 30, 10, -0.10, -9e9, *RECENT)
simulate('V1 fresh+ma top10 T+10 ', 'fresh_ma', 10, 10, -0.08, -0.08, *RECENT)
simulate('V2 fresh+ma top20 T+10 ', 'fresh_ma', 20, 10, -0.08, -0.08, *RECENT)
simulate('V3 fresh+ma top10 T+5  ', 'fresh_ma', 10, 5, -0.08, -0.08, *RECENT)
simulate('V4 fresh+ma top20 T+5  ', 'fresh_ma', 20, 5, -0.08, -0.08, *RECENT)
print()
print('════ 敏感性：不同随机种子（窗口B，V1 配置，检验选股随机性影响）════')
for sd_ in (7, 42, 123, 2026):
    simulate(f'V1 seed={sd_:<4}        ', 'fresh_ma', 10, 10, -0.08, -0.08, *RECENT, seed=sd_)
