# -*- coding: utf-8 -*-
"""fresh_ma · QMT 内置策略入口（Python 3.6 兼容）。

职责边界很清楚：本文件只做「和 QMT 打交道」——取数、报单、读写状态。
所有决策逻辑在 core.py（已在平台上完成 5/5 天全市场一致性验证）。
这样换终端（QMT → PTrade）只改本文件，内核一行不动。

⚠️ 三个必须在真实环境实测的点（猜错任何一个都会静默失效）：
  1. handlebar 的触发频率。日线周期下是收盘后触发；分钟周期下才有 14:45。
     策略用「当天没跑过 + 时间到」双条件，两种周期都能工作。
  2. passorder 的参数含义。opType 23/24（买/卖）、prType 11（限价）为常见口径，
     但各券商版本有差异，**必须以开户券商的 API 文档为准**。
  3. get_market_data_ex 的分批大小。全市场一次取可能超时，这里按 600 只分批。

合规前提：本策略在 QMT 客户端内运行，指令由终端生成，平台只提供参数与代码。
不得在本文件中发起任何网络请求、不得读写本地文件（2026-10-23 起被沙箱禁止）。
"""
from __future__ import print_function

import json
import time

import core
import chanlun_py36 as chan
from bootstrap_exc import BOOTSTRAP_EXC


# ══════════════════════ 配置（首次部署改这里）══════════════════════

ACCOUNT = "YOUR_ACCOUNT_ID"      # 资金账号
PERIOD = "1d"                    # K 线周期
COUNT = 260                      # 取多少根
BATCH = 600                      # get_market_data_ex 分批大小
RUN_AFTER = "14:50"              # 该时点之后才执行（分钟周期用）
ENABLED = ["chan_buy", "high_tight_flag"]
HB_EVERY_MIN = 15                # 心跳间隔（分钟）。看门狗靠它判断策略还活着

PENDING = {}          # userOrderId → 委托状态（防超单）
ACCOUNT_LOG = []      # 当日成交明细（收盘对账用）

KEY_HITS = "fm_hits"             # 近 5 日命中名单（算 streak）
KEY_EXC = "fm_exc"               # 近 90 个信号日超额（算门控）
KEY_LAST = "fm_last"             # 最后执行日期（防重复下单）
HITS_KEEP = 6                    # streak<=2 只需 3 天，留 6 天冗余
EXC_KEEP = 95


# ══════════════════════ 代码格式（必须统一，否则全市场会被滤空）══════════════════════

def to_platform(code):
    """QMT 代码 → 平台 symbol。600000.SH / 600000 → sh600000。

    QMT 用 6 位数字（可能带 .SH/.SZ 后缀），平台用 sh/sz 前缀。两套格式混用会
    直接导致股票池被过滤成空（实测第一版就是这样，0 只标的）。
    """
    c = str(code).split(".")[0].strip()
    if len(c) != 6 or not c.isdigit():
        return ""
    if c[0] == "6":
        return "sh" + c
    if c[0] in ("0", "3"):
        return "sz" + c
    return ""


def to_qmt(symbol):
    """平台 symbol → QMT 代码。sh600000 → 600000.SH。下单与取数都用它。"""
    if len(symbol) < 8:
        return symbol
    return symbol[2:] + "." + symbol[:2].upper()


# ══════════════════════ 状态（QMT 服务端 KV，替代被禁的文件）══════════════════════

def kv_get(key, default):
    """读策略状态。QMT 关掉了本地文件读写，只能用券商端 KV。

    get_py_key(key) -> (value, errid, errmsg)；失败时返回默认值，
    **不能因为读不到就当空**——那样 streak 会全部重算成 1，选股直接跑偏。
    """
    try:
        r = get_py_key(key)      # noqa: F821  QMT 注入的全局函数
        val = r[0] if isinstance(r, (tuple, list)) else r
        if val:
            return json.loads(val)
    except Exception as e:       # noqa: BLE001
        print("[kv] 读取 %s 失败：%s" % (key, e))
    return default


def kv_set(key, value):
    try:
        return save_py_key(key, json.dumps(value, separators=(",", ":")))  # noqa: F821
    except Exception as e:       # noqa: BLE001
        print("[kv] 写入 %s 失败：%s" % (key, e))
        return None


# ══════════════════════ 取数 ══════════════════════

def fetch_bars(ContextInfo, symbols):
    """批量取后复权日线，返回 {symbol: [bar, ...]}。

    bar 字段与 core 约定一致：date(YYYYMMDD) / open / high / low / close / volume / raw_close。
    后复权用 dividend_type='back'；raw_close 单独取不复权，只用于下单报价。
    """
    out = {}
    fields = ["time", "open", "high", "low", "close", "volume"]
    code_map = dict((to_qmt(s), s) for s in symbols)     # QMT 代码 → 平台 symbol
    qmt_codes = list(code_map.keys())
    for i in range(0, len(qmt_codes), BATCH):
        chunk = qmt_codes[i:i + BATCH]
        try:
            data = ContextInfo.get_market_data_ex(
                fields, chunk, period=PERIOD, count=COUNT,
                dividend_type="back", fill_data=True)          # noqa: F821
        except Exception as e:                                  # noqa: BLE001
            print("[data] 第 %d 批取数失败：%s" % (i // BATCH + 1, e))
            continue

        for qmt_code in chunk:
            sym = code_map.get(qmt_code, "")
            df = data.get(qmt_code) if isinstance(data, dict) else None
            if df is None and isinstance(data, dict):
                df = data.get(to_platform(qmt_code))
            if df is None or len(df) == 0:
                continue
            bars = []
            cols = [str(c).lower() for c in df.columns]

            def pick(row, name, fallback_idx):
                """优先按列名取；缺列时按位置兜底。

                注意：pandas Series 的 row[i] 是**标签**索引，不是位置索引，
                直接 row[1] 会抛 out-of-bounds（实测踩过）。位置必须用 iloc。
                """
                idx = getattr(row, "index", None)
                if idx is not None and name in idx:
                    return float(row[name])
                if hasattr(row, "iloc") and fallback_idx < len(row):
                    return float(row.iloc[fallback_idx])
                return 0.0

            try:
                for i in range(len(df)):
                    row = df.iloc[i]
                    bars.append({
                        "date": str(df.index[i]).replace("-", "")[:8],
                        "open": pick(row, "open", 1),
                        "high": pick(row, "high", 2),
                        "low": pick(row, "low", 3),
                        "close": pick(row, "close", 4),
                        "volume": pick(row, "volume", 5),
                        "raw_close": 0.0,     # 0 = 未取到不复权价，下单时会被拦下
                    })
            except Exception as e:                              # noqa: BLE001
                print("[data] %s 解析失败：%s" % (sym, e))
                continue
            if len(bars) >= 120:
                out[sym] = bars

    # 不复权价（下单报价）：单独一批取，只取最后一根
    try:
        raw = ContextInfo.get_market_data_ex(
            ["close"], [to_qmt(s) for s in out.keys()], period=PERIOD, count=1,
            dividend_type="none", fill_data=True)               # noqa: F821
        for code, df in (raw or {}).items():
            sym = to_platform(code) if to_platform(code) in out else code
            if sym not in out or df is None or not len(df):
                continue
            # 必须按列名取：df.values[-1][-1] 拿到的是最后一列（volume），
            # 实测把成交量当价格报出去，价格差了四个数量级。
            try:
                col = "close" if "close" in [str(c).lower() for c in df.columns] else None
                if col and col in df.columns:
                    out[sym][-1]["raw_close"] = float(df[col].iloc[-1])
                else:
                    out[sym][-1]["raw_close"] = float(df.iloc[-1, 0])
            except Exception:                                   # noqa: BLE001
                pass
    except Exception as e:                                      # noqa: BLE001
        print("[data] 不复权价获取失败：%s" % e)
    miss = [s for s, b in out.items() if not b[-1].get("raw_close")]
    if miss:
        print("[data] ⚠ %d 只缺未复权价，这些将拒绝下单（不影响其余）" % len(miss))

    return out


def get_positions(ContextInfo):
    """读实际持仓 {symbol: {shares, entry_price, entry_date, peak}}。

    entry_date / peak 是策略风控的状态，券商只给持仓数量，这两个必须自己维护：
    在 KV 里存 `fm_pos`，首次建仓写入，之后每日更新 peak。
    """
    meta = kv_get("fm_pos", {})
    try:
        pos = ContextInfo.get_trade_detail_data(ACCOUNT, "stock", "position")  # noqa: F821
        live = {}
        for p in pos:
            qty = int(getattr(p, "m_nVolume", 0) or 0)
            if qty > 0:
                code = str(getattr(p, "m_strInstrumentID", ""))
                live[code] = qty
        # 与 KV 对齐：只保留仍有持仓的票，避免幽灵记录
        for sym in list(meta.keys()):
            if sym not in live:
                meta.pop(sym, None)
        for sym, qty in live.items():
            if sym not in meta:
                meta[sym] = {"shares": qty, "entry_price": 0.0,
                             "entry_date": "", "peak": 0.0}
            else:
                meta[sym]["shares"] = qty
        return meta
    except Exception as e:                                      # noqa: BLE001
        print("[pos] 读取持仓失败：%s" % e)
        return meta


def get_cash(ContextInfo):
    try:
        acc = ContextInfo.get_trade_detail_data(ACCOUNT, "stock", "account")    # noqa: F821
        if acc:
            return float(getattr(acc[0], "m_dAvailable", 0.0))
    except Exception as e:                                      # noqa: BLE001
        print("[acc] 读取可用资金失败：%s" % e)
    return 0.0


def get_total_asset(ContextInfo):
    try:
        acc = ContextInfo.get_trade_detail_data(ACCOUNT, "stock", "account")    # noqa: F821
        if acc:
            return float(getattr(acc[0], "m_dBalance", 0.0))
    except Exception:                                           # noqa: BLE001
        pass
    return 0.0


# ══════════════════════ 下单 ══════════════════════

def send_order(ContextInfo, symbol, side, qty, price, remark=""):
    """限价报单。

    ⚠️ 参数顺序是本函数唯一的坑，写错不会报错、只会静默丢单：
        passorder(opType, orderType, accountid, orderCode, prType, price, volume,
                  strategyName, quickTrade, userOrderId, ContextInfo)
    其中 quickTrade 必须传 2 ——
      0（默认）= 只在 K 线结束分笔时产生有效信号；
      1        = 只在最新 K 线时产生；
      2        = 任何位置调用都产生委托。
    日频策略在 14:50 这种非 K 线边界的时点下单，传 0/1 会被静默丢弃：
    策略日志打了 [order]，券商委托列表里什么都没有。

    userOrderId 是委托去重键，也是防超单字典的 key，必须唯一且可回溯。
    opType 23=买入 / 24=卖出；prType 11=限价（各券商版本略有差异，以文档为准）。
    """
    op = 23 if side == "BUY" else 24
    uid = remark or ("fm_%s_%s" % (symbol.split(".")[0], side))
    try:
        passorder(op, 1101, ACCOUNT, symbol, 11, price, qty,             # noqa: F821
                  "fresh_ma", 2, uid, ContextInfo)
        PENDING[uid] = {"symbol": symbol, "side": side, "qty": qty,
                        "price": price, "status": "待报"}
        print("[order] %s %s %d @ %.2f uid=%s" % (side, symbol, qty, price, uid))
        return True
    except Exception as e:                                      # noqa: BLE001
        print("[order] 报单失败 %s：%s" % (symbol, e))
        return False


def deal_callback(ContextInfo, dealInfo):
    """成交回报（QMT 主推，实盘推荐用它而不是轮询）。

    必须在 init 里 set_account 才能收到。这里只做两件事：更新委托状态、记账。
    真正的持仓以 get_trade_detail_data 为准，回调可能漏推，收盘再对一次账。
    """
    try:
        code = str(dealInfo.m_strInstrumentID)
        vol = int(dealInfo.m_nVolume)
        px = float(dealInfo.m_dPrice)
        ACCOUNT_LOG.append({"code": code, "vol": vol, "price": px})
        print("[deal] 成交 %s %d @ %.2f" % (code, vol, px))
    except Exception as e:                                      # noqa: BLE001
        print("[deal] 回报解析失败：%s" % e)


ORDER_TERMINAL = (53, 54, 56, 57)      # 已成 / 已撤 / 废单 / 部撤
UID_PREFIX = "fm_"                      # 本策略委托的 userOrderId 前缀
_LAST_SYNC = 0


def _parse_uid(uid):
    """从 userOrderId 反解代码与方向：fm_600148_BUY → ("600148", "BUY")。"""
    try:
        parts = str(uid).split("_")
        if len(parts) >= 3 and parts[0].strip() == "fm":
            return parts[1], parts[2]
    except Exception:                                           # noqa: BLE001
        pass
    return "", ""


def sync_pending_from_broker(force=False):
    """用柜台的未终结委托重建 PENDING。

    为什么必须做：PENDING 是内存字典，客户端重启或策略重载后就空了。
    而看门狗会自动重启客户端 —— 如果没有这一步，防超单形同虚设，
    一次重启就可能把同一笔买/卖再下一遍，仓位直接翻倍。
    所以每次下单前都用柜台的真实委托对一次账，认 uid 前缀 fm_。
    """
    global _LAST_SYNC
    now = time.time()
    if not force and (now - _LAST_SYNC) < 60:
        return 0
    _LAST_SYNC = now
    try:
        orders = get_trade_detail_data(ACCOUNT, "stock", "order")      # noqa: F821
    except Exception:                                           # noqa: BLE001
        return 0
    added = 0
    for o in orders:
        uid = str(getattr(o, "m_strRemark", "") or "")
        if not uid.startswith(UID_PREFIX):
            continue                      # 不是本策略下的单，别碰
        st = int(getattr(o, "m_nOrderStatus", 0) or 0)
        if st in ORDER_TERMINAL:
            PENDING.pop(uid, None)
            continue
        if uid in PENDING:
            PENDING[uid]["status"] = st
            continue
        code, side = _parse_uid(uid)
        PENDING[uid] = {
            "symbol": str(getattr(o, "m_strInstrumentID", "") or "") or code,
            "side": side,
            "qty": int(getattr(o, "m_nVolumeTotalOriginal", 0) or 0),
            "price": float(getattr(o, "m_dLimitPrice", 0) or 0),
            "status": st,
            "from_broker": True,          # 标记：这笔是重启后从柜台捞回来的
        }
        added += 1
    if added:
        print("[order] 从柜台恢复 %d 笔未完成委托：%s"
              % (added, ",".join(sorted(PENDING.keys())[:5])))
    return added


def refresh_pending():
    """冲刷委托状态：先跟柜台对齐，再把终态清掉。

    防超单的核心：只要某标的还有未完成委托，就不给它下新单。
    否则一次重复触发就会变成双倍仓位。
    """
    sync_pending_from_broker()
    try:
        orders = get_trade_detail_data(ACCOUNT, "stock", "order")      # noqa: F821
    except Exception:                                           # noqa: BLE001
        return dict(PENDING)
    stat = {}
    for o in orders:
        uid = str(getattr(o, "m_strRemark", "") or "")
        if uid:
            stat[uid] = int(getattr(o, "m_nOrderStatus", 0) or 0)
    for uid in list(PENDING.keys()):
        st = stat.get(uid)
        if st is None:
            continue
        PENDING[uid]["status"] = st
        if st in ORDER_TERMINAL:
            PENDING.pop(uid, None)
    return dict(PENDING)


def has_open_order(symbol):
    """该标的是否还有未完成委托。

    每次都先跟柜台对齐 —— 内存里的 PENDING 不可信（重启就没了）。
    """
    refresh_pending()
    q = to_qmt(symbol) if "." in symbol else symbol
    for v in PENDING.values():
        if v["symbol"] in (symbol, q):
            return True
    return False


# ══════════════════════ 主流程 ══════════════════════

_HB_LAST = None      # 上次心跳的「当日分钟数」，模块级，重启后重新计时


def heartbeat(today):
    """每 HB_EVERY_MIN 分钟打一行 [hb]。

    这是看门狗判断「策略还活着」的唯一抓手：策略禁网、禁文件，
    没法自己报平安，只能靠 print 落到 QMT 客户端日志里，由外部脚本去读。
    必须放在 handlebar 最前面（当日去重 return 之前），否则当天跑完之后
    就再没有任何输出，看起来跟卡死一样。
    """
    global _HB_LAST
    hm = time.strftime("%H:%M")
    try:
        slot = int(hm[:2]) * 60 + int(hm[3:5])
    except Exception:                                           # noqa: BLE001
        slot = -1
    if _HB_LAST is not None and slot >= 0 and 0 <= slot - _HB_LAST < HB_EVERY_MIN:
        return
    _HB_LAST = slot
    print("[hb] %s %s alive" % (today, hm))


def init(ContextInfo):
    """初始化：只做一次。不要在这里取数（此时账户与行情可能未就绪）。"""
    print("[fresh_ma] init · 账户 %s · enabled=%s" % (ACCOUNT, ",".join(ENABLED)))
    try:
        ContextInfo.set_account(ACCOUNT)                        # noqa: F821
    except Exception:                                           # noqa: BLE001
        pass
    ContextInfo.set_universe([])                                # noqa: F821


def handlebar(ContextInfo):
    """每根 bar 触发。日频策略的全部逻辑都在这里按时间闸门放行。"""
    today = str(ContextInfo.get_bar_trading_day())              # noqa: F821
    heartbeat(today)          # 必须在当日去重之前打，跑完了也要继续报平安
    last = kv_get(KEY_LAST, "")
    if last == today:
        return                                                  # 当天已执行，防重复下单

    now = str(ContextInfo.get_bar_timelist()[-1])[:14] if hasattr(ContextInfo, "get_bar_timelist") else ""
    if now and len(now) >= 14:
        hm = now[8:10] + ":" + now[10:12]
        if hm < RUN_AFTER:
            return                                              # 未到执行时点

    # ⓪ 冲刷上一轮委托状态（防超单前置）
    pending = refresh_pending()
    if pending:
        print("[order] 仍有 %d 笔未完成委托：%s"
              % (len(pending), ",".join(sorted(pending.keys())[:5])))

    # ① 取全市场代码
    try:
        universe = ContextInfo.get_stock_list_in_sector("沪深A股")  # noqa: F821
    except Exception as e:                                      # noqa: BLE001
        print("[univ] 取股票池失败：%s" % e)
        return
    # QMT 返回 600000.SH 这类格式，统一转平台 symbol 再过滤（含北交所剔除）
    universe = [to_platform(c) for c in universe]
    universe = [s for s in universe if s]

    # ② 取数
    bars_map = fetch_bars(ContextInfo, universe)
    print("[data] 有效标的 %d 只 · %s" % (len(bars_map), today))

    # ③ 候选（内核）
    hits = kv_get(KEY_HITS, {})
    dates = sorted(hits.keys())[-HITS_KEEP:]
    if today not in dates:
        dates.append(today)
    hit_today = {}
    for sym, bars in bars_map.items():
        st = []
        if "chan_buy" in ENABLED and core.chan_buy(bars, chan.analyze_chanlun, chan.Candle):
            st.append("chan_buy")
        if "high_tight_flag" in ENABLED and core.high_tight_flag(bars):
            st.append("high_tight_flag")
        if st:
            hit_today[sym] = st
    hits[today] = sorted(hit_today.keys())
    for d in sorted(hits.keys())[:-HITS_KEEP]:
        hits.pop(d, None)

    cands = core.build_candidates(bars_map, chan.analyze_chanlun, chan.Candle,
                                  ENABLED, dates, hits)

    # ④ 门控
    # 冷启动问题：首次部署 KV 为空 → 门控永远关闭 → 策略永不建仓。
    # 用平台导出的 95 个成熟信号日超额做引导，保证第一天就与回测门控状态一致。
    exc = kv_get(KEY_EXC, {})
    if not exc and BOOTSTRAP_EXC:
        exc = dict(BOOTSTRAP_EXC)
        kv_set(KEY_EXC, exc)
        print("[gate] 首次运行，载入引导超额 %d 条" % len(exc))
    gate = core.gate_check(exc)

    # ⑤ 持仓与资金
    meta = get_positions(ContextInfo)
    cash = get_cash(ContextInfo)
    total = get_total_asset(ContextInfo) or (cash + sum(
        (bars_map.get(s, [{}])[-1].get("raw_close", 0) or 0) * m["shares"]
        for s, m in meta.items()))
    cfg = dict(core.DEFAULT_CONFIG)
    cfg["capital"] = total

    # ⑥ 卖出（止损先于买入，释放资金）
    date_index = dict((b["date"], i) for b in bars_map.get("sh000001", []))
    exits = core.check_exits(meta, bars_map, date_index, cfg)
    for e in exits:
        qcode = to_qmt(e["symbol"])
        if has_open_order(qcode):
            print("[skip] %s 已有未完成委托，本轮不重复卖出" % qcode)
            continue
        px = bars_map.get(e["symbol"], [{}])[-1].get("raw_close", 0)
        if px > 0:
            send_order(ContextInfo, qcode, "SELL", e["qty"], round(px * 0.98, 2),
                       remark="fm_%s_SELL" % e["symbol"][2:])

    # ⑦ 买入
    orders = []
    if gate["open"]:
        targets = core.select_targets(cands, dict((s, m["shares"]) for s, m in meta.items()), cfg)
        prev_close = {}
        for s, bars in bars_map.items():
            if len(bars) >= 2:
                prev_close[s] = bars[-2]["raw_close"]
        orders = core.build_orders(targets, dict((s, m["shares"]) for s, m in meta.items()),
                                   bars_map, total, cash, prev_close, {}, cfg)
        for o in orders:
            if o.get("blocked"):
                print("[skip] %s %s" % (o["symbol"], o["blocked"]))
                continue
            qcode = to_qmt(o["symbol"])
            if has_open_order(qcode):
                print("[skip] %s 已有未完成委托，本轮不重复买入" % qcode)
                continue
            send_order(ContextInfo, qcode, "BUY", o["qty"], o["price"],
                       remark="fm_%s_BUY" % o["symbol"][2:])

    # ⑧ 写回状态（必须在报单之后，保证「已执行」标记落地）
    kv_set(KEY_HITS, hits)
    kv_set(KEY_EXC, exc)
    kv_set(KEY_LAST, today)
    kv_set("fm_pos", meta)
    n_buy = len([o for o in orders if not o.get("blocked")])
    print("[done] %s · 门控=%s · 候选 %d · 买 %d · 卖 %d"
          % (today, "开" if gate["open"] else "关", len(cands), n_buy, len(exits)))

    # 无交易自检：门控开着、有候选、还有仓位空间，却一笔没下 —— 一定是哪里卡住了。
    # 这行日志是运维脚本的抓手（QMT 禁网，策略自己推不了告警）。
    slots = cfg["max_positions"] - len([1 for m in meta.values() if m.get("shares")])
    if gate["open"] and cands and n_buy == 0 and slots > 0:
        print("[alert] ANOMALY 门控开启、候选 %d、空位 %d，但未产生任何买单"
              % (len(cands), slots))
