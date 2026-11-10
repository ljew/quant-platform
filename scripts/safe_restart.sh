#!/bin/sh
# 安全重启后端：先优雅停，再重建。
#
# 为什么不能直接 `docker compose up -d --build backend`：
# 2026-11-10 19:43 事故 —— 模拟盘子进程正在写 6GB 的 quant_dev.db 时，
# 这个命令强杀了容器，Docker 的卷共享层在写入中断处把 SQLite 文件头写坏，
# 文件变成 "SQLite format 3" 之外的东西，所有接口 500。最后用 16:14 的
# VACUUM INTO 备份才恢复（丢了 3.5 小时的写入）。
#
# 正确顺序：stop（等进程收尾）→ 检查运行标记 → build/up。
set -e
cd "$(dirname "$0")/.."

if [ -f data/.paper_running ]; then
    echo "⚠️  模拟盘任务正在跑（data/.paper_running）："
    cat data/.paper_running
    echo "   等它跑完（组合任务约 1 分钟）再重启，或强来：$0 --force"
    [ "$1" = "--force" ] || exit 1
fi

echo "→ 停前先把 WAL 落盘"
# 2026-11-10 22:26 事故：12 分钟内连续 6 次 rebuild，最后 quick_check 报出
# 「2nd reference to page」—— 两张表的 b-tree 指向同一个页（页交叉引用），
# 用 .recover 重建才修好（丢 2 条 news + 101 条 etf_share）。
# 主动 checkpoint 把 WAL 合并进主库，容器就没什么可「半写」的了。
docker exec quant-backend python3 -c "
import sqlite3
c = sqlite3.connect('/data/quant_dev.db', timeout=20)
print('  wal_checkpoint:', c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
c.close()
" 2>/dev/null || echo "  （容器内 checkpoint 跳过：进程可能已退出）"

echo "→ 优雅停止（等 SQLite 写完）"
docker compose stop backend

echo "→ 重建并启动"
docker compose up -d --build backend

echo "→ 等健康检查"
i=0
while [ $i -lt 30 ]; do
    if curl -sf --noproxy '*' -o /dev/null http://127.0.0.1:8000/openapi.json; then
        break
    fi
    i=$((i + 1))
    sleep 2
done
if [ $i -ge 30 ]; then
    echo "❌ 90 秒内没起来，看日志：docker logs --tail 50 quant-backend"
    exit 1
fi

# openapi 只是路由表，不碰数据库。重建容器后 Docker 卷要重新挂载，
# 这中间几秒 SQLite 会报 disk I/O error（库本身没坏，宿主 quick_check 是 ok 的）。
# 之前只探 openapi 就宣布恢复，结果马上请求接口拿到 500 —— 多等这一步。
echo "→ 等数据库可读"
j=0
while [ $j -lt 15 ]; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' \
        "http://127.0.0.1:8000/api/v1/strategy/optimize/runs?limit=1")
    [ "$code" = "200" ] && { echo "✅ 已恢复（$(date +%H:%M:%S)）"; exit 0; }
    j=$((j + 1))
    sleep 2
done
echo "⚠️  路由已起但数据库接口返回 $code —— 等 20 秒再试；"
echo "    若持续报 disk I/O error 而宿主 sqlite3 quick_check 正常，就是卷挂载问题："
echo "    docker compose restart backend"
exit 1
