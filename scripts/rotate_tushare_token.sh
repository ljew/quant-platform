#!/usr/bin/env bash
# 轮换 Tushare token —— 先验证、后落盘、失败自动回滚
#
# 为什么需要脚本而不是手改 .env
# ----------------------------
# 1. docker-compose.yml 里写的是 QUANT_TUSHARE_TOKEN=${QUANT_TUSHARE_TOKEN:-}，
#    这个 ${} 是 compose 在**创建容器时**做的变量插值，来源是项目根 .env。
#    所以改完 .env 必须 `docker compose up -d`（会 recreate 容器），
#    **`docker restart` 不会重读 .env** —— 容器会一直带着旧值跑，
#    表现为「token 明明换了，抓数还是旧额度」，极难排查。
# 2. 新 token 填错（少一位、复制带空格、后台还没生效）时，直接落盘会让
#    整条数据管道静默失败到下一个定时周期。本脚本先在容器里用候选值
#    试调一次接口，通过才写盘。
# 3. 出任何问题自动回滚 .env 并重建，不留下半截状态。
#
# 用法
# ----
#   ./scripts/rotate_tushare_token.sh --check        # 体检：链路三方是否一致且可用
#   ./scripts/rotate_tushare_token.sh <新token>       # 轮换（带预验证与回滚）
#   ./scripts/rotate_tushare_token.sh <新token> -y    # 跳过交互确认
#   ./scripts/rotate_tushare_token.sh <当前token> -y --force   # 强制重建（排障/验证用）
#
# 前置：Docker Desktop 在跑，容器 quant-backend 已存在。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
SERVICE="backend"
CONTAINER="quant-backend"
KEY="QUANT_TUSHARE_TOKEN"

if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_ERR=$'\033[31m'; C_WARN=$'\033[33m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
  C_OK=''; C_ERR=''; C_WARN=''; C_DIM=''; C_RST=''
fi
ok()   { printf '%s✓%s %s\n' "$C_OK" "$C_RST" "$*"; }
bad()  { printf '%s✗%s %s\n' "$C_ERR" "$C_RST" "$*"; }
warn() { printf '%s!%s %s\n' "$C_WARN" "$C_RST" "$*"; }
dim()  { printf '%s%s%s\n' "$C_DIM" "$*" "$C_RST"; }
die()  { bad "$*"; exit 1; }

# 只暴露 sha 前缀，避免把 token 打进终端滚动记录/截图
fp() { printf '%s' "$1" | shasum -a 256 | cut -c1-12; }
mask() { local t="$1"; printf '%s…%s(len=%d)' "${t:0:6}" "${t: -4}" "${#t}"; }

env_token() { grep -m1 "^${KEY}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"\r' | sed 's/[[:space:]]*$//'; }
ctr_token() { docker exec "$CONTAINER" printenv "$KEY" 2>/dev/null | tr -d '\r\n'; }

container_health() {
  docker inspect "$CONTAINER" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' 2>/dev/null
}

wait_healthy() {
  local limit="${1:-150}" waited=0 st
  while [ "$waited" -lt "$limit" ]; do
    st="$(container_health)"
    [ "$st" = "healthy" ] && { ok "容器健康（${waited}s）"; return 0; }
    [ "$st" = "unhealthy" ] && { bad "容器进入 unhealthy"; return 1; }
    sleep 3; waited=$((waited + 3))
  done
  bad "等待健康超时（${limit}s），当前状态=${st:-unknown}"
  return 1
}

# 用指定 token 在容器里实调一次 Tushare。$1 为空则用容器现有 env。
# 走 _tushare_token 模块，测的是线上真实代码路径，不是另写一套。
probe_api() {
  local tok="${1:-}" out
  if [ -n "$tok" ]; then
    out="$(docker exec -e "$KEY=$tok" "$CONTAINER" python3 -c "
import sys; sys.path.insert(0,'/app/scripts')
from _tushare_token import require_pro
pro = require_pro()
df = pro.trade_cal(exchange='SSE', start_date='20261102', end_date='20261106')
assert len(df) > 0, '返回 0 行'
print('OK', len(df))
" 2>&1)"
  else
    out="$(docker exec "$CONTAINER" python3 -c "
import sys; sys.path.insert(0,'/app/scripts')
from _tushare_token import require_pro
pro = require_pro()
df = pro.trade_cal(exchange='SSE', start_date='20261102', end_date='20261106')
assert len(df) > 0, '返回 0 行'
print('OK', len(df))
" 2>&1)"
  fi
  if printf '%s' "$out" | grep -q '^OK'; then
    ok "Tushare 接口可用（trade_cal $(printf '%s' "$out" | awk '{print $2}') 行）"
    return 0
  fi
  bad "Tushare 接口调用失败："
  printf '%s\n' "$out" | tail -4 | sed 's/^/    /'
  return 1
}

# ─────────────────────────── --check ───────────────────────────
do_check() {
  echo "── Tushare token 链路体检 ──"
  local fail=0

  [ -f "$ENV_FILE" ] || die "找不到 $ENV_FILE"

  local e c
  e="$(env_token)"
  if [ -z "$e" ]; then
    bad ".env 里没有 ${KEY}（或为空）"; fail=1
  else
    ok ".env 有值 ${C_DIM}$(mask "$e") sha=$(fp "$e")${C_RST}"
  fi

  c="$(ctr_token)"
  if [ -z "$c" ]; then
    bad "容器内没有 $KEY —— 容器可能是用旧 .env 创建的"
    warn "修法：docker compose up -d ${SERVICE}（不能用 restart）"
    fail=1
  else
    ok "容器内有值 ${C_DIM}$(mask "$c") sha=$(fp "$c")${C_RST}"
  fi

  if [ -n "$e" ] && [ -n "$c" ]; then
    if [ "$e" = "$c" ]; then
      ok ".env 与容器一致"
    else
      bad ".env 与容器**不一致** —— 改了 .env 但没重建容器"
      warn "修法：docker compose up -d $SERVICE"
      fail=1
    fi
  fi

  # 源码不该有明文（这次改造守住的不变量）
  local leaks
  leaks="$(grep -rlE '\b[0-9a-f]{56}\b' "$ROOT/backend/scripts" --include='*.py' 2>/dev/null | head -5)"
  if [ -z "$leaks" ]; then
    ok "backend/scripts 下无硬编码 token"
  else
    bad "疑似硬编码 token：$leaks"; fail=1
  fi

  # 实调一次，确认当前容器里的 token 真的能用
  probe_api "" || fail=1

  echo
  if [ "$fail" -eq 0 ]; then
    ok "链路正常：.env = 容器 = 可用"
    return 0
  fi
  bad "体检发现 $fail 项问题"
  return 1
}

# ─────────────────────────── 轮换 ───────────────────────────
do_rotate() {
  local new="$1"
  shift
  local assume_yes="" force=""
  for a in "$@"; do
    case "$a" in
      -y|--yes) assume_yes="-y" ;;
      --force)  force="1" ;;
      *) die "未知参数：$a" ;;
    esac
  done

  # 1) 格式校验：Tushare token 是长 hex 串，长度不写死（后台可能调整），
  #    但必须无空白、全字母数字、长度合理。
  case "$new" in
    *[[:space:]]*) die "token 里含空白字符（多半是复制时带进来的）" ;;
  esac
  if ! printf '%s' "$new" | grep -qE '^[0-9a-zA-Z]{32,128}$'; then
    die "token 格式可疑（要求 32-128 位字母数字，实际 ${#new} 位）"
  fi

  local cur
  cur="$(env_token)"
  if [ "$new" = "$cur" ]; then
    if [ -z "$force" ]; then
      warn "新值与 .env 现有值相同（sha=$(fp "$new")），只做复验，不改配置"
      probe_api "$new" || die "当前 token 本身不可用"
      do_check
      return $?
    fi
    warn "--force：值与现有相同，仍强制走「写盘 + 重建 + 复验」全链路"
    dim "用途：验证轮换流程、清理容器中可能残留的旧 env"
  fi

  echo "── 轮换 Tushare token ──"
  dim "当前 $(mask "$cur") sha=$(fp "$cur")"
  dim "新值 $(mask "$new") sha=$(fp "$new")"

  if [ "$assume_yes" != "-y" ]; then
    printf '确认轮换并重建容器 %s？[y/N] ' "$CONTAINER"
    read -r ans
    case "$ans" in y|Y|yes|YES) ;; *) echo "已取消"; return 1 ;; esac
  fi

  # 2) 预验证：先用候选值试调，**不碰任何配置**。填错就在这一步挡住。
  echo "[1/5] 预验证新 token"
  if ! probe_api "$new"; then
    bad "新 token 试调失败 —— 配置未改动，服务不受影响"
    warn "常见原因：复制不完整、后台尚未生效、额度/权限变更"
    return 1
  fi

  # 3) 备份 + 原子写（保留 600 权限，不留 .env.tmp 残骸）
  echo "[2/5] 写入 .env"
  local bak="$ENV_FILE.bak.$(date +%Y%m%d_%H%M%S)"
  cp -p "$ENV_FILE" "$bak" || die "备份失败，已中止（未做任何修改）"
  ok "备份 $bak"

  if ! python3 - "$ENV_FILE" "$KEY" "$new" <<'PY'
import os, sys, tempfile

path, key, token = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, encoding="utf-8") as f:
    lines = f.read().splitlines()

out, seen = [], False
for ln in lines:
    if ln.strip().startswith(key + "="):
        out.append(f"{key}={token}")
        seen = True
    else:
        out.append(ln)
if not seen:
    out.append(f"{key}={token}")

fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".env.rot.")
os.close(fd)
try:
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)          # 原子替换，不会出现半写的 .env
except Exception:
    if os.path.exists(tmp):
        os.unlink(tmp)
    raise
PY
  then
    cp -p "$bak" "$ENV_FILE"
    die "写 .env 失败，已回滚"
  fi
  ok ".env 已更新（权限 $(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || echo 600)）"

  # 4) 重建容器。restart 不会重新插值 ${}，必须 up -d。
  #    compose 只在配置哈希变化时才 Recreate；值变了必然重建，
  #    没变则输出 Running —— 后者正是下面复验要抓的情况。
  echo "[3/5] 重建容器（docker compose up -d ${SERVICE}）"
  local up_out
  if ! up_out="$(docker compose -f "$ROOT/docker-compose.yml" up -d "$SERVICE" 2>&1)"; then
    printf '%s\n' "$up_out" | tail -5 | sed 's/^/    /'
    bad "重建失败，回滚 .env"
    cp -p "$bak" "$ENV_FILE"
    docker compose -f "$ROOT/docker-compose.yml" up -d "$SERVICE" >/dev/null 2>&1
    die "已回滚到旧 token，请检查 docker compose 输出"
  fi
  printf '%s\n' "$up_out" | tail -3 | sed 's/^/    /'
  if printf '%s' "$up_out" | grep -qiE 'Recreated|Created'; then
    ok "容器已重建（新 env 生效）"
  else
    warn "compose 未重建容器 —— 配置哈希没变（多半是新旧值相同）"
  fi

  # 5) 复验：容器内值是否已换、接口是否可用
  echo "[4/5] 等待健康检查"
  if ! wait_healthy 150; then
    bad "健康检查未通过，回滚 .env"
    cp -p "$bak" "$ENV_FILE"
    docker compose -f "$ROOT/docker-compose.yml" up -d "$SERVICE" >/dev/null 2>&1
    die "已回滚到旧 token"
  fi

  echo "[5/5] 复验"
  local now
  now="$(ctr_token)"
  if [ "$now" != "$new" ]; then
    # 容器里还是旧值 = compose 没重建（或重建了但没读到新 .env）。
    # 这种状态下文件与实际运行态不一致，必须回滚文件，否则下次任何一次
    # 意外重启都会让「以为换过」的 token 突然生效一半。
    bad "容器内仍是旧值（sha=$(fp "$now")）—— compose 没有重建容器"
    warn "已回滚 .env，保持文件与运行态一致"
    cp -p "$bak" "$ENV_FILE"
    warn "手工排查：docker compose -f $ROOT/docker-compose.yml config | grep TUSHARE"
    return 1
  fi
  ok "容器内已切换为新值（sha=$(fp "$now")）"
  probe_api "" || die "重建后接口不可用（配置已落盘，请手动检查）"

  echo
  ok "轮换完成"
  dim "旧值备份在 ${bak}（确认稳定后可删）"
  return 0
}

# ─────────────────────────── 入口 ───────────────────────────
main() {
  [ -f "$ROOT/docker-compose.yml" ] || die "不在预期目录：$ROOT"
  docker info >/dev/null 2>&1 || die "Docker 未运行"

  case "${1:-}" in
    ""|-h|--help)
      sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    --check)
      do_check
      ;;
    *)
      do_rotate "$@"
      ;;
  esac
}

main "$@"
