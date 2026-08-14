"""种子脚本：用 akshare 业绩报表批量入库质量/成长基本面（ROE / 营收同比 / 利润同比）。

数据源：akshare.stock_yjbb_em(date=报告期) —— 一次调用返回全市场业绩报表，含：
    - 净资产收益率(%)、营业总收入-同比增长(%)、净利润-同比增长(%)
存储在 stocks 表的 roe / revenue_yoy / profit_yoy 字段，供因子库的质量/成长因子使用。

用法（在 backend 目录，PYTHONPATH=$(pwd)）：
    python scripts/seed_fundamentals.py                 # 自动挑选最近一个有数据的报告期
    python scripts/seed_fundamentals.py --date 20251231 # 指定报告期
    python scripts/seed_fundamentals.py --dry-run       # 仅打印将写入的字段，不入库

说明：tushare 无 token 时改用 akshare（无需鉴权）。本脚本只更新基本面快照，
与现有 pe_ttm/pb/market_cap 同为「当前截面」口径（非时点时间序列），保持数据一致性。
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import date as _date

from sqlalchemy import select, update

from app.database import SessionLocal
from app.models import Stock, FundamentalsHistory
from app.services.data_source import normalize_symbol

# akshare 业绩报表列名 -> (Stock 字段, 是否百分比)
COL_MAP = {
    "净资产收益率": ("roe", True),
    "营业总收入-同比增长": ("revenue_yoy", True),
    "净利润-同比增长": ("profit_yoy", True),
}
CODE_COL = "股票代码"
REPORT_COL = "报告期"

# 自动模式按此顺序挑选最近一个有数据的报告期
DEFAULT_DATES = [
    "20251231", "20250930", "20250630",
    "20241231", "20240630", "20231231", "20230630",
]

# 历史快照模式：默认拉取近 5 个年报（用于 PEAD 时序因子）
HISTORY_YEARS = [2021, 2022, 2023, 2024, 2025]


def _clean(v):
    """把 akshare 返回的 NaN/None 规范为 float 或 None。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def fetch_report(date: str):
    """返回 (列名映射确认, [(symbol, {field: value})]) 或 None（无数据）。"""
    import akshare as ak
    df = ak.stock_yjbb_em(date=date)
    if df is None or df.empty:
        return None
    cols = list(df.columns)
    code_present = CODE_COL in cols
    if not code_present:
        # 退回按列名模糊匹配代码列
        code_candidates = [c for c in cols if "代码" in c]
        if not code_candidates:
            print(f"  [{date}] 缺少股票代码列，跳过", file=sys.stderr)
            return None
        code_col = code_candidates[0]
    else:
        code_col = CODE_COL

    missing = [c for c in COL_MAP if c not in cols]
    if missing:
        print(f"  [{date}] 缺少列 {missing}，可用列: {cols}", file=sys.stderr)
        return None

    rows = []
    for _, row in df.iterrows():
        raw = str(row[code_col]).strip()
        if not raw or raw.lower() == "nan":
            continue
        # 去掉可能的交易所前缀/后缀，仅留 6 位数字
        digits = "".join(ch for ch in raw if ch.isdigit())[-6:]
        if len(digits) != 6:
            continue
        sym, _ = normalize_symbol(digits)
        vals = {}
        for col, (field, _pct) in COL_MAP.items():
            vals[field] = _clean(row[col])
        rows.append((sym, vals))
    return rows


def _parse_report_date(v):
    """把 akshare 报告期解析为 date：支持 '20251231' / '2025-12-31' / Timestamp。"""
    if v is None:
        return None
    if hasattr(v, "year"):  # Timestamp / date
        return _date(v.year, v.month, v.day)
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8:
        try:
            return _date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            return None
    return None


def fetch_history_report(date: str):
    """拉取某报告期全市场业绩报表，返回 [(symbol, {roe,revenue_yoy,profit_yoy}, report_date)]。

    与 fetch_report 区别：额外解析报告期(report_date)，用于写入 fundamentals_history 时序表。
    """
    import akshare as ak
    df = ak.stock_yjbb_em(date=date)
    if df is None or df.empty:
        return None
    cols = list(df.columns)
    code_col = CODE_COL if CODE_COL in cols else next((c for c in cols if "代码" in c), None)
    if code_col is None:
        return None
    rep_col = REPORT_COL if REPORT_COL in cols else None
    missing = [c for c in COL_MAP if c not in cols]
    if missing:
        print(f"  [{date}] 缺少列 {missing}", file=sys.stderr)
        return None

    rows = []
    for _, row in df.iterrows():
        raw = str(row[code_col]).strip()
        if not raw or raw.lower() == "nan":
            continue
        digits = "".join(ch for ch in raw if ch.isdigit())[-6:]
        if len(digits) != 6:
            continue
        sym, _ = normalize_symbol(digits)
        vals = {field: _clean(row[col]) for col, (field, _pct) in COL_MAP.items()}
        rd = _parse_report_date(row[rep_col]) if rep_col else _parse_report_date(date)
        if rd is None:
            continue
        rows.append((sym, vals, rd))
    return rows


def seed_history(dates, dry_run: bool = False):
    """多报告期入库到 fundamentals_history（PEAD 时序因子用）。"""
    db = SessionLocal()
    total = 0
    try:
        for d in dates:
            print(f"拉取历史报告期 {d} ...")
            try:
                rows = fetch_history_report(d)
            except Exception as e:
                print(f"  失败: {e}", file=sys.stderr)
                continue
            if not rows:
                print(f"  {d} 无数据，跳过")
                continue
            # 同 (symbol, report_date) 去重，保留最后一条
            seen: dict = {}
            for sym, vals, rd in rows:
                seen[(sym, rd)] = vals
            for (sym, rd), vals in seen.items():
                if dry_run:
                    print(f"  {sym} {rd}: {vals}")
                    continue
                existing = db.execute(
                    select(FundamentalsHistory.id).where(
                        FundamentalsHistory.symbol == sym,
                        FundamentalsHistory.report_date == rd,
                    )
                ).scalar()
                if existing:
                    db.execute(
                        update(FundamentalsHistory).where(
                            FundamentalsHistory.id == existing
                        ).values(**vals)
                    )
                else:
                    db.add(FundamentalsHistory(symbol=sym, report_date=rd, **vals))
                total += 1
            if not dry_run:
                db.commit()
                print(f"  {d} 已并入（累计 {total} 条）")
    finally:
        db.close()
    print(f"历史快照入库完成，累计 {total} 条记录。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="指定报告期 YYYYMMDD，缺省自动挑选")
    ap.add_argument("--dry-run", action="store_true", help="仅预览，不写库")
    ap.add_argument("--history", action="store_true",
                    help="种子模式：拉取多年报写入 fundamentals_history（PEAD 时序因子）")
    ap.add_argument("--years", default=",".join(str(y) for y in HISTORY_YEARS),
                    help="--history 模式下的年份列表，逗号分隔，如 2021,2022,2023,2024,2025")
    args = ap.parse_args()

    if args.history:
        years = [int(y) for y in args.years.split(",") if y.strip()]
        dates = [f"{y}1231" for y in years]
        seed_history(dates, dry_run=args.dry_run)
        return

    dates = [args.date] if args.date else DEFAULT_DATES
    rows = None
    used_date = None
    for d in dates:
        print(f"尝试拉取报告期 {d} ...")
        try:
            rows = fetch_report(d)
        except Exception as e:
            print(f"  失败: {e}", file=sys.stderr)
            rows = None
        if rows is not None:
            used_date = d
            break
    if rows is None:
        print("所有报告期均无数据，退出。", file=sys.stderr)
        sys.exit(1)
    print(f"报告期 {used_date} 拉取成功，共 {len(rows)} 行。")

    db = SessionLocal()
    try:
        # 建立 symbol -> Stock.id 映射
        sym_to_id = {s.symbol: s.id for s in db.execute(select(Stock.symbol, Stock.id)).all()}
        updated = 0
        for sym, vals in rows:
            sid = sym_to_id.get(sym)
            if sid is None:
                continue
            if args.dry_run:
                print(f"  {sym}: {vals}")
                continue
            db.execute(
                update(Stock).where(Stock.id == sid).values(
                    roe=vals["roe"], revenue_yoy=vals["revenue_yoy"], profit_yoy=vals["profit_yoy"]
                )
            )
            updated += 1
        if not args.dry_run:
            db.commit()
            print(f"已更新 {updated} 只标的的基本面字段（报告期 {used_date}）。")
        else:
            print(f"[dry-run] 本应更新 {updated} 只标的。")
    finally:
        db.close()


if __name__ == "__main__":
    main()
    from app.services.duckdb_sync import sync_after_seed

    sync_after_seed(["fundamentals_history"])
