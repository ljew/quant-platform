#!/bin/zsh
# 量化智能体的启动器 —— **独立于日常 dsh**，跑在自己的 DSH_HOME 里。
#
# 为什么不能和标准 dsh 共用一个服务（2026-11-11 老刘定的）：
#   智能体要改前端（品牌、角色条、工具卡）、改 preset 白名单、改模型和 MCP，
#   这些都是**profile 级**配置。挂在 ~/.dsh/profiles/web 上就等于改了他日常
#   用的那个 dsh —— 他打开 dsh 会发现界面变了、工具少了、左侧多出一堆
#   量化会话。所以另起一套：
#
#     DSH_HOME = ~/quant-agent          独立 home（sessions / storages / 配置）
#     profile  = quant                  独立 profile（bundles 只要 base + web-app）
#     port     = 3081                   不与标准 dsh 的 3080 冲突
#
#   package store 用软链共用（~/.dsh/profiles/node_modules），不重复装 279 个包。
#
# 为什么必须带 --public-url / --trusted-host：
#   平台后端把 /agent/* 反代到宿主 :3081，浏览器看到的 origin 是 localhost:8000。
#   dsh 的 /api 有一道 browser-trust fence 校验 authority，不带这两个参数时
#   反代过去的请求会被判成外来来源 → 401。--public-url 还负责让 dsh 生成的
#   资源链接带 /agent 前缀。
#
# 日志必须落到 $LOG：后端从里面读最后一次的 ?token=，首次进 /agent/ 时自动
# 引导登录（种 HttpOnly cookie），不需要手工复制 token。
#
# 用法：./scripts/dsh-web.sh start | stop | status | log

set -u

DSH_HOME_DIR="$HOME/quant-agent"
LOG="$HOME/quant-platform/data/agent_portal.log"
PORT=3081
DSH="$HOME/dsh.sh"
PROFILE=quant
PUBLIC_URL="http://localhost:8000/agent/"

case "${1:-start}" in
  start)
    if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
      echo "量化智能体已在 :$PORT 运行"
      exit 0
    fi
    mkdir -p "$(dirname "$LOG")" "$DSH_HOME_DIR/profiles"
    # 只保留最近一次启动的输出，避免后端读到旧 token
    : > "$LOG"
    # cwd 决定工作区分组（会话目录名按 cwd 生成）。从仓库根起，
    # 新会话才落在 quant-platform 下，而不是 $HOME。
    cd "$HOME/quant-platform" || exit 1
    # 子命令就是 profile 名（`dsh web` 等价于 `dsh --profile web`），
    # 再叠 --profile 会报 "select a profile only once"。
    DSH_HOME="$DSH_HOME_DIR" nohup "$DSH" "$PROFILE" --port "$PORT" --no-open \
      --public-url "$PUBLIC_URL" \
      --trusted-host localhost:8000 \
      --trusted-host 127.0.0.1:8000 \
      >> "$LOG" 2>&1 &
    echo $! > "$DSH_HOME_DIR/.dsh-web.pid"
    for i in $(seq 1 60); do
      if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
        echo "量化智能体已启动：$PUBLIC_URL（profile=$PROFILE, home=$DSH_HOME_DIR）"
        grep -o "token=[A-Za-z0-9_-]*" "$LOG" | tail -1
        exit 0
      fi
      sleep 0.5
    done
    echo "启动超时，看日志：$LOG" >&2
    exit 1
    ;;
  stop)
    if [ -f "$DSH_HOME_DIR/.dsh-web.pid" ]; then
      kill "$(cat "$DSH_HOME_DIR/.dsh-web.pid")" 2>/dev/null
      rm -f "$DSH_HOME_DIR/.dsh-web.pid"
    fi
    pids=$(lsof -tnP -iTCP:$PORT -sTCP:LISTEN 2>/dev/null)
    [ -n "$pids" ] && echo "$pids" | xargs kill 2>/dev/null
    echo "量化智能体已停止"
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
