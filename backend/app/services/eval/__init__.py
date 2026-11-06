"""策略与因子评估方法论（可复用模块）。

模块划分
-------
factor_ic      因子有效性检验：四池 IC 对照、年度衰减、分层单调性
strategy_eval  策略绩效、同区间 A/B、配对显著性检验、样本外分段
attribution    逐笔归因：往返配对、持仓周期分桶、止损有效性、alpha 检验

共同约定
-------
· 取价一律走 app.services.price（口径统一，研究用 hfq 后复权）
· 池子一律走 index_membership（'ALLA' 为逐月重建的全A PIT 快照）
· 显著性判断：|t| < 2 只能写「无法判别」，不得宣称有效
· 净值/交易记录直接读 backtests.equity_curve_json / trades_json
"""
