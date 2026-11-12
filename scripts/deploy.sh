#!/usr/bin/env bash
# 安全部署 —— 把「改代码 → 重启」这条日常路径变成有门槛的流程。
#
# 为什么需要它（2026-11-12 复盘）：
#   4 天里 9 次 FAILED，其中 4 次是「重启把正在跑的编排杀了」；另有 1 次是
#   重启才暴露的 openapi 500（把容器 healthcheck 一起拖挂）。两件事都不是代码
#   缺陷，是**部署动作本身没有关卡**。这个脚本就是那道关卡：
#
#     ① 有编排在跑 → 拒绝（除非 --force）
#     ② 冒烟不通过 → 拒绝
#     ③ 切换后接口不全绿 → 打印回滚命令
#
# 用法：
#   ./scripts/deploy.sh                 # 只重启（改了热挂载的代码）
#   ./scripts/deploy.sh --build         # 先重建镜像，再部署（改了依赖/镜像内文件）
#   ./scripts/deploy.sh --force         # 有编排在跑也强行部署（不推荐）
#   ./scripts/deploy.sh --wait 600      # 有编排在跑时最多等 600 秒
#
# 退出码：0 部署成功 / 1 被拦下或部署后不健康

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"

DO_BUILD=0
FORCE=0
WAIT_SEC=0

while [ $# -gt 0 ]; do
  case "$1" in
    --build) DO_BUILD=1; shift ;;
    --force) FORCE=1; shift ;;
    --wait)  WAIT_SEC="${2:-0}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) printf '未知参数 %s\n' "$1"; exit 2 ;;
  esac
done

ok()   { printf '  \033[32m通过\033[0m  %s\n' "$*"; }
bad()  { printf '  \033[31m失败\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33m注意\033[0m  %s\n' "$*"; }
info() { printf '  ....  %s\n' "$*"; }
head1() { printf '\n%s\n' "$*"; }

API="http://127.0.0.1:8000"
CURL=(curl -s --noproxy '*' -m 15)

# ── 1. 编排占用检查 ─────────────────────────────────────────────────────
# 重启会直接杀掉正在跑的 run（4 天里已发生 4 次，2 次是我造成的）。
head1 "① 检查是否有编排在跑"

running_of() {
  "${CURL[@]}" "$API/api/v1/agent/state" 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin); o=d.get('orchestrator') or {}
    print('1' if o.get('running') else '0', o.get('run_id') or '', o.get('step') or '')
except Exception:
    print('?')
" 2>/dev/null
}

STATE="$(running_of)"
if [ "${STATE%% *}" = "?" ]; then
  warn "读不到编排状态（服务可能没起来），按「没在跑」继续"
elif [ "${STATE%% *}" = "1" ]; then
  RID="$(printf '%s' "$STATE" | awk '{print $2}')"
  if [ "$FORCE" = "1" ]; then
    warn "编排 run ${RID} 正在跑，--force 强行部署（它会被打断）"
  elif [ "$WAIT_SEC" -gt 0 ]; then
    info "编排 run ${RID} 正在跑，最多等 ${WAIT_SEC} 秒"
    WAITED=0
    while [ "$WAITED" -lt "$WAIT_SEC" ]; do
      sleep 10; WAITED=$((WAITED + 10))
      S2="$(running_of)"
      [ "${S2%% *}" = "0" ] && break
      printf '        %ss 仍在跑（%s）\n' "$WAITED" "$(printf '%s' "$S2" | cut -d' ' -f3)"
    done
    if [ "${S2%% *}" = "1" ]; then
      bad "等了 ${WAIT_SEC} 秒仍在跑 → 先别部署"
      exit 1
    fi
    ok "编排已结束"
  else
    bad "编排 run ${RID} 正在跑 —— 重启会杀掉它"
    printf '        等它跑完，或用 --wait 600 自动等，或 --force 承担后果\n'
    exit 1
  fi
else
  ok "没有编排在跑"
fi

# ── 2. 构建（可选）──────────────────────────────────────────────────────
if [ "$DO_BUILD" = "1" ]; then
  head1 "② 重建镜像"
  if docker compose build backend 2>&1 | tail -4; then
    ok "镜像已重建"
  else
    bad "镜像构建失败 → 停止"
    exit 1
  fi
else
  head1 "② 跳过构建（热挂载代码改动，restart 即可）"
fi

# ── 3. 冒烟 ─────────────────────────────────────────────────────────────
head1 "③ 冒烟测试"
if "$ROOT/scripts/smoke_test.sh"; then
  ok "冒烟通过"
else
  bad "冒烟未通过 —— 主容器保持原状，不会被打断"
  exit 1
fi

# ── 4. 切换 ─────────────────────────────────────────────────────────────
head1 "④ 切换主容器"
OLD_IMG="$(docker inspect quant-backend --format '{{.Image}}' 2>/dev/null || echo unknown)"
info "当前镜像 ${OLD_IMG:0:19}"

if [ "$DO_BUILD" = "1" ]; then
  if ! docker compose up -d --no-deps backend 2>&1 | tail -3; then
    bad "切换失败"
    exit 1
  fi
else
  if ! docker compose restart backend 2>&1 | tail -2; then
    bad "重启失败"
    exit 1
  fi
fi

# ── 5. 复查 ─────────────────────────────────────────────────────────────
head1 "⑤ 复查"

UP=0
for i in $(seq 1 45); do
  C="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$API/openapi.json" 2>/dev/null)"
  if [ "$C" = "200" ]; then UP=1; UP_SEC=$((i * 2)); break; fi
  sleep 2
done

if [ "$UP" != "1" ]; then
  bad "启动后 /openapi.json 不是 200（最后 ${C:-超时}）"
  printf '        回滚：docker compose stop backend && docker tag %s quant-platform-backend:latest && docker compose start backend\n' "$OLD_IMG"
  docker logs quant-backend --tail 25 2>&1 | sed 's/^/        /'
  exit 1
fi
ok "/openapi.json 200（${UP_SEC} 秒）"

FAILN=0
for P in /api/v1/strategy/strategies /api/v1/paper/tasks /api/v1/monitor/framework-health \
         /api/v1/monitor/table-freshness /api/v1/signal/dates; do
  C="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$API$P" 2>/dev/null)"
  if [ "$C" = "200" ]; then
    ok "$P"
  else
    bad "$P → $C"; FAILN=$((FAILN + 1))
  fi
done

# 容器有没有因为这次部署开始报 I/O 错（WAL 层坏过一次，值得每次都看）
STARTED="$(docker inspect quant-backend --format '{{.State.StartedAt}}' 2>/dev/null)"
IOERR="$(docker logs quant-backend --timestamps 2>&1 | awk -v c="$STARTED" '$1 >= c' | grep -c 'disk I/O error')"
if [ "${IOERR:-0}" -gt 0 ]; then
  bad "启动后有 ${IOERR} 条 disk I/O error"
  FAILN=$((FAILN + 1))
else
  ok "无 disk I/O error"
fi

head1 ""
if [ "$FAILN" = "0" ]; then
  printf '\033[32m部署完成\033[0m —— 冒烟通过、5 条关键接口全绿、无 I/O 错误。\n'
  exit 0
fi
printf '\033[31m部署后不健康\033[0m（%s 项失败）\n' "$FAILN"
printf '  回滚：docker tag %s quant-platform-backend:latest && docker compose restart backend\n' "$OLD_IMG"
exit 1
