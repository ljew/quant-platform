"""预拉取任意指数成分股日K（前复权）入库。

用途：指数增强回测需要全股票池的历史数据。直接回测时若本地无数据会逐只回源，
较慢；本脚本把指定指数的成分股日K批量拉取并落地到本地库（断点续传、并发加速）。

用法：
    python scripts/seed_index.py --index 000906          # 中证800（默认）
    python scripts/seed_index.py --index 000300          # 沪深300
    python scripts/seed_index.py --index 000300 --since 2023 --workers 8
    python scripts/seed_index.py --index 000300 --limit 60   # 仅前 N 只（验证用）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

# 项目路径
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("QUANT_DATABASE_URL",
                      f"sqlite:///{os.path.join(ROOT, 'data', 'quant_dev.db')}")

from app.database import SessionLocal  # noqa: E402
from app.services import data_source, ingestion  # noqa: E402
from app.models import KlineDaily  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("seed_index")


def save_constituents_json(constituents, path):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(constituents, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"保存成分股种子文件失败: {e}")


def worker(symbol: str, since: int):
    db = SessionLocal()
    try:
        Kline = KlineDaily
        latest = db.query(Kline).filter(
            Kline.symbol == symbol, Kline.adj == "qfq",
        ).order_by(Kline.trade_date.desc()).first()
        if latest and latest.trade_date >= date(date.today().year, 1, 1):
            return symbol, 0, "skip"
        sd = date(since, 1, 1)
        rows = data_source.get_stock_daily_qfq(symbol, sd)
        if not rows:
            return symbol, 0, "empty"
        ingestion.upsert_kline(db, rows, symbol, "qfq")
        db.commit()
        return symbol, len(rows), "ok"
    except Exception as e:
        db.rollback()
        return symbol, 0, f"err:{e}"
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="000906", help="指数代码：000906=中证800, 000300=沪深300, 000905=中证500")
    ap.add_argument("--since", type=int, default=2021, help="起始年份")
    ap.add_argument("--workers", type=int, default=8, help="并发数")
    ap.add_argument("--limit", type=int, default=0, help="仅前 N 只（验证用，0=全部）")
    args = ap.parse_args()

    logger.info(f"获取指数 {args.index} 成分股列表…")
    try:
        constituents = data_source.get_index_constituents(args.index)
    except Exception as e:
        logger.error(f"获取成分股失败: {e}")
        sys.exit(1)
    logger.info(f"成分股数量: {len(constituents)}")

    # 保存归一化成分股到 data/<index>.json（作为 akshare/tushare 不可用时的兜底）
    save_constituents_json(constituents, os.path.join(ROOT, "data", f"{args.index.lower()}.json"))

    syms = [c["symbol"] for c in constituents]
    if args.limit:
        syms = syms[: args.limit]

    ok = skip = err = empty = 0
    total_rows = 0

    if args.workers <= 1:
        for done, sym in enumerate(syms, 1):
            sym, n, status = worker(sym, args.since)
            if status == "ok":
                ok += 1; total_rows += n
            elif status == "skip":
                skip += 1
            elif status == "empty":
                empty += 1
            else:
                err += 1
                if err <= 10:
                    logger.warning(f"{sym} {status}")
            if done % 50 == 0:
                logger.info(f"进度 {done}/{len(syms)}  ok={ok} skip={skip} empty={empty} err={err}")
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(worker, s, args.since): s for s in syms}
            done = 0
            for fut in as_completed(futs):
                sym, n, status = fut.result()
                done += 1
                if status == "ok":
                    ok += 1; total_rows += n
                elif status == "skip":
                    skip += 1
                elif status == "empty":
                    empty += 1
                else:
                    err += 1
                    if err <= 10:
                        logger.warning(f"{sym} {status}")
                if done % 50 == 0:
                    logger.info(f"进度 {done}/{len(syms)}  ok={ok} skip={skip} empty={empty} err={err}")

    logger.info(f"完成: ok={ok} skip={skip} empty={empty} err={err} 新增K线条数≈{total_rows}")


if __name__ == "__main__":
    main()
