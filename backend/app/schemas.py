"""Pydantic 响应 / 请求模型。"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, field_validator, Field, ConfigDict

from app.core.cost_defaults import COMMISSION, SLIPPAGE


# ===== 行情 =====
# A 股展示口径（2026-11-07 统一，只在 API 出口层取整，不改库内原始数据，
# 也不影响回测/因子的计算精度）：
#   价格 / 指数点位：2 位小数（交易所最小报价单位 0.01）
#   涨跌幅：2 位小数（百分数）
#   成交量：手，整数     成交额：千元，2 位小数（tushare 原单位）
# 前复权价是「原始价 × 复权因子」算出来的，会产生 23.0826705940108 这类
# 浮点尾巴；同花顺等行情终端一律显示 2 位，这里在出口层收敛。
class KlinePoint(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float

    @field_validator("open", "high", "low", "close", "amount")
    @classmethod
    def _r2(cls, v: float) -> float:
        return round(float(v), 2)

    @field_validator("volume")
    @classmethod
    def _int(cls, v: int) -> int:
        return int(v)


class Quote(BaseModel):
    symbol: str
    name: str
    price: float | None = None
    change_pct: float | None = None
    updated_at: datetime | None = None

    @field_validator("price", "change_pct")
    @classmethod
    def _r2(cls, v: float | None) -> float | None:
        return None if v is None else round(float(v), 2)


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
    # 前端左栏折叠面板的分组名（组合构建/因子权重/…）。jk 系列参数多达 30 个，
    # 平铺渲染时无法定位、也看不出改过哪些，故由 registry 统一打标分组。
    group: str | None = None


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
    commission: float = COMMISSION
    slippage: float = SLIPPAGE
    adj: str = "qfq"
    # W46-1：网格选格写回；省略则按 commission/slippage 是否显式推断 default/user
    cost_source: str | None = None  # default|user|grid_pick（写入侧由 eval_profile 归一）


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
    # 同 BacktestSummary：历史记录里可能为空或被写成非数值，读时容错而非崩
    initial_cash: float | None = None
    final_equity: float | None = None
    total_return: float | None = None
    annual_return: float | None = None
    max_drawdown: float | None = None
    sharpe: float | None = None
    win_rate: float | None = None
    trade_count: int | None = None
    round_trips: int | None = None
    benchmark_symbol: str = ""
    benchmark_total_return: float | None = None
    excess_return: float | None = None
    info_ratio: float | None = None
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
    # W45-badge：评估口径徽章（只增字段，旧前端可忽略）
    cost_profile: dict | None = None
    valuation_pit: dict | None = None
    pead_pit: dict | None = None
    oos: dict | None = None


class BacktestSummary(BaseModel):
    id: int
    symbol: str
    strategy_key: str
    strategy_name: str
    multi_asset: bool = False
    start_date: str
    end_date: str
    # 全部允许 None：历史记录里这些列可能为空，也可能被写成非数值字符串
    # （2026-11-08 实测 5 条 info_ratio 存了 meta JSON，整页 500）。
    # 用「读时容错」而不是「读时崩」，前端展示「—」即可。
    total_return: float | None = None
    annual_return: float | None = None
    max_drawdown: float | None = None
    sharpe: float | None = None
    win_rate: float | None = None
    trade_count: int | None = None
    benchmark_total_return: float | None = None
    excess_return: float | None = None
    info_ratio: float | None = None
    created_at: str | None = None


# ===== 参数寻优 =====
class OptimizeRequest(BaseModel):
    symbol: str
    start: str
    end: str
    strategy: str
    param_ranges: dict[str, list[float | int]]  # {"fast": [5,10,20], "slow": [20,40,60]}
    initial_cash: float = 1_000_000.0
    commission: float = COMMISSION
    slippage: float = SLIPPAGE
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
    # W45-badge：任务级口径（各 trial 相同，便于前端从任意行读取）
    cost_profile: dict | None = None
    valuation_pit: dict | None = None
    pead_pit: dict | None = None
    oos: dict | None = None


# ===== W46-1 / M3-7 成本敏感网格 =====
class CostSensitivityRequest(BaseModel):
    """与 BacktestRequest 同名同义的回测身份字段 + 网格轴。stamp_tax 不上轴。"""
    symbol: str
    start: str
    end: str
    strategy: str
    params: dict = {}
    initial_cash: float = 1_000_000.0
    adj: str = "qfq"
    mode: str = "grid"  # grid | multipliers
    commissions: list[float] | None = None
    slippages: list[float] | None = None
    multipliers: list[float] | None = None
    base_commission: float | None = None
    base_slippage: float | None = None
    metric: str = "excess_return"  # excess_return | sharpe | total_return


class CostGridCell(BaseModel):
    commission: float
    slippage: float
    multiplier: float | None = None
    is_default: bool = False
    total_return: float = 0.0
    annual_return: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    excess_return: float = 0.0
    trade_count: int = 0
    final_equity: float = 0.0


class CostSensitivitySummary(BaseModel):
    default_cell_ok: bool = False
    double_cost_excess_positive: bool = False
    fragile: bool = False
    note: str = ""


class CostSensitivityAxes(BaseModel):
    commissions: list[float] | None = None
    slippages: list[float] | None = None
    multipliers: list[float] | None = None


class CostSensitivityResult(BaseModel):
    mode: str
    metric: str
    base_cost_profile: dict
    axes: CostSensitivityAxes
    cells: list[CostGridCell]
    summary: CostSensitivitySummary
    strategy_key: str
    symbol: str
    start: str
    end: str

# ===== W46-2 / M3-3 walk-forward OOS =====
class WalkForwardRequest(BaseModel):
    """与 BacktestRequest 同名同义的回测身份字段 + 滚动切窗参数。"""
    symbol: str
    start: str
    end: str
    strategy: str
    params: dict = {}
    initial_cash: float = 1_000_000.0
    commission: float | None = None  # 缺省 → cost_defaults
    slippage: float | None = None
    adj: str = "qfq"
    mode: str = "walk_forward"  # walk_forward | single
    oos_ratio: float = 0.3
    n_windows: int = 3
    scheme: str = "expanding"
    metric: str = "sharpe"  # sharpe | excess_return
    min_is_bars: int = 60
    min_oos_bars: int = 20


class WalkForwardSegmentMetrics(BaseModel):
    n_bars: int = 0
    sharpe: float = 0.0
    total_return: float = 0.0
    excess_return: float = 0.0
    max_drawdown: float = 0.0
    trade_count: int = 0


class WalkForwardWindow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    index: int
    is_start: str
    is_end: str
    oos_start: str
    oos_end: str
    is_: WalkForwardSegmentMetrics = Field(alias="is")
    oos: WalkForwardSegmentMetrics
    verdict: str
    note: str = ""


class WalkForwardLegacy(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    split_date: str
    oos_start: str
    is_: WalkForwardSegmentMetrics = Field(alias="is")
    oos: WalkForwardSegmentMetrics
    verdict: str
    note: str = ""


class WalkForwardSummary(BaseModel):
    final_verdict: str
    agg_verdict: str
    legacy_verdict: str
    gate_ok: bool = False
    fragile_regime: bool = False
    note: str = ""


class WalkForwardResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    mode: str
    scheme: str
    metric: str
    oos_ratio: float
    n_windows: int
    cost_profile: dict
    oos: dict
    legacy_single: WalkForwardLegacy | None = None
    windows: list[WalkForwardWindow]
    summary: WalkForwardSummary
    strategy_key: str
    symbol: str
    start: str
    end: str

