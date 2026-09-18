#!/bin/sh
# quant-platform 后端启动包装（launchd 用）
#
# 为什么需要它：plist 原先直接跑 uvicorn，当 8000 已被占用时 uvicorn 会 bind 失败、
# 以退出码 1 退出；配合 KeepAlive=true，launchd 每 5 秒重启一次形成死循环。
# 更糟的是：失败进程已执行完 lifespan startup（启动了 data_scheduler 的 daemon 线程，
# 该线程正阻塞在 subprocess.run 跑数据管道），导致进程无法干净退出，变成"占着 8000
# 的新僵尸"——于是循环自我维持，并反复 unlink SQLite 的 -wal 文件，
# 把正常服务进程的 wal fd 变成幽灵 inode（症状：database disk image is malformed）。
#
# 本脚本在端口已被占用时直接 exit 0（正常退出）。配合 plist 里的
#   KeepAlive = { SuccessfulExit = false }
# launchd 不会重启，从设计上杜绝上述循环。
#
# 用法：plist 的 ProgramArguments 改为 /bin/sh + 本脚本绝对路径。

PORT=8000
PY=/Users/happyljew/.workbuddy/binaries/python/envs/quant/bin/python
BACKEND=/Users/happyljew/Desktop/kimiwork/Quant/quant-platform/backend

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') 端口 $PORT 已被占用，正常退出（不触发 KeepAlive 重启）"
  exit 0
fi

cd "$BACKEND" || exit 1
export PYTHONPATH="$BACKEND"

# exec 让 uvicorn 成为主进程，launchd 才能直接管理它的生死
exec "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
