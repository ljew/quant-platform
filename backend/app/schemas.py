"""Pydantic 响应 / 请求模型。"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


# ===== 行情 =====
class KlinePoint(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float


class Quote(BaseModel):
    symbol: str
    name: str
    price: float | None = None
    change_pct: float | None = None
    updated_at: datetime | None = None


class StockItem(BaseModel):
    symbol: str
    name: str
    market: str
    industry: str | None = None
    list_date: date | None = None


# ===== 数据接入 =====
class IngestRequest(BaseModel):
    # 为空则全市场；否则指定代码列表
    symbols: list[str] | None = None
    # 起始年份（覆盖默认）
    start_year: int | None = None
    adj: str = "qfq"


class IngestResponse(BaseModel):
    task: str
    status: str
    message: str
    symbols_total: int = 0


# ===== 通用 =====
class HealthResponse(BaseModel):
    status: str
    version: str
    db: str
    data_source: str


# ===== 策略 / 回测 =====
class ParamField(BaseModel):
    key: str
    label: str
    type: str  # int / float / str
    # ⚠️ 必须是 float|str：枚举参数（_opt）的 default 是字符串，若只声明 float
    #    会让 /strategy/strategies 整个接口 500 → 前端策略下拉框空白。
    default: float | str | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: list[str] | None = None   # 枚举型参数的可选值（type=str 时用）
    desc: str | None = None            # 参数说明


class StrategyInfo(BaseModel):
    key: str
    name: str
    description: str
    default_params: dict
    param_schema: list[ParamField]
    multi_asset: bool = False
    index_code: str | None = None
    index_symbol: str | None = None
    index_name: str | None = None


class BacktestRequest(BaseModel):
    symbol: str
    start: str  # YYYY-MM-DD
    end: str  # YYYY-MM-DD
    strategy: str  # 策略 key
    params: dict = {}
    initial_cash: float = 1_000_000.0
    commission: float = 0.0003
    slippage: float = 0.0
    adj: str = "qfq"


class TradePoint(BaseModel):
    date: str
    symbol: str = ""
    side: str
    price: float
    shares: float
    cash_after: float
    commission: float
    pnl: float = 0.0
    signal_type: str = ""
    signal_reason: str = ""


class EquityPointModel(BaseModel):
    date: str
    equity: float
    benchmark: float
    hedged: float = 0.0


class BacktestResult(BaseModel):
    id: int | None = None
    symbol: str
    start: str
    end: str
    strategy_key: str
    strategy_name: str
    multi_asset: bool = False
    universe_size: int = 0
    symbols_used: int = 0
    params: dict
    initial_cash: float
    final_equity: float
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float
    win_rate: float
    trade_count: int
    round_trips: int
    benchmark_total_return: float = 0.0
    excess_return: float = 0.0
    info_ratio: float = 0.0
    equity_curve: list[EquityPointModel]
    trades: list[TradePoint]
    holdings: list[dict] = []
    industry_distribution: dict = {}
    factor_analysis: dict | None = None
    # 风险硬上限（借鉴 ai-hedge-fund risk/limits）：生效的上限配置与被截断的审计记录
    risk_limits: dict | None = None
    risk_clamps: list[dict] = []
    # 市场中性（对冲 beta）视角绩效
    hedged_beta: float = 0.0
    hedged_total_return: float = 0.0
    hedged_annual_return: float = 0.0
    hedged_sharpe: float = 0.0
    hedged_max_drawdown: float = 0.0
    created_at: str | None = None


class BacktestSummary(BaseModel):
    id: int
    symbol: str
    strategy_key: str
    strategy_name: str
    multi_asset: bool = False
    start_date: str
    end_date: str
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float
    win_rate: float
    trade_count: int
    benchmark_total_return: float = 0.0
    excess_return: float = 0.0
    info_ratio: float = 0.0
    created_at: str | None = None


# ===== 参数寻优 =====
class OptimizeRequest(BaseModel):
    symbol: str
    start: str
    end: str
    strategy: str
    param_ranges: dict[str, list[float | int]]  # {"fast": [5,10,20], "slow": [20,40,60]}
    initial_cash: float = 1_000_000.0
    commission: float = 0.0003
    slippage: float = 0.0
    adj: str = "qfq"
    rank_by: str = "sharpe"  # sharpe / total_return / max_drawdown / oos_sharpe / robustness
    oos_ratio: float = 0.3
    """样本外验证段占比（0~0.6）。按**交易日数量**从区间末尾切出一段做验证。
    0 表示不切分（等价于纯样本内网格搜索）。
    网格搜索挑出的最优参数几乎必然对样本内过拟合，没有 OOS 等于白跑，故默认 0.3。"""


class OptimizeTrial(BaseModel):
    params: dict
    # 样本内（IS）：start ~ split_date
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float
    win_rate: float
    trade_count: int
    final_equity: float
    trades: list[TradePoint] = []
    equity_curve: list[EquityPointModel] = []
    # 样本外（OOS）：split_date ~ end。未启用切分时为 None
    oos_start: str | None = None
    oos_total_return: float | None = None
    oos_annual_return: float | None = None
    oos_max_drawdown: float | None = None
    oos_sharpe: float | None = None
    oos_win_rate: float | None = None
    oos_trade_count: int | None = None
    # 稳健性：该组参数在网格中的「邻居」平均表现，偏离越小说明越不是孤峰（运气）
    neighbor_count: int = 0
    neighbor_sharpe: float | None = None
    robustness: float | None = None
