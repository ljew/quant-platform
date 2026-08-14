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


class KlineDaily(Base):
    """日K线表。

    生产环境 TimescaleDB 会将此表转为 Hypertable（按 trade_date 分区），
    索引 (symbol, trade_date) 保证回测时按标的+时间范围的高速查询。
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
    amount: Mapped[float] = mapped_column(Float, default=0.0)  # 成交额（元）
    # 复权类型：qfq 前复权 / hfq 后复权 / None 不复权
    adj: Mapped[str] = mapped_column(String(4), default="qfq")
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
    commission: Mapped[float] = mapped_column(Float, default=0.0003)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
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
    commission: Mapped[float] = mapped_column(Float, default=0.0003)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
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
