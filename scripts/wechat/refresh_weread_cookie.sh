#!/bin/sh
# 微信读书 Cookie 每日续期 —— launchd / 手动都调这个
#
# launchd 的 PATH 极简，也没有 WorkBuddy 注入的环境变量，所以这里显式找 node
# 并设置 NODE_PATH。WorkBuddy 升级会换版本目录，故用通配符取最新一个。
set -eu

ROOT="/Users/happyljew/quant-platform"
NODE="${WEREAD_NODE:-}"

if [ -z "$NODE" ]; then
  for c in /Users/happyljew/.workbuddy/binaries/node/versions/*/bin/node \
           /opt/homebrew/bin/node /usr/local/bin/node; do
    if [ -x "$c" ]; then NODE="$c"; break; fi
  done
fi
[ -n "$NODE" ] || { echo "找不到 node 可执行文件"; exit 1; }

NODE_PATH="${NODE_PATH:-/Users/happyljew/.workbuddy/binaries/node/workspace/node_modules}"
export NODE_PATH

exec "$NODE" "$ROOT/scripts/wechat/refresh_weread_cookie.js"
