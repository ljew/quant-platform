#!/bin/sh
# wewe-rss 服务启动包装（launchd 用）
#
# 职责：把 Nest 服务拉起来（:4000）。它按 .env 里的 CRON_EXPRESSION（每 3 小时）
# 从微信读书接口拉各订阅号的最新文章列表，写入 data/wechat/wewe-rss.db。
# 正文抓取由 run_daily_crawl.sh 另跑，两者解耦。
#
# 端口预检：已被占用就 exit 0 正常退出。配合 KeepAlive={SuccessfulExit:false}
# 可杜绝「bind 失败 → 退出码 1 → 每 5 秒重启」的死循环。

set -u

PORT=4000
PROJ=/Users/happyljew/quant-platform
SERVER="$PROJ/vendor/wechat-rss"
LOG_DIR="$PROJ/data/wechat/state"
LOG="$LOG_DIR/wewe-rss.log"

mkdir -p "$LOG_DIR"

# —— node 定位 ——
# 本机没有系统 node，只有 WorkBuddy 自带的。版本升级会换路径，所以按目录扫描取最新。
NODE=""
for cand in $(ls -d /Users/happyljew/.workbuddy/binaries/node/versions/*/bin/node 2>/dev/null | sort -V -r); do
  [ -x "$cand" ] && NODE="$cand" && break
done
if [ -z "$NODE" ]; then
  NODE="$(command -v node 2>/dev/null || true)"
fi
if [ -z "$NODE" ] || [ ! -x "$NODE" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 找不到 node，无法启动 wewe-rss" >> "$LOG"
  exit 1
fi

# —— 端口预检 ——
if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] :$PORT 已被占用，本次不启动" >> "$LOG"
  exit 0
fi

# 日志超 5MB 就地截断（mv 会换 inode）
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 5242880 ]; then
  : > "$LOG"
fi

cd "$SERVER" || exit 1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启动 wewe-rss node=${NODE}" >> "$LOG"
exec "$NODE" dist/main >> "$LOG" 2>&1
