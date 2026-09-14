#!/bin/bash
# 量化平台后端常驻服务脚本（后端 8000）
# 用法：
#   ./dev_services.sh guard    守护模式：前台循环运行 uvicorn，崩溃/退出后自动重启（推荐，无需 cron）
#   ./dev_services.sh start    后台拉起一次（nohup，适合手动；进程不随本脚本存活）
#   ./dev_services.sh stop     停止 8000 上的服务
#   ./dev_services.sh status   查看状态
#
# 说明：
#   - 前端已由后端托管 web/dist 构建产物（访问 http://localhost:8000），5173 已废弃，不再启动 vite。
#   - Docker 部署走 docker-compose.yml，端口已改挂 8001 且 restart: no；
#     容器与本机 uvicorn 共用 data/quant_dev.db，禁止同时运行，否则会损坏 SQLite。

set -u

ROOT="/Users/happyljew/Desktop/kimiwork/Quant/quant-platform"
PY="/Users/happyljew/.workbuddy/binaries/python/envs/quant/bin/python"
LOG_DIR="/tmp"
LOG="$LOG_DIR/quant-backend.log"
SCHEDULE_ENV="${QUANT_DATA_SCHEDULE:-1}"   # ETL 每交易日 19:00 自动增量

port_pid() { /usr/sbin/lsof -ti :"$1" 2>/dev/null | head -1; }

start_backend_blocking() {
  cd "$ROOT/backend" || exit 1
  PYTHONPATH="$ROOT/backend" QUANT_DATA_SCHEDULE="$SCHEDULE_ENV" \
    "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 >> "$LOG" 2>&1
}

start_backend() {
  if [ -n "$(port_pid 8000)" ]; then
    echo "[backend] 运行中 (PID $(port_pid 8000))"
    return 0
  fi
  cd "$ROOT/backend" || exit 1
  nohup env PYTHONPATH="$ROOT/backend" QUANT_DATA_SCHEDULE="$SCHEDULE_ENV" \
    "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 \
    >> "$LOG" 2>&1 &
  disown 2>/dev/null
  sleep 2
  echo "[backend] 已启动 (PID $(port_pid 8000), ETL调度=$SCHEDULE_ENV)"
}

guard() {
  echo "[guard] 守护启动：后端 8000 崩溃/退出将自动重启（日志 $LOG）"
  while true; do
    echo "$(date '+%F %T') [guard] 启动后端"
    start_backend_blocking
    code=$?
    echo "$(date '+%F %T') [guard] 后端退出 (code=$code)，5s 后重启"
    sleep 5
  done
}

stop_service() {
  local pid
  pid=$(port_pid 8000)
  if [ -n "$pid" ]; then kill "$pid" 2>/dev/null; echo "[backend] 已停止 (PID $pid)"; else echo "[backend] 未运行"; fi
}

status() {
  local bp
  bp=$(port_pid 8000)
  echo "后端 8000: ${bp:+运行中 PID $bp}${bp:-未运行}"
  echo "ETL 调度: $SCHEDULE_ENV (1=每交易日19:00自动)"
  echo "日志: $LOG"
}

case "${1:-status}" in
  guard)  guard ;;
  start)  start_backend ;;
  stop)   stop_service ;;
  status) status ;;
  *) echo "用法: $0 guard|start|stop|status"; exit 1 ;;
esac
