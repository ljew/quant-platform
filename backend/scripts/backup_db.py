"""SQLite 热备份：ETL 成功后自动跑，保留最近 N 份。

## 为什么用 VACUUM INTO

2026-11-09 发生过一次真损坏（88 张表里 factor_daily 的 B-tree 坏了），
靠逐表 ATTACH 复制才救回 87 张表。事后把备份全删了 → 库里一份备份都没有，
再来一次就没这么幸运。

`VACUUM INTO` 比直接 `cp` 好在两点：
1. **事务一致**：直接 cp 一个正在写的库（尤其 WAL 模式）会拿到半截状态 ——
   这是很多「备份恢复出来也是坏的」的根源。VACUUM INTO 在单个读事务里读全库。
2. **顺带整理碎片**：产物是紧凑的，quick_check 一定是 ok。

实测 5.4G 库 → 38 秒、产物 5.89G。

## 保留策略

按文件名里的日期排序，只留最近 `keep` 份（默认 3）。5.89G × 3 ≈ 18G。

用法：
    docker exec quant-backend python /app/scripts/backup_db.py
    docker exec quant-backend python /app/scripts/backup_db.py --keep 5
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

DB = os.getenv("QUANT_DB_PATH", "/data/quant_dev.db")
BACKUP_DIR = os.getenv("QUANT_BACKUP_DIR", "/data/backups")
KEEP = 3


def backup(keep: int = KEEP, db_path: str = DB, out_dir: str = BACKUP_DIR) -> str:
    """做一份备份，返回产物路径。失败抛异常（由调用方决定是否吞掉）。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    os.makedirs(out_dir, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")  # 秒级：分钟级同分钟两次会互相覆盖
    out = os.path.join(out_dir, f"quant_dev_{stamp}.db")
    if os.path.exists(out):
        os.remove(out)

    t0 = time.time()
    # 只读连接：备份过程绝不修改源库；WAL 里未 checkpoint 的内容也会被读到
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.execute(f"VACUUM INTO '{out}'")
    finally:
        con.close()

    size_gb = os.path.getsize(out) / 1e9
    print(f"备份完成：{out}  {size_gb:.2f} GB  {time.time() - t0:.1f}s")

    # 滚动清理：按文件名（带时间戳）倒序，保留最近 keep 份
    files = sorted(
        (p for p in Path(out_dir).glob("quant_dev_*.db")),
        key=lambda p: p.name, reverse=True,
    )
    for old in files[keep:]:
        try:
            old.unlink()
            print(f"清理旧备份：{old.name}")
        except OSError as e:
            print(f"清理失败 {old.name}: {e}")

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="SQLite 热备份（VACUUM INTO）")
    ap.add_argument("--keep", type=int, default=KEEP, help="保留份数（默认 3）")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--dir", default=BACKUP_DIR)
    args = ap.parse_args()
    try:
        backup(args.keep, args.db, args.dir)
    except Exception as e:  # noqa: BLE001
        print(f"备份失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
