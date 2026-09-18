#!/bin/sh
# 把 quant-platform 后端切回 launchd 托管（改进版配置）
#
# 背景：2026-09-17 排查「数据监控/数据管道打不开」时发现——
#   旧 plist 直接跑 uvicorn + KeepAlive=true。一旦 8000 被占用，
#   uvicorn bind 失败以退出码 1 退出 → launchd 每 5 秒重启一次（死循环）；
#   更糟的是失败进程已跑完 lifespan startup（拉起 data_scheduler daemon 线程，
#   正阻塞在 subprocess.run 跑数据管道），无法干净退出 → 变成占着 8000 的新僵尸，
#   循环自我维持，并反复 unlink SQLite 的 -wal → 正常服务进程的 fd 变成幽灵 inode
#   → 所有 SQLite 接口 500（database disk image is malformed）。
#
# 本脚本做三件事：
#   1) 卸载旧的 job、杀掉当前占用 8000 的进程
#   2) 用改进后的 plist 重新加载（包装脚本 + KeepAlive 仅异常退出时重启）
#   3) 校验服务与 job 状态
#
# ⚠️ 必须在**你自己的终端**运行——launchd 的 bootstrap 在受控沙箱里会报
#    "Bootstrap failed: 5: Input/output error"，这是环境限制，不是配置错误。

set -e
LABEL=com.quant.backend
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_=$(id -u)

echo "== 1/4 校验 plist =="
plutil -lint "$PLIST"

echo "== 2/4 卸载旧 job + 释放 8000 =="
launchctl bootout "gui/$UID_/$LABEL" 2>/dev/null || echo "  (job 本未加载)"
PID=$(lsof -ti:8000 -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$PID" ]; then
  echo "  杀掉占用 8000 的进程: $PID"
  kill $PID 2>/dev/null || true
  sleep 3
else
  echo "  8000 已空闲"
fi

echo "== 3/4 用新配置加载 =="
launchctl bootstrap "gui/$UID_" "$PLIST"
sleep 5

echo "== 4/4 校验 =="
launchctl print "gui/$UID_/$LABEL" 2>&1 | grep -E "^	state|active count|runs =|pid =" | head -5
echo -n "  openapi: "
curl -s -m 10 -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/openapi.json

echo
echo "完成。若上面显示 active count = 1 且 openapi: 200，即已切回 launchd 托管。"
echo "之后【不要】再手动起第二个 uvicorn——那正是本次故障的触发条件。"
