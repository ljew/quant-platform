"""ORM 模型。

Phase 1 实现 Stock（标的基础信息）与 KlineDaily（日K线）。
后续 Phase 逐步补充 Strategy / Backtest / Order / Position / Factor 等。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.core.cost_defaults import COMMISSION, SLIPPAGE

from app.database import Base


class Stock(Base):
    """标的基础信息表（A股为主，预留港股/美股）。"""

    __tablename__ = "stocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 统一代码：A股 sh600519 / sz000858；港股 hk00700；美股 usAAPL
    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    market: Mapped[str] = mapped_column(String(8), index=True)  # sh / sz / hk / us
    # 交易所内部代码（如 600519），方便对接不同数据源
    raw_code: Mapped[str] = mapped_column(String(16))
    industry: Mapped[str | None] = mapped_column(String(64), nullable=True)
    list_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # ---- 上市生命周期（PIT 成分重建的依据，缺失会导致幸存者偏差）----
    # 全A 没有官方成分快照，只能靠 list_date / delist_date 逐月重建「当时可交易的股票」。
    # 若不记录退市日，2019 年的池子会包含当年尚未上市的公司（未来函数）。
    delist_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    list_status: Mapped[str | None] = mapped_column(String(2), nullable=True)  # L上市/D退市/P暂停
    # 基本面截面快照（来自 tushare daily_basic，回测期初就近交易日，用于行业/市值中性化与估值因子）
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)  # 总市值（亿元）
    pe_ttm: Mapped[float | None] = mapped_column(Float, nullable=True)
    pb: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 质量/成长基本面快照（来自 akshare 业绩报表，由 scripts/seed_fundamentals.py 入库）
    roe: Mapped[float | None] = mapped_column(Float, nullable=True)          # 净资产收益率(%)
    revenue_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)  # 营业总收入同比增长(%)
    profit_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)   # 净利润同比增长(%)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Stock {self.symbol} {self.name}>"


class MarketValuationDaily(Base):
    """全市场估值日频聚合（2019 起），供「估值 × 资金流」条件判断。

    为什么单独一张表、而不是每次从 `stock_valuation_daily` 现算：
    源表 900 万行，按日算中位数要扫全表（秒级、且吃内存）；聚合后只有 1900 行，
    看板每次请求直接读，O(1)。

    口径（写死在代码里，改动会影响历史可比性）：
      · pe_median  = 全 A 股 PE(TTM) 中位数，**剔除亏损（pe≤0）与 >300 倍的极端值**
      · pe_weighted= 总市值加权（Σ总市值 / Σ总市值÷PE），同样只含 pe>0 的样本
      · pb_median  = PB 中位数，剔除 pb≤0 与 >30
      · pct_pe     = 当日 pe_median 在过去 1250 个交易日（约 5 年）中的分位，%
                     —— 滚动窗口而非全样本，贴近「估值星级」的直觉；
                     前 250 个交易日样本不足，留空

    为什么用中位数而不是加权：A 股 PE 分布极右偏（少数百倍股会拉高均值），
    中位数更稳；加权值同时存下来用于交叉校验。
    """

    __tablename__ = "market_valuation_daily"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    pe_median: Mapped[float | None] = mapped_column(Float, nullable=True)
    pe_weighted: Mapped[float | None] = mapped_column(Float, nullable=True)
    pb_median: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    pct_pe: Mapped[float | None] = mapped_column(Float, nullable=True)
    pct_pb: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class StockValuationDaily(Base):
    """估值时序表（symbol × trade_date），**PIT 因子挖掘的数据基础**。

    为什么必须有这张表（B1，2026-10-08）：
      `stocks` 表的 pe_ttm / pb / market_cap 是**最新一列快照**，被
      `_attrs_map`（factor_mining.py）复用于所有历史截面 → 2019 年的截面
      用的是 2026 年的 PE，属**明确前视偏差**。实测：茅台 2019-01-10 的
      pe_ttm=24.42、pb=7.59，与今日值完全不同。

    任何含估值/市值因子的历史结论，在本表回填前都不可信。

    数据来源：tushare `daily_basic`（按 trade_date 全市场拉取，
    实测 3555 行/0.6 秒，回填 2019 至今约 640 万行 / 0.4GB / 20 分钟）。

    字段口径与 `stocks` 对齐（total_mv 单位：万元 → 存亿元）。
    """

    __tablename__ = "stock_valuation_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    pe_ttm: Mapped[float | None] = mapped_column(Float, nullable=True)
    pb: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_mv: Mapped[float | None] = mapped_column(Float, nullable=True)   # 总市值（亿元）
    circ_mv: Mapped[float | None] = mapped_column(Float, nullable=True)    # 流通市值（亿元）
    turnover_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_svd_symbol_date"),
        Index("ix_svd_date_symbol", "trade_date", "symbol"),
    )


class FundamentalsHistory(Base):
    """基本面历史快照（多报告期），用于时序类因子（如 PEAD 盈余惊喜）。

    区别于 stocks 表的截面快照：此处按报告期存储多个时点（如 2021~2025 年报），
    回测时按『报告期 <= 调仓日』取最近一条，实现 point-in-time 基本面，杜绝前视偏差。
    """

    __tablename__ = "fundamentals_history"
    __table_args__ = (
        UniqueConstraint("symbol", "report_date", name="uq_fund_symbol_date"),
        Index("ix_fund_symbol_date", "symbol", "report_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)  # 报告期(年末)，如 2025-12-31
    # 与 stocks 表同口径的基本面字段
    roe: Mapped[float | None] = mapped_column(Float, nullable=True)            # 净资产收益率(%)
    revenue_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)    # 营业总收入同比增长(%)
    profit_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)     # 净利润同比增长(%)

    def __repr__(self):
        return f"<FundamentalsHistory {self.symbol} {self.report_date}>"


class FinancialsRaw(Base):
    """财务原始三表（按公告日 ann_date 存原始字段）—— 支撑现金流类因子。

    为什么另建表而不是扩 fundamentals_history：
    后者只有 roe / revenue_yoy / profit_yoy 三个加工后的比率，而策略的核心因子
    FCF/OE = (经营活动现金流净额 − 资本开支) / 总资产，需要**绝对额**字段才能算。
    三个字段分别来自 fina_indicator(roe) / cashflow(现金流+资本开支) / balancesheet(总资产)。

    按 ann_date 而非 end_date 存储：回测在 t 日只能知道 t 之前已公告的财报。
    用 end_date 会引入前视（如 2020 年报的实际公布日是 2021-04-28，
    若按 end_date=2020-12-31 对齐，等于提前 4 个月看到数据）。

    唯一键取 (symbol, end_date) 而非 (symbol, ann_date)：财务数据的天然主键是
    「哪家公司哪一期」；公告日只是属性。同一公告日可能同时更正多期，按 ann_date
    去重会静默丢数据。
    """

    __tablename__ = "financials_raw"
    __table_args__ = (
        UniqueConstraint("symbol", "end_date", name="uq_fin_symbol_end"),
        Index("ix_fin_symbol_ann", "symbol", "ann_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)        # 报告期（主键组成）
    ann_date: Mapped[date] = mapped_column(Date, nullable=False)        # 公告日（对齐基准）
    roe: Mapped[float | None] = mapped_column(Float, nullable=True)             # 净资产收益率(%)
    n_cashflow_act: Mapped[float | None] = mapped_column(Float, nullable=True)  # 经营活动现金流净额
    capex: Mapped[float | None] = mapped_column(Float, nullable=True)           # 资本开支（购建固定/无形/其他长期资产）
    total_assets: Mapped[float | None] = mapped_column(Float, nullable=True)    # 总资产

    def __repr__(self):
        return f"<FinancialsRaw {self.symbol} {self.ann_date}>"


class KlineDaily(Base):
    """日K线表。

    生产环境 TimescaleDB 会将此表转为 Hypertable（按 trade_date 分区），
    索引 (symbol, trade_date) 保证回测时按标的+时间范围的高速查询。

    价格口径（2026-09 起）：**存未复权原始价**，`adj='none'`。
    复权因子单独存 adj_factor_daily，取价时由 utils.price 按需构造前复权/后复权。
    为什么要改：原先直接存前复权价，历史价会随新的分红送股事件**回溯变动**
    —— 同一个 2020 年的日期，在 2024 年和 2026 年查到的价格不同，
    跨期结果不可复现，且严格来说含有前视。存原始价 + 因子则完全可复现。
    """

    __tablename__ = "kline_daily"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_kline_symbol_date"),
        Index("ix_kline_symbol_date", "symbol", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)  # 成交额（千元，tushare 原单位）
    # 复权类型：all-a 建仓后统一为 none（存原始价）；qfq/hfq 为历史遗留口径
    adj: Mapped[str] = mapped_column(String(4), default="none")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<KlineDaily {self.symbol} {self.trade_date}>"


class IndexKlineDaily(Base):
    """指数日K线表（与个股 kline_daily 分离，避免混淆）。

    回测/模拟盘的基准指数优先读此表；库无数据时由 _load_index_bars
    在线拉取并写回缓存，避免每次都依赖外部数据源（防止在线抖动导致
    “未获取到基准指数”报错）。symbol 用新浪格式如 sh000906 / sh000300。
    """

    __tablename__ = "index_kline_daily"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_index_kline_symbol_date"),
        Index("ix_index_kline_symbol_date", "symbol", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<IndexKlineDaily {self.symbol} {self.trade_date}>"


class Strategy(Base):
    """策略模板登记（用户自建策略的元数据）。"""

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 策略类型：dual_ma / ma_cross / momentum / custom
    type: Mapped[str] = mapped_column(String(32), index=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 参数默认值（JSON 字符串）
    params_json: Mapped[str] = mapped_column(String(1024), default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Strategy {self.name} {self.type}>"


class Backtest(Base):
    """回测记录（每次运行落库，便于复盘与对比）。"""

    __tablename__ = "backtests"
    __table_args__ = (Index("ix_bt_symbol_date", "symbol", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy_key: Mapped[str] = mapped_column(String(32))
    start_date: Mapped[str] = mapped_column(String(10))
    end_date: Mapped[str] = mapped_column(String(10))
    adj: Mapped[str] = mapped_column(String(4), default="qfq")

    initial_cash: Mapped[float] = mapped_column(Float, default=1_000_000.0)
    commission: Mapped[float] = mapped_column(Float, default=COMMISSION)
    slippage: Mapped[float] = mapped_column(Float, default=SLIPPAGE)
    params_json: Mapped[str] = mapped_column(String(1024), default="{}")

    # 绩效指标
    total_return: Mapped[float] = mapped_column(Float, default=0.0)
    annual_return: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    sharpe: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    round_trips: Mapped[int] = mapped_column(Integer, default=0)
    final_equity: Mapped[float] = mapped_column(Float, default=0.0)

    # 组合回测（指数增强等）扩展指标
    multi_asset: Mapped[bool] = mapped_column(default=False)
    universe_size: Mapped[int] = mapped_column(Integer, default=0)
    symbols_used: Mapped[int] = mapped_column(Integer, default=0)
    benchmark_symbol: Mapped[str] = mapped_column(String(16), default="")
    benchmark_total_return: Mapped[float] = mapped_column(Float, default=0.0)
    excess_return: Mapped[float] = mapped_column(Float, default=0.0)
    info_ratio: Mapped[float] = mapped_column(Float, default=0.0)

    # 曲线与成交明细（JSON）。组合回测成交较多，用 Text 避免长度溢出
    equity_curve_json: Mapped[str] = mapped_column(Text, default="[]")
    trades_json: Mapped[str] = mapped_column(Text, default="[]")
    # 组合回测附加信息（持仓时序、行业分布、因子暴露等），JSON
    extra_json: Mapped[str] = mapped_column(Text, default="{}")

    status: Mapped[str] = mapped_column(String(16), default="done")  # done/error
    error_msg: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Backtest {self.symbol} {self.strategy_key} {self.start_date}>"


class PaperTask(Base):
    """模拟盘(模拟交易)任务配置：把已有策略接实时行情，跟踪虚拟账户。"""

    __tablename__ = "paper_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 策略类型 key（如 chan / rsi_reversal / enhanced_factor）
    strategy_key: Mapped[str] = mapped_column(String(32), index=True)
    # single=单标的 on_bar 驱动；portfolio=组合 rebalance 驱动
    kind: Mapped[str] = mapped_column(String(16), default="single")
    # 单标的代码（逗号分隔，可多个）；组合策略此字段可空，改用 index_code
    symbols: Mapped[str] = mapped_column(String(255), default="")
    # 组合策略的基准/选股指数（如 000906 中证800 / 000300 沪深300）
    index_code: Mapped[str] = mapped_column(String(16), default="")
    params_json: Mapped[str] = mapped_column(String(1024), default="{}")

    initial_cash: Mapped[float] = mapped_column(Float, default=1_000_000.0)
    commission: Mapped[float] = mapped_column(Float, default=COMMISSION)
    slippage: Mapped[float] = mapped_column(Float, default=SLIPPAGE)
    adj: Mapped[str] = mapped_column(String(4), default="qfq")

    enabled: Mapped[bool] = mapped_column(default=False)  # 是否加入自动定时轮询
    # 上次处理到的行情日期（用于增量提取新增成交），None=尚未运行过
    last_bar_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # 账户建账日期（首次运行日）；净值曲线只展示此日之后
    account_start: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # 模拟盘起始日（建仓日，可早于首次运行日）；净值曲线与首次成交从此日起算（可选）
    start_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # 当前账户状态 JSON：{cash, positions:{sym:{shares,cost}}, equity}
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    # 最近一次运行的完整每日净值曲线（date>=建仓日），用于前端展示
    equity_curve_json: Mapped[str] = mapped_column(Text, default="[]")
    # 组合任务最近一次运行的因子研究（IC/IR/分层/PEAD 等），供模拟盘详情页展示
    factor_analysis_json: Mapped[str] = mapped_column(Text, default="{}")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<PaperTask {self.name} {self.strategy_key} {self.kind}>"


class PaperTrade(Base):
    """模拟盘成交明细（增量记录，带 signal_type/reason）。"""

    __tablename__ = "paper_trades"
    __table_args__ = (Index("ix_pt_task_date", "task_id", "date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    date: Mapped[str] = mapped_column(String(10))
    symbol: Mapped[str] = mapped_column(String(16), default="")  # 单标的为空或同 symbol
    side: Mapped[str] = mapped_column(String(4))  # BUY / SELL
    price: Mapped[float] = mapped_column(Float)
    shares: Mapped[float] = mapped_column(Float)
    cash_after: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    signal_type: Mapped[str] = mapped_column(String(32), default="")
    signal_reason: Mapped[str] = mapped_column(Text, default="")

    def __repr__(self):
        return f"<PaperTrade {self.task_id} {self.date} {self.side} {self.symbol}>"


class PaperSnapshot(Base):
    """模拟盘账户净值快照（每日/每 tick 一条，用于画净值曲线）。"""

    __tablename__ = "paper_snapshots"
    __table_args__ = (Index("ix_ps_task_date", "task_id", "date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)  # 快照生成时间
    date: Mapped[str] = mapped_column(String(10))  # 对应行情日期
    equity: Mapped[float] = mapped_column(Float)        # 账户总权益
    cash: Mapped[float] = mapped_column(Float)
    market_value: Mapped[float] = mapped_column(Float)  # 持仓市值
    pnl: Mapped[float] = mapped_column(Float, default=0.0)        # 累计盈亏
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)    # 累计收益率
    positions_json: Mapped[str] = mapped_column(Text, default="[]")

    def __repr__(self):
        return f"<PaperSnapshot {self.task_id} {self.date} {self.equity}>"


class IndexMembership(Base):
    """指数成分股时点快照（落库缓存，根治在线数据源波动导致回测不可复现）。

    trade_date = 该快照对应的成分股生效日（tushare index_weight 每月最后一个
    交易日）。回测时取「≤ 调仓日的最新快照」作合法股票池（point-in-time，
    消除前视/幸存者偏差）。首次缺失时在线拉取并回填，之后回测完全离线可复现。
    """

    __tablename__ = "index_membership"
    __table_args__ = (
        UniqueConstraint("index_code", "trade_date", "symbol", name="uq_membership"),
        Index("ix_membership_code_date", "index_code", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    index_code: Mapped[str] = mapped_column(String(8))      # 纯数字，如 000906
    trade_date: Mapped[date] = mapped_column(Date)
    symbol: Mapped[str] = mapped_column(String(16))          # 归一化后的 symbol（sh600519）
    weight: Mapped[float] = mapped_column(Float, default=0.0)  # 成分权重（%），备用

    def __repr__(self):
        return f"<IndexMembership {self.index_code} {self.trade_date} {self.symbol}>"


class BrokerOrder(Base):
    """券商订单（设计 v1.0 orders 表；当前由模拟券商写入）。

    状态机：PENDING → SUBMITTED → PARTIAL_FILLED → FILLED / CANCELLED / REJECTED
    """

    __tablename__ = "broker_orders"
    __table_args__ = (Index("ix_bo_symbol", "symbol"), Index("ix_bo_created", "created_at"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(8))        # BUY / SELL
    price: Mapped[float] = mapped_column(Float, default=0.0)
    quantity: Mapped[int] = mapped_column(Integer, default=100)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    filled_price: Mapped[float] = mapped_column(Float, default=0.0)
    strategy_key: Mapped[str] = mapped_column(String(32), default="manual")
    tag: Mapped[str] = mapped_column(String(128), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<BrokerOrder {self.order_id} {self.symbol} {self.side} {self.status}>"


class FactorDaily(Base):
    """因子日截面宽表（ETL 每日落库，供选股/因子研究/监控）。

    trade_date = 因子计算截面日；每 (symbol, trade_date) 一行，14 个因子列。
    值取「越大越优」取向（与多因子策略 report_factor 一致）。
    回测仍由引擎实时计算因子，本表服务当前截面分析。
    """

    __tablename__ = "factor_daily"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_factor_daily"),
        Index("ix_fd_date", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16))
    trade_date: Mapped[date] = mapped_column(Date)

    # 14 因子（与 factor_library.FACTORS 对齐）
    ep: Mapped[float | None] = mapped_column(Float, nullable=True)
    bp: Mapped[float | None] = mapped_column(Float, nullable=True)
    momentum: Mapped[float | None] = mapped_column(Float, nullable=True)
    reversal: Mapped[float | None] = mapped_column(Float, nullable=True)
    low_vol: Mapped[float | None] = mapped_column(Float, nullable=True)
    size: Mapped[float | None] = mapped_column(Float, nullable=True)
    beta: Mapped[float | None] = mapped_column(Float, nullable=True)
    idio_vol: Mapped[float | None] = mapped_column(Float, nullable=True)
    skewness: Mapped[float | None] = mapped_column(Float, nullable=True)
    tail_risk: Mapped[float | None] = mapped_column(Float, nullable=True)
    roe: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    earnings_surprise: Mapped[float | None] = mapped_column(Float, nullable=True)

    computed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<FactorDaily {self.symbol} {self.trade_date}>"


FACTOR_COLUMNS = [
    "ep", "bp", "momentum", "reversal", "low_vol", "size", "beta",
    "idio_vol", "skewness", "tail_risk", "roe", "revenue_yoy", "profit_yoy",
    "earnings_surprise",
]


class FactorMineResult(Base):
    """因子挖掘结果（自定义表达式检验报告）。"""

    __tablename__ = "factor_mine_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    expr: Mapped[str] = mapped_column(Text)
    rating: Mapped[str] = mapped_column(String(16), default="")
    ic_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    icir: Mapped[float | None] = mapped_column(Float, nullable=True)
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    # 影响结果的全部入参签名（start|end|groups|forward）。
    # 只比 expr 不够：同表达式换区间/调 forward 结果本就不同，必须一并纳入查重键。
    params_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<FactorMineResult #{self.id} {self.name} {self.rating}>"


class NewsMarketDaily(Base):
    """市场新闻情绪日频因子（文本数据管道第一层，词典法打分）。"""

    __tablename__ = "news_market_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, unique=True)
    n_articles: Mapped[int] = mapped_column(Integer, default=0)
    n_finance: Mapped[int] = mapped_column(Integer, default=0)
    bull_score: Mapped[int] = mapped_column(Integer, default=0)
    bear_score: Mapped[int] = mapped_column(Integer, default=0)
    net_sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)

    def __repr__(self):
        return f"<NewsMarketDaily {self.date} senti={self.net_sentiment}>"


class NewsFeedItem(Base):
    """统一资讯流：各来源的原文条目归一存放，供「全部资讯」页浏览与检索。

    为什么不复用旧的 `doc_document`（6.4 万行）：那张表是已下线的 kb 模块留下的，
    最后写入停在 2026-10-24，且无任何代码引用；它的 `content_hash / dup_group_id /
    is_canonical` 三件套是给去重合并设计的，本场景不需要这么重。

    设计要点：
    - `uid` = sha1(source + 自然主键)，靠唯一约束做幂等，重复同步不会长胖
    - `summary` 截断存（长文只留前 600 字），全文在各来源自己的库/页面
    - 各源时间口径不同（unix 秒 / ISO 字符串），入库前统一成 datetime
    """

    __tablename__ = "news_feed"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(24), index=True)      # cls/eastmoney/...
    source_label: Mapped[str] = mapped_column(String(32), default="")
    title: Mapped[str] = mapped_column(String(400), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(600), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    symbols: Mapped[str] = mapped_column(String(200), default="")
    extra_json: Mapped[str] = mapped_column(Text, default="{}")

    def __repr__(self):
        return f"<NewsFeedItem {self.source} {self.title[:16]}>"


class HotBrief(Base):
    """每小时热点简报（非结构化资讯 → LLM 筛选解读）。

    输入是三腿：① 财联社一手快讯；② 境外日爬（news_feed fn_* / O*）；
    ③ DeepSeek 网页搜索 `/v1/tools/search`（覆盖兜底）。
    模型只负责按「对 A 股有没有实质影响」筛选判断。

    ⚠️ 2026-11-07 我曾误判「DeepSeek API 无联网能力」——当时只试了
    `tools:[{type:"web_search"}]`（422）和几个错误路径（404），没查文档。
    实际联网做成了独立端点 `/v1/tools/search` + `/v1/tools/fetch`，见 ds_search.py。

    `items_json` 存结构化条目（theme/direction/sectors/tickers/horizon/why/strength），
    原文快照留在 bronze，需要回溯时按 window 时间取。
    """

    __tablename__ = "hot_brief"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    n_input: Mapped[int] = mapped_column(Integer, default=0)      # 输入资讯条数（快讯+搜索）
    n_flash: Mapped[int] = mapped_column(Integer, default=0)      # 其中：财联社一手快讯
    n_search: Mapped[int] = mapped_column(Integer, default=0)     # 其中：DeepSeek 网页搜索
    n_foreign: Mapped[int] = mapped_column(Integer, default=0)    # 其中：境外日爬 fn_*
    n_items: Mapped[int] = mapped_column(Integer, default=0)      # 模型保留条数
    items_json: Mapped[str] = mapped_column(Text, default="[]")
    # 本轮喂给模型的**原始输入快照**（编号/来源/时间/标题/链接）。
    # 不存正文，只存可回溯的定位信息：2026-11-07 之前只存模型输出，
    # 想核对「这条是从哪看到的」只能翻日志，而日志里根本没有。
    inputs_json: Mapped[str] = mapped_column(Text, default="[]")
    mainline: Mapped[str] = mapped_column(Text, default="")       # 一句话主线（≤60 字）
    # 今日综述：350~600 字、分政策/产业/海外/资金四段。每轮重写并覆盖当日整体，
    # 不是只总结本小时 —— 老刘 2026-11-07 反馈「首页总结太简单，要全面一点」。
    summary: Mapped[str] = mapped_column(Text, default="")
    # 今日合并后的主题列表（LLM 去重，带 first_seen）。
    # 每轮生成时重算：字符相似度判不出「暴雪黄色预警覆盖东北华北」和
    # 「京津冀暴雪与强冷空气」是同一件事（Jaccard 仅 0.05），必须靠语义。
    today_items_json: Mapped[str] = mapped_column(Text, default="[]")
    model: Mapped[str] = mapped_column(String(40), default="")
    error: Mapped[str] = mapped_column(Text, default="")

    def __repr__(self):
        return f"<HotBrief {self.created_at:%m-%d %H:%M} n={self.n_items}>"


class SocialHeatDaily(Base):
    """社交关注度日频（雪球 A 股热股 / 微博热搜）。

    与 `news_market_daily` 的区别：那边是「多空情绪」（有正文可打分），这边是
    「关注度」（只有热度值、没有正文）。两者互补：一个答「偏多还是偏空」，
    一个答「大家在聊什么、聊得有多热」。

    `source` 区分平台（雪球/微博），`n_finance` 是命中财经关键词的条数，
    `heat_total` 量纲随平台不同（雪球热度 vs 微博搜索量），跨平台不可直接相加。
    """

    __tablename__ = "social_heat_daily"
    __table_args__ = (UniqueConstraint("date", "source", name="uq_social_heat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(12))
    n_items: Mapped[int] = mapped_column(Integer, default=0)
    n_finance: Mapped[int] = mapped_column(Integer, default=0)
    heat_total: Mapped[float] = mapped_column(Float, default=0.0)
    heat_top10: Mapped[float] = mapped_column(Float, default=0.0)

    def __repr__(self):
        return f"<SocialHeatDaily {self.date} {self.source} n={self.n_items}>"


class MpSentimentDaily(Base):
    """公众号文章情绪日频聚合（非结构化 · 公众号语料）。

    为什么要独立一张表：`news_market_daily` 被两个 step 写，口径不同 ——
    东财新闻是**累加**（增量去重后 +=），而公众号的 gold 步骤是**覆盖**（重算）。
    塞进同一张表会互相冲掉（公众号每天 30~60 篇，覆盖会把东财 100~500 篇冲没）。
    分表后 `news_event_test(source="news"|"mp"|"both")` 可按需组合。
    """

    __tablename__ = "mp_sentiment_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, unique=True)
    n_articles: Mapped[int] = mapped_column(Integer, default=0)
    n_finance: Mapped[int] = mapped_column(Integer, default=0)
    bull_score: Mapped[int] = mapped_column(Integer, default=0)
    bear_score: Mapped[int] = mapped_column(Integer, default=0)
    net_sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)

    def __repr__(self):
        return f"<MpSentimentDaily {self.date} senti={self.net_sentiment}>"


class NewsStockDaily(Base):
    """个股新闻情绪日频因子（文章内公司简称匹配 + 多空词典计分）。"""

    __tablename__ = "news_stock_daily"
    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uq_news_stock"),
        Index("ix_nsd_date", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16))
    date: Mapped[date] = mapped_column(Date)
    mentions: Mapped[int] = mapped_column(Integer, default=0)
    bull_score: Mapped[int] = mapped_column(Integer, default=0)
    bear_score: Mapped[int] = mapped_column(Integer, default=0)
    net_sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)

    def __repr__(self):
        return f"<NewsStockDaily {self.symbol} {self.date} m={self.mentions}>"


class PipelineRun(Base):
    """数据管道运行记录（监控）。"""

    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual/scheduler
    status: Mapped[str] = mapped_column(String(12), default="RUNNING")  # RUNNING/SUCCESS/FAILED
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self):
        return f"<PipelineRun #{self.id} {self.status}>"


class PipelineStepLog(Base):
    """管道步骤日志。"""

    __tablename__ = "pipeline_run_steps"
    __table_args__ = (Index("ix_prs_run", "run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(10), default="OK")  # OK/SKIP/FAIL
    duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")


class FactorRegistry(Base):
    """因子注册表：挖掘/文本因子生命周期管理（登记→验证→启用→生产）。"""

    __tablename__ = "factor_registry"
    __table_args__ = (UniqueConstraint("name", name="uq_fr_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    expr: Mapped[str] = mapped_column(Text)
    direction: Mapped[int] = mapped_column(Integer, default=1)  # 1 正向 -1 反向
    category: Mapped[str] = mapped_column(String(24), default="mined")
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 来源 mine result
    status: Mapped[str] = mapped_column(String(12), default="candidate")  # candidate/enabled/disabled
    ic_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FactorMinedDaily(Base):
    """启用挖掘因子的日频截面值（长表）。"""

    __tablename__ = "factor_mined_daily"
    __table_args__ = (
        UniqueConstraint("factor_name", "date", "symbol", name="uq_fmd"),
        Index("ix_fmd_date", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    factor_name: Mapped[str] = mapped_column(String(64))
    date: Mapped[date] = mapped_column(Date)
    symbol: Mapped[str] = mapped_column(String(16))
    value: Mapped[float | None] = mapped_column(Float, nullable=True)


class AdjFactorDaily(Base):
    """复权因子缓存表。

    tushare adj_factor 接口限速极严（1 次/小时），无法逐股调用。
    按 trade_date 批量拉取后缓存在此表，历史因子不再变化，一次入库永久复用。
    """

    __tablename__ = "adj_factor_daily"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_adj"),
        Index("ix_adj_date", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16))       # 平台格式 sh600000
    trade_date: Mapped[date] = mapped_column(Date)
    adj_factor: Mapped[float] = mapped_column(Float)


class HealthRule(Base):
    """数据健康度检查规则（可配置）。"""

    __tablename__ = "health_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    layer: Mapped[str] = mapped_column(String(16))  # collect/process/apply
    metric: Mapped[str] = mapped_column(String(32))  # 检查器类型
    params: Mapped[str] = mapped_column(Text, default="{}")  # JSON 参数
    comparator: Mapped[str] = mapped_column(String(4), default=">=")  # >=/<=/==/!=
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    level: Mapped[str] = mapped_column(String(8), default="error")  # 未通过时的告警级别
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    last_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(8), nullable=True)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LlmProvider(Base):
    """大模型通道配置（页面上维护，运行时生效，无需重启）。

    为什么落库而不是只读 .env：.env 是**启动时**加载的，改一次要重启后端；本表由
    ``services/llm_config.resolve_active()`` 在每次调用时读，页面改完立即生效。

    不变量：只要有记录，就有且仅有一条 ``is_default=1``（由服务层维护），
    因此不存在「全都不是默认」的悬空态。

    安全：``api_key`` 为明文存储 —— 与 ``.env`` 同级（本机单用户、库文件同权限），
    不做假加密；API 返回一律走 :func:`llm_config.mask_key` 脱敏，绝不出明文。
    """

    __tablename__ = "llm_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))  # 显示名，如「DeepSeek 主号」
    vendor: Mapped[str] = mapped_column(String(24), default="custom")  # 预设标识
    base_url: Mapped[str] = mapped_column(String(255))
    api_key: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(120))
    is_default: Mapped[int] = mapped_column(Integer, default=0)
    # 最近一次连通性测试结果（页面直接展示，省得每次重测）
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_ok: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_test_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_test_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


# ────────────────────────── 信号中心（Sequoia-X 迁移，2026-09-30）──────────────────────────

class SignalDaily(Base):
    """每日选股明细：某交易日、某策略命中了哪些股票。

    这是信号引擎的**原始事实层**——streak（连续出现天数）、共振数、动作判定
    全部由它聚合而来，不做任何人工修正。UNIQUE(date, strategy, symbol) 保证
    重跑幂等（同日同策略同股只留一行）。
    """

    __tablename__ = "signal_daily"
    __table_args__ = (
        UniqueConstraint("date", "strategy", "symbol", name="uq_signal_daily_dss"),
        Index("ix_signal_daily_date", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date)            # 数据日期（= 交易日）
    strategy: Mapped[str] = mapped_column(String(40))   # 策略 key，如 ma_volume
    symbol: Mapped[str] = mapped_column(String(16))     # 平台格式，如 sz000001
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON：命中时的关键指标
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class SignalAction(Base):
    """每日买卖动作判定（signal_engine._judge 的输出）。

    action ∈ BUY_STRONG / WATCH / NEW / REDUCE / SELL / EXIT（与 Sequoia 雷达口径一致）。
    判定顺序风控优先：止损/止盈/超期 → MA 破位 → 信号强度。UNIQUE(date, symbol)。
    """

    __tablename__ = "signal_actions"
    __table_args__ = (
        UniqueConstraint("date", "symbol", name="uq_signal_actions_ds"),
        Index("ix_signal_actions_date", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date)
    symbol: Mapped[str] = mapped_column(String(16))
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(16))
    streak: Mapped[int] = mapped_column(Integer, default=0)       # 连续出现交易日数
    resonance: Mapped[int] = mapped_column(Integer, default=0)    # 当日共振策略数
    strategies: Mapped[str] = mapped_column(Text, default="")     # 命中策略名，顿号分隔
    quality_tier: Mapped[str] = mapped_column(String(8), default="neutral")
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    ma10: Mapped[float | None] = mapped_column(Float, nullable=True)
    ma20: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AgentEvent(Base):
    """Agent 事件流：研究过程的每一步动作都落这里，SSE 由此推送到驾驶舱。

    run_id 关联 research_runs；level ∈ info/warn/error；status 用于步骤型事件
    （running/ok/fail）。append-only，不做更新——事件流的历史就是研究过程的回放。
    """

    __tablename__ = "agent_events"
    __table_args__ = (Index("ix_agent_events_run", "run_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent: Mapped[str] = mapped_column(String(24))     # data/quality/factor/research/...
    step: Mapped[str] = mapped_column(String(64), default="")
    level: Mapped[str] = mapped_column(String(8), default="info")
    status: Mapped[str | None] = mapped_column(String(12), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ResearchRun(Base):
    """一次研究运行（每日流水线或手动触发）的顶层记录。

    gate_status ∈ none/pending/approved/rejected —— RiskAgent 审查闸门状态，
    pending 时流水线挂起等待人工（或倒计时自动放行）。
    """

    __tablename__ = "research_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual/scheduler
    status: Mapped[str] = mapped_column(String(12), default="RUNNING")
    data_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # 本轮处理的数据日期
    gate_status: Mapped[str] = mapped_column(String(12), default="none")
    gate_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 闸门倒计时截止时刻：超过则自动放行（保证无人值守），人工放行/否决可提前
    gate_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 注意：必须用 datetime.now（本地时区），与 orchestrator 的 finished_at 口径一致。
    # 曾误用 utcnow，导致 started_at 比 finished_at 早 8 小时，所有 run 耗时虚增。
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class NotifyLog(Base):
    """发布记录：每次邮件/飞书推送一条，便于追溯与去重。"""

    __tablename__ = "notify_log"
    __table_args__ = (Index("ix_notify_log_dedup", "dedup_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(16))   # email/feishu
    subject: Mapped[str] = mapped_column(String(255), default="")
    dedup_key: Mapped[str] = mapped_column(String(128), default="")  # 如 signal|2026-09-30
    status: Mapped[str] = mapped_column(String(12), default="ok")    # ok/fail/skip
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class PortfolioState(Base):
    """镜像组合快照（Sequoia watchlist 迁移）：按信号自动模拟的真实成本组合。

    每次 BacktestAgent 运行按数据日 upsert（同日覆盖）；equity/holdings/trades
    均为 JSON 数组，前端直接渲染。
    """

    __tablename__ = "portfolio_state"
    __table_args__ = (UniqueConstraint("as_of_date", name="uq_portfolio_state_d"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of_date: Mapped[date] = mapped_column(Date)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    equity_json: Mapped[str] = mapped_column(Text, default="[]")
    holdings_json: Mapped[str] = mapped_column(Text, default="[]")
    trades_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class PortfolioExpState(Base):
    """实验组合快照（2026-10-14 重新优化）：fresh_ma 规则的第二条 paper sleeve。

    与 portfolio_state（经典 BUY_STRONG 规则）完全隔离、并行运行，互不影响；
    实验结论未稳定前不接入生产信号管道。按数据日 upsert（同日覆盖）。
    equity/holdings/trades 均为 JSON 数组，前端直接渲染。
    """

    __tablename__ = "portfolio_exp_state"
    __table_args__ = (UniqueConstraint("as_of_date", name="uq_portfolio_exp_d"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of_date: Mapped[date] = mapped_column(Date)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    equity_json: Mapped[str] = mapped_column(Text, default="[]")
    holdings_json: Mapped[str] = mapped_column(Text, default="[]")
    trades_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AutoResearchRun(Base):
    """自动研究闭环运行记录（每日自动挖矿 / 每周自动复盘）。

    summary_json 存结构化摘要（挖掘：评级分布/入库名单；复盘：归因要点/建议），
    report_path 指向 data/reports/ 下的完整 HTML 报告。
    """

    __tablename__ = "auto_research_runs"
    __table_args__ = (
        UniqueConstraint("job", "schedule_key", name="uq_auto_research_schedule"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(16))          # mining / review
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # scheduler / manual
    schedule_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(12), default="RUNNING")  # RUNNING/SUCCESS/FAILED
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    report_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ResearchCard(Base):
    """研究卡：一个策略/因子从想法到生产的标准化载体（docs/research-card-v0.1.md）。

    状态机：watch 观察 → candidate 候选 → enabled 启用 → downgraded 降级 → retired 退役。
    本表是生产门禁的唯一真相：信号定档（xq/runner → resolve_strategy_quality）与
    因子每日计算（datahub/runner → enabled_factor_names）都读这里；
    STRATEGY_QUALITY / factor_registry.status 仅作冗余校验与无卡回退。
    """

    __tablename__ = "research_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_type: Mapped[str] = mapped_column(String(12))      # strategy / factor
    key: Mapped[str] = mapped_column(String(64))            # 策略 key 或因子名（类型内唯一）
    name: Mapped[str] = mapped_column(String(64))           # 中文名
    hypothesis: Mapped[str] = mapped_column(Text, default="")        # 假设（一句话）
    data_spec: Mapped[str] = mapped_column(Text, default="")         # 数据与口径
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")   # 证据（窗口超额/样本量/t 值/IC）
    cost_note: Mapped[str] = mapped_column(Text, default="")         # 成本与容量
    crowd_note: Mapped[str] = mapped_column(Text, default="")        # 拥挤度/相关性
    fail_condition: Mapped[str] = mapped_column(Text, default="")    # 失效条件（预先写明）
    state: Mapped[str] = mapped_column(String(16), default="watch")
    history_json: Mapped[str] = mapped_column(Text, default="[]")    # 状态迁移留痕
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ══════════════════════════════════════════════════════════════════════
# 实盘系统（L3-L7，docs/LIVE-TRADING-DESIGN.md）
#
# 与 paper_* / broker_orders 物理隔离：实盘数据不可重算，不能被研究流水线
# 覆盖，也不能与「重演型」模拟盘混用语义。
# 唯一事实源：live_position 只由成交回报（live_fill）驱动，绝不由模型推演写入。
# ══════════════════════════════════════════════════════════════════════


class LivePosition(Base):
    """实盘实际持仓（唯一事实源）。

    entry_date / peak_close 是策略风控的状态变量（硬止损、移动止损、时间止损），
    必须随持仓一起维护——回测里这些是内存变量，实盘必须落库，否则重启即丢。
    """

    __tablename__ = "live_position"
    __table_args__ = (UniqueConstraint("strategy_key", "symbol", name="uq_live_pos"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_key: Mapped[str] = mapped_column(String(32), default="fresh_ma")
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    shares: Mapped[int] = mapped_column(Integer, default=0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0.0)      # 未复权成交均价
    entry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    peak_close: Mapped[float] = mapped_column(Float, default=0.0)    # 持仓期最高收盘（未复权）
    last_mark_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 onupdate=datetime.now)


class LiveDecisionSnapshot(Base):
    """决策快照（不可变，可复现的锚点）。

    为什么必须存：候选资格依赖 resolve_strategy_quality(db)，而卡片档位会变。
    今天的 enabled 集合与三个月后回看时算出来的不是同一套——不冻结就永远无法回答
    「那天为什么买这只」。
    """

    __tablename__ = "live_decision_snapshot"
    __table_args__ = (UniqueConstraint("trade_date", "strategy_key", name="uq_live_snap"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    strategy_key: Mapped[str] = mapped_column(String(32), default="fresh_ma")
    data_date: Mapped[date | None] = mapped_column(Date, nullable=True)   # 行情实际最新日
    data_ok: Mapped[int] = mapped_column(Integer, default=0)              # 完整性闸门 0/1
    data_note: Mapped[str] = mapped_column(Text, default="")
    enabled_json: Mapped[str] = mapped_column(Text, default="[]")         # 当日 enabled 策略
    card_tiers_json: Mapped[str] = mapped_column(Text, default="{}")      # 九策略档位
    gate_open: Mapped[int] = mapped_column(Integer, default=0)
    gate_exc: Mapped[float | None] = mapped_column(Float, nullable=True)
    gate_n: Mapped[int] = mapped_column(Integer, default=0)
    candidates_json: Mapped[str] = mapped_column(Text, default="[]")      # 候选池明细
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class LiveOrderIntent(Base):
    """订单意图 = 目标持仓 − 实际持仓。

    幂等键 (trade_date, strategy_key, symbol, side)：同一天重复跑流水线只更新
    不新增，这是「重跑无害」的基础。
    状态机：DRAFT → APPROVED → SENT → FILLED / PARTIAL / REJECTED / EXPIRED
    """

    __tablename__ = "live_order_intent"
    __table_args__ = (
        UniqueConstraint("trade_date", "strategy_key", "symbol", "side",
                         name="uq_live_intent"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)     # 信号日（次日执行）
    exec_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    strategy_key: Mapped[str] = mapped_column(String(32), default="fresh_ma")
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    side: Mapped[str] = mapped_column(String(4))                   # BUY / SELL
    target_shares: Mapped[int] = mapped_column(Integer, default=0)
    actual_shares: Mapped[int] = mapped_column(Integer, default=0)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    ref_price: Mapped[float] = mapped_column(Float, default=0.0)   # 未复权参考价（收盘）
    limit_price: Mapped[float] = mapped_column(Float, default=0.0) # 建议限价
    est_amount: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    filled_qty: Mapped[int] = mapped_column(Integer, default=0)
    filled_price: Mapped[float] = mapped_column(Float, default=0.0)
    snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 onupdate=datetime.now)


class LiveFill(Base):
    """成交回报（人工录入 / 对账单 CSV 导入）。

    source: manual / csv / broker。对账以本表为准，不以 intent 为准。
    external_id 是券商成交编号或导入器生成的稳定标识；同一来源内重复写入必须无副作用。
    """

    __tablename__ = "live_fill"
    __table_args__ = (
        UniqueConstraint("strategy_key", "source", "external_id", name="uq_live_fill_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    intent_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    strategy_key: Mapped[str] = mapped_column(String(32), default="fresh_ma")
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[int] = mapped_column(Integer, default=0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    stamp_tax: Mapped[float] = mapped_column(Float, default=0.0)
    transfer_fee: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(12), default="manual")
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class LiveCorporateAction(Base):
    """公司行动事件：送拆股比例与每股现金分红均按除权日前持仓计算。"""

    __tablename__ = "live_corporate_action"
    __table_args__ = (
        UniqueConstraint("strategy_key", "source", "external_id", name="uq_live_ca_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    strategy_key: Mapped[str] = mapped_column(String(32), default="fresh_ma")
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    share_ratio: Mapped[float] = mapped_column(Float, default=1.0)
    cash_dividend_per_share: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(12), default="manual")
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class LiveEquity(Base):
    """每日净值：实盘 + 影子对照。

    divergence_pct = 实盘累计收益 − 影子累计收益，等于执行成本的度量。
    这个值长期稳定在成本量级 = 系统健康；突然扩大 = 执行或数据出问题。
    """

    __tablename__ = "live_equity"
    __table_args__ = (UniqueConstraint("trade_date", "strategy_key", name="uq_live_eq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    strategy_key: Mapped[str] = mapped_column(String(32), default="fresh_ma")
    cash: Mapped[float] = mapped_column(Float, default=0.0)
    market_value: Mapped[float] = mapped_column(Float, default=0.0)
    total: Mapped[float] = mapped_column(Float, default=0.0)
    n_positions: Mapped[int] = mapped_column(Integer, default=0)
    shadow_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    divergence_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ══════════════════════════════════════════════════════════════════════
# ETF 资金流（国家队 ETF 资金看板）
#
# 定位：**观察视图**，不是信号。信号仍是 NSI（`data/research/nsi_signal.csv`，
# 5 只核心 ETF 份额口径，T+1 开盘执行，见 docs/NSI-SPEC.md）。
# 本组表格把覆盖面从 5 只扩到 20 只、并补上 NSI 明确放弃的盘中量能，
# 用于回答「今天资金在买哪一类宽基、量能是否异常」，不参与任何仓位决策。
#
# 单位约定（统一在入库时换算好，查询侧不再二次换算）：
#   fd_share   万份（tushare 原单位，不做复权——份额本身无复权概念）
#   unit_nav   元/份
#   amount / turnover / net_inflow  亿元
# ══════════════════════════════════════════════════════════════════════


class EtfUniverse(Base):
    """重点宽基 ETF 样本池（20 只，按规模+成交额自动筛选，可人工覆盖）。

    category 是归一化后的宽基类别（沪深300/中证A500/科创50…），不是 tushare 的
    benchmark 原文 —— 后者写法极不统一（「上海证券交易所50成份指数」=上证50、
    「中证小盘500指数」=中证500），必须走别名表映射，否则主力 ETF 会漏掉。
    """

    __tablename__ = "etf_universe"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    category: Mapped[str] = mapped_column(String(16), index=True, default="")
    scale: Mapped[float] = mapped_column(Float, default=0.0)   # 存量规模（亿元）
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class EtfShareDaily(Base):
    """ETF 份额日频（一级申赎数据，T 日盘后 → T+1 盘前可见）。

    ⚠️ 份额折算是这张表最大的坑：2024-09-20 易方达沪深300ETF（510310）做 1:2
    合并，净值 1.60→3.20、份额腰斩。直接用「Δ份额 × 净值」会算出 −1906 亿的
    假赎回。判据：份额与净值同时大幅反向变动、且两者乘积≈1 → 该日该只不计入
    净流入（见 services/etf_flow.py 的 split 检测）。
    """

    __tablename__ = "etf_share_daily"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_etf_share_symbol_date"),
        Index("ix_etf_share_date", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    fd_share: Mapped[float] = mapped_column(Float, nullable=False)   # 万份
    unit_nav: Mapped[float | None] = mapped_column(Float, nullable=True)  # 元/份


class EtfMinuteBar(Base):
    """ETF 与指数的 1 分钟线（仅最近 25 个交易日，用于同期对比与形态分析）。

    tushare 的 `stk_mins` 对 `000001.SH` 同样有效，指数分钟线一并落在这里，
    symbol 用 `000001.SH` 区分。盘中取不到当日数据（返回空），只能盘后补；
    盘中成交额走 `etf_quote_intraday` 的累计值差分。
    """

    __tablename__ = "etf_minute_bar"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_time", name="uq_etf_min_symbol_time"),
        Index("ix_etf_min_time", "trade_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    amount: Mapped[float] = mapped_column(Float, default=0.0)   # 该分钟成交额（亿元）
    close: Mapped[float] = mapped_column(Float, default=0.0)


class EtfFlowDaily(Base):
    """20 只 ETF 汇总的日度资金流（看板第三条主线）。

    与 nsi_signal.csv 的区别：口径更宽（20 只 / 9 类宽基 vs 5 只核心），
    且含主题 ETF 的自发申购噪声 —— 所以**不能拿来当信号**，
    只用于观察「护盘重心在哪一类」。
    """

    __tablename__ = "etf_flow_daily"
    __table_args__ = (UniqueConstraint("trade_date", name="uq_etf_flow_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    turnover: Mapped[float] = mapped_column(Float, default=0.0)     # 20 只成交额合计（亿元）
    net_inflow: Mapped[float] = mapped_column(Float, default=0.0)   # 20 只净流入合计（亿元）
    index_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    index_chg: Mapped[float | None] = mapped_column(Float, nullable=True)  # 涨跌幅（%）


class EtfCategoryDaily(Base):
    """12 类宽基 ETF 的分类日度净申赎（看板热力矩阵）。

    按 tushare `fund_basic.benchmark` 的归一化别名归类，覆盖全市场被动股票型 ETF，
    比 20 只样本更全 —— 样本池只取每类前几名，矩阵反映的是整类资金的进出。
    """

    __tablename__ = "etf_category_daily"
    __table_args__ = (
        UniqueConstraint("trade_date", "category", name="uq_etf_cat_date"),
        Index("ix_etf_cat_date", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    net_inflow: Mapped[float] = mapped_column(Float, default=0.0)   # 亿元
    turnover: Mapped[float] = mapped_column(Float, default=0.0)     # 亿元
    scale: Mapped[float] = mapped_column(Float, default=0.0)        # 该类存量规模（亿元）


class EtfQuoteIntraday(Base):
    """盘中实时采样（`realtime_quote` 的累计成交额，逐分钟落一行）。

    为什么需要它：tushare 盘中不提供 1min 线，而看板要「今天到现在成交了多少」。
    一次采样拿 20 只 ETF + 1 个指数（**1 次接口调用**，不是 21 次），
    相邻两次的 cum_amount 差分即为该分钟成交额。
    收盘后由当日完整 1min 线覆盖，所以这张表只保留当日数据。
    """

    __tablename__ = "etf_quote_intraday"
    __table_args__ = (
        UniqueConstraint("symbol", "sample_time", name="uq_etf_quote_symbol_time"),
        Index("ix_etf_quote_time", "sample_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    sample_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    cum_amount: Mapped[float | None] = mapped_column(Float, nullable=True)  # 当日累计成交额（亿元）
    pre_close: Mapped[float | None] = mapped_column(Float, nullable=True)


class EtfDailyBar(Base):
    """单只 ETF 的日线（支撑看板里的「单只 ETF 当日表现」拆解）。

    为什么单独一张表：`etf_flow_daily` 只存 20 只的汇总，看不出「到底是谁在放量」。
    拆开之后才能判断资金是「越跌越买」（防御性托底）还是「追涨」（进攻性抢筹）——
    这比只看总量更能区分资金性质。

    单位：amount 亿元（tushare 原单位千元，入库已换算），pct_chg 百分数原值。
    """

    __tablename__ = "etf_daily_bar"
    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_etf_bar_symbol_date"),
        Index("ix_etf_bar_date", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    close: Mapped[float] = mapped_column(Float, default=0.0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)     # 成交额（亿元）
    pct_chg: Mapped[float | None] = mapped_column(Float, nullable=True)


class QuoteSnapshot(Base):
    """收盘行情快照：每交易日由定时任务从 tushare 拉回后落库，供 /market/quote 只读。

    为什么单独一张表：原先 /market/quote 每次请求都实时查 akshare 全市场截面
    （stock_zh_a_spot_em 分页拉 59 页，约 11 秒），首页四个指数 + 行情看板标题
    一起打开就把服务拖到看门狗重启。行情展示属于「一天一更就够」的数据，
    不该在请求链路里访问外部数据源（2026-11-07）。

    口径：price 为当日收盘价（2 位小数），change_pct 用 tushare pct_chg
    （已按 pre_close 处理除权，比「相邻两根 close 相除」准确），保留 2 位。
    """

    __tablename__ = "quote_snapshot"
    __table_args__ = (Index("ix_quote_snapshot_date", "trade_date"),)

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(16), default="tushare")  # tushare / kline_fallback
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

# ───────────── 公募基金 / 基金经理（2026-11-08 补）─────────────
# 为什么要建：老刘在做基金经理/基金的因子发现，但库里只有 ETF 行情，
# 连「谁管哪只基金、任期多久、什么背景」都没有。tushare 的 fund_basic /
# fund_manager / fund_nav 三个接口都通（实测 3014 只 ETF、5000 条经理记录、
# 按 nav_date 一次能拉全市场 10500 行），只是从来没建表入库。

class FundBasic(Base):
    """基金基础信息（tushare fund_basic，market=E 为场内 ETF/LOF）。

    低频变化：一只基金成立后这些字段基本不动，所以按 ts_code 全量覆盖即可。
    """

    __tablename__ = "fund_basic"

    ts_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    management: Mapped[str] = mapped_column(String(64), default="")   # 基金管理人
    custodian: Mapped[str] = mapped_column(String(64), default="")    # 托管人
    fund_type: Mapped[str] = mapped_column(String(24), default="")    # 股票型/债券型/…
    found_date: Mapped[str | None] = mapped_column(String(8), nullable=True)
    list_date: Mapped[str | None] = mapped_column(String(8), nullable=True)
    issue_date: Mapped[str | None] = mapped_column(String(8), nullable=True)
    delist_date: Mapped[str | None] = mapped_column(String(8), nullable=True)
    issue_amount: Mapped[float | None] = mapped_column(Float, nullable=True)   # 发行份额（亿份）
    m_fee: Mapped[float | None] = mapped_column(Float, nullable=True)          # 管理费率 %
    c_fee: Mapped[float | None] = mapped_column(Float, nullable=True)          # 托管费率 %
    p_value: Mapped[float | None] = mapped_column(Float, nullable=True)        # 面值
    min_amount: Mapped[float | None] = mapped_column(Float, nullable=True)     # 最小申购（元）
    benchmark: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(4), default="")        # L 上市 / D 摘牌
    invest_type: Mapped[str] = mapped_column(String(24), default="")
    type: Mapped[str] = mapped_column(String(24), default="")
    market: Mapped[str] = mapped_column(String(4), default="E")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:
        return f"<FundBasic {self.ts_code} {self.name[:12]}>"


class FundManager(Base):
    """基金经理任职记录（tushare fund_manager）。

    一只基金会有多个经理、一个经理管多只基金，所以主键是
    (ts_code, name, begin_date) 的三元组，不是单列。
    `resume` 是简历原文，做「经理背景因子」时要用（学历/从业年限/跳槽次数）。
    """

    __tablename__ = "fund_manager"
    __table_args__ = (
        UniqueConstraint("ts_code", "name", "begin_date", name="uq_fund_manager"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), index=True)
    ann_date: Mapped[str | None] = mapped_column(String(8), nullable=True)
    name: Mapped[str] = mapped_column(String(32), index=True)
    gender: Mapped[str] = mapped_column(String(2), default="")
    birth_year: Mapped[str | None] = mapped_column(String(6), nullable=True)
    edu: Mapped[str] = mapped_column(String(16), default="")
    nationality: Mapped[str] = mapped_column(String(24), default="")
    begin_date: Mapped[str | None] = mapped_column(String(8), nullable=True)
    end_date: Mapped[str | None] = mapped_column(String(8), nullable=True)
    resume: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:
        return f"<FundManager {self.name} {self.ts_code}>"


class FundNav(Base):
    """基金净值日频（tushare fund_nav，按 nav_date 可一次拉全市场）。

    ⚠️ 单位净值 `unit_nav` 与累计净值 `accum_nav` 不要混用：分红拆分会体现在
    前者下跌，做收益计算要么用 `adj_nav`，要么用 accum_nav 的差分。
    """

    __tablename__ = "fund_nav"
    __table_args__ = (
        UniqueConstraint("ts_code", "nav_date", name="uq_fund_nav"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_code: Mapped[str] = mapped_column(String(16), index=True)
    ann_date: Mapped[str | None] = mapped_column(String(8), nullable=True)
    nav_date: Mapped[str] = mapped_column(String(8), index=True)
    unit_nav: Mapped[float | None] = mapped_column(Float, nullable=True)
    accum_nav: Mapped[float | None] = mapped_column(Float, nullable=True)
    accum_div: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_asset: Mapped[float | None] = mapped_column(Float, nullable=True)
    adj_nav: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:
        return f"<FundNav {self.ts_code} {self.nav_date}>"

# ───────────── 券商研报（2026-11-08 补）─────────────
# 为什么值得单独建表：这是唯一同时带「评级 + 三年盈利预测 + 机构」的公开源。
# 盈利预测修正是 A 股最强的单因子之一，而此前平台完全没有这类数据。

class ResearchReport(Base):
    """个股研报元数据（东方财富研报中心 reportapi）。

    正文在 PDF 里（`pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf`），本表只存结构化
    字段 + info_code：先把「谁在什么时候把评级/盈利预测改成了什么」这个最有用
    的部分拿下来，正文按需再抓。

    ⚠️ `publish_date` 接口给的是 `2026-11-08 00:00:00.000`，统一截成 date。
    """

    __tablename__ = "research_report"

    info_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    stock_code: Mapped[str] = mapped_column(String(8), index=True, default="")
    stock_name: Mapped[str] = mapped_column(String(32), default="")
    org_code: Mapped[str] = mapped_column(String(16), default="")
    org_name: Mapped[str] = mapped_column(String(64), default="")
    org_sname: Mapped[str] = mapped_column(String(32), default="")     # 机构简称
    publish_date: Mapped[date] = mapped_column(Date, index=True)
    rating: Mapped[str] = mapped_column(String(16), default="")       # 买入/增持/中性…
    industry: Mapped[str] = mapped_column(String(32), default="")
    # 三年盈利预测（EPS 元 / PE 倍）。用于「预测上调幅度」因子。
    eps_this: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_next: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_next2: Mapped[float | None] = mapped_column(Float, nullable=True)
    pe_this: Mapped[float | None] = mapped_column(Float, nullable=True)
    pe_next: Mapped[float | None] = mapped_column(Float, nullable=True)
    pe_next2: Mapped[float | None] = mapped_column(Float, nullable=True)
    url: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:
        return f"<ResearchReport {self.stock_name} {self.rating} {self.publish_date}>"

# ───────────── 当日全景综述（2026-11-08 补）─────────────
# 与 HotBrief 的分工：HotBrief 是「每轮增量」（一天 8 次，flash，只看自己那个窗口），
# 本表是「全天全景」（每天 1 次，v4-pro，看全天所有轮次 + 全量资讯统计）。
# 分开存的原因：一天只能有一份日报，覆盖式更新；而简报是流水，一轮一行。
class BriefDigest(Base):
    """某一天的资讯全景综述（收盘后生成，覆盖式更新）。"""

    __tablename__ = "brief_digest"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[str] = mapped_column(String(10), index=True, unique=True)   # YYYY-MM-DD
    summary: Mapped[str] = mapped_column(Text, default="")
    mainline: Mapped[str] = mapped_column(String(200), default="")          # 首段截断，列表页用
    stats_json: Mapped[str] = mapped_column(Text, default="{}")             # 规模/来源/热度
    model: Mapped[str] = mapped_column(String(40), default="")
    n_items: Mapped[int] = mapped_column(Integer, default=0)                # 当天提炼要点数
    n_news: Mapped[int] = mapped_column(Integer, default=0)                 # 当天原始资讯数
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:
        return f"<BriefDigest {self.day} {len(self.summary or '')}字>"
