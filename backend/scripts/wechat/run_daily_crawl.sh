#!/bin/sh
# 微信公众号每日增量抓取（launchd 用）
#
# 干什么：
#   1. 确认 wewe-rss（:4000）活着 —— 它负责按 cron 从微信读书拉文章列表
#   2. 跑 crawl_content.py --since-days N 把新文章的正文抓进 knowledge.db
#
# 为什么只抓近 N 天：历史 7000 篇已在库，每日增量通常 10-60 篇。不限天数的话
#   一旦列表库积压几千篇未抓，脚本会闷头跑几小时且高频请求 mp.weixin.qq.com
#   容易触发风控。默认 7 天，留足补抓窗口。
#
# 端口预检的意义：wewe-rss 挂了不代表库不能用，此时直接跳过（exit 0），
#   不该让 launchd 判定失败后疯狂重启。

set -u

PORT=4000
PY=/Users/happyljew/.workbuddy/binaries/python/envs/quant/bin/python
PROJ=/Users/happyljew/quant-platform
SCRIPT="$PROJ/backend/scripts/wechat/crawl_content.py"
LOG_DIR="$PROJ/data/wechat/state"
LOG="$LOG_DIR/crawl_daily.log"
DAYS="${WECHAT_CRAWL_DAYS:-7}"

mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# 日志超 5MB 就地截断（mv 会换 inode，launchd 仍写旧文件）
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 5242880 ]; then
  : > "$LOG"
fi

# —— 1. wewe-rss 存活检查 ——
if ! lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  log "wewe-rss 未在 :$PORT 监听，跳过本次抓取（列表库无法更新）"
  exit 0
fi

# —— 2. 抓正文 ——
log "开始增量抓取（近 $DAYS 天）"
cd "$PROJ/backend/scripts/wechat" || exit 1
"$PY" "$SCRIPT" --since-days "$DAYS" >> "$LOG" 2>&1
RC=$?
log "抓取结束 rc=$RC"

# 抓取失败（连续 blocked 等）不算 launchd 故障，避免反复重启
exit 0
