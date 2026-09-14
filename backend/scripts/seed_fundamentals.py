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

# 各报告期的法定披露截止日（月, 日）；年报次年 4 月底才披露完
_PERIOD_DEADLINE = {"0331": (4, 30), "0630": (8, 31), "0930": (10, 31), "1231": (4, 30)}
_PERIOD_OF_QUARTER = {1: "0331", 2: "0630", 3: "0930", 4: "1231"}


def _published_periods(n: int = 12, today: _date | None = None) -> list[str]:
    """按当前日期倒推最近 n 个「已过披露截止日」的报告期，最新在前。

    曾经把候选报告期写死成常量列表（DEFAULT_DATES），结果 2026 一季报/中报
    披露完毕后脚本仍在用 2025 年报 —— 基本面因子默默用了一年多前的旧数据。
    因此改为按日历动态推算：只返回已到披露截止日的报告期。
    """
    today = today or _date.today()
    out: list[str] = []
    y, q = today.year, (today.month - 1) // 3 + 1
    for _ in range(n + 8):
        period = _PERIOD_OF_QUARTER[q]
        mm, dd = _PERIOD_DEADLINE[period]
        deadline_year = y + 1 if period == "1231" else y   # 年报次年披露
        if _date(deadline_year, mm, dd) <= today:
            out.append(f"{y}{period}")
            if len(out) >= n:
                break
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    return out


# 历史快照模式：默认回填的期数（季报口径，约 3 年）
HISTORY_PERIODS = 12


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


def seed_history(dates, dry_run: bool = False, only_known: bool = True):
    """多报告期入库到 fundamentals_history（PEAD 时序因子用）。

    only_known=True 时只保留 stocks 表中存在的标的 —— akshare 业绩报表会返回
    B 股/退市股等约 11500 个代码，而实际可研究标的只有 5539 只，全量写入会让
    fundamentals_history 的「覆盖标的」虚高一倍，失真且无用途。
    """
    db = SessionLocal()
    known = None
    if only_known:
        known = {s for s, in db.execute(select(Stock.symbol)).all()}
        print(f"仅保留 stocks 表内标的（{len(known)} 只）")
    total = 0
    skipped = 0
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
                if known is not None and sym not in known:
                    skipped += 1
                    continue
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
    print(f"历史快照入库完成，累计 {total} 条记录"
          + (f"（已过滤 {skipped} 条非 stocks 表标的）" if skipped else "") + "。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="指定报告期 YYYYMMDD，缺省自动挑选")
    ap.add_argument("--dry-run", action="store_true", help="仅预览，不写库")
    ap.add_argument("--history", action="store_true",
                    help="种子模式：按季报口径回填多个报告期写入 fundamentals_history（PEAD 时序因子）")
    ap.add_argument("--periods", type=int, default=HISTORY_PERIODS,
                    help="--history 模式下回填的报告期数量（季报口径，默认 12 ≈ 3 年）")
    ap.add_argument("--all-symbols", action="store_true",
                    help="--history 模式下不按 stocks 表过滤（保留 B 股/退市股等全部代码）")
    args = ap.parse_args()

    if args.history:
        dates = _published_periods(args.periods)
        if not dates:
            print("没有已到披露截止日的报告期，退出。", file=sys.stderr)
            sys.exit(1)
        print(f"待回填报告期（{len(dates)} 期，最新在前）: {', '.join(dates)}")
        seed_history(dates, dry_run=args.dry_run, only_known=not args.all_symbols)
        return

    dates = [args.date] if args.date else _published_periods(4)
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
