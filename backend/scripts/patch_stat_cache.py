#!/usr/bin/env python3
"""合并/覆写 fund_stat_cache 里的 payload 块。

为什么需要单独一个脚本
----------------------
`build_fund_stats.py` 每次重算都会**整份覆盖** `persistence` 这一行，
而持仓归因、频率成本这些块由独立脚本算（跑得慢、依赖不同数据源），
重算统计时会被一起冲掉。这个脚本负责把它们按 key 合并回去，不必重跑整条链路。

用法（必须在容器内执行，否则会和后端抢 WAL 写锁）
-------------------------------------------------
    docker exec -i quant-backend python /app/scripts/patch_stat_cache.py merge < blocks.json
    docker exec -i quant-backend python /app/scripts/patch_stat_cache.py set some_key < payload.json

    merge：把 stdin 的 JSON 对象逐 key 合并进 `persistence`（只更新出现的 key）
    set  ：把 stdin 的 JSON 整个存为指定 key 的新行

为什么要放进容器
----------------
`quant_dev.db` 是 WAL 模式。宿主机进程与容器内后端各自维护 `-shm`，
从宿主机写入会让容器读到损坏镜像（2026-11-08 真实事故，见 skill `quant-platform-ops`）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

DB = os.getenv("QUANT_DB", "/data/quant_dev.db")
TABLE = "fund_stat_cache"
DEFAULT_KEY = "persistence"


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("merge", "set"):
        print(__doc__)
        return 2
    op = sys.argv[1]
    key = sys.argv[2] if (op == "set" and len(sys.argv) > 2) else DEFAULT_KEY
    payload = json.load(sys.stdin)

    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    try:
        row = con.execute(
            f"select payload_json from {TABLE} where key=?", (DEFAULT_KEY,)).fetchone()
        cur = json.loads(row[0]) if row and row[0] else {}

        if op == "merge":
            if not isinstance(payload, dict):
                print("merge 需要 JSON 对象")
                return 2
            cur.update(payload)
            out, k = cur, DEFAULT_KEY
        else:
            out, k = payload, key

        con.execute(f"insert or replace into {TABLE} values (?, ?, ?)",
                    (k, json.dumps(out, ensure_ascii=False), cur.get("generated_at", "")))
        con.commit()
        keys = sorted(out.keys()) if isinstance(out, dict) else type(out).__name__
        print(f"[{op}] {k} → {keys}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
