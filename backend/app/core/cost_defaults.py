"""A股回测/模拟盘成本单一真相源（W45-2 / P0-3；W45-xq / P1-2）。

权威数字与引擎 M1-3 对齐：佣金万2.5 + 分向滑点万1 + 卖出印花税万5。
API schema / PaperTaskCreate / ORM 列默认 / 引擎构造默认 / xq 组合轨
（xq/portfolio.py、portfolio_fresh.py、live/target.py）均应 import 本模块，
避免再出现「引擎已修、schema 仍 0.0003/0.0」或「xq 滑点仍 0.001」的双轨漂移。

单标的、组合回测与模拟盘均从本模块取默认成本；成交时点统一为次日开盘。
"""
from __future__ import annotations

COMMISSION = 0.00025      # 万 2.5（双边佣金率）
SLIPPAGE = 0.0001         # 万 1（分向：买加卖减）
STAMP_TAX = 0.0005        # 万 5（仅卖出）
MIN_COMMISSION = 5.0      # 单笔最低佣金（元）
RISK_FREE_RATE = 0.025    # Sharpe 减 rf
