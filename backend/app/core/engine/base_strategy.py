"""策略 SDK 基类（Phase 2 回测引擎核心，此处先放骨架）。

设计遵循设计方案第 6 节：所有策略继承 StandardStrategy，实现 init() 与 on_bar()。
回测与实盘共用同一份代码，仅通过 ctx.mode 区分。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Mode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


@dataclass
class Context:
    """策略运行上下文，由引擎注入。回测/实盘提供不同实现。"""

    mode: Mode = Mode.BACKTEST
    params: dict[str, Any] = field(default_factory=dict)
    # 以下由具体引擎填充
    indicator: Any = None
    factor: Any = None
    order: Any = None
    risk: Any = None
    positions: dict[str, float] = field(default_factory=dict)

    def subscribe(self, symbols: list[str]) -> None:
        ...

    def is_long(self, symbol: str) -> bool:
        return self.positions.get(symbol, 0) > 0


class StandardStrategy:
    """所有策略的基类。用户只需实现 init 和 on_bar。"""

    params: dict[str, Any] = {}

    def init(self, ctx: Context) -> None:
        """策略初始化：设置参数、订阅标的、预计算因子。"""
        raise NotImplementedError

    def on_bar(self, ctx: Context, bar: Any) -> None:
        """每根 K 线触发一次，在此生成交易信号。"""
        raise NotImplementedError

    def on_close(self, ctx: Context) -> None:
        """收盘后调用（可选）：调仓、复盘、日志。"""
        pass


class PortfolioStrategy:
    """组合策略基类（多标的、横截面因子、定期调仓）。

    与 StandardStrategy 不同：引擎在『调仓日』一次性把整个股票池的行情注入
    ctx，策略在 rebalance() 中通过横截面因子打分完成选股与调仓。
    目前内置引擎为多头组合、按目标仓位调仓。
    """

    params: dict[str, Any] = {}

    def init(self, ctx: "PortfolioContext") -> None:
        raise NotImplementedError

    def rebalance(self, ctx: "PortfolioContext", date: str) -> None:
        raise NotImplementedError
