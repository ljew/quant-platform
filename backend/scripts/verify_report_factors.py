"""研报因子的三种构造方式对比：评级 / 覆盖热度 / EPS 修正。

## 为什么要试三种

数据实际情况和预期不一样，必须先探明哪条路有信号：

1. **评级是单边乐观的**：88,013 篇里「买入」74%、「增持」20%、「卖出」只有 3 篇。
   A 股卖方几乎不写卖出，所以评级的绝对水平没有区分度 —— 只能看**边际变化**
   （某只票从增持转买入，比一直是买入更有信息）。
2. **覆盖热度**：研报数量上升 = 机构关注度上升。A 股关注度因子通常有效，
   而且这条对数据要求最低。
3. **EPS 修正**：本以为是最强因子，但东财的 EPS 预测字段 **2025 年才有**
   （2022-2024 覆盖率 0%），样本只有 22 个月，不够下结论。

统一用：近 90 天 vs 前 90 天，月末截面，对未来 20 交易日收益做 rank IC。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal  # noqa: E402

RATING_MAP = {"买入": 2.0, "增持": 1.0, "持有": 0.0, "中性": 0.0, "减持": -1.0, "卖出": -1.0}


def _to_sym(c: str) -> str:
    c = str(c).zfill(6)
    if c.startswith("6"):
        return "sh" + c
    if c.startswith(("0", "3")):
        return "sz" + c
    return ""


def load(db, start: str, end: str) -> pd.DataFrame:
    rows = db.execute(text(
        "select stock_code, publish_date, rating, eps_this, eps_next "
        "from research_report where publish_date between :a and :b"
    ), {"a": start, "b": end}).fetchall()
    df = pd.DataFrame(rows, columns=["code", "date", "rating", "eps_this", "eps_next"])
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["code"].map(_to_sym)
    df = df[df["symbol"] != ""]
    df["rscore"] = df["rating"].map(RATING_MAP)
    # 滚动 12 个月前瞻 EPS：按已过月份加权，避免跨财年跳变
    w = (df["date"].dt.month - 1) / 12.0
    df["fwd12"] = (1 - w) * df["eps_this"] + w * df["eps_next"]
    df.loc[df["fwd12"] <= 0, "fwd12"] = np.nan
    return df


def build(df: pd.DataFrame, month_ends: list[pd.Timestamp],
          lookback: int = 90, gap: int = 90) -> pd.DataFrame:
    """一个月末一行：每只股票三个因子值。"""
    out = []
    for t in month_ends:
        rec = df[(df["date"] > t - pd.Timedelta(days=lookback)) & (df["date"] <= t)]
        base = df[(df["date"] > t - pd.Timedelta(days=lookback + gap))
                  & (df["date"] <= t - pd.Timedelta(days=lookback))]
        if rec.empty or base.empty:
            continue

        n_rec = rec.groupby("symbol").size()
        n_base = base.groupby("symbol").size()
        # ① 覆盖热度：log 比值（抗基数差异）
        cov = np.log1p(n_rec) - np.log1p(n_base.reindex(n_rec.index).fillna(0))

        # ② 评级边际：均值差（只在有评级的样本上）
        r_rec = rec.dropna(subset=["rscore"]).groupby("symbol")["rscore"].mean()
        r_base = base.dropna(subset=["rscore"]).groupby("symbol")["rscore"].mean()
        rat = (r_rec - r_base.reindex(r_rec.index)).reindex(n_rec.index)

        # ③ EPS 修正：fwd12 中位数比值
        e_rec = rec.dropna(subset=["fwd12"]).groupby("symbol")["fwd12"].median()
        e_base = base.dropna(subset=["fwd12"]).groupby("symbol")["fwd12"].median()
        common = e_rec.index.intersection(e_base.index)
        eps = (e_rec[common] / e_base[common] - 1.0).reindex(n_rec.index)

        panel = pd.DataFrame({"cov": cov, "rat": rat, "eps": eps})
        panel = panel[n_rec >= 2]
        panel["date"] = t
        panel["n_rec"] = n_rec.reindex(panel.index)
        out.append(panel.reset_index())
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def forward_returns(db, symbols: list[str], fwd_days: int = 20) -> pd.DataFrame:
    ph = ",".join(f":s{i}" for i in range(len(symbols)))
    params = {f"s{i}": s for i, s in enumerate(symbols)}
    rows = db.execute(text(
        f"select symbol, trade_date, close from kline_daily where symbol in ({ph}) "
        f"order by symbol, trade_date"), params).fetchall()
    px = pd.DataFrame(rows, columns=["symbol", "date", "close"])
    if px.empty:
        return px
    px["date"] = pd.to_datetime(px["date"])
    px = px.sort_values(["symbol", "date"])
    px["fwd"] = px.groupby("symbol")["close"].shift(-fwd_days) / px["close"] - 1.0
    return px[["symbol", "date", "fwd"]]


def report(merged: pd.DataFrame, col: str, label: str) -> None:
    sub = merged.dropna(subset=[col, "fwd"])
    if sub.empty:
        print(f"\n【{label}】无样本")
        return
    ics = []
    for t, g in sub.groupby("date"):
        if len(g) < 20:
            continue
        ic = g[col].rank().corr(g["fwd"].rank())
        if ic is not None and not np.isnan(ic):
            ics.append(float(ic))
    if not ics:
        print(f"\n【{label}】样本不足")
        return
    arr = np.array(ics)
    mean, std = arr.mean(), arr.std(ddof=1)
    t = mean / (std / np.sqrt(len(arr))) if std > 0 else 0.0

    sub = sub.copy()
    sub["q"] = sub.groupby("date")[col].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
    g = sub.groupby("q")["fwd"].mean()
    ls = (g.get(4, np.nan) - g.get(0, np.nan)) if len(g) >= 5 else np.nan

    print(f"\n【{label}】样本 {len(sub)} 行 / {sub['date'].nunique()} 个月")
    print(f"  均值IC {mean:+.4f} | ICIR {mean/std if std else 0:+.3f} | "
          f"t={t:+.2f} | 胜率 {(arr > 0).mean():.0%}")
    print(f"  分组收益 " + "  ".join(f"Q{int(k)+1}:{v:+.2%}" for k, v in g.items()))
    print(f"  多空 Q5-Q1 {ls:+.3%}" if not np.isnan(ls) else "")


def main() -> int:
    db = SessionLocal()
    try:
        r = db.execute(text(
            "select min(publish_date), max(publish_date), count(*) from research_report"
        )).fetchone()
        print(f"研报：{r[0]} ~ {r[1]}，{r[2]} 篇")
        df = load(db, str(r[0]), str(r[1]))

        months = pd.date_range("2022-07-01", str(r[1])[:10], freq="ME")
        ends = sorted({df[df["date"] <= m]["date"].max() for m in months} - {pd.NaT})
        print(f"月末截面 {len(ends)} 个：{ends[0].date()} ~ {ends[-1].date()}")

        panel = build(df, ends)
        print(f"因子面板 {len(panel)} 行 / {panel['symbol'].nunique()} 只股票")

        px = forward_returns(db, panel["symbol"].unique().tolist())
        merged = panel.merge(px, on=["symbol", "date"], how="inner")
        print(f"可合并样本 {len(merged)} 行")

        report(merged, "cov", "① 覆盖热度（研报数 log 变化）")
        report(merged, "rat", "② 评级边际（买入2/增持1/中性0 均值差）")
        report(merged, "eps", "③ EPS 修正（fwd12 中位数变化）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
