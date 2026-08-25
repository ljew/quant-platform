#!/bin/bash
# 量化平台常驻服务守护脚本（后端 8000 + React 前端 5173）
# 用途：幂等启动/停止/状态检查；配合 crontab 每分钟自愈（崩溃自动拉起）
# 用法：./dev_services.sh start|stop|status
# 环境：macOS（需 cron 常驻自愈；launchd 在虚拟化环境不可用时用本方案）

set -u

ROOT="/Users/happyljew/Desktop/kimiwork/Quant/quant-platform"
PY="/Users/happyljew/.workbuddy/binaries/python/envs/quant/bin/python"
NODE="/Users/happyljew/.workbuddy/binaries/node/versions/22.22.2/bin/node"
VITE="$ROOT/web/node_modules/vite/bin/vite.js"
LOG_DIR="/tmp"
SCHEDULE_ENV="${QUANT_DATA_SCHEDULE:-1}"   # ETL 每晚 17:00 自动增量

port_pid() { /usr/sbin/lsof -ti :"$1" 2>/dev/null | head -1; }

start_backend() {
  if [ -n "$(port_pid 8000)" ]; then
    echo "[backend] 运行中 (PID $(port_pid 8000))"
    return 0
  fi
  cd "$ROOT/backend" || exit 1
  nohup env PYTHONPATH="$ROOT/backend" QUANT_DATA_SCHEDULE="$SCHEDULE_ENV" \
    "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 \
    >> "$LOG_DIR/quant-backend.log" 2>&1 &
  disown 2>/dev/null
  echo "[backend] 已启动 (ETL调度=$SCHEDULE_ENV)"
}

start_web() {
  if [ -n "$(port_pid 5173)" ]; then
    echo "[web] 运行中 (PID $(port_pid 5173))"
    return 0
  fi
  cd "$ROOT/web" || exit 1
  nohup "$NODE" "$VITE" --host 127.0.0.1 --port 5173 \
    >> "$LOG_DIR/quant-web.log" 2>&1 &
  disown 2>/dev/null
  echo "[web] 已启动"
}

stop_service() {
  local port="$1" name="$2"
  local pid
  pid=$(port_pid "$port")
  if [ -n "$pid" ]; then kill "$pid" 2>/dev/null; echo "[$name] 已停止 (PID $pid)"; else echo "[$name] 未运行"; fi
}

status() {
  local bp wp
  bp=$(port_pid 8000); wp=$(port_pid 5173)
  echo "后端 8000: ${bp:+运行中 PID $bp}${bp:-未运行}"
  echo "前端 5173: ${wp:+运行中 PID $wp}${wp:-未运行}"
  echo "ETL 调度: $SCHEDULE_ENV (1=每晚17:00自动)"
}

case "${1:-status}" in
  start) start_backend; start_web ;;
  stop)  stop_service 8000 backend; stop_service 5173 web ;;
  status) status ;;
  *) echo "用法: $0 start|stop|status"; exit 1 ;;
esac
