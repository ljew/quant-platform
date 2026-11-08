"""验证券商「盈利预测修正」因子对 A 股的预测力。

## 因子定义

分析师上调盈利预测是 A 股最强的单因子之一（比任何技术指标更早反映基本面）。

难点：`eps_this` / `eps_next` 是**财年标签**而不是「未来 12 个月」，直接跨年比较
会跳变（2024-12 的 eps_this 是 2024 年，2025-01 的 eps_this 变成 2025 年，
中间没有任何预测变化，差值却是整年的增长）。

解法：按已过月份加权插值出**滚动 12 个月前瞻 EPS**：

    w = (month - 1) / 12
    fwd12 = (1 - w) * eps_this + w * eps_next

    2024-01 → w=0      → 全年看 2024，即 eps_this
    2024-06 → w=0.417  → 0.58*2024 + 0.42*2025
    2024-12 → w=0.917  → 0.08*2024 + 0.92*2025，平滑过渡到下一财年

再算修正：

    revision(t) = median(fwd12 近 90 天) / median(fwd12 前 90~180 天) - 1

用中位数抗单家机构的极端预测。

## 验证

每月末取因子值，对未来 20 个交易日收益做 Spearman IC，并做五分组看单调性。
⚠️ 只算 IC 不做判断 —— 单因子 IC 在 0.02~0.05 就算有效，但要看过不过 t 检验。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal  # noqa: E402


def load_reports(db, start: str, end: str) -> pd.DataFrame:
    rows = db.execute(text(
        "select stock_code, publish_date, eps_this, eps_next from research_report "
        "where publish_date between :a and :b "
        "and eps_this is not null and eps_next is not null"
    ), {"a": start, "b": end}).fetchall()
    df = pd.DataFrame(rows, columns=["code", "date", "eps_this", "eps_next"])
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df["month"] = df["date"].dt.month
    w = (df["month"] - 1) / 12.0
    df["fwd12"] = (1 - w) * df["eps_this"] + w * df["eps_next"]
    # 只保留正数：亏损股的 EPS 为负，比值没有意义
    df = df[df["fwd12"] > 0]
    # 纯 6 位代码 → 带前缀（6=沪, 0/3=深）
    def to_sym(c: str) -> str:
        c = str(c).zfill(6)
        if c.startswith("6"):
            return "sh" + c
        if c.startswith(("0", "3")):
            return "sz" + c
        return ""
    df["symbol"] = df["code"].map(to_sym)
    return df[df["symbol"] != ""]


def revision_panel(df: pd.DataFrame, month_ends: list[pd.Timestamp],
                   lookback: int = 90, gap: int = 90) -> pd.DataFrame:
    """每个月末算一次截面因子值。"""
    out = []
    for t in month_ends:
        recent = df[(df["date"] > t - pd.Timedelta(days=lookback)) & (df["date"] <= t)]
        base = df[(df["date"] > t - pd.Timedelta(days=lookback + gap))
                  & (df["date"] <= t - pd.Timedelta(days=lookback))]
        if recent.empty or base.empty:
            continue
        r = recent.groupby("symbol")["fwd12"].median()
        b = base.groupby("symbol")["fwd12"].median()
        both = pd.concat([r.rename("recent"), b.rename("base")], axis=1).dropna()
        both = both[both["base"] > 0]
        if both.empty:
            continue
        both["rev"] = both["recent"] / both["base"] - 1.0
        # 覆盖太少的样本噪音大：只有 1 家机构看过，改个预测就翻天。
        # 要求近 90 天至少 2 篇研报（含基准窗口的覆盖则不受限）。
        cnt = recent.groupby("symbol").size()
        both["n_recent"] = cnt.reindex(both.index).fillna(0).astype(int)
        both = both[both["n_recent"] >= 2]
        # 截尾：单家机构的极端上调（如 10 倍）会主导截面
        lo, hi = both["rev"].quantile([0.01, 0.99])
        both["rev"] = both["rev"].clip(lo, hi)
        both["date"] = t
        out.append(both.reset_index())
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def forward_returns(db, symbols: list[str], month_ends: list[pd.Timestamp],
                    fwd_days: int = 20) -> pd.DataFrame:
    """从 kline_daily 直接算：月末收盘 → 20 个交易日后收盘。"""
    if not symbols:
        return pd.DataFrame()
    ph = ",".join(f":s{i}" for i in range(len(symbols)))
    params = {f"s{i}": s for i, s in enumerate(symbols)}
    rows = db.execute(text(
        f"select symbol, trade_date, close from kline_daily "
        f"where symbol in ({ph}) order by symbol, trade_date"
    ), params).fetchall()
    px = pd.DataFrame(rows, columns=["symbol", "date", "close"])
    if px.empty:
        return px
    px["date"] = pd.to_datetime(px["date"])
    px = px.sort_values(["symbol", "date"])
    px["fwd"] = px.groupby("symbol")["close"].shift(-fwd_days) / px["close"] - 1.0
    return px


def main() -> int:
    db = SessionLocal()
    try:
        r = db.execute(text(
            "select min(publish_date), max(publish_date), count(*) from research_report"
        )).fetchone()
        print(f"研报：{r[0]} ~ {r[1]}，{r[2]} 篇")
        if r[2] < 5000:
            print("⚠️ 数据太少，先跑 backfill_research_report.py")
            return 1

        df = load_reports(db, str(r[0]), str(r[1]))
        print(f"有效预测样本（fwd12>0）：{len(df)} 条，{df['symbol'].nunique()} 只股票")

        # 月末截面（2022-07 起，留半年给基准窗口）
        months = pd.date_range("2022-07-01", str(r[1])[:10], freq="ME")
        month_ends = []
        for m in months:
            # 取该月末前最后一个有研报的日期
            cand = df[df["date"] <= m]["date"]
            if cand.empty:
                continue
            month_ends.append(cand.max())
        month_ends = sorted(set(month_ends))
        print(f"截面月份：{len(month_ends)} 个（{month_ends[0].date()} 起）")

        panel = revision_panel(df, month_ends)
        if panel.empty:
            print("因子面板为空")
            return 1
        print(f"因子面板：{len(panel)} 行，{panel['symbol'].nunique()} 只股票")

        px = forward_returns(db, panel["symbol"].unique().tolist(), month_ends)
        merged = panel.merge(px, on=["symbol", "date"], how="inner")
        merged = merged.dropna(subset=["rev", "fwd"])
        print(f"可用样本：{len(merged)} 行\n")

        # —— IC 时间序列 ——
        ics = []
        for t, g in merged.groupby("date"):
            if len(g) < 20:
                continue
            # 容器里没有 scipy，pandas 的 method="spearman" 内部也要它。
            # Spearman = 两列各自取秩后的 Pearson，自己算等价且不引依赖。
            ic = g["rev"].rank().corr(g["fwd"].rank())
            if ic is not None and not np.isnan(ic):
                ics.append({"date": t, "ic": float(ic), "n": len(g)})
        ic_df = pd.DataFrame(ics)
        if ic_df.empty:
            print("样本不足，算不出 IC")
            return 1

        mean_ic = ic_df["ic"].mean()
        std_ic = ic_df["ic"].std(ddof=1)
        t_stat = mean_ic / (std_ic / np.sqrt(len(ic_df))) if std_ic > 0 else 0.0
        print("=== IC（未来 20 交易日）===")
        print(f"  均值 IC   {mean_ic:+.4f}")
        print(f"  标准差    {std_ic:.4f}")
        print(f"  ICIR      {mean_ic / std_ic:+.3f}" if std_ic > 0 else "  ICIR n/a")
        print(f"  t 值      {t_stat:+.2f}   （|t|>2 才算显著）")
        print(f"  胜率      {(ic_df['ic'] > 0).mean():.1%}")
        print(f"  样本月数  {len(ic_df)}")

        # —— 五分组 ——
        print("\n=== 五分组平均未来 20 日收益 ===")
        merged["grp"] = merged.groupby("date")["rev"].transform(
            lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
        g = merged.groupby("grp")["fwd"].agg(["mean", "count"])
        for i, row in g.iterrows():
            print(f"  Q{int(i) + 1}  {row['mean']:+.3%}   样本 {int(row['count'])}")
        if len(g) >= 5:
            long_short = g.loc[4, "mean"] - g.loc[0, "mean"]
            print(f"  多空（Q5-Q1）{long_short:+.3%}")

        ic_df.to_csv("/tmp/eps_revision_ic.csv", index=False)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
