"""实盘系统（L3-L7）。

分层（docs/LIVE-TRADING-DESIGN.md）：
  target.py     L3 组合意图：单日决策纯函数（本包地基）
  intent.py     L4 订单意图：target − actual
  guard.py      L5 风控闸门（三层）
  recon.py      L7 对账：成交回报 → 持仓/净值

设计约束（三条，全部服务「可复现」）：
1. 单一事实源 = 成交回报。持仓只由 live_fill 驱动，不由模型推演写入。
2. 规则是纯函数：target = rule(信号, 实际持仓, 门控, 日历)。同输入必同输出，
   因此重跑无害，回测与实盘同源。
3. 决策快照不可变（live_decision_snapshot）。卡片档位会变，不冻结就无法复现。
"""

STRATEGY_KEY = "fresh_ma"
