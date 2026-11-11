#!/bin/zsh
# dsh web 的启动器 —— 让智能体从平台同域 /agent/ 打开，而不是另开一个 :3080。
#
# 为什么必须带 --public-url / --trusted-host：
#   平台后端把 /agent/* 反代到宿主 :3080，浏览器看到的 origin 是 localhost:8000。
#   dsh 的 /api 有一道 browser-trust fence 校验 authority，不带这两个参数时
#   反代过去的请求会被判成外来来源 → 401。--public-url 还负责让 dsh 生成的
#   资源链接带 /agent 前缀。
#
# 日志必须落到 $LOG：后端从里面读最后一次的 ?token=，首次进 /agent/ 时自动
# 引导登录（种 HttpOnly cookie），所以不需要手工复制 token。
#
# 用法：./scripts/dsh-web.sh start | stop | status | log

set -u

LOG="$HOME/quant-platform/data/agent_portal.log"
PORT=3080
DSH="$HOME/dsh.sh"
PUBLIC_URL="http://localhost:8000/agent/"

case "${1:-start}" in
  start)
    if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
      echo "dsh 已在 :$PORT 运行"
      exit 0
    fi
    mkdir -p "$(dirname "$LOG")"
    # 只保留最近一次启动的输出，避免后端读到旧 token
    : > "$LOG"
    # cwd 决定 dsh 的工作区分组（会话目录名按 cwd 生成）。从 $HOME 起会让
    # 左侧「工作区」挂着别的项目；从仓库根起，新会话才落在 quant-platform 下。
    cd "$HOME/quant-platform" || exit 1
    nohup "$DSH" web --no-open \
      --public-url "$PUBLIC_URL" \
      --trusted-host localhost:8000 \
      --trusted-host 127.0.0.1:8000 \
      >> "$LOG" 2>&1 &
    echo $! > "$HOME/.dsh/dsh-web.pid"
    for i in $(seq 1 40); do
      if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
        echo "dsh 已启动：$PUBLIC_URL"
        grep -o "token=[A-Za-z0-9_-]*" "$LOG" | tail -1
        exit 0
      fi
      sleep 0.5
    done
    echo "启动超时，看日志：$LOG" >&2
    exit 1
    ;;
  stop)
    if [ -f "$HOME/.dsh/dsh-web.pid" ]; then
      kill "$(cat "$HOME/.dsh/dsh-web.pid")" 2>/dev/null
      rm -f "$HOME/.dsh/dsh-web.pid"
    fi
    pids=$(lsof -tnP -iTCP:$PORT -sTCP:LISTEN 2>/dev/null)
    [ -n "$pids" ] && echo "$pids" | xargs kill 2>/dev/null
    echo "dsh 已停止"
    ;;
  status)
    if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
      echo "running :$PORT"
      grep -o "token=[A-Za-z0-9_-]*" "$LOG" | tail -1
    else
      echo "stopped"
    fi
    ;;
  log)
    tail -40 "$LOG"
    ;;
  *)
    echo "用法: $0 start|stop|status|log" >&2
    exit 2
    ;;
esac
