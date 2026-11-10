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

echo "→ 优雅停止（等 SQLite 写完）"
docker compose stop backend

echo "→ 重建并启动"
docker compose up -d --build backend

echo "→ 等健康检查"
i=0
while [ $i -lt 30 ]; do
    if curl -sf --noproxy '*' -o /dev/null http://127.0.0.1:8000/openapi.json; then
        echo "✅ 已恢复（$(date +%H:%M:%S)）"
        exit 0
    fi
    i=$((i + 1))
    sleep 2
done
echo "❌ 90 秒内没起来，看日志：docker logs --tail 50 quant-backend"
exit 1
