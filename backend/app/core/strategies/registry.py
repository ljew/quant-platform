"""策略注册表：引擎与前端共用。

每个条目包含：
- name / description：展示用
- cls：策略类（继承自 StandardStrategy）
- default_params：默认参数
- param_schema：前端动态渲染参数表单的元数据
"""
from __future__ import annotations

from typing import Any, Callable

from app.core.strategies.dual_ma import DualMAStrategy
from app.core.strategies.ma_cross import MACrossStrategy
from app.core.strategies.momentum import MomentumStrategy
from app.core.strategies.rsi_reversal import RSIReversalStrategy
from app.core.strategies.bollinger import BollingerStrategy
from app.core.strategies.turtle import TurtleStrategy
from app.core.strategies.ma_alignment import MAAlignmentStrategy
from app.core.strategies.csi800_enhanced import Csi800EnhancedStrategy
from app.core.strategies.multi_factor import EnhancedFactorStrategy
from app.core.strategies.chan_strategy import ChanStrategy


def _int(key, label, default, mn, mx, step=1):
    return {"key": key, "label": label, "type": "int", "default": default, "min": mn, "max": mx, "step": step}


def _float(key, label, default, mn, mx, step):
    return {"key": key, "label": label, "type": "float", "default": default, "min": mn, "max": mx, "step": step}


STRATEGY_REGISTRY: dict[str, dict[str, Any]] = {
    "dual_ma": {
        "key": "dual_ma",
        "name": "双均线趋势",
        "description": "快线高于慢线时持仓，否则空仓。趋势跟随，参数小则灵敏、大则平滑。",
        "cls": DualMAStrategy,
        "default_params": {"fast": 5, "slow": 20},
        "param_schema": [
            _int("fast", "快线周期(天)", 5, 2, 60),
            _int("slow", "慢线周期(天)", 20, 5, 250),
        ],
    },
    "ma_cross": {
        "key": "ma_cross",
        "name": "均线金叉/死叉",
        "description": "基于 EMA 快慢线，仅在金叉买入、死叉卖出。交易次数更少、抗震荡。",
        "cls": MACrossStrategy,
        "default_params": {"fast": 5, "slow": 20},
        "param_schema": [
            _int("fast", "快线周期(天)", 5, 2, 60),
            _int("slow", "慢线周期(天)", 20, 5, 250),
        ],
    },
    "momentum": {
        "key": "momentum",
        "name": "动量突破",
        "description": "N 日收益率(ROC)大于阈值时持仓，否则空仓。追涨杀跌型。",
        "cls": MomentumStrategy,
        "default_params": {"lookback": 20, "threshold": 0.0},
        "param_schema": [
            _int("lookback", "动量回看(天)", 20, 3, 120),
            _float("threshold", "触发阈值(%)", 0.0, -20.0, 20.0, 0.5),
        ],
    },
    "rsi_reversal": {
        "key": "rsi_reversal",
        "name": "RSI反转",
        "description": "RSI 跌破超卖线(默认30)买入、突破超买线(默认70)卖出，典型均值回归策略，适合震荡市，与趋势类策略互补。",
        "cls": RSIReversalStrategy,
        "default_params": {"period": 14, "oversold": 30, "overbought": 70},
        "param_schema": [
            _int("period", "RSI周期(天)", 14, 2, 60),
            _int("oversold", "超卖线", 30, 5, 50),
            _int("overbought", "超买线", 70, 50, 95),
        ],
    },
    "bollinger": {
        "key": "bollinger",
        "name": "布林带均值回归",
        "description": "价格跌破下轨买入、突破上轨卖出（基于N日移动均线与标准差）。震荡市的逆势策略，单边趋势可能反复止损。",
        "cls": BollingerStrategy,
        "default_params": {"period": 20, "num_std": 2.0},
        "param_schema": [
            _int("period", "均线周期(天)", 20, 5, 120),
            _float("num_std", "通道倍数(σ)", 2.0, 1.0, 4.0, 0.1),
        ],
    },
    "turtle": {
        "key": "turtle",
        "name": "唐奇安通道突破(海龟)",
        "description": "收盘价创N日新高买入、创M日新低卖出（海龟/唐奇安通道）。突破追涨型趋势跟随，长周期捕捉大趋势。",
        "cls": TurtleStrategy,
        "default_params": {"entry": 20, "exit": 10},
        "param_schema": [
            _int("entry", "入场通道(日)", 20, 5, 120, 1),
            _int("exit", "离场通道(日)", 10, 3, 60, 1),
        ],
    },
    "ma_alignment": {
        "key": "ma_alignment",
        "name": "均线多头排列",
        "description": "短/中/长三条均线呈多头排列(短>中>长)时持仓，空头排列时空仓。对趋势确认更严格，过滤部分震荡噪音。",
        "cls": MAAlignmentStrategy,
        "default_params": {"short": 5, "mid": 20, "long": 60},
        "param_schema": [
            _int("short", "短期均线(天)", 5, 2, 30),
            _int("mid", "中期均线(天)", 20, 5, 120),
            _int("long", "长期均线(天)", 60, 20, 250),
        ],
    },
    "csi800_enhanced": {
        "key": "csi800_enhanced",
        "name": "中证800指数增强",
        "description": "在中证800成分股池内，用『动量+反转+低波动』多因子横截面打分，每月选得分最高的 N 只等权配置，跑赢基准指数。基准为中证800指数。",
        "cls": Csi800EnhancedStrategy,
        "multi_asset": True,
        "index_code": "000906",
        "index_symbol": "sh000906",
        "index_name": "中证800",
        "default_params": {
            "top_n": 50, "rebalance_period": 21,
            "momentum_lookback": 60, "reversal_lookback": 5, "vol_lookback": 20,
            "w_mom": 0.5, "w_rev": 0.2, "w_vol": 0.3, "max_weight": 0.05,
        },
        "param_schema": [
            _int("top_n", "选股数量 TopN", 50, 5, 200, 5),
            _int("rebalance_period", "调仓周期(交易日)", 21, 5, 60, 1),
            _int("momentum_lookback", "动量回看(天)", 60, 10, 250, 5),
            _int("reversal_lookback", "反转回看(天)", 5, 1, 20, 1),
            _int("vol_lookback", "波动回看(天)", 20, 5, 120, 5),
            _float("w_mom", "动量权重", 0.5, 0.0, 1.0, 0.05),
            _float("w_rev", "反转权重", 0.2, 0.0, 1.0, 0.05),
            _float("w_vol", "低波权重", 0.3, 0.0, 1.0, 0.05),
            _float("w_value", "价值(估值)权重", 0.2, 0.0, 1.0, 0.05),
            _float("max_weight", "单只最大权重", 0.05, 0.01, 0.2, 0.01),
            _int("neutralize_industry", "行业中性(1开/0关)", 1, 0, 1),
            _int("neutralize_marketcap", "市值中性(1开/0关)", 1, 0, 1),
            _int("weight_method", "加权方式(0=IC自适应 / 1=固定权重)", 0, 0, 1),
        ],
    },
    "hs300_enhanced": {
        "key": "hs300_enhanced",
        "name": "沪深300指数增强",
        "description": "在沪深300成分股池内，用『动量+反转+低波动』多因子横截面打分，每月选得分最高的 N 只等权配置，跑赢基准指数。成分股仅 300 只、比中证800更少，适合不想覆盖太多标的的场景。基准为沪深300指数。",
        "cls": Csi800EnhancedStrategy,
        "multi_asset": True,
        "index_code": "000300",
        "index_symbol": "sh000300",
        "index_name": "沪深300",
        "default_params": {
            "top_n": 30, "rebalance_period": 21,
            "momentum_lookback": 60, "reversal_lookback": 5, "vol_lookback": 20,
            "w_mom": 0.5, "w_rev": 0.2, "w_vol": 0.3, "max_weight": 0.05,
        },
        "param_schema": [
            _int("top_n", "选股数量 TopN", 30, 5, 200, 5),
            _int("rebalance_period", "调仓周期(交易日)", 21, 5, 60, 1),
            _int("momentum_lookback", "动量回看(天)", 60, 10, 250, 5),
            _int("reversal_lookback", "反转回看(天)", 5, 1, 20, 1),
            _int("vol_lookback", "波动回看(天)", 20, 5, 120, 5),
            _float("w_mom", "动量权重", 0.5, 0.0, 1.0, 0.05),
            _float("w_rev", "反转权重", 0.2, 0.0, 1.0, 0.05),
            _float("w_vol", "低波权重", 0.3, 0.0, 1.0, 0.05),
            _float("w_value", "价值(估值)权重", 0.2, 0.0, 1.0, 0.05),
            _float("max_weight", "单只最大权重", 0.05, 0.01, 0.2, 0.01),
            _int("neutralize_industry", "行业中性(1开/0关)", 1, 0, 1),
            _int("neutralize_marketcap", "市值中性(1开/0关)", 1, 0, 1),
            _int("weight_method", "加权方式(0=IC自适应 / 1=固定权重)", 0, 0, 1),
        ],
    },
    "enhanced_factor": {
        "key": "enhanced_factor",
        "name": "研究级多因子(中证800)",
        "description": "对齐 2025-2026 主流金工研报的 10 因子模型：价值(EP/BP)、动量、反转、低波动、小市值、BETA、特异度(残差波动)、偏度、尾部风险。横截面 z-score 合成、行业/市值中性、月度调仓。基准为中证800。所有因子均上报做 IC/IR 研究。",
        "cls": EnhancedFactorStrategy,
        "multi_asset": True,
        "index_code": "000906",
        "index_symbol": "sh000906",
        "index_name": "中证800",
        "default_params": {
            "top_n": 50, "rebalance_period": 21,
            "momentum_lookback": 120, "reversal_lookback": 5, "vol_lookback": 60,
            "beta_lookback": 120, "tail_lookback": 120,
            "w_mom": 0.10, "w_rev": 0.12, "w_vol": 0.20, "w_size": 0.10,
            "w_beta": 0.05, "w_idio": 0.15, "w_skew": 0.05, "w_tail": 0.05,
            "w_ep": 0.15, "w_bp": 0.10, "max_weight": 0.05, "weight_method": "ic",
        },
        "param_schema": [
            _int("top_n", "选股数量 TopN", 50, 5, 200, 5),
            _int("rebalance_period", "调仓周期(交易日)", 21, 5, 60, 1),
            _int("momentum_lookback", "动量回看(天)", 120, 20, 250, 5),
            _int("reversal_lookback", "反转回看(天)", 5, 1, 20, 1),
            _int("vol_lookback", "波动回看(天)", 60, 10, 120, 5),
            _int("beta_lookback", "BETA回看(天)", 120, 20, 250, 5),
            _int("tail_lookback", "尾部风险回看(天)", 120, 20, 250, 5),
            _float("w_mom", "动量权重", 0.4, 0.0, 1.0, 0.05),
            _float("w_rev", "反转权重", 0.15, 0.0, 1.0, 0.05),
            _float("w_vol", "低波权重", 0.15, 0.0, 1.0, 0.05),
            _float("w_size", "小市值权重", 0.15, 0.0, 1.0, 0.05),
            _float("w_beta", "BETA权重", 0.1, 0.0, 1.0, 0.05),
            _float("w_idio", "特异度权重", 0.1, 0.0, 1.0, 0.05),
            _float("w_skew", "偏度权重", 0.05, 0.0, 1.0, 0.05),
            _float("w_tail", "尾部风险权重", 0.05, 0.0, 1.0, 0.05),
            _float("w_ep", "EP(盈利收益率)权重", 0.15, 0.0, 1.0, 0.05),
            _float("w_bp", "BP(账面价值比)权重", 0.15, 0.0, 1.0, 0.05),
            _float("max_weight", "单只最大权重", 0.05, 0.01, 0.2, 0.01),
            _int("neutralize_industry", "行业中性(1开/0关)", 1, 0, 1),
            _int("neutralize_marketcap", "市值中性(1开/0关)", 1, 0, 1),
            _int("weight_method", "加权方式(0=IC自适应 / 1=固定权重)", 0, 0, 1),
        ],
    },
    "enhanced_factor_hs300": {
        "key": "enhanced_factor_hs300",
        "name": "研究级多因子(沪深300)",
        "description": "研究级 10 因子模型（价值EP/BP、动量、反转、低波、小市值、BETA、特异度、偏度、尾部风险）在沪深300成分股池内的版本。基准为沪深300指数。",
        "cls": EnhancedFactorStrategy,
        "multi_asset": True,
        "index_code": "000300",
        "index_symbol": "sh000300",
        "index_name": "沪深300",
        "default_params": {
            "top_n": 30, "rebalance_period": 21,
            "momentum_lookback": 120, "reversal_lookback": 5, "vol_lookback": 60,
            "beta_lookback": 120, "tail_lookback": 120,
            "w_mom": 0.10, "w_rev": 0.12, "w_vol": 0.20, "w_size": 0.10,
            "w_beta": 0.05, "w_idio": 0.15, "w_skew": 0.05, "w_tail": 0.05,
            "w_ep": 0.15, "w_bp": 0.10, "max_weight": 0.05, "weight_method": "ic",
        },
        "param_schema": [
            _int("top_n", "选股数量 TopN", 30, 5, 200, 5),
            _int("rebalance_period", "调仓周期(交易日)", 21, 5, 60, 1),
            _int("momentum_lookback", "动量回看(天)", 120, 20, 250, 5),
            _int("reversal_lookback", "反转回看(天)", 5, 1, 20, 1),
            _int("vol_lookback", "波动回看(天)", 60, 10, 120, 5),
            _int("beta_lookback", "BETA回看(天)", 120, 20, 250, 5),
            _int("tail_lookback", "尾部风险回看(天)", 120, 20, 250, 5),
            _float("w_mom", "动量权重", 0.4, 0.0, 1.0, 0.05),
            _float("w_rev", "反转权重", 0.15, 0.0, 1.0, 0.05),
            _float("w_vol", "低波权重", 0.15, 0.0, 1.0, 0.05),
            _float("w_size", "小市值权重", 0.15, 0.0, 1.0, 0.05),
            _float("w_beta", "BETA权重", 0.1, 0.0, 1.0, 0.05),
            _float("w_idio", "特异度权重", 0.1, 0.0, 1.0, 0.05),
            _float("w_skew", "偏度权重", 0.05, 0.0, 1.0, 0.05),
            _float("w_tail", "尾部风险权重", 0.05, 0.0, 1.0, 0.05),
            _float("w_ep", "EP(盈利收益率)权重", 0.15, 0.0, 1.0, 0.05),
            _float("w_bp", "BP(账面价值比)权重", 0.15, 0.0, 1.0, 0.05),
            _float("max_weight", "单只最大权重", 0.05, 0.01, 0.2, 0.01),
            _int("neutralize_industry", "行业中性(1开/0关)", 1, 0, 1),
            _int("neutralize_marketcap", "市值中性(1开/0关)", 1, 0, 1),
            _int("weight_method", "加权方式(0=IC自适应 / 1=固定权重)", 0, 0, 1),
        ],
    },
    "chan": {
        "key": "chan",
        "name": "缠论买卖点",
        "description": "基于缠论『分型→笔→中枢→三类买卖点』识别趋势背驰与回调机会：一买(下跌背驰末端)、二买(回调不破前低)、三买(突破中枢回踩不破) 买入；对称卖点(一/二/三卖)离场。低频逆向策略，与趋势/均值回归类互补，买卖点类型随成交回传标注。",
        "cls": ChanStrategy,
        "default_params": {"bi_gap": 4, "need_trend": 2, "use_sell": 1},
        "param_schema": [
            _int("bi_gap", "笔最小间隔(根K线)", 4, 2, 20),
            _int("need_trend", "一买所需下降笔数", 2, 1, 5),
            _int("use_sell", "用缠论卖点平仓(1开/0关)", 1, 0, 1),
        ],
    },
}


def get_strategy(key: str) -> dict:
    if key not in STRATEGY_REGISTRY:
        raise KeyError(f"未知策略: {key}")
    return STRATEGY_REGISTRY[key]


def list_strategies() -> list[dict]:
    return [
        {
            "key": k,
            "name": v["name"],
            "description": v["description"],
            "default_params": v["default_params"],
            "param_schema": v["param_schema"],
            "multi_asset": v.get("multi_asset", False),
            "index_code": v.get("index_code"),
            "index_symbol": v.get("index_symbol"),
            "index_name": v.get("index_name"),
        }
        for k, v in STRATEGY_REGISTRY.items()
    ]
