"""Sequoia-X 选股/信号引擎迁移包（2026-09-30，dev）。

原项目：~/Workbuddy/2026-09-16-15-41-08/Sequoia-X（Python，tushare 自带管道）。
迁移原则：**只迁逻辑，不搬数据** —— 行情统一读本平台 DuckDB（未复权原价 +
adj_factor_daily 累计复权因子），复权口径与阈值语义在 data.py 里统一处理。

模块：
- data.py       面板数据适配层（一次载入全市场 N 日行情，供全部策略共享）
- strategies.py 8 个选股策略（向量化重写，规则与阈值与原实现逐条对齐）
- engine.py     信号引擎（streak 聚合 + 动作判定，阈值 = Sequoia 实测校准值）
- runner.py     每日跑批入口：策略 → 落 signal_daily → 判定 → 落 signal_actions
"""
