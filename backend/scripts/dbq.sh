#!/bin/sh
# 主库只读查询：**必须走容器**，不要在宿主机直接 sqlite3。
#
# 为什么（两次真实事故）
# ---------------------
# data/ 是 bind mount 进容器的（./data → /data），容器内 SQLite 用 WAL 模式。
# WAL 的正确性依赖 -wal / -shm 两个伴随文件里的**跨进程共享内存**，而
# Docker Desktop 的 bind mount（virtiofs / gRPC-FUSE）无法在宿主机与容器之间
# 正确共享这部分内存。于是：
#
#   宿主机 sqlite3 打开主库（哪怕只是 SELECT）+ 容器同时在写
#     → database disk image is malformed / file is not a database
#
#   * 2026-09-10：宿主机 uvicorn 与容器同时写 → "file is not a database"
#   * 2026-11-08：宿主机只读查询 fund_master + 容器在写 → malformed，
#                  后端启动即失败，最后靠 sqlite3 .recover 重建 6.2GB 才恢复
#
# 结论：宿主机侧对 quant_dev.db 只做**文件级**操作（cp / mv / ls），
#       任何 SQL 一律进容器。本脚本就是那个入口。
#
# 用法
# ----
#   sh backend/scripts/dbq.sh "SELECT COUNT(*) FROM kline_daily"
#   echo "SELECT ..." | sh backend/scripts/dbq.sh
#   QUANT_DB=/data/fund_research.db sh backend/scripts/dbq.sh "SELECT ..."
#
# 只读：连接使用 mode=ro，写操作会直接报错，避免脚本误改生产库。
set -eu

DB_PATH="${QUANT_DB:-/data/quant_dev.db}"
SQL="${1:-}"

if [ -z "$SQL" ]; then
    SQL="$(cat)"
fi

exec docker exec -i quant-backend python3 - "$DB_PATH" "$SQL" <<'PY'
import sqlite3
import sys

db, sql = sys.argv[1], sys.argv[2]
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
try:
    cur = con.execute(sql)
    if cur.description:
        cols = [d[0] for d in cur.description]
        print(" | ".join(cols))
        print("-" * 60)
        for row in cur.fetchall():
            print(" | ".join("" if v is None else str(v) for v in row))
    else:
        print(f"affected={cur.rowcount}")
finally:
    con.close()
PY
