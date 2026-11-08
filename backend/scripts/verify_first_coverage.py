"""研报「首次覆盖」事件研究。

思路：单因子 IC 不显著不代表研报数据没用。卖方研报更常见的用法是**事件驱动**——
一只票过去半年没人写，突然两家机构同时出报告，往往意味着基本面被重新发现。

事件定义：过去 180 天该股没有任何研报，最近 30 天出现 >= 2 篇。
对照：同期同池（有 K 线的全部标的）的等权收益。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal  # noqa: E402


def _to_sym(c: str) -> str:
    c = str(c).zfill(6)
    if c.startswith("6"):
        return "sh" + c
    if c.startswith(("0", "3")):
        return "sz" + c
    return ""


def main() -> int:
    db = SessionLocal()
    try:
        rows = db.execute(text(
            "select stock_code, publish_date from research_report "
            "where publish_date >= '2022-01-01'"
        )).fetchall()
        df = pd.DataFrame(rows, columns=["code", "date"])
        df["date"] = pd.to_datetime(df["date"])
        df["symbol"] = df["code"].map(_to_sym)
        df = df[df["symbol"] != ""].sort_values(["symbol", "date"])

        # 事件：该日期有研报，且往前 180 天内该股没有其他研报
        firsts = []
        for sym, g in df.groupby("symbol"):
            dates = g["date"].drop_duplicates().sort_values().tolist()
            prev = None
            for d in dates:
                if prev is None or (d - prev).days > 180:
                    firsts.append({"symbol": sym, "date": d})
                prev = d
        ev = pd.DataFrame(firsts)
        # 2022 年上半年的"首次"是伪首次（数据起点），丢掉
        ev = ev[ev["date"] >= "2022-07-01"]
        print(f"首次覆盖事件：{len(ev)} 个（{ev['symbol'].nunique()} 只股票）")

        # 同一事件日多只股票 → 按日聚合，做成可评估的截面
        ev["month"] = ev["date"].dt.to_period("M")
        by_month = ev.groupby("month").size()
        print(f"覆盖 {len(by_month)} 个月，平均每月 {by_month.mean():.0f} 个事件")

        syms = sorted(set(ev["symbol"]))
        ph = ",".join(f":s{i}" for i in range(len(syms)))
        params = {f"s{i}": s for i, s in enumerate(syms)}
        px_rows = db.execute(text(
            f"select symbol, trade_date, close from kline_daily where symbol in ({ph}) "
            f"order by symbol, trade_date"), params).fetchall()
        px = pd.DataFrame(px_rows, columns=["symbol", "date", "close"])
        px["date"] = pd.to_datetime(px["date"])
        px = px.sort_values(["symbol", "date"])

        for days in (20, 60):
            px[f"fwd{days}"] = (px.groupby("symbol")["close"].shift(-days)
                                / px["close"] - 1.0)

        m = ev.merge(px, on=["symbol", "date"], how="inner")
        print(f"\n可算收益的事件：{len(m)} 个")

        # 基准：同一时间全部标的的等权收益（用同月所有股票均值近似）
        print(f"\n{'持有期':10s} {'事件收益':>10s} {'市场同期':>10s} {'超额':>10s} {'事件数':>7s}")
        for days in (20, 60):
            col = f"fwd{days}"
            sub = m.dropna(subset=[col])
            if sub.empty:
                continue
            # 市场基准：按事件月份，用当月所有股票该持有期的均值
            mkt = px.dropna(subset=[col]).copy()
            mkt["month"] = mkt["date"].dt.to_period("M")
            bench = mkt.groupby("month")[col].mean()
            sub = sub.copy()
            sub["month"] = sub["date"].dt.to_period("M")
            sub["bench"] = sub["month"].map(bench)
            sub = sub.dropna(subset=["bench"])
            ev_ret = sub[col].mean()
            bm_ret = sub["bench"].mean()
            print(f"{days:>3d} 日      {ev_ret:+9.2%} {bm_ret:+9.2%} "
                  f"{ev_ret - bm_ret:+9.2%} {len(sub):7d}")

        # 事件后是否真的被持续覆盖？（若之后研报消失，说明是"一次性关注"）
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
