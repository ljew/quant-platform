#!/bin/sh
# 量化平台 · SQLite 热备份（online backup API，不锁库、不中断服务）
#
# 为什么必须有：
#   2026-11-06 检查时 data/backup 里只有 10-26 的一份 5GB 备份，此后 10 天没有新备份。
#   主库 quant_dev.db 已 5.0GB，一旦文件损坏（历史上 knowledge.db 真损坏过一次），
#   没有近期快照就只能重建全部数据。
#
# 用法：
#   sh backend/scripts/backup_db.sh          # 手动
#   launchd com.quant.backup 每日 03:00      # 自动（plist 见 scripts/com.quant.backup.plist）
#
# 保留策略：最近 KEEP 份（默认 5），更早的自动删除。
set -eu

ROOT="/Users/happyljew/quant-platform"
DEST="$ROOT/data/backup"
KEEP="${QUANT_BACKUP_KEEP:-5}"
TS="$(date +%Y%m%d_%H%M%S)"
OUT="quant_dev_${TS}.db"

mkdir -p "$DEST"

echo "[backup] $(date '+%F %T') 开始 → $DEST/$OUT"
docker exec -i quant-backend python3 - "$OUT" <<'PY'
import sqlite3
import sys

out = sys.argv[1]
src = sqlite3.connect("/data/quant_dev.db", timeout=120)
dst = sqlite3.connect(f"/data/backup/{out}")
# online backup API：一致性快照，不阻塞正在跑的写入
src.backup(dst)
dst.close()
src.close()
print("[backup] snapshot written:", out)
PY

echo "[backup] 校验完整性…"
docker exec -i quant-backend python3 - "$OUT" <<'PY'
import sqlite3
import sys

out = sys.argv[1]
con = sqlite3.connect(f"file:/data/backup/{out}?mode=ro", uri=True)
q = con.execute("PRAGMA quick_check").fetchone()[0]
n = con.execute("SELECT count(*) FROM kline_daily").fetchone()[0]
con.close()
print(f"[backup] quick_check={q} kline_rows={n:,}")
if q != "ok":
    raise SystemExit("[backup] 备份文件校验失败，保留现场但不删除旧备份")
PY

# 只保留最近 KEEP 份（按修改时间倒序）
COUNT=$(ls -1t "$DEST"/quant_dev_*.db 2>/dev/null | wc -l | tr -d ' ')
if [ "$COUNT" -gt "$KEEP" ]; then
  ls -1t "$DEST"/quant_dev_*.db | tail -n +$((KEEP + 1)) | while read -r f; do
    echo "[backup] 删除旧备份 $f"
    rm -f "$f"
  done
fi

SIZE=$(stat -f%z "$DEST/$OUT" 2>/dev/null || echo 0)
if [ "$SIZE" -lt 1000000000 ]; then
  echo "[backup] ❌ 备份文件只有 $SIZE 字节（应 >1GB），判定失败"
  exit 1
fi

echo "[backup] 完成，当前保留 $(ls -1t "$DEST"/quant_dev_*.db | wc -l | tr -d ' ') 份"
