"""因子库：以『表达式』声明所有横截面因子。

每个因子一条 FactorDef，包含：
- name / cn          ：英文键与中文名（中文名用于前端展示）
- category           ：分类（价值 / 动量反转 / 风险 / 规模 / 质量 / 成长）
- expr              ：factor_expr 引擎可求值的表达式
- default_dir       ：长期共识默认方向（+1=越大越优 / -1=越小越优），仅滚动 IC 样本不足时兜底
- default_weight    ：用户偏好先验权重（实际合成再乘 |滚动IC| 归一化）
- desc              ：说明
- neutralize_exempt ：是否跳过市值中性化（如 size 本身即规模因子）

调用方（多因子策略）按策略参数切片好各窗口收盘价序列，并注入截面属性，构建命名空间：
    c_m / c_r / c_v / c_b / c_t  : 各窗口收盘价序列（momentum/reversal/vol/beta/tail）
    mkt_b                        : 与 c_b 对齐的基准收盘价序列（用于 BETA/特异度）
    pe_ttm / pb / market_cap     : 估值与市值截面属性
    roe / revenue_yoy / profit_yoy : 质量/成长基本面（由 seed_fundamentals 入库）
    industry                     : 行业（用于行业中性）

新增因子 = 在此登记一条 + 保证 expr 引用的属性已就绪，策略选股逻辑与前端均无需改动。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class FactorDef:
    name: str
    cn: str
    category: str
    expr: str
    default_dir: int            # +1 越大越优, -1 越小越优
    default_weight: float
    desc: str = ""
    neutralize_exempt: bool = False


# ============================ 因子定义 ============================
FACTORS: List[FactorDef] = [
    # —— 价值（估值，越大越优）——
    FactorDef("ep", "EP", "价值",
              "safe_inv(pe_ttm, 0, 1000)", 1, 0.15,
              "盈利收益率 = 1 / PE_TTM（剔除无效估值）"),
    FactorDef("bp", "BP", "价值",
              "safe_inv(pb, 0, 1000)", 1, 0.10,
              "账面价值比 = 1 / PB（剔除无效估值）"),

    # —— 动量 / 反转（方向由滚动 IC 自适应，此处取中性原始值）——
    FactorDef("momentum", "动量", "动量反转",
              "c_m[-1] / c_m[0] - 1", -1, 0.10,
              "中期动量 = 窗口内收益率(ROC)；A股中期动量弱、反转强，默认取反转取向"),
    FactorDef("reversal", "反转", "动量反转",
              "c_r[-1] / c_r[0] - 1", -1, 0.12,
              "短期反转 = 短期收益率；默认取反转取向"),

    # —— 风险 ——
    FactorDef("low_vol", "低波", "风险",
              "std(returns(c_v))", -1, 0.20,
              "收益率波动率（越低越优）；低波动溢价，方向由IC自适应"),
    FactorDef("size", "规模", "规模",
              "log(market_cap)", -1, 0.10,
              "ln 总市值（小市值溢价）；方向由IC自适应", neutralize_exempt=True),
    FactorDef("beta", "Beta", "风险",
              "beta(c_b, mkt_b)", 1, 0.05,
              "CAPM 系统性风险（个股对基准斜率）"),
    FactorDef("idio_vol", "特异度", "风险",
              "idio_vol(c_b, mkt_b)", -1, 0.15,
              "CAPM 残差波动率（越低越优）"),
    FactorDef("skewness", "偏度", "风险",
              "skew(returns(c_v))", -1, 0.05,
              "收益率偏度（反彩票、低正偏更优）"),
    FactorDef("tail_risk", "尾部", "风险",
              "maxdd(c_t)", 1, 0.05,
              "区间最大回撤（负值，越接近0越优 → 越大越优）"),

    # —— 质量（需 roe 字段，seed_fundamentals 入库）——
    FactorDef("roe", "ROE", "质量",
              "roe", 1, 0.10,
              "净资产收益率(%)：盈利能力，越大越优"),
    # —— 成长（需 revenue_yoy / profit_yoy 字段）——
    FactorDef("revenue_yoy", "营收增速", "成长",
              "revenue_yoy", 1, 0.08,
              "营业总收入同比增长(%)：成长动能，越大越优"),
    FactorDef("profit_yoy", "利润增速", "成长",
              "profit_yoy", 1, 0.08,
              "净利润同比增长(%)：成长动能，越大越优"),
]

# 派生索引，供策略与前端快速访问
FACTOR_NAMES: List[str] = [f.name for f in FACTORS]
FACTOR_MAP: dict[str, FactorDef] = {f.name: f for f in FACTORS}
CN_MAP: dict[str, str] = {f.name: f.cn for f in FACTORS}
CATEGORY_MAP: dict[str, str] = {f.name: f.category for f in FACTORS}
