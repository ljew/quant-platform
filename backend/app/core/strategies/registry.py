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
from app.core.strategies.turtle_classic import TurtleClassicStrategy
from app.core.strategies.ma_alignment import MAAlignmentStrategy
from app.core.strategies.macd_boll import MacdBollStrategy
from app.core.strategies.csi800_enhanced import Csi800EnhancedStrategy
from app.core.strategies.multi_factor import EnhancedFactorStrategy
from app.core.strategies.chan_strategy import ChanStrategy
from app.core.strategies.jk_series import JKFactorStrategy


def _int(key, label, default, mn, mx, step=1):
    return {"key": key, "label": label, "type": "int", "default": default, "min": mn, "max": mx, "step": step}


def _float(key, label, default, mn, mx, step):
    return {"key": key, "label": label, "type": "float", "default": default, "min": mn, "max": mx, "step": step}


def _opt(key, label, default, options, desc=""):
    """枚举型参数（用于口径切换类的对照实验）。"""
    return {"key": key, "label": label, "type": "str", "default": default,
            "options": options, "desc": desc}


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
    "turtle_classic": {
        "key": "turtle_classic",
        "name": "经典海龟交易法(ATR仓位)",
        "description": "完整海龟四要素：唐奇安通道突破入场 + ATR(N)波动率定仓位 + 金字塔加仓(最多4单元) + 2N硬止损 / 10日通道离场。与简化版『唐奇安通道突破(海龟)』的区别：不一次性满仓，而是按波动率做风险等权——N 大(波动高)则仓位小、N 小则仓位大，并在趋势延续时每 0.5N 加一个单元。震荡市靠 2N 止损控制回撤，趋势市逐步加满。",
        "cls": TurtleClassicStrategy,
        "default_params": {"entry": 20, "exit": 10, "atr_period": 20, "risk_pct": 0.01,
                           "max_units": 4, "add_step": 0.5, "stop_n": 2.0, "use_exit_channel": 1},
        "param_schema": [
            _int("entry", "入场通道(日)", 20, 5, 120, 1),
            _int("exit", "离场通道(日)", 10, 3, 60, 1),
            _int("atr_period", "N(ATR周期,日)", 20, 5, 60, 1),
            _float("risk_pct", "单单元风险占比", 0.01, 0.002, 0.05, 0.001),
            _int("max_units", "最大单元数", 4, 1, 6),
            _float("add_step", "加仓间隔(N倍)", 0.5, 0.25, 2.0, 0.25),
            _float("stop_n", "止损幅度(N倍)", 2.0, 1.0, 4.0, 0.5),
            _int("use_exit_channel", "启用通道离场(1/0)", 1, 0, 1),
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
    "macd_boll": {
        "key": "macd_boll",
        "name": "MACD+布林+风控",
        "description": "MACD金叉且价在布林中轨上方做多，死叉/跌破下轨离场；内置回撤/单日亏损/仓位软风控。完整版 SDK 示例（新指标库 + ctx.risk）。",
        "cls": MacdBollStrategy,
        "default_params": {"fast": 12, "slow": 26, "signal": 9, "boll_period": 20, "boll_k": 2.0,
                           "max_drawdown_limit": 0.15, "daily_loss_limit": 0.0, "position_limit": 0.0},
        "param_schema": [
            _int("fast", "MACD快线(天)", 12, 3, 60),
            _int("slow", "MACD慢线(天)", 26, 10, 200),
            _int("signal", "MACD信号(天)", 9, 2, 60),
            _int("boll_period", "布林周期(天)", 20, 5, 120),
            _float("boll_k", "布林倍数", 2.0, 0.5, 4.0, 0.1),
            _float("max_drawdown_limit", "最大回撤止损(0=关)", 0.15, 0.0, 0.6, 0.01),
            _float("daily_loss_limit", "单日亏损上限(0=关)", 0.0, 0.0, 0.15, 0.005),
            _float("position_limit", "仓位上限(0=关)", 0.0, 0.0, 1.0, 0.05),
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
    "jk001": {
        "key": "jk001",
        "name": "jk001 · 自由现金流+低波动(沪深300)",
        "description": "移植自聚宽 jk001_live_v1：沪深300 内剔除科创/金融/次新，FCF/OE=(经营现金流−|资本开支|)/总资产 与 20日低波动 各半加权打分取前30等权（单只≤10%），准入 ROE>0 且 FCF/OE>−0.1、动量(12-1与6-1各半)>0、站上MA20、当日涨幅<5%；月频调仓；固定止损15% + 阶梯移动止损(≤10日10%，之后15%，峰值取建仓以来最高价)；按指数周线趋势给 100%/90%/0% 三档仓位。",
        "cls": JKFactorStrategy,
        "multi_asset": True,
        "index_code": "000300",
        "index_symbol": "sh000300",
        "index_name": "沪深300",
        "default_params": {
            "rebalance_period": 21, "stock_num": 30, "max_stock_weight": 0.10,
            "max_industry_num": 10, "w_fundamental": 0.5, "w_lowvol": 0.5,
            "w_price_volume": 0.0, "w_momentum": 0.0,
            "enable_trend_filter": 1, "max_intraday_chg": 0.05,
            "stop_loss_pct": 0.15, "enable_ladder_stop": 1, "ladder_hold_days": 10,
            "ladder_trailing_pct": 0.15, "trailing_stop_pct": 0.08,
            "bullish_position": 1.0, "neutral_position": 0.9, "bearish_position": 0.0,
            # 空头判据：'ma'=补 MA 空头排列（修原版 bearish 永不触发的缺陷）
            # 'origin'=原版 score<=-2（实测 1449 日只触发 5 天）
            "trend_bear_mode": "ma", "bear_immediate": 1,
            # 波动率目标约束（原版未实现的文件头 R3 建议）：0=关闭（等价原版）
            "vol_target": 0.0, "vol_lookback": 60, "vol_min_scale": 0.3,
            "vol_max_scale": 1.0, "vol_react_thresh": 0.05,
            "reb_thresh": 0.02, "allow_star_market": 0,
            # ⚠️ exclude_st 故意「不进 param_schema」、也就不会出现在 UI 上：
            #    平台没有历史 ST 标记，开启后只能用「当前名称」匹配 → 拿今天的 ST 名单
            #    去剔除历史股票 = 前视偏差。实测开启后 jk002 样本外从 +7.97% "改善" 到
            #    +9.43%，但这 1.5pp 是凭空变出来的。要真正启用，先补 st_history 表
            #    （按公告日记录 ST/*ST 进出），再按 PIT 口径引用。
            "exclude_financial": 1, "exclude_st": 0, "min_list_days": 250,
            "industry_level": 1,
            "max_position_pct": 0.0, "max_gross_exposure": 0.0,
            # 内部参数：动量/波动需要 260 日回看，引擎预热天数要够
            "warmup_days": 270,
        },
        "param_schema": [
            _int("rebalance_period", "调仓周期(交易日)", 21, 5, 60, 1),
            _int("stock_num", "持股数量", 30, 5, 100, 1),
            _float("max_stock_weight", "单只最大权重", 0.10, 0.02, 0.5, 0.01),
            _int("max_industry_num", "单一行业最多只数", 10, 1, 50, 1),
            _float("w_fundamental", "FCF/OE 因子权重", 0.5, 0.0, 1.0, 0.05),
            _float("w_lowvol", "低波动因子权重", 0.5, 0.0, 1.0, 0.05),
            _int("enable_trend_filter", "站上MA20准入(1开/0关)", 1, 0, 1),
            _float("max_intraday_chg", "当日涨幅上限(不追高)", 0.05, 0.01, 0.2, 0.01),
            _float("stop_loss_pct", "固定止损(%)", 0.15, 0.02, 0.5, 0.01),
            _int("enable_ladder_stop", "阶梯移动止损(1开/0关)", 1, 0, 1),
            _int("ladder_hold_days", "阶梯切换(持有交易日)", 10, 1, 60, 1),
            _float("ladder_trailing_pct", "阶梯后段回撤阈值", 0.15, 0.02, 0.5, 0.01),
            _float("trailing_stop_pct", "阶梯前段回撤阈值", 0.08, 0.02, 0.5, 0.01),
            _float("bullish_position", "看多仓位", 1.0, 0.0, 1.0, 0.05),
            _float("neutral_position", "中性仓位", 0.9, 0.0, 1.0, 0.05),
            _float("bearish_position", "看空仓位", 0.0, 0.0, 1.0, 0.05),
            _float("reb_thresh", "调仓偏离阈值", 0.02, 0.0, 0.2, 0.005),
            _opt("trend_bear_mode", "空头判据", "ma", ["ma", "origin"],
                 "ma=MA空头排列(last<MA20<MA60<MA120)额外-2分（修原版bearish几乎不触发的缺陷）；"
                 "origin=原版score<=-2（2019-2024全程1449日仅触发5天，风控形同虚设）"),
            _int("bear_immediate", "看空即时清仓(1开/0关)", 1, 0, 1),
            _float("vol_target", "波动率目标(年化,0=关闭)", 0.0, 0.0, 0.4, 0.01),
            _int("vol_lookback", "波动率回看(交易日)", 60, 20, 120, 5),
            _float("vol_min_scale", "波动率缩放下限", 0.30, 0.0, 1.0, 0.05),
            _float("vol_max_scale", "波动率缩放上限(≤1不加杠杆)", 1.00, 0.0, 1.0, 0.05),
            _float("vol_react_thresh", "波动率减仓触发阈值", 0.05, 0.01, 0.3, 0.01),
            _int("allow_star_market", "允许科创板(1开/0关)", 0, 0, 1),
            _int("industry_level", "行业口径(1申万一级/0东财细分)", 1, 0, 1),
            _opt("momentum_filter_mode", "动量准入口径", "m121", ["m121", "avg"],
                 "m121=只用12-1动量（与聚宽原版代码一致）；avg=12-1与6-1各半（按原版docstring）"),
            _int("financial_pit", "财务PIT(1公告日/0报告期)", 1, 0, 1),
            _int("exclude_financial", "剔除金融股(1开/0关)", 1, 0, 1),
            _int("min_list_days", "最少上市天数", 250, 0, 1000, 10),
        ],
    },
    "jk002": {
        "key": "jk002",
        "name": "jk002 · 自由现金流+低波动(中证全指)",
        "description": "jk001 的扩池版本：股票池扩到「平台全A」（pool_mode=all，含科创板 allow_star_market=1），基准用国证A指 sz399317（近似全市场），其余选股/止损/仓位逻辑与 jk001 完全一致，用于检验 alpha 在大盘股池之外是否仍然存在。注意：中证全指 000985 的历史 PIT 成分与行情都取不到（tushare index_weight 受权限限制、新浪指数源 000985 停在 2016 年），故池子用平台全A近似、基准换国证A指。",
        "cls": JKFactorStrategy,
        "multi_asset": True,
        "index_code": "000985",
        "index_symbol": "sz399317",
        "index_name": "国证A指",
        "default_params": {
            "rebalance_period": 21, "stock_num": 30, "max_stock_weight": 0.10,
            "max_industry_num": 10, "w_fundamental": 0.5, "w_lowvol": 0.5,
            "w_price_volume": 0.0, "w_momentum": 0.0,
            "enable_trend_filter": 1, "max_intraday_chg": 0.05,
            "stop_loss_pct": 0.15, "enable_ladder_stop": 1, "ladder_hold_days": 10,
            "ladder_trailing_pct": 0.15, "trailing_stop_pct": 0.08,
            "bullish_position": 1.0, "neutral_position": 0.9, "bearish_position": 0.0,
            # 空头判据：'ma'=补 MA 空头排列（修原版 bearish 永不触发的缺陷）
            # 'origin'=原版 score<=-2（实测 1449 日只触发 5 天）
            "trend_bear_mode": "ma", "bear_immediate": 1,
            # 波动率目标约束（原版未实现的文件头 R3 建议）：0=关闭（等价原版）
            "vol_target": 0.0, "vol_lookback": 60, "vol_min_scale": 0.3,
            "vol_max_scale": 1.0, "vol_react_thresh": 0.05,
            "reb_thresh": 0.02, "allow_star_market": 1,
            # 同 jk001：exclude_st 故意不暴露到 UI（无历史 ST 标记，开启即前视偏差）
            "exclude_financial": 1, "exclude_st": 0, "min_list_days": 250,
            "industry_level": 1, "pool_mode": "all",
            "max_position_pct": 0.0, "max_gross_exposure": 0.0,
            # 内部参数：动量/波动需要 260 日回看，引擎预热天数要够
            "warmup_days": 270,
        },
        "param_schema": [
            _int("rebalance_period", "调仓周期(交易日)", 21, 5, 60, 1),
            _int("stock_num", "持股数量", 30, 5, 100, 1),
            _float("max_stock_weight", "单只最大权重", 0.10, 0.02, 0.5, 0.01),
            _int("max_industry_num", "单一行业最多只数", 10, 1, 50, 1),
            _float("w_fundamental", "FCF/OE 因子权重", 0.5, 0.0, 1.0, 0.05),
            _float("w_lowvol", "低波动因子权重", 0.5, 0.0, 1.0, 0.05),
            _int("enable_trend_filter", "站上MA20准入(1开/0关)", 1, 0, 1),
            _float("max_intraday_chg", "当日涨幅上限(不追高)", 0.05, 0.01, 0.2, 0.01),
            _float("stop_loss_pct", "固定止损(%)", 0.15, 0.02, 0.5, 0.01),
            _int("enable_ladder_stop", "阶梯移动止损(1开/0关)", 1, 0, 1),
            _int("ladder_hold_days", "阶梯切换(持有交易日)", 10, 1, 60, 1),
            _float("ladder_trailing_pct", "阶梯后段回撤阈值", 0.15, 0.02, 0.5, 0.01),
            _float("trailing_stop_pct", "阶梯前段回撤阈值", 0.08, 0.02, 0.5, 0.01),
            _float("bullish_position", "看多仓位", 1.0, 0.0, 1.0, 0.05),
            _float("neutral_position", "中性仓位", 0.9, 0.0, 1.0, 0.05),
            _float("bearish_position", "看空仓位", 0.0, 0.0, 1.0, 0.05),
            _float("reb_thresh", "调仓偏离阈值", 0.02, 0.0, 0.2, 0.005),
            _opt("trend_bear_mode", "空头判据", "ma", ["ma", "origin"],
                 "ma=MA空头排列(last<MA20<MA60<MA120)额外-2分（修原版bearish几乎不触发的缺陷）；"
                 "origin=原版score<=-2（2019-2024全程1449日仅触发5天，风控形同虚设）"),
            _int("bear_immediate", "看空即时清仓(1开/0关)", 1, 0, 1),
            _float("vol_target", "波动率目标(年化,0=关闭)", 0.0, 0.0, 0.4, 0.01),
            _int("vol_lookback", "波动率回看(交易日)", 60, 20, 120, 5),
            _float("vol_min_scale", "波动率缩放下限", 0.30, 0.0, 1.0, 0.05),
            _float("vol_max_scale", "波动率缩放上限(≤1不加杠杆)", 1.00, 0.0, 1.0, 0.05),
            _float("vol_react_thresh", "波动率减仓触发阈值", 0.05, 0.01, 0.3, 0.01),
            _int("allow_star_market", "允许科创板(1开/0关)", 1, 0, 1),
            _int("industry_level", "行业口径(1申万一级/0东财细分)", 1, 0, 1),
            _opt("momentum_filter_mode", "动量准入口径", "m121", ["m121", "avg"],
                 "m121=只用12-1动量（与聚宽原版代码一致）；avg=12-1与6-1各半（按原版docstring）"),
            _int("financial_pit", "财务PIT(1公告日/0报告期)", 1, 0, 1),
            _int("exclude_financial", "剔除金融股(1开/0关)", 1, 0, 1),
            _int("min_list_days", "最少上市天数", 250, 0, 1000, 10),
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
            "max_position_pct": 0.0, "max_gross_exposure": 0.0,
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
            _float("max_position_pct", "单只最大权重上限(0=关闭)", 0.0, 0.0, 1.0, 0.01),
            _float("max_gross_exposure", "总敞口上限(0=关闭)", 0.0, 0.0, 1.0, 0.05),
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
            "max_position_pct": 0.0, "max_gross_exposure": 0.0,
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
            _float("max_position_pct", "单只最大权重上限(0=关闭)", 0.0, 0.0, 1.0, 0.01),
            _float("max_gross_exposure", "总敞口上限(0=关闭)", 0.0, 0.0, 1.0, 0.05),
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
