"""策略模板包。"""
from app.core.strategies.dual_ma import DualMAStrategy
from app.core.strategies.ma_cross import MACrossStrategy
from app.core.strategies.momentum import MomentumStrategy
from app.core.strategies.rsi_reversal import RSIReversalStrategy
from app.core.strategies.bollinger import BollingerStrategy
from app.core.strategies.turtle import TurtleStrategy
from app.core.strategies.ma_alignment import MAAlignmentStrategy

__all__ = [
    "DualMAStrategy", "MACrossStrategy", "MomentumStrategy",
    "RSIReversalStrategy", "BollingerStrategy", "TurtleStrategy", "MAAlignmentStrategy",
]
