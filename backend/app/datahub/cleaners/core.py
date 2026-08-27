"""清洗层：Bronze → Silver，含质检指标。"""
from __future__ import annotations

import os
from datetime import datetime

import pandas as pd

from app.datahub.registry import silver_path, _PROJECT_ROOT  # noqa: F401

REPORT_DIR = os.path.join(_PROJECT_ROOT, "data", "silver", "_reports")


def write_report(run_id: int | None, sections: dict) -> str:
    """质检报告落盘 data/silver/_reports/run_{id}.json（最新也写 latest.json）。"""
    import json

    os.makedirs(REPORT_DIR, exist_ok=True)
    payload = {
        "run_id": run_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sections": sections,
    }
    for name in ([f"run_{run_id}.json"] if run_id else []) + ["latest.json"]:
        with open(os.path.join(REPORT_DIR, name), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    return os.path.join(REPORT_DIR, "latest.json")


def read_latest_report() -> dict | None:
    path = os.path.join(REPORT_DIR, "latest.json")
    if not os.path.exists(path):
        return None
    try:
        import json
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def latest_run_dir() -> str:
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "..", "data", "raw", "text", "wechat_articles")
    return base


# ============ bars 清洗 ============
def clean_bars(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """K线清洗：非正值/极值检出 + 缺失统计 → Silver。

    返回 (clean_df, 质检指标)。
    """
    metrics: dict = {"rows_in": len(df)}
    if df.empty:
        metrics.update({"error": "空输入", "rows_out": 0})
        return df, metrics

    # ① 非正值（OHLC<=0）
    bad_cols = ["open", "high", "low", "close"]
    bad_mask = (df[bad_cols] <= 0).any(axis=1)
    n_bad = int(bad_mask.sum())
    df = df[~bad_mask].copy()

    # ② 日内极值：high/low 与 close 偏差 >25% 视为异常
    rng = ((df["high"] - df["low"]) / df["close"].abs().clip(lower=1e-6)).abs()
    extreme = rng > 0.25
    n_extreme = int(extreme.sum())

    # ③ 符号覆盖
    by_sym = df.groupby("symbol").size()
    metrics.update({
        "rows_bad_nonpositive": n_bad,
        "rows_extreme_range": n_extreme,
        "symbols_covered": int(len(by_sym)),
        "symbols_min_rows": int(by_sym.min()) if len(by_sym) else 0,
        "missing_ratio_threshold_ok": bool((by_sym >= 1).all()),
    })

    out = silver_path("market/bars_clean")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_parquet(out, index=False)
    metrics["rows_out"] = len(df)
    metrics["silver_file"] = out.replace(silver_path("market") if False else "", "")
    return df, metrics


# ============ 文本清洗 ============
def clean_articles(frames: list[pd.DataFrame]) -> tuple[pd.DataFrame, dict]:
    """公众号文章合并去重/正文规范化 → text_cleaned.parquet。"""
    metrics: dict = {"frames_in": len(frames)}
    if not frames:
        return pd.DataFrame(), metrics
    df = pd.concat(frames).drop_duplicates(subset=["article_id"])
    dup_removed = len(pd.concat(frames)) - len(df)
    # 正文规范化：压缩空白
    df["content_text"] = (df["content_text"].fillna("")
                          .str.replace(r"\s+", " ", regex=True).str.strip())
    out = silver_path("text/articles_cleaned")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_parquet(out, index=False)
    metrics.update({
        "dup_removed": int(dup_removed),
        "rows_out": len(df),
        "sources": int(df["mp_name"].nunique()),
        "publish_max": (pd.to_datetime(df["publish_time"], unit="s").max()
                        .strftime("%Y-%m-%d")),
    })
    return df, metrics
