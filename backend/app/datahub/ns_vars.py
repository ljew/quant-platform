"""统一变量命名空间构造（全平台单一口径）。

窗口定义（与 multi_factor 语义注释对齐）：
  c_m = 126 收盘（动量）      c_r = 26  收盘（反转）
  c_v = 61  收盘（波动/偏度）  c_b = 126 收盘（Beta/特异度）
  c_t = 121 收盘（尾部最大回撤）
  mkt_b = 与 c_b 等长的基准序列

量能序列（与 c_* 同窗口同长度，可直接与 returns(c_v) 逐点对齐）：
  vol_m / vol_r / vol_v / vol_b / vol_t = 成交量
  amt_m / amt_r / amt_v / amt_b / amt_t = 成交额

财务（PIT：按 ann_date ≤ 截面日取最新一期，由 fin_asof 选出后传入）：
  ocf             经营活动现金流净额（元）
  capex           资本开支（元，正数表示流出）
  total_assets    总资产（元）
  fcf_yield       自由现金流收益率 = (ocf − |capex|) ÷ 总市值
  ocf_to_assets   经营现金流 ÷ 总资产（盈利质量）
  capex_intensity 资本开支强度 = |capex| ÷ 总资产

⚠️ 单位口径：stocks.market_cap 存的是「亿元」，financials_raw 的
   ocf/capex/total_assets 存的是「元」，两者相差 1e8。凡是 market_cap 与
   财务量相除，都必须先换算，否则结果差 8 个数量级（fcf_yield 内部已处理）。

⚠️ 数据缺失一律给 None 而不是空列表：空列表会让 std() 返回 1e9、mean() 返回 0，
   产生「看着有值实则无意义」的假因子；None 会让表达式求值失败并返回 None，
   由上层按缺失处理。宁可缺，不可假。

GP 探索 / 单因子检验 / ETL 生产 三端共用本函数，
保证"检验时的表达式含义 == 注册后每日计算的含义"。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Callable, Iterable

# stocks.market_cap 单位为亿元，财务量为元 → 换算系数
MCAP_UNIT = 1e8


def make_ns(seg: list[float], mkt_all: list[float], attrs: dict,
            news: float | None = None, esv=None,
            vols: list[float] | None = None,
            amts: list[float] | None = None,
            fin: dict | None = None) -> dict:
    """构建因子表达式命名空间。

    vols / amts 与 seg 逐点对齐（同一日期轴、同一长度）；
    fin 为已按 PIT 选定的财务字典（见 fin_asof）。
    新增参数全部可选：未传入时对应变量为 None，老表达式行为完全不变。
    """
    seg = list(seg)
    if not seg:
        return {}

    vols = list(vols) if vols else None
    amts = list(amts) if amts else None

    def w(n: int, src: list[float]) -> list[float]:
        return src[-n:] if len(src) >= n else list(src)

    def wv(n: int, src: list[float] | None) -> list[float] | None:
        return w(n, src) if src else None

    cm = w(126, seg)
    cb = w(126, seg)
    mb = tail_of(mkt_all, len(cb)) if mkt_all else []
    ns = {
        "c_m": cm,
        "c_r": w(26, seg),
        "c_v": w(61, seg),
        "c_b": cb,
        "c_t": w(121, seg),
        "mkt_b": mb,
        "vol_m": wv(126, vols),
        "vol_r": wv(26, vols),
        "vol_v": wv(61, vols),
        "vol_b": wv(126, vols),
        "vol_t": wv(121, vols),
        "amt_m": wv(126, amts),
        "amt_r": wv(26, amts),
        "amt_v": wv(61, amts),
        "amt_b": wv(126, amts),
        "amt_t": wv(121, amts),
        "pe_ttm": attrs.get("pe_ttm"),
        "pb": attrs.get("pb"),
        "market_cap": attrs.get("market_cap"),
        "roe": attrs.get("roe"),
        "revenue_yoy": attrs.get("revenue_yoy"),
        "profit_yoy": attrs.get("profit_yoy"),
        "earnings_surprise": esv if esv is not None else attrs.get("_es"),
        "news_senti": news,
        "industry": attrs.get("industry"),
    }
    ns.update(fin_vars(fin, attrs.get("market_cap")))
    return ns


def fin_vars(fin: dict | None, market_cap) -> dict:
    """财务变量派生：原始量直传，比值量做单位换算与分母保护。"""
    out = {"ocf": None, "capex": None, "total_assets": None,
           "fcf_yield": None, "ocf_to_assets": None, "capex_intensity": None}
    if not fin:
        return out
    ocf, capex, ta = fin.get("ocf"), fin.get("capex"), fin.get("total_assets")
    out["ocf"], out["capex"], out["total_assets"] = ocf, capex, ta
    if ta and ta > 0:
        if ocf is not None:
            out["ocf_to_assets"] = ocf / ta
        if capex is not None:
            out["capex_intensity"] = abs(capex) / ta
    if ocf is not None and capex is not None and market_cap and market_cap > 0:
        out["fcf_yield"] = (ocf - abs(capex)) / (market_cap * MCAP_UNIT)
    return out


_VAR_CACHE: frozenset[str] | None = None


def var_names() -> frozenset[str]:
    """全部可用变量名 —— 从 make_ns 的输出自动派生，新增变量无需另行登记。

    校验层（validate_expr 的 AST 白名单）直接引用本函数，避免「ns_vars 加了
    变量、白名单忘了加」导致新因子被误判为非法表达式。
    """
    global _VAR_CACHE
    if _VAR_CACHE is None:
        probe = make_ns([1.0], [1.0], {}, news=0.0, esv=0.0,
                        vols=[1.0], amts=[1.0],
                        fin={"ocf":1.0, "capex":1.0, "total_assets":1.0})
        _VAR_CACHE = frozenset(probe)
    return _VAR_CACHE


# 变量中文说明：变量清单本身由 var_names() 自动派生，此处只补人类可读描述。
# 新增变量时登记一条说明；未登记者仍会进入白名单与前端展示，只是描述留空。
VAR_DOC: dict[str, str] = {
    "c_m": "收盘序列(动量窗126)", "c_r": "收盘序列(反转窗26)",
    "c_v": "收盘序列(波动窗61)", "c_b": "收盘序列(回归窗126)",
    "c_t": "收盘序列(尾部窗121)",
    "vol_m": "成交量序列(126)", "vol_r": "成交量序列(26)", "vol_v": "成交量序列(61)",
    "vol_b": "成交量序列(126)", "vol_t": "成交量序列(121)",
    "amt_m": "成交额序列(126)", "amt_r": "成交额序列(26)", "amt_v": "成交额序列(61)",
    "amt_b": "成交额序列(126)", "amt_t": "成交额序列(121)",
    "mkt_b": "基准(中证800)对齐序列",
    "pe_ttm": "市盈率", "pb": "市净率", "market_cap": "总市值(亿元)",
    "roe": "净资产收益率(%)", "revenue_yoy": "营收增速(%)", "profit_yoy": "利润增速(%)",
    "earnings_surprise": "盈余惊喜(PEAD)", "news_senti": "个股新闻情绪(-1~1)",
    "industry": "行业",
    "ocf": "经营现金流净额(元)", "capex": "资本开支(元)", "total_assets": "总资产(元)",
    "fcf_yield": "自由现金流收益率=(ocf-|capex|)/总市值",
    "ocf_to_assets": "经营现金流/总资产", "capex_intensity": "资本开支强度",
}


def tail_of(xs: list[float], n: int) -> list[float]:
    return xs[-n:] if len(xs) >= n else list(xs)


def fill_missing(xs: list) -> list:
    """前值填充缺失值；若首端仍缺失则返回空列表（上层据此把变量置 None）。

    量能数据的缺失率极低，但一旦出现 None 会让 std/mean 抛错或产生假值，
    因此统一在此收敛：宁可整体判缺，不可局部造假。
    """
    out: list = []
    for x in xs:
        out.append(x if x is not None else (out[-1] if out else None))
    if out and out[0] is None:
        return []
    return out


def lookup_recent(hist: dict, asof, max_days: int = 3):
    """按日期查找最近 ≤max_days 自然日的值均值（news_senti 等）。"""
    if not hist:
        return None
    vals = []
    for d, v in hist.items():
        gap = (asof - d).days
        if 0 <= gap <= max_days:
            vals.append(v)
    return (sum(vals) / len(vals)) if vals else None


def news_lookup_factory(hist: dict) -> Callable:
    def f(sym: str, asof):
        h = hist.get(sym)
        return lookup_recent(h, asof) if h else None
    return f


# ============ 财务数据 PIT 查找 ============
def as_date(v) -> date | None:
    """宽松日期解析：date/datetime/字符串 均可。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        return None


def fin_hist_map(rows: Iterable) -> dict:
    """把 financials_raw 查询结果整理为 {symbol: [(ann_date, {ocf,capex,total_assets}), …]}。

    rows 每行顺序：symbol, ann_date, n_cashflow_act, capex, total_assets
    返回按 ann_date 升序，便于 fin_asof 顺序扫描。
    """
    out: dict[str, list[tuple[date, dict]]] = {}
    for r in rows:
        sym = r[0]
        ann = as_date(r[1])
        if sym is None or ann is None:
            continue
        out.setdefault(sym, []).append(
            (ann, {"ocf": r[2], "capex": r[3], "total_assets": r[4]})
        )
    for recs in out.values():
        recs.sort(key=lambda t: t[0])
    return out


def fin_asof(hist: dict, sym: str, asof) -> dict | None:
    """严格 PIT：取 ann_date ≤ asof 的最新一期财务；无则 None。

    口径说明：用「公告日」而非「报告期」判断可见性——2025 年报在 2026-04
    才公告，站在 2025-12 的截面不该看到它。这是防前视的关键。
    """
    recs = hist.get(sym)
    if not recs:
        return None
    asof = as_date(asof)
    if asof is None:
        return None
    best = None
    for ann, payload in recs:
        if ann <= asof:
            best = payload
        else:
            break
    return best
