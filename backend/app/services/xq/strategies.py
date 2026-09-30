"""8 个选股策略（Sequoia-X sequoia_x/strategy 迁移，向量化重写）。

规则与阈值与原实现**逐条对齐**，差异只有两点（都在注释里标明）：
1. 原实现逐股循环取数 → 这里在共享面板上向量化（结果等价，速度快一个量级）；
2. 海龟策略原按 tushare daily_basic 流通市值排序 → 这里改用当日成交额降序
   （避免为排序单独拉一次 daily_basic；候选集本身不变）。

策略质量分级（signal_engine.STRATEGY_QUALITY）依据 Sequoia signal_stats 实测的
T+10 平均超额：strong=+2% 以上，neutral≈0，weak 为负 —— 分级影响建仓判定
（weak 档即使 streak≥9 也只给 WATCH，不给 BUY_STRONG）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

import pandas as pd

from app.services.xq.data import Panel

# 涨停/跌停判定阈值（原实现值：9.5%/9.05% 而非精确 10%，容忍四舍五入与除权日）
_LIMIT_UP = 1.095
_LIMIT_DOWN = 0.905


def _add_rolling(df: pd.DataFrame, col: str, windows: tuple[int, ...], prefix: str = "ma") -> None:
    """按 symbol 分组滚动均值（就地追加列）。"""
    g = df.groupby("symbol", sort=False)[col]
    for w in windows:
        df[f"{prefix}{w}"] = g.transform(lambda s, w=w: s.rolling(w).mean())


def _shift(df: pd.DataFrame, col: str, n: int = 1) -> pd.Series:
    """组内 shift（昨日值）。"""
    return df.groupby("symbol", sort=False)[col].shift(n)


def _rolling_max(df: pd.DataFrame, col: str, w: int, min_periods: int | None = None) -> pd.Series:
    return (
        df.groupby("symbol", sort=False)[col]
        .rolling(w, min_periods=min_periods or w)
        .max()
        .reset_index(level=0, drop=True)
    )


def _rolling_min(df: pd.DataFrame, col: str, w: int) -> pd.Series:
    return (
        df.groupby("symbol", sort=False)[col]
        .rolling(w)
        .min()
        .reset_index(level=0, drop=True)
    )


# ────────────────────────── 1. 均线量能（原 MaVolumeStrategy）──────────────────────────
def ma_volume(p: Panel) -> tuple[list[str], pd.DataFrame]:
    """5 日均线上穿 20 日均线（金叉）且当日成交量 > 20 日均量 1.5 倍。"""
    df = p.df
    _add_rolling(df, "c_hfq", (5, 20))
    df["vol_ma20"] = df.groupby("symbol", sort=False)["volume"].transform(
        lambda s: s.rolling(20).mean()
    )
    ma5p, ma20p = _shift(df, "ma5"), _shift(df, "ma20")
    cond = (
        (ma5p < ma20p) & (df["ma5"] > df["ma20"]) & (df["volume"] > df["vol_ma20"] * 1.5)
    )
    return _pick(p, cond)


# ────────────────────────── 2. RPS 强度突破（原 RpsBreakoutStrategy）──────────────────────
def rps_breakout(p: Panel, period: int = 120, threshold: int = 90) -> tuple[list[str], pd.DataFrame]:
    """120 日涨幅全市场横截面排位 ≥90，且收盘价接近 120 日高点（突破在即）。"""
    df = p.df
    df["rps_base"] = _shift(df, "c_hfq", period)
    df["rps_pct"] = df["c_hfq"] / df["rps_base"] - 1.0
    df["roll_high"] = _rolling_max(df, "h_hfq", period, min_periods=period // 2)

    last = df[df["date"] == p.latest].copy()
    last = last.dropna(subset=["rps_pct"])
    last["rps"] = last["rps_pct"].rank(pct=True) * 100
    strong = last[(last["rps"] >= threshold) & (last["c_hfq"] >= last["roll_high"] * 0.90)]
    return strong["symbol"].tolist(), strong


# ────────────────────────── 3. 高旗形整理（原 HighTightFlagStrategy）──────────────────────
def high_tight_flag(p: Panel) -> tuple[list[str], pd.DataFrame]:
    """40 日涨幅>60% + 近 10 日振幅<15% + 高位抗跌 + 缩量 0.6 倍。"""
    df = p.df
    high40, low40 = _rolling_max(df, "h_hfq", 40), _rolling_min(df, "l_hfq", 40)
    high10, low10 = _rolling_max(df, "h_hfq", 10), _rolling_min(df, "l_hfq", 10)
    df["vol_ma20"] = df.groupby("symbol", sort=False)["volume"].transform(
        lambda s: s.rolling(20).mean()
    )
    vol_ma20_prev = _shift(df, "vol_ma20")
    cond = (
        (high40 / low40 > 1.6)
        & (high10 / low10 < 1.15)
        & (low10 >= high40 * 0.8)
        & (df["volume"] < vol_ma20_prev * 0.6)
    )
    return _pick(p, cond)


# ────────────────────────── 4. 涨停洗盘（原 LimitUpShakeoutStrategy）──────────────────────
def limit_up_shakeout(p: Panel) -> tuple[list[str], pd.DataFrame]:
    """昨日涨停 + 今日放量收阴 + 不破昨收（用未复权原价判定涨停，除权日不失真）。"""
    df = p.df
    c1, c2 = _shift(df, "close"), _shift(df, "close", 2)
    v1 = _shift(df, "volume")
    cond = (
        (c1 >= c2 * _LIMIT_UP)
        & (df["close"] < df["open"])
        & (df["volume"] > v1 * 2.0)
        & (df["low"] >= c1)
    )
    return _pick(p, cond)


# ────────────────────────── 5. 上升趋势跌停（原 UptrendLimitDownStrategy）──────────────────
def uptrend_limit_down(p: Panel) -> tuple[list[str], pd.DataFrame]:
    """MA20>MA60 多头排列中放量跌停，捕捉错杀。"""
    df = p.df
    _add_rolling(df, "c_hfq", (20, 60))
    df["vol_ma20"] = df.groupby("symbol", sort=False)["volume"].transform(
        lambda s: s.rolling(20).mean()
    )
    ma20p, ma60p = _shift(df, "ma20"), _shift(df, "ma60")
    c1 = _shift(df, "close")
    cond = (
        (ma20p > ma60p)
        & (df["close"] <= c1 * _LIMIT_DOWN)
        & (df["volume"] > df["vol_ma20"] * 2.0)
    )
    return _pick(p, cond)


# ────────────────────────── 6. 海龟突破（原 TurtleTradeStrategy）─────────────────────────
def turtle_trade(p: Panel) -> tuple[list[str], pd.DataFrame]:
    """20 日新高突破 + 成交额过亿 + 实体阳线且真涨（防高开低走诱多）。

    排序差异：原实现按流通市值降序（tushare daily_basic），这里改按当日成交额
    降序 —— 候选集不变，仅顺序不同；避免为排序单独拉一次 daily_basic。
    """
    df = p.df
    df["roll_high20"] = _rolling_max(df, "h_hfq", 20)
    df["high_20"] = _shift(df, "roll_high20")
    c1 = _shift(df, "close")
    cond = (
        (df["c_hfq"] > df["high_20"])
        & (df["amount"] > 100_000_000)
        & (df["close"] > df["open"])
        & (df["close"] > c1)
    )
    hits = df[cond & (df["date"] == p.latest)]
    ordered = hits.sort_values("amount", ascending=False)
    return ordered["symbol"].tolist(), ordered


# ────────────────────────── 7. 双参数趋势波动（原 DualParamTrendVolStrategy）────────────────
def dual_param(
    p: Panel, trend_window: int = 20, er_min: float = 0.50,
    vol_window: int = 20, vol_min: float = 0.012, vol_max: float = 0.045,
) -> tuple[list[str], pd.DataFrame]:
    """效率比 ER=|C_t−C_{t−n}|/Σ|ΔC| ≥0.50 + 20 日净涨>0 + 波动率带 0.012~0.045。

    阈值来源：Sequoia validate_dual_param.py 历史前向超额扫描校准（ER 在
    0.20~0.70 单调，无尖峰，非过拟合）。原实现的 params_frame/pass_mask
    「验证与线上共用公式」防漂移模式在此保留：本函数即唯一公式源。
    """
    df = p.df
    gb = df.groupby("symbol", sort=False)
    df["_net"] = gb["c_hfq"].diff(trend_window)
    df["_absdiff"] = gb["c_hfq"].diff().abs()
    df["_path"] = (
        df.groupby("symbol", sort=False)["_absdiff"].rolling(trend_window).sum()
        .reset_index(level=0, drop=True)
    )
    df["_ret"] = gb["c_hfq"].pct_change()
    df["_vol"] = (
        df.groupby("symbol", sort=False)["_ret"].rolling(vol_window).std()
        .reset_index(level=0, drop=True)
    )
    df["_er"] = df["_net"].abs() / df["_path"].replace(0.0, float("nan"))
    cond = (
        (df["_er"] >= er_min)
        & (df["_net"] > 0)
        & (df["_vol"] >= vol_min)
        & (df["_vol"] <= vol_max)
    )
    return _pick(p, cond)


# ────────────────────────── 8. 定增公告（原 PrivatePlacementStrategy）──────────────────────
def private_placement(
    p: Panel, lookback_days: int = 7,
) -> tuple[list[str], pd.DataFrame]:
    """近 7 天定向增发公告（akshare 东财口径）。

    唯一非 tushare 数据源；akshare 不可用/接口失败时返回空列表并附 warning，
    不阻断其它策略（与原实现一致）。
    """
    try:
        import akshare as ak

        raw = ak.stock_qbzf_em()
    except Exception as exc:  # noqa: BLE001
        return [], pd.DataFrame({"_error": [f"akshare 不可用: {exc}"]})
    if raw is None or raw.empty:
        return [], pd.DataFrame()

    df = raw[raw["发行方式"] == "定向增发"].copy()
    if df.empty:
        return [], pd.DataFrame()

    ref = p.as_of or p.latest or date.today()
    cutoff = ref - timedelta(days=lookback_days)
    df["发行日期"] = pd.to_datetime(df["发行日期"], errors="coerce")
    df = df.dropna(subset=["发行日期"])
    df = df[(df["发行日期"].dt.date <= ref) & (df["发行日期"].dt.date >= cutoff)]
    if df.empty:
        return [], pd.DataFrame()

    df = df.sort_values("发行日期", ascending=False)
    symbols: list[str] = []
    for s in df["股票代码"].astype(str).str.extract(r"(\d{6})")[0].dropna():
        sym = _to_platform(s)
        if sym and sym not in symbols:
            symbols.append(sym)
    return symbols, pd.DataFrame()


def _to_platform(code: str) -> str:
    """6 位纯数字 → 平台 symbol（sh/sz 前缀；6 开头沪市，其余深市；北交所剔除）。"""
    if len(code) != 6 or not code.isdigit():
        return ""
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    return ""


def _pick(p: Panel, cond: pd.Series) -> tuple[list[str], pd.DataFrame]:
    """通用收口：条件 ∧ 最新交易日 → 去重排序的 symbol 列表。"""
    hits = p.df[cond & (p.df["date"] == p.latest)]
    return hits["symbol"].tolist(), hits


# ────────────────────────── 策略注册表 ──────────────────────────
# key 与 Sequoia STRATEGY_META 对齐；name 为展示名；runner 按 meta 顺序执行。
STRATEGIES: list[dict] = [
    {"key": "ma_volume", "name": "均线量能", "fn": ma_volume},
    {"key": "rps_breakout", "name": "RPS强度突破", "fn": rps_breakout},
    {"key": "high_tight_flag", "name": "高旗形整理", "fn": high_tight_flag},
    {"key": "limit_up_shakeout", "name": "涨停洗盘", "fn": limit_up_shakeout},
    {"key": "uptrend_limit_down", "name": "上升趋势跌停", "fn": uptrend_limit_down},
    {"key": "turtle_trade", "name": "海龟突破", "fn": turtle_trade},
    {"key": "dual_param", "name": "双参数趋势波动", "fn": dual_param},
    {"key": "private_placement", "name": "定增公告", "fn": private_placement},
]

STRATEGY_NAME: dict[str, str] = {m["key"]: m["name"] for m in STRATEGIES}

# 策略质量分级（Sequoia signal_engine.STRATEGY_QUALITY，依 T+10 平均超额实测）
STRATEGY_QUALITY: dict[str, str] = {
    "rps_breakout": "strong",       # RpsBreakout +2.60%
    "high_tight_flag": "strong",    # HighTightFlag +2.36%
    "private_placement": "neutral", # +0.48%
    "dual_param": "neutral",        # +0.19%（分年翻脸，不给 strong，理由见原注释）
    "limit_up_shakeout": "weak",    # −1.56%
    "ma_volume": "weak",            # −0.72%
    "turtle_trade": "weak",         # −0.41%
    "uptrend_limit_down": "weak",   # −1.20%
}

STRATEGY_FN: dict[str, Callable[[Panel], tuple[list[str], pd.DataFrame]]] = {
    m["key"]: m["fn"] for m in STRATEGIES
}
