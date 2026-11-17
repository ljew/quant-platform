"""当日市场快照：把「行情事实」喂给日报/简报，别让模型从资讯里猜行情。

为什么要有这个（2026-11-17 老刘指出）
--------------------------------------
当天的「当日全景」里写着「缺成交额与涨跌家数」「看不到 ETF 申赎、回购、
投顾仓位、龙虎榜、北向」—— 这些数据平台**本来就有**：

  · 成交额、涨跌家数、指数涨跌 → `kline_daily` / `index_kline_daily` 一条 SQL
  · 涨停跌停                     → tushare `limit_list_d`（实测 11-16 有 108 行）
  · 北向资金                     → tushare `moneyflow_hsgt`
  · 龙虎榜                       → tushare `top_list`（11-16 有 70 行）
  · 回购                         → tushare `repurchase`（11-16 有 156 条）
  · 行业涨跌                     → `kline_daily` join `stocks.industry`

问题不在数据源，在 `brief_digest._gather()` 只汇总了资讯文本，一个行情数字都
没给模型；而提示词第④段又要求它写「资金与筹码」—— 它只能老实说「看不到」。
**给模型的素材缺什么，它就只好说没有。**

真拿不到的只有两项，写进 `missing` 让模型别硬编：
  · BDTI / VLCC TCE（航运运价）—— tushare 无，需波交所/Clarksons 付费源
  · 投顾仓位 —— 无公开接口

口径约定
--------
· 涨跌家数按「收盘 vs 上一交易日收盘」算，不用 pct_chg（kline_daily 没这列）
· 成交额单位：库内 `amount` 是**千元**，这里统一折「亿元」
· 涨停/跌停优先用 tushare `limit_list_d`（含 ST/北交所口径），拿不到才用
  「涨跌幅 ≥9.5%」近似，并在 note 里标明
· 北向 `north_money` 单位**万元**，折「亿元」
· 每一项独立 try/except：任何一项拿不到都不影响其它项，更不影响日报主体
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import text

logger = logging.getLogger("market_snapshot")

# 主要指数（跟「行情」页保持同一套）
_INDEXES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("sh000906", "中证800"),
]

# 真拿不到的（写进快照，让模型别编）
_MISSING = [
    "BDTI / VLCC TCE 等航运运价（tushare 无，需波交所/Clarksons 付费源）",
    "基金投顾仓位（无公开接口）",
]


def _prev_trading_day(db, day: str) -> str | None:
    row = db.execute(text(
        "select max(trade_date) from kline_daily where trade_date < :d"), {"d": day}).scalar()
    return str(row)[:10] if row else None


def _index_pct(db, day: str, prev: str) -> list[dict]:
    """主要指数当日涨跌幅。"""
    rows = db.execute(text(
        "with t as ("
        "  select symbol, trade_date, close,"
        "         lag(close) over (partition by symbol order by trade_date) prev"
        "  from index_kline_daily where trade_date in (:p, :d))"
        " select symbol, close, prev from t where trade_date = :d and prev is not null"
    ), {"p": prev, "d": day}).fetchall()
    got = {r[0]: (float(r[1]), float(r[2])) for r in rows}
    out = []
    for sym, name in _INDEXES:
        if sym in got:
            close, p = got[sym]
            out.append({"name": name, "close": round(close, 2),
                        "pct": round((close / p - 1) * 100, 2)})
    return out


def _breadth(db, day: str, prev: str) -> dict:
    """涨跌家数 + 成交额（全 A，按收盘 vs 上一交易日收盘）。"""
    r = db.execute(text(
        "with t as ("
        "  select symbol, trade_date, close, amount,"
        "         lag(close) over (partition by symbol order by trade_date) prev"
        "  from kline_daily where trade_date in (:p, :d))"
        " select count(*) n,"
        "        sum(case when close > prev then 1 else 0 end) up,"
        "        sum(case when close < prev then 1 else 0 end) down,"
        "        sum(case when close = prev then 1 else 0 end) flat,"
        # 成交额不按 prev 过滤：新股/复牌的那几只没有前收，但成交额是真金白银，
        # 跟涨跌家数用同一个 where 会让总额偏小（差几十亿）。
        "        round(sum(amount) / 1e5, 1) turn"
        " from t where trade_date = :d"
    ), {"p": prev, "d": day}).fetchone()
    if not r or not r[0]:
        return {}
    return {"n": int(r[0]), "up": int(r[1] or 0), "down": int(r[2] or 0),
            "flat": int(r[3] or 0), "turnover_yi": float(r[4] or 0)}


def _limit_counts(day: str) -> dict | None:
    """涨跌停家数（tushare limit_list_d，含 ST/北交所口径）。拿不到返回 None。"""
    try:
        import sys

        sys.path.insert(0, "/app/scripts")
        from _tushare_token import require_token

        import tushare as ts

        pro = ts.pro_api(require_token())
        df = pro.limit_list_d(trade_date=day.replace("-", ""))
        if df is None or df.empty:
            return None
        up = int((df["limit"] == "U").sum()) if "limit" in df.columns else None
        dn = int((df["limit"] == "D").sum()) if "limit" in df.columns else None
        return {"up": up, "down": dn, "src": "tushare limit_list_d"}
    except Exception as e:  # noqa: BLE001
        logger.info("涨跌停板获取失败（降级为涨跌幅近似）：%s", str(e)[:80])
        return None


def _limit_approx(db, day: str, prev: str) -> dict:
    """涨停/跌停的近似值（±9.5%）。只在 tushare 拿不到时用。"""
    r = db.execute(text(
        "with t as ("
        "  select symbol, trade_date, close,"
        "         lag(close) over (partition by symbol order by trade_date) prev"
        "  from kline_daily where trade_date in (:p, :d))"
        " select sum(case when close / prev - 1 >= 0.095 then 1 else 0 end),"
        "        sum(case when close / prev - 1 <= -0.095 then 1 else 0 end)"
        " from t where trade_date = :d and prev is not null"
    ), {"p": prev, "d": day}).fetchone()
    return {"up": int(r[0] or 0) if r else 0, "down": int(r[1] or 0) if r else 0,
            "src": "±9.5% 近似（未区分板块涨跌幅限制）"}


def _sector_pct(db, day: str, prev: str, top: int = 6) -> dict:
    """行业涨跌幅（按 `stocks.industry` 等权平均，样本≥8 只才计入）。"""
    rows = db.execute(text(
        "with t as ("
        "  select k.symbol, k.trade_date, k.close,"
        "         lag(k.close) over (partition by k.symbol order by k.trade_date) prev"
        "  from kline_daily k where k.trade_date in (:p, :d))"
        " select s.industry, count(*) n, round(avg(t.close / t.prev - 1) * 100, 2) pct"
        " from t join stocks s on s.symbol = t.symbol"
        " where t.trade_date = :d and t.prev is not null"
        "   and s.industry is not null and s.industry != ''"
        " group by s.industry having n >= 8 order by pct desc"
    ), {"p": prev, "d": day}).fetchall()
    if not rows:
        return {}
    return {"top": [{"name": r[0], "pct": float(r[2]), "n": int(r[1])} for r in rows[:top]],
            "bottom": [{"name": r[0], "pct": float(r[2]), "n": int(r[1])}
                       for r in rows[-top:]][::-1]}


def _north(day: str) -> dict | None:
    """北向资金净流入（tushare moneyflow_hsgt，单位万元 → 亿元）。"""
    try:
        import sys

        sys.path.insert(0, "/app/scripts")
        from _tushare_token import require_token

        import tushare as ts

        pro = ts.pro_api(require_token())
        df = pro.moneyflow_hsgt(trade_date=day.replace("-", ""))
        if df is None or df.empty:
            return None
        v = float(df.iloc[0].get("north_money") or 0) / 1e4
        return {"net_yi": round(v, 2)}
    except Exception as e:  # noqa: BLE001
        logger.info("北向资金获取失败：%s", str(e)[:80])
        return None


def _top_list(day: str, limit: int = 8) -> dict | None:
    """龙虎榜：净买入前几名。"""
    try:
        import sys

        sys.path.insert(0, "/app/scripts")
        from _tushare_token import require_token

        import tushare as ts

        pro = ts.pro_api(require_token())
        df = pro.top_list(trade_date=day.replace("-", ""))
        if df is None or df.empty:
            return None
        # 同一只票可能因「日涨幅偏离」「换手率」等多条原因各占一行，
        # 不去重会出现「慈文传媒、慈文传媒」这种重复（第一版实测就是这样）。
        df = df.drop_duplicates(subset=["ts_code"], keep="first")
        if "net_amount" in df.columns:
            df = df.sort_values("net_amount", ascending=False)
            rows = [{"name": str(r.get("name")), "pct": float(r.get("pct_change") or 0),
                     "net_yi": round(float(r.get("net_amount") or 0) / 1e8, 2)}
                    for _, r in df.head(limit).iterrows()]
        else:
            rows = [{"name": str(r.get("name")), "pct": float(r.get("pct_change") or 0),
                     "net_yi": None} for _, r in df.head(limit).iterrows()]
        return {"n": len(df), "rows": rows}
    except Exception as e:  # noqa: BLE001
        logger.info("龙虎榜获取失败：%s", str(e)[:80])
        return None


def _repurchase(day: str, days: int = 5) -> dict | None:
    """近 N 天回购公告汇总（家数 + 金额上限合计）。"""
    try:
        import sys

        sys.path.insert(0, "/app/scripts")
        from _tushare_token import require_token

        import tushare as ts

        pro = ts.pro_api(require_token())
        end = day.replace("-", "")
        start = (date.fromisoformat(day) - timedelta(days=days)).strftime("%Y%m%d")
        df = pro.repurchase(start_date=start, end_date=end)
        if df is None or df.empty:
            return None
        amt = float(df["amount"].fillna(0).sum()) / 1e8 if "amount" in df.columns else None
        return {"n": int(len(df)), "n_codes": int(df["ts_code"].nunique()),
                "amount_yi": round(amt, 2) if amt else None, "days": days}
    except Exception as e:  # noqa: BLE001
        logger.info("回购数据获取失败：%s", str(e)[:80])
        return None


def _etf(db, day: str, prev: str, top: int = 4) -> dict:
    """全市场 ETF 净申赎（`etf_share_market`，约 1720 只）。

    以前这里用的是 `etf_share_daily`（20 只看板样本），算出来 0.01 亿份，
    看着像「没动」，实际全市场根本没看 —— 给一个像结论的残缺数字最糟。
    现在按日拉了全市场（一次调用），直接算「Δ份额 × 收盘价」。

    ⚠️ **份额折算**是唯一的坑：1:2 合并会让 Δ份额腰斩、算出天量假赎回
    （2024-09-20 的 510310 就是）。判据：单只份额变动超过 50% 的一律剔除，
    宁可少算几只，也不要让一个折算把全市场合计数带歪。
    """
    try:
        rows = db.execute(text(
            "select a.ts_code, a.name, a.fd_share, a.close, b.fd_share prev_share"
            " from etf_share_market a join etf_share_market b"
            "   on b.ts_code = a.ts_code and b.trade_date = :p"
            " where a.trade_date = :d and a.close is not null and b.fd_share > 0"
        ), {"p": prev, "d": day}).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.info("ETF 份额读取失败：%s", str(e)[:80])
        return {}

    items = []
    dropped = 0
    for code, name, sh, close, psh in rows:
        try:
            ratio = float(sh) / float(psh) - 1
        except (TypeError, ZeroDivisionError):
            continue
        if abs(ratio) > 0.5:      # 份额折算，剔除
            dropped += 1
            continue
        items.append({"code": code, "name": name,
                      "net_yi": round((float(sh) - float(psh)) * 1e4 * float(close) / 1e8, 2),
                      "chg_pct": round(ratio * 100, 1)})
    if not items:
        return {}
    items.sort(key=lambda x: x["net_yi"], reverse=True)
    total = round(sum(x["net_yi"] for x in items), 1)

    # 近 5 个交易日趋势：单日 +152 亿说明不了什么，得看它是不是扭转了上周的净赎回。
    # 2026-11 实测：11-03 +796 / 11-04 +442 → 11-12 −315 / 11-13 −234 → 11-16 +152，
    # 这个转折比单日数字有信息量得多。
    trend = []
    try:
        rows2 = db.execute(text(
            "select a.trade_date, round(sum((a.fd_share - b.fd_share) * 1e4 * a.close / 1e8), 1)"
            " from etf_share_market a join etf_share_market b"
            "   on b.ts_code = a.ts_code and b.trade_date = ("
            "     select max(trade_date) from etf_share_market where trade_date < a.trade_date)"
            " where a.close is not null and b.fd_share > 0"
            " group by a.trade_date order by a.trade_date desc limit 6"
        )).fetchall()
        trend = [{"date": str(d), "net_yi": float(v)} for d, v in rows2][::-1]
    except Exception as e:  # noqa: BLE001
        logger.info("ETF 申赎趋势读取失败：%s", str(e)[:80])

    return {"n": len(items), "net_yi": total, "dropped": dropped, "trend": trend,
            "top_in": items[:top], "top_out": list(reversed(items[-top:]))}


def build(db, day: str | None = None) -> dict:
    """汇总当日市场快照。任何一项失败都不影响其它项。"""
    day = day or date.today().isoformat()
    prev = _prev_trading_day(db, day)
    out: dict = {"trade_date": day, "prev_date": prev, "missing": list(_MISSING)}
    if not prev:
        out["error"] = f"库里没有 {day} 之前的 K 线，无法计算涨跌"
        return out

    for key, fn in (("index", lambda: _index_pct(db, day, prev)),
                    ("breadth", lambda: _breadth(db, day, prev)),
                    ("sector", lambda: _sector_pct(db, day, prev)),
                    ("etf", lambda: _etf(db, day, prev))):
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001
            logger.info("快照 %s 失败：%s", key, str(e)[:100])
            out[key] = {}

    # 涨跌家数降级：日报 21:45 跑时库里有当天 K 线（19:00 ETL），走库内；
    # 但盘中手动跑日报、或 ETL 还没跑完时，库里没有当天数据 → 退回东财盘中口径。
    if not out.get("breadth"):
        try:
            from app.services.data_source import intraday_breadth

            br = intraday_breadth()
            if br:
                out["breadth"] = {"n": br["up"] + br["down"] + br["flat"],
                                  "up": br["up"], "down": br["down"],
                                  "flat": br["flat"], "turnover_yi": None,
                                  "src": "东财盘中（沪+深，不含北交所）"}
        except Exception as e:  # noqa: BLE001
            logger.info("盘中涨跌家数降级失败：%s", str(e)[:80])

    lim = _limit_counts(day) or _limit_approx(db, day, prev)
    out["limit"] = lim
    out["north"] = _north(day)
    out["top_list"] = _top_list(day)
    out["repurchase"] = _repurchase(day)
    return out


def to_prompt_block(s: dict) -> str:
    """渲染成给模型看的文本块。写清楚「有」和「没有」，避免模型两头瞎猜。"""
    if not s or s.get("error"):
        return "（当日行情快照不可用：" + str((s or {}).get("error") or "未知") + "）"

    lines: list[str] = [f"交易日 {s['trade_date']}（上一交易日 {s['prev_date']}）"]

    idx = s.get("index") or []
    if idx:
        lines.append("指数：" + "、".join(f"{i['name']} {i['pct']:+.2f}%" for i in idx))

    b = s.get("breadth") or {}
    if b:
        lines.append(f"涨跌家数：{b['n']} 只交易，涨 {b['up']} / 跌 {b['down']} / 平 {b['flat']}"
                     f"（涨家占比 {b['up'] / b['n'] * 100:.0f}%）"
                     + (f"［{b['src']}］" if b.get("src") else ""))
        if b.get("turnover_yi"):
            lines.append(f"两市成交额：{b['turnover_yi']} 亿元")

    lim = s.get("limit") or {}
    if lim:
        lines.append(f"涨停 {lim.get('up')} 家 / 跌停 {lim.get('down')} 家（{lim.get('src')}）")

    sec = s.get("sector") or {}
    if sec.get("top"):
        lines.append("行业涨幅前：" + "、".join(f"{x['name']} {x['pct']:+.2f}%" for x in sec["top"]))
        lines.append("行业跌幅前：" + "、".join(f"{x['name']} {x['pct']:+.2f}%" for x in sec["bottom"]))

    n = s.get("north")
    lines.append(f"北向资金：{n['net_yi']:+.2f} 亿元" if n else "北向资金：未取到")

    tl = s.get("top_list")
    if tl and tl.get("rows"):
        lines.append(f"龙虎榜：{tl['n']} 条，净买入靠前 "
                     + "、".join(f"{r['name']}({r['pct']:+.1f}%"
                                + (f", 净买入 {r['net_yi']} 亿" if r.get("net_yi") else "") + ")"
                                for r in tl["rows"][:6]))
    else:
        lines.append("龙虎榜：未取到")

    rp = s.get("repurchase")
    if rp:
        lines.append(f"回购公告（近 {rp['days']} 日）：{rp['n']} 条 / {rp['n_codes']} 家公司"
                     + (f"，拟回购金额上限合计 {rp['amount_yi']} 亿元" if rp.get("amount_yi") else ""))
    else:
        lines.append("回购公告：未取到")

    e = s.get("etf") or {}
    if e.get("n"):
        lines.append(f"ETF 净申赎（全市场 {e['n']} 只）：当日 {e['net_yi']:+.1f} 亿元"
                     + (f"（{e['dropped']} 只因份额折算剔出）" if e.get("dropped") else ""))
        tr = e.get("trend") or []
        if len(tr) >= 2:
            lines.append("  近 %d 个交易日：" % len(tr)
                         + " → ".join(f"{x['date'][5:]} {x['net_yi']:+.0f}亿" for x in tr))
        if e.get("top_in"):
            lines.append("  净申购靠前：" + "、".join(
                f"{x['name']} {x['net_yi']:+.1f} 亿" for x in e["top_in"]))
        if e.get("top_out"):
            lines.append("  净赎回靠前：" + "、".join(
                f"{x['name']} {x['net_yi']:+.1f} 亿" for x in e["top_out"]))
    else:
        lines.append("ETF 净申赎：未取到")

    lines.append("⚠️ 本快照**没有**的数据（不要推测，也不要写成「看不到」就停）："
                 + "；".join(s.get("missing") or []))
    lines.append("以上数字是权威口径，写复盘时**直接用**，不要再从资讯正文里二次推断行情。")
    return "\n".join(lines)
