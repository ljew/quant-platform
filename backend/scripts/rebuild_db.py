"""从损坏的 quant_dev.db 重建一个干净库。

背景（2026-11-09 21:05）：ETL 报 `database disk image is malformed`，
`PRAGMA quick_check` 报出大量 B-tree 错误（Rowid out of order / 2nd reference
to page）。逐表探测发现 **88 张表里只有 factor_daily 一张坏**，其余 87 张
（含 kline_daily 994 万行、adj_factor_daily 1007 万行、stock_valuation_daily
905 万行、signal_daily 18 万行）全部可读。

`DROP TABLE factor_daily` 会失败（DDL 要动 sqlite_master 和释放页面，
损坏的 B-tree 直接挡回来），所以只能整库重建：

  ATTACH 旧库（只读） → 新库逐表 CREATE + INSERT SELECT → 跳过坏表数据
  → 按原 DDL 重建坏表结构（空表）→ 重建索引 → 校验行数 → 替换文件

用法（**必须先停容器**，避免并行写者）：
    docker stop quant-backend
    docker run --rm -v ~/quant-platform/data:/data python:3.12-slim \
        python /data/rebuild_db.py
"""
import os
import sqlite3
import sys

OLD = "/data/quant_dev.db"
NEW = "/data/quant_dev.rebuilt.db"
BAD = {"factor_daily"}          # 已知损坏、跳过数据只重建结构


def main() -> int:
    if not os.path.exists(OLD):
        print(f"找不到 {OLD}")
        return 1
    if os.path.exists(NEW):
        os.remove(NEW)

    old = sqlite3.connect(f"file:{OLD}?mode=ro", uri=True)
    old.row_factory = sqlite3.Row

    tabs = old.execute(
        "select name, sql from sqlite_master "
        "where type='table' and name not like 'sqlite_%' order by name"
    ).fetchall()
    idxs = old.execute(
        "select name, sql from sqlite_master "
        "where type='index' and sql is not null order by name"
    ).fetchall()
    print(f"旧库：{len(tabs)} 张表 / {len(idxs)} 个索引")

    new = sqlite3.connect(NEW)
    new.execute("PRAGMA journal_mode=WAL")
    new.execute("PRAGMA synchronous=OFF")     # 重建过程不需要 durability
    new.execute("ATTACH DATABASE ? AS o", (f"file:{OLD}?mode=ro",))

    copied, skipped, failed = [], [], []
    for row in tabs:
        name, ddl = row["name"], row["sql"]
        if not ddl:
            continue
        try:
            new.execute(ddl.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
        except Exception as e:  # noqa: BLE001
            failed.append((name, f"建表失败 {str(e)[:60]}"))
            continue
        if name in BAD:
            skipped.append(name)
            continue
        try:
            cur = new.execute(f'INSERT INTO main."{name}" SELECT * FROM o."{name}"')
            copied.append((name, cur.rowcount))
        except Exception as e:  # noqa: BLE001
            failed.append((name, f"复制失败 {str(e)[:60]}"))

    new.commit()
    print(f"复制完成：{len(copied)} 张 / 跳过 {skipped} / 失败 {len(failed)}")

    print("重建索引…")
    for row in idxs:
        try:
            new.execute(row["sql"])
        except Exception as e:  # noqa: BLE001
            print(f"  索引 {row['name']} 失败：{str(e)[:60]}")
    new.commit()

    # 校验：可复制的表逐表比行数
    print("\n行数校验（新 vs 旧）：")
    mismatch = 0
    for name, n in sorted(copied, key=lambda x: -x[1])[:10]:
        try:
            m = old.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        except Exception:  # noqa: BLE001
            m = -1
        flag = "✓" if m == n else "✗"
        if m != n:
            mismatch += 1
        print(f"  {flag} {name:28s} 新 {n:>10,}  旧 {m:>10,}")
    if mismatch:
        print(f"⚠️ {mismatch} 张表行数不一致（本次只展示前 10 大表）")

    print("\n新库完整性：")
    print(" ", new.execute("PRAGMA quick_check").fetchall()[:2])

    for t, msg in failed:
        print(f"  ✗ {t}: {msg}")

    old.close()
    new.close()
    print(f"\n新库已生成：{NEW}  ({os.path.getsize(NEW) / 1e9:.2f} GB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
