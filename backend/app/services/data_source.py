"""数据源封装层（多后端）。

设计遵循「数据原生、以平台做聚合」：统一对外暴露 get_stock_list / get_daily_kline
/ get_spot_quotes，内部按优先级选择可用后端：

  1. akshare   —— 免 token，覆盖 A/港/美股；正常情况下首选
  2. westock   —— 调用 WorkBuddy 内置 westock-data Node CLI（腾讯自选股接口）
                 在 akshare 不可用（如受限网络）时作为兜底，沙箱环境即用此路

所有 akshare 调用均为 lazy import，未安装时自动回退 westock，平台始终可运行。
"""
from __future__ import annotations

import importlib
import logging
import os
import shutil
import subprocess
from datetime import date, datetime, timedelta

from app.config import settings

logger = logging.getLogger(__name__)

# ============ akshare 探测 ============
_AKSHARE_AVAILABLE = None


def check_akshare() -> bool:
    global _AKSHARE_AVAILABLE
    if _AKSHARE_AVAILABLE is None:
        try:
            importlib.import_module("akshare")
            _AKSHARE_AVAILABLE = True
        except ImportError:
            _AKSHARE_AVAILABLE = False
    return _AKSHARE_AVAILABLE


def _require_ak():
    if not check_akshare():
        raise RuntimeError("数据源 akshare 未安装。请执行: pip install akshare")
    import akshare as ak
    return ak


# ============ westock CLI 探测 ============
_WESTOCK_CLI = None  # 缓存找到的可执行脚本路径


def _find_westock_cli() -> str | None:
    global _WESTOCK_CLI
    if _WESTOCK_CLI is not None:
        return _WESTOCK_CLI
    # 1) 环境变量显式指定
    env_path = os.environ.get("QUANT_WESTOCK_CLI")
    if env_path and os.path.exists(env_path):
        _WESTOCK_CLI = env_path
        return _WESTOCK_CLI
    # 2) WorkBuddy 内置 skill 默认路径
    candidates = [
        "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-skills/westock-data/scripts/index.js",
    ]
    for c in candidates:
        if os.path.exists(c):
            _WESTOCK_CLI = c
            return _WESTOCK_CLI
    # 3) PATH 中的 westock-data
    in_path = shutil.which("westock-data")
    if in_path:
        _WESTOCK_CLI = in_path
        return _WESTOCK_CLI
    _WESTOCK_CLI = ""
    return None


def check_westock() -> bool:
    return bool(_find_westock_cli())


def _run_westock(args: list[str], timeout: int = 60) -> str:
    cli = _find_westock_cli()
    if not cli:
        raise RuntimeError("westock-data CLI 未找到")
    node = os.environ.get("QUANT_NODE_BIN") or shutil.which("node") or "node"
    # 若 cli 是 .js 文件则用 node 运行，否则直接执行
    if cli.endswith(".js"):
        cmd = [node, cli, *args]
    else:
        cmd = [cli, *args]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"westock 调用失败: {res.stderr[:300]}")
    return res.stdout


def _parse_md_table(text: str) -> list[dict]:
    """解析 westock 输出的 Markdown 表格为 dict 列表。"""
    rows = []
    lines = [l for l in text.splitlines() if l.strip().startswith("|")]
    if len(lines) < 2:
        return rows
    headers = [h.strip() for h in lines[0].strip().strip("|").split("|")]
    for line in lines[2:]:  # 跳过表头与分隔行
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != len(headers):
            continue
        rows.append(dict(zip(headers, cells)))
    return rows


# ============ tushare 探测 ============
_TUSHARE_AVAILABLE = None
_TUSHARE_PRO = None


def check_tushare() -> bool:
    global _TUSHARE_AVAILABLE
    if _TUSHARE_AVAILABLE is None:
        try:
            if not getattr(settings, "tushare_token", ""):
                _TUSHARE_AVAILABLE = False
            else:
                importlib.import_module("tushare")
                _TUSHARE_AVAILABLE = True
        except Exception:
            _TUSHARE_AVAILABLE = False
    return _TUSHARE_AVAILABLE


def _require_tushare_pro():
    """返回 tushare pro_api 实例（懒初始化并缓存）。"""
    global _TUSHARE_PRO
    if _TUSHARE_PRO is not None:
        return _TUSHARE_PRO
    if not check_tushare():
        raise RuntimeError("tushare 未安装或未配置 token")
    import tushare as ts
    ts.set_token(settings.tushare_token)
    _TUSHARE_PRO = ts.pro_api()
    return _TUSHARE_PRO


def to_ts_code(symbol: str) -> str:
    """sh600519 -> 600519.SH ；sz000858 -> 000858.SZ ；sh000906 -> 000906.SH（指数）。"""
    if symbol.startswith(("sh", "sz", "hk", "us")):
        code = symbol[2:]
        prefix = "SH" if symbol[:2] == "sh" else ("SZ" if symbol[:2] == "sz" else symbol[:2].upper())
        return f"{code}.{prefix}"
    return symbol


# ============ tushare 实现 ============
def _get_stock_list_tushare() -> list[dict]:
    pro = _require_tushare_pro()
    df = pro.stock_basic(exchange="", list_status="L",
                         fields="ts_code,symbol,name,industry,market,list_date")
    out = []
    for _, row in df.iterrows():
        code = str(row["symbol"]).zfill(6)
        sym, market = normalize_symbol(code)
        out.append({
            "symbol": sym, "name": row["name"], "market": market,
            "raw_code": code, "industry": row.get("industry") or None,
            "list_date": (str(row["list_date"]) if row.get("list_date") else None),
        })
    return out


def _get_daily_kline_tushare(symbol: str, start_date: date, end_date: date,
                             limit: int | None = None) -> list[dict]:
    pro = _require_tushare_pro()
    df = pro.daily(
        ts_code=to_ts_code(symbol),
        start_date=start_date.strftime("%Y%m%d"),
        end_date=end_date.strftime("%Y%m%d"),
    )
    if df is None or df.empty:
        return []
    out = []
    for _, r in df.iterrows():
        out.append({
            "trade_date": date.fromisoformat(r["trade_date"]),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": int(r["vol"]), "amount": float(r["amount"]),
        })
    out.sort(key=lambda x: x["trade_date"])
    if limit:
        out = out[-limit:]
    return out


def _get_index_constituents_tushare(index_code: str) -> list[dict]:
    pro = _require_tushare_pro()
    ts_code = f"{index_code}.SH"
    ed = date.today()
    sd = ed - timedelta(days=45)
    df = pro.index_weight(
        index_code=ts_code,
        start_date=sd.strftime("%Y%m%d"),
        end_date=ed.strftime("%Y%m%d"),
    )
    if df is None or df.empty:
        # 回退到年初某窗口（指数成分变动不频繁）
        df = pro.index_weight(index_code=ts_code, start_date="20240101", end_date="20240131")
    if df is None or df.empty:
        raise RuntimeError("tushare 未返回指数成分")
    latest = df["trade_date"].max()
    sub = df[df["trade_date"] == latest]
    out = []
    for _, row in sub.iterrows():
        code = str(row["con_code"]).split(".")[0]
        sym, market = normalize_symbol(code)
        out.append({"symbol": sym, "name": "", "raw_code": code, "market": market})
    return out


def _get_index_daily_kline_tushare(symbol: str, start_date: date, end_date: date | None = None,
                                  limit: int | None = None) -> list[dict]:
    pro = _require_tushare_pro()
    ed = end_date or date.today()
    df = pro.index_daily(
        ts_code=to_ts_code(symbol),
        start_date=start_date.strftime("%Y%m%d"),
        end_date=ed.strftime("%Y%m%d"),
    )
    if df is None or df.empty:
        return []
    out = []
    for _, r in df.iterrows():
        out.append({
            "trade_date": date.fromisoformat(r["trade_date"]),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": int(r["vol"]) if "vol" in r else 0,
            "amount": float(r["amount"]) if "amount" in r else 0.0,
        })
    out.sort(key=lambda x: x["trade_date"])
    if limit:
        out = out[-limit:]
    return out


# ============ 代码转换 ============
def to_ak_code(symbol: str) -> str:
    if symbol.startswith(("sh", "sz")):
        return symbol[2:]
    if symbol.startswith("hk"):
        return symbol[2:]
    return symbol


def normalize_symbol(raw_code: str) -> tuple[str, str]:
    raw_code = raw_code.zfill(6)
    if raw_code.startswith(("60", "68", "9", "5", "11", "113")):
        return f"sh{raw_code}", "sh"
    if raw_code.startswith(("00", "30", "15", "12")):
        return f"sz{raw_code}", "sz"
    return f"sh{raw_code}", "sh"


# ============ 截面属性（行业 / 市值 / 估值） ============
def get_stock_attrs(symbols: list[str]) -> dict[str, dict]:
    """批量返回标的截面属性 {symbol: {industry, market_cap(亿元), pe_ttm, pb}}。

    优先 tushare：行业来自 stock_basic；市值/估值来自 daily_basic 最近交易日。
    用于指数增强的行业/市值中性化与估值因子。缺失字段为 None。
    """
    out = {s: {"industry": None, "market_cap": None, "pe_ttm": None, "pb": None} for s in symbols}
    if not check_tushare():
        return out
    # 1) 行业（全市场一次性拉取）
    try:
        pro = _require_tushare_pro()
        df = pro.stock_basic(exchange="", list_status="L", fields="ts_code,symbol,industry")
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                code = str(row["symbol"]).zfill(6)
                sym, _ = normalize_symbol(code)
                if sym in out:
                    out[sym]["industry"] = row.get("industry") or None
    except Exception:
        pass
    # 2) 市值/估值（daily_basic 最近有数据的交易日）
    try:
        pro = _require_tushare_pro()
        ed = date.today()
        mv_map: dict[str, dict] = {}
        for back in range(0, 12):
            td = (ed - timedelta(days=back)).strftime("%Y%m%d")
            try:
                df = pro.daily_basic(trade_date=td, fields="ts_code,total_mv,pe_ttm,pb")
            except Exception:
                df = None
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    code = str(row["ts_code"]).split(".")[0]
                    sym, _ = normalize_symbol(code)
                    total_mv = row.get("total_mv")
                    mv_map[sym] = {
                        "market_cap": (float(total_mv) / 10000.0) if total_mv else None,  # 万元 -> 亿元
                        "pe_ttm": float(row["pe_ttm"]) if row.get("pe_ttm") else None,
                        "pb": float(row["pb"]) if row.get("pb") else None,
                    }
                break
        for sym in out:
            if sym in mv_map:
                out[sym].update(mv_map[sym])
    except Exception:
        pass
    return out


# ============ 对外 API ============
def get_stock_list() -> list[dict]:
    """A股全市场基础信息。优先 akshare，回退 tushare，再回退内置样本。"""
    # 1) akshare
    try:
        ak = _require_ak()
        df = ak.stock_info_a_code_name()
        out = []
        for _, row in df.iterrows():
            code = str(row["code"]).zfill(6)
            sym, market = normalize_symbol(code)
            out.append({
                "symbol": sym, "name": row["name"], "market": market,
                "raw_code": code, "industry": None, "list_date": None,
            })
        return out
    except Exception:
        pass
    # 2) tushare
    if check_tushare():
        try:
            return _get_stock_list_tushare()
        except Exception:
            pass
    # 3) 沙箱兜底：仅返回常用标的，保证看板可用
    return [
        {"symbol": "sh600519", "name": "贵州茅台", "market": "sh", "raw_code": "600519"},
        {"symbol": "sz300750", "name": "宁德时代", "market": "sz", "raw_code": "300750"},
        {"symbol": "sh601318", "name": "中国平安", "market": "sh", "raw_code": "601318"},
        {"symbol": "sz000858", "name": "五粮液", "market": "sz", "raw_code": "000858"},
        {"symbol": "sh600036", "name": "招商银行", "market": "sh", "raw_code": "600036"},
    ]


def get_daily_kline(symbol: str, start_date: date, end_date: date | None = None,
                    adj: str = "qfq", limit: int | None = None) -> list[dict]:
    """单标的日K线。优先 akshare（history），回退 westock CLI。"""
    ed = end_date or date.today()
    # 1) akshare
    if check_akshare():
        try:
            ak = _require_ak()
            code = to_ak_code(symbol)
            sd = start_date.strftime("%Y%m%d")
            ed_s = ed.strftime("%Y%m%d")
            adjust = "" if adj == "none" else adj
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=sd, end_date=ed_s, adjust=adjust,
            )
            out = []
            for _, r in df.iterrows():
                out.append({
                    "trade_date": r["日期"].date() if hasattr(r["日期"], "date") else r["日期"],
                    "open": float(r["开盘"]), "high": float(r["最高"]),
                    "low": float(r["最低"]), "close": float(r["收盘"]),
                    "volume": int(r["成交量"]), "amount": float(r["成交额"]),
                })
            if             limit:
                out = out[-limit:]
            return out
        except Exception as e:
            # 回退 tushare / westock
            pass
    # 2) tushare（兜底）
    if check_tushare():
        try:
            return _get_daily_kline_tushare(symbol, start_date, ed, limit)
        except Exception:
            pass
    # 3) westock CLI
    if check_westock():
        return _get_kline_westock(symbol, limit or 400)
    raise RuntimeError(f"无可用数据源获取 {symbol} 日K线")


def _get_kline_westock(symbol: str, limit: int) -> list[dict]:
    text = _run_westock(["kline", symbol, "--period", "day", "--limit", str(limit)])
    rows = _parse_md_table(text)
    out = []
    for r in rows:
        # 列名可能含 date/open/last/high/low/volume/amount
        try:
            td = date.fromisoformat(r.get("date", ""))
        except Exception:
            continue
        out.append({
            "trade_date": td,
            "open": float(r.get("open", 0)),
            "close": float(r.get("last", r.get("close", 0))),
            "high": float(r.get("high", 0)),
            "low": float(r.get("low", 0)),
            "volume": int(float(r.get("volume", 0))),
            "amount": float(r.get("amount", 0)),
        })
    out.sort(key=lambda x: x["trade_date"])
    return out


def get_spot_quotes(symbols: list[str] | None = None) -> list[dict]:
    """实时行情快照。优先 akshare（东方财富），回退 westock CLI。"""
    if check_akshare():
        try:
            ak = _require_ak()
            df = ak.stock_zh_a_spot_em()
            rows = df
            if symbols:
                wanted = {to_ak_code(s) for s in symbols}
                rows = df[df["代码"].isin(wanted)]
            return [
                {
                    "symbol": normalize_symbol(str(r["代码"]).zfill(6))[0],
                    "name": r["名称"], "price": float(r["最新价"]),
                    "change_pct": float(r["涨跌幅"]),
                }
                for _, r in rows.iterrows()
            ]
        except Exception:
            pass
    # westock 兜底：取个股实时截面
    out = []
    targets = symbols or ["sh600519", "sz300750", "sh601318", "sz000858", "sh600036"]
    for sym in targets:
        try:
            text = _run_westock(["quote", sym])
            rows = _parse_md_table(text)
            if rows:
                r = rows[0]
                price = float(r.get("current", r.get("price", r.get("last", 0))))
                chg = float(r.get("change_percent", r.get("涨跌额", 0)))
            out.append({"symbol": sym, "name": r.get("name", sym),
                        "price": price, "change_pct": chg})
        except Exception:
            continue
    return out


# 实时价缓存（避免每 tick 拉全市场快照）
_RT_PRICE_CACHE: tuple | None = None  # (timestamp, {symbol: quote})
_RT_PRICE_CACHE_TTL = 5  # 秒


def get_realtime_prices(symbols: list[str]) -> list:
    """轻量实时价查询（按指定标的），优先 westock 单只 quote，回退 akshare 全市场快照。

    返回与 symbols 顺序一致的列表，缺失为 None。非交易时段 akshare 通常返回最近收盘价，
    故本函数天然兼容「盘后/非交易日」的模拟盘演练（用最近收盘作为当前价）。
    """
    global _RT_PRICE_CACHE
    now = datetime.now()
    cached: dict = {}
    if _RT_PRICE_CACHE:
        ts, cache = _RT_PRICE_CACHE
        if (now - ts).total_seconds() < _RT_PRICE_CACHE_TTL:
            cached = {s: cache[s] for s in symbols if s in cache}
        if len(cached) == len(symbols):
            return [cached.get(s) for s in symbols]
    out: dict = dict(cached)
    # 1) westock 单只 quote（沙箱兜底，最快）
    for sym in (s for s in symbols if s not in out):
        try:
            text = _run_westock(["quote", sym])
            rows = _parse_md_table(text)
            if rows:
                r = rows[0]
                price = float(r.get("current", r.get("price", r.get("last", 0))))
                out[sym] = {"symbol": sym, "name": r.get("name", sym),
                            "price": price, "change_pct": float(r.get("change_percent", 0))}
        except Exception:
            continue
    # 2) 仍缺失 → akshare 全市场快照（一次拉全量，整体缓存放回）
    missing = [s for s in symbols if s not in out]
    if missing and check_akshare():
        try:
            ak = _require_ak()
            df = ak.stock_zh_a_spot_em()
            for _, r in df.iterrows():
                sym = normalize_symbol(str(r["代码"]).zfill(6))[0]
                out[sym] = {"symbol": sym, "name": r["名称"],
                            "price": float(r["最新价"]), "change_pct": float(r["涨跌幅"])}
        except Exception:
            pass
    _RT_PRICE_CACHE = (now, out)
    return [out.get(s) for s in symbols]


# ============ 指数 / 组合相关 ============
def get_index_constituents(index_code: str = "000906") -> list[dict]:
    """指数成分股列表（如 000906 中证800 / 000300 沪深300）。

    优先级：tushare（任意指数均支持，最稳）→ akshare(csindex) → 内置种子文件。
    """
    # 1) tushare（支持任意指数，最可靠）
    if check_tushare():
        try:
            return _get_index_constituents_tushare(index_code)
        except Exception:
            pass
    # 2) akshare csindex
    try:
        ak = _require_ak()
        df = ak.index_stock_cons_csindex(symbol=index_code)
        out = []
        for _, row in df.iterrows():
            code = str(row["成分券代码"]).zfill(6)
            sym, market = normalize_symbol(code)
            out.append({"symbol": sym, "name": row["成分券名称"], "raw_code": code, "market": market})
        return out
    except Exception:
        pass
    # 3) 回退：内置种子文件（CSI800 真实列表）
    try:
        import json, os
        p = os.path.join(os.path.dirname(__file__), "..", "..", "data", f"{index_code.lower()}.json")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        p2 = os.path.join(os.path.dirname(__file__), "..", "..", "data", "csi800.json")
        if os.path.exists(p2):
            with open(p2, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    raise RuntimeError(f"无可用数据源获取指数 {index_code} 成分股（请先运行 seed 脚本预拉取）")


# 指数成分股成员资格快照缓存：key=(index_code, sd, ed) -> [(trade_date_str, {symbol}), ...]
# 避免同一回测窗口重复调用 tushare。
_MEMBERSHIP_CACHE: dict = {}


def get_index_membership(index_code: str, sd: date, ed: date) -> list[tuple[str, set]]:
    """指数成分股的『时点(point-in-time)』成员资格（**已迁移**，勿再直连 tushare）。

    返回按交易日升序的快照列表 ``[(trade_date_str, {symbol,...}), ...]``。
    每个快照是截至该交易日指数实际包含的成分股集合——回测时应在每个调仓日
    取「≤ 该日期的最新快照」作为合法股票池，从而消除『用当前成分股回测整段
    历史』带来的前视/幸存者偏差。

    实现（2026-09 起）：委托 ``membership_store.get_membership`` —— **先读
    index_membership 落库快照**（2020-01 起按月齐全），缺失月份才走 csindex/
    sina 在线兜底。历史背景：本函数原直连 tushare ``index_weight`` 在线拉取，
    但该接口需较高积分，个人 token 已无权限会静默返回空，故整体收敛到
    membership_store（库优先 + 免费源兜底），彻底不再依赖 tushare 权限。
    """
    key = (index_code, sd.isoformat(), ed.isoformat())
    if key in _MEMBERSHIP_CACHE:
        return _MEMBERSHIP_CACHE[key]
    from app.database import SessionLocal
    from app.services.membership_store import get_membership as _gm

    try:
        with SessionLocal() as db:
            ordered = sorted(_gm(db, index_code, sd, ed))
    except Exception as e:  # noqa: BLE001
        logger.warning("指数 %s 成分快照获取异常（%s）", index_code, e)
        ordered = []
    if not ordered:
        # 库内缺失 + 在线兜底均不可用（如历史月份无免费源）——必须响一声，不能静默
        logger.warning(
            "指数 %s 在 %s~%s 无任何成分快照（库内缺失且在线兜底不可用）；"
            "可运行 `python scripts/seed_membership.py --index %s` 检查可拉取的月份",
            index_code, sd, ed, index_code,
        )
    _MEMBERSHIP_CACHE[key] = ordered
    return ordered


def get_index_daily_kline(symbol: str, start_date: date, end_date: date | None = None,
                          limit: int | None = None) -> list[dict]:
    """指数日K线（如 sh000906 中证800 / sh000300 沪深300）。

    优先级：tushare → akshare → westock。
    """
    ed = end_date or date.today()
    # 1) tushare
    if check_tushare():
        try:
            return _get_index_daily_kline_tushare(symbol, start_date, ed, limit)
        except Exception:
            pass
    # 2) akshare
    if check_akshare():
        try:
            ak = _require_ak()
            df = ak.stock_zh_index_daily(symbol=symbol)
            if df is not None and not df.empty:
                out = []
                for _, r in df.iterrows():
                    d = r["date"]
                    if isinstance(d, str):
                        d = date.fromisoformat(d)
                    if d < start_date or d > ed:
                        continue
                    out.append({
                        "trade_date": d,
                        "open": float(r["open"]), "high": float(r["high"]),
                        "low": float(r["low"]), "close": float(r["close"]),
                        "volume": int(float(r.get("volume", 0))),
                        "amount": float(r.get("amount", 0)),
                    })
                out.sort(key=lambda x: x["trade_date"])
                if limit:
                    out = out[-limit:]
                return out
        except Exception:
            pass
    # 3) westock
    if check_westock():
        return _get_kline_westock(symbol, limit or 400)
    raise RuntimeError(f"无可用数据源获取指数 {symbol} 日K线")


def get_stock_daily_qfq(symbol: str, start_date: date, end_date: date | None = None) -> list[dict]:
    """个股日K线（前复权）。优先 akshare sina 源，回退 tushare，再回退通用日K。"""
    ed = end_date or date.today()
    if check_akshare():
        try:
            ak = _require_ak()
            df = ak.stock_zh_a_daily(
                symbol=symbol, start_date=start_date.strftime("%Y%m%d"),
                end_date=ed.strftime("%Y%m%d"), adjust="qfq",
            )
            if df is not None and not df.empty:
                out = []
                for _, r in df.iterrows():
                    d = r["date"]
                    if isinstance(d, str):
                        d = date.fromisoformat(d)
                    out.append({
                        "trade_date": d,
                        "open": float(r["open"]), "high": float(r["high"]),
                        "low": float(r["low"]), "close": float(r["close"]),
                        "volume": int(float(r.get("volume", 0))),
                        "amount": float(r.get("amount", 0)),
                    })
                return out
        except Exception:
            pass
    # 回退 tushare
    if check_tushare():
        try:
            return _get_daily_kline_tushare(symbol, start_date, ed)
        except Exception:
            pass
    # 回退通用日K（可能不复权）
    return get_daily_kline(symbol, start_date, ed, adj="qfq", limit=None)
