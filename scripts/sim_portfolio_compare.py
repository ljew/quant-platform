"""镜像组合规则对照模拟：基线 vs 优化（退出缓冲 + 入场放宽 + 移动止损）。"""
import sys, json
sys.path.insert(0, 'backend')
from datetime import date, timedelta
import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.models import SignalAction
from app.services.xq.data import load_panel

DB = 'sqlite:////Users/happyljew/Desktop/kimiwork/Quant/quant-platform/data/quant_dev.db'
DUCK = '/Users/happyljew/Desktop/kimiwork/Quant/quant-platform/data/quant.duckdb'

eng = create_engine(DB)
with Session(eng) as db:
    actions = db.scalars(select(SignalAction).order_by(SignalAction.date)).all()
panel = load_panel(DUCK, lookback=260)
px = panel.df.set_index(["date", "symbol"])[["open", "close"]].sort_index()
px_d = {}
for (d, s), r in px.iterrows():
    px_d[(d, s)] = (float(r['open']), float(r['close']))

act_map = {}
for a in actions:
    act_map.setdefault(a.date, {})[a.symbol] = (a.action, (a.streak or 0))
dates = sorted(act_map)

def get_px(d, sym, i):
    v = px_d.get((d, sym))
    return v[i] if v else None

COST_BUY, COST_SELL = 1 + 0.00025 + 0.00001 + 0.001, 1 - 0.00025 - 0.0005 - 0.00001 - 0.001

def simulate(name, buy_rule, exit_grace_days, stop_loss, trailing_stop, take_profit,
             max_pos, single_pct, capital=2_000_000):
    cash = capital; holdings = {}; pending_b = []; pending_s = []
    grace = {}  # sym -> 连续跌出持有名单天数
    eq = []
    for d in dates:
        a_today = act_map.get(d, {})
        # 开盘执行挂单
        for s in list(pending_s):
            if s in holdings:
                p = get_px(d, s, 0)
                if p:
                    cash += p * COST_SELL * holdings[s]['shares']
                    del holdings[s]
        pending_s.clear()
        for s in list(pending_b):
            if s in holdings or len(holdings) >= max_pos: continue
            p = get_px(d, s, 0)
            if not p: continue
            target = min(capital * single_pct, cash / max(max_pos - len(holdings), 1))
            shares = int(target / (p * COST_BUY) // 100 * 100)
            if shares <= 0 or p * COST_BUY * shares > cash: continue
            cash -= p * COST_BUY * shares
            holdings[s] = {'shares': shares, 'entry': p, 'peak': p, 'grace': 0}
        pending_b.clear()
        # 收盘：更新峰值、生成挂单
        mv = 0
        for s, h in list(holdings.items()):
            c = get_px(d, s, 1)
            if not c: mv += h['entry'] * h['shares']; continue
            mv += c * h['shares']
            h['peak'] = max(h['peak'], c)
            pnl = c / h['entry'] - 1
            act = a_today.get(s, ('EXIT', 0))[0]
            if pnl <= stop_loss or (h['peak'] and c / h['peak'] - 1 <= trailing_stop) or pnl >= take_profit:
                pending_s.append(s); continue
            if act not in ('BUY_STRONG', 'WATCH'):
                h['grace'] += 1
                if h['grace'] >= exit_grace_days: pending_s.append(s)
            else:
                h['grace'] = 0
        # 新买入候选
        for s, (act, streak) in a_today.items():
            if s not in holdings and buy_rule(act, streak) and s not in pending_b:
                pending_b.append(s)
        eq.append({'date': d.isoformat(), 'total': cash + mv, 'n': len(holdings),
                   'pos': mv / (cash + mv) if cash + mv else 0})
    total_ret = (eq[-1]['total'] / capital - 1) * 100
    peak = capital; mdd = 0
    for e in eq:
        peak = max(peak, e['total']); mdd = min(mdd, (e['total'] / peak - 1) * 100)
    avg_pos = sum(e['pos'] for e in eq) / len(eq) * 100
    avg_n = sum(e['n'] for e in eq) / len(eq)
    print(f'{name}: 收益 {total_ret:+.1f}% | 最大回撤 {mdd:.1f}% | 平均仓位 {avg_pos:.0f}% | 平均持仓 {avg_n:.1f} 只')
    return eq

# A) 基线：现状规则
simulate('A 基线(现状)      ', lambda a, s: a == 'BUY_STRONG', 1, -0.10, -9e9, 0.30, 30, 0.05)
# B) 仅加退出缓冲：连续3天跌出持有名单才卖
simulate('B 退出缓冲3天     ', lambda a, s: a == 'BUY_STRONG', 3, -0.10, -9e9, 0.30, 30, 0.05)
# C) B + 移动止损(峰值回撤10%)替代固定止损
simulate('C B+移动止损10%   ', lambda a, s: a == 'BUY_STRONG', 3, -9e9, -0.10, 0.30, 30, 0.05)
# D) C + 入场放宽：WATCH连续≥5天也可买，单只8%，最多10只
simulate('D C+放宽入场      ', lambda a, s: a == 'BUY_STRONG' or (a == 'WATCH' and s >= 5), 3, -9e9, -0.10, 0.30, 10, 0.08)
# E) D 但不放宽止损（保留-10%硬止损兜底 + 移动止损）
simulate('E D+硬止损兜底    ', lambda a, s: a == 'BUY_STRONG' or (a == 'WATCH' and s >= 5), 3, -0.15, -0.10, 0.30, 10, 0.08)

print()
# F) 同一批稀缺信号，提高单只仓位到10%、最多10只 + 退出缓冲3天 + 移动止损8%
simulate('F 信号不变仓位10% ', lambda a, s: a == 'BUY_STRONG', 3, -9e9, -0.08, 0.30, 10, 0.10)
# G) F + 更集中：单只15%、最多6只
simulate('G 更集中15%/6只   ', lambda a, s: a == 'BUY_STRONG', 3, -9e9, -0.08, 0.30, 6, 0.15)
# H) F + 止盈放宽到50%（让利润跑）
simulate('H F+止盈放宽50%   ', lambda a, s: a == 'BUY_STRONG', 3, -9e9, -0.08, 0.50, 10, 0.10)
