#!/usr/bin/env bash
# 部署前冒烟测试 —— 用当前代码起一个「只提供 HTTP、不跑调度」的影子实例。
#
# 为什么需要它（2026-11-12）：
#   代码是热挂载进容器的（./backend/app → /app/app），改完文件重启进程即生效。
#   好处是不必 build，代价是**没有任何关卡**：一个 import 错误、一个字符串前向
#   引用，都会让容器起不来或 /openapi.json 500。当天 13:02 加的一条路由就是这么
#   埋下的 —— 进程里 openapi 缓存过所以 4 小时没暴露，一重启就炸，而容器
#   healthcheck 探的正是 /openapi.json → 一直 healthy: starting → 看门狗误报。
#   同一时段还有两次「重启杀掉了正在跑的编排」（4 天 9 次 FAILED 里 4 次是这个）。
#
# 两层验证：
#   L1  镜像内 import app.main + 生成 openapi（约 10 秒，抓 import/前向引用/路由冲突）
#   L2  起影子容器（空 data + 调度全关）探真实 HTTP（约 40 秒，抓启动钩子/uvicorn）
#
# 用法：
#   ./scripts/smoke_test.sh              # 用现有镜像 + 当前挂载代码
#   SMOKE_PORT=24002 ./scripts/smoke_test.sh
#
# 退出码：0 通过 / 1 不通过（不要在这时候部署）

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"

IMAGE="${SMOKE_IMAGE:-quant-platform-backend:latest}"
PORT="${SMOKE_PORT:-24001}"
WAIT_SEC="${SMOKE_WAIT:-90}"
NAME="quant-smoke-$$"
TMPDATA="$(mktemp -d "${TMPDIR:-/tmp}/quant-smoke-data-XXXXXX")"
OUT="$(mktemp "${TMPDIR:-/tmp}/quant-smoke-out-XXXXXX")"
FAILED=0

# 故障演练（可选）：把宿主机文件覆盖进影子容器，例如
#   SMOKE_EXTRA_VOLUME="/tmp/bad_main.py:/app/app/main.py" ./scripts/smoke_test.sh
# ⚠️ 不加引号展开（靠 word split 分出 -v 和 path:path），路径含空格会失效。
EXTRA_V=""
if [ -n "${SMOKE_EXTRA_VOLUME:-}" ]; then
  EXTRA_V="-v $SMOKE_EXTRA_VOLUME"
  printf '  ....  已注入覆盖挂载 %s\n' "$SMOKE_EXTRA_VOLUME"
fi

ok()   { printf '  \033[32m通过\033[0m  %s\n' "$*"; }
bad()  { printf '  \033[31m失败\033[0m  %s\n' "$*"; FAILED=1; }
info() { printf '  ....  %s\n' "$*"; }
head1() { printf '\n%s\n' "$*"; }

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1
  rm -rf "$TMPDATA" "$OUT"
}
trap cleanup EXIT INT TERM

head1 "冒烟测试 · 镜像 $IMAGE · 端口 $PORT"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  bad "镜像不存在：${IMAGE}（先 docker compose build backend）"
  exit 1
fi

# ── L1：代码可导入 + openapi 可生成 ─────────────────────────────────────
# 这一步是全套里最值钱的：FastAPI 生成 schema 时会解析每个路由的参数注解和
# response_model，任何前向引用/循环导入/签名错误都在这里暴露，不必等真实请求。
head1 "L1 · 代码可导入且 openapi 可生成"

PROBE=$(cat <<'PROBE_EOF'
import sys

from app.main import app

spec = app.openapi()          # 前向引用解析失败的抛点
paths = spec["paths"]
print(f"ROUTES {len(paths)}")

required = [
    "/health",
    "/api/v1/strategy/strategies",
    "/api/v1/paper/tasks",
    "/api/v1/monitor/framework-health",
    "/api/v1/monitor/table-freshness",
    "/api/v1/agent/state",
    "/api/v1/signal/dates",
    "/api/v1/news/stream",
    "/api/v1/research/cards",
]
missing = [r for r in required if r not in paths]
if missing:
    print("MISSING " + ",".join(missing))
    sys.exit(2)

if len(paths) < 200:
    print(f"TOO_FEW 只有 {len(paths)} 条路由，疑似某个 router 没挂上")
    sys.exit(3)

print("OK")
PROBE_EOF
)

if docker run --rm --entrypoint python3 \
    -e QUANT_SCHEDULERS=0 \
    -e QUANT_DATABASE_URL="sqlite:////data/smoke.db" \
    -e TZ=Asia/Shanghai \
    -v "$ROOT/backend/app:/app/app" \
    -v "$ROOT/backend/scripts:/app/scripts" \
    -v "$ROOT/config:/app/config" \
    -v "$TMPDATA:/data" \
    $EXTRA_V \
    "$IMAGE" -c "$PROBE" >"$OUT" 2>&1; then
  ROUTES=$(awk '/^ROUTES /{print $2}' "$OUT")
  ok "可导入 · openapi 生成成功 · ${ROUTES:-?} 条路由 · 9 条核心路由齐全"
else
  bad "代码导入或 openapi 生成失败（部署会直接 500）"
  grep -vE '^\s*$' "$OUT" | tail -25 | sed 's/^/        /'
fi

# ── L2：影子容器能否真的起来 ─────────────────────────────────────────────
# data 用**临时空目录**，绝不能挂 ./data —— 那会成为第二个写者（SQLite 双写会损坏，
# 2026-09-10 已经真坏过一次）。空库足够验证「建表 + 路由 + uvicorn 绑定」。
head1 "L2 · 影子容器可启动并响应 HTTP"

info "启动 ${NAME}（空 data、调度全关）"
docker run -d --name "$NAME" \
  --memory 1500m \
  -e QUANT_SCHEDULERS=0 \
  -e QUANT_DATA_SCHEDULE=0 \
  -e QUANT_FUND_SCHEDULE=0 \
  -e QUANT_DEBUG=false \
  -e TZ=Asia/Shanghai \
  -e QUANT_DATABASE_URL="sqlite:////data/quant_dev.db" \
  -e QUANT_FRONTEND_DIR=/frontend \
  -e QUANT_SOURCES_YAML=/app/config/sources.yaml \
  -e QUANT_WECHAT_DATA_DIR=/data/wechat \
  -v "$TMPDATA:/data" \
  -v "$ROOT/config:/app/config" \
  -v "$ROOT/backend/app:/app/app" \
  -v "$ROOT/backend/scripts:/app/scripts" \
  -v "$ROOT/web/dist:/frontend:ro" \
  $EXTRA_V \
  -p "127.0.0.1:$PORT:8000" \
  "$IMAGE" >"$OUT" 2>&1

if [ $? -ne 0 ]; then
  bad "容器起不来"
  tail -15 "$OUT" | sed 's/^/        /'
else
  # 轮询 openapi
  READY=0
  for i in $(seq 1 "$WAIT_SEC"); do
    CODE=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' -m 3 \
           "http://127.0.0.1:$PORT/openapi.json" 2>/dev/null)
    if [ "$CODE" = "200" ]; then READY=1; READY_SEC=$i; break; fi
    if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then break; fi
    sleep 1
  done

  if [ "$READY" = "1" ]; then
    ok "shadow /openapi.json 200（${READY_SEC:-?} 秒内就绪）"
  else
    bad "shadow 未能就绪（最后一码 ${CODE}）"
    docker logs "$NAME" 2>&1 | tail -25 | sed 's/^/        /'
  fi

  # 再探两条真实业务路由：能连上不等于 router 挂上了
  if [ "$READY" = "1" ]; then
    for P in /api/v1/strategy/strategies /api/v1/monitor/table-freshness; do
      C=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' -m 15 \
          "http://127.0.0.1:$PORT$P" 2>/dev/null)
      if [ "$C" = "200" ]; then
        ok "$P → 200"
      else
        bad "${P} → ${C}（空库上这条应能返回空列表，非 200 说明实现有问题）"
      fi
    done
  fi
fi

# ── 结论 ────────────────────────────────────────────────────────────────
head1 ""
if [ "$FAILED" = "0" ]; then
  printf '\033[32m冒烟通过\033[0m —— 当前代码可以部署。\n'
  exit 0
fi
printf '\033[31m冒烟未通过\033[0m —— 不要部署。上面每一条都要先修掉。\n'
exit 1
