#!/bin/sh
# Docker Desktop 兜底看门狗 —— 每 5 分钟检查一次后端端口，没起来就把 Docker 拉起来。
#
# 为什么需要（2026-11-16）：
#   Docker Desktop 的「Start Docker Desktop when you sign in」是**登录项**，
#   只有用户登录进桌面才触发。11-16 机器 08:51 开机，但直到 14:09 才有人登录，
#   于是后端整整 5 小时 18 分不可用 —— 期间所有定时任务（06:20 采集、17:30 编排）
#   全部错过。settings-store.json 里 AutoStart 一直是 True，配置没错，
#   错在「开机 ≠ 登录」。
#
# 这个脚本补的就是那段空窗：登录后最多 5 分钟，服务就会被拉起来。
#
# 退出码恒为 0 —— launchd 的 StartInterval 任务报错只会刷日志，没有意义。

PORT=8000
LOG_DIR="$HOME/quant-platform/data"
LOG="$LOG_DIR/docker-guard.log"

mkdir -p "$LOG_DIR" 2>/dev/null
say() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG" 2>/dev/null; }

port_up() {
  /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
}

if port_up; then
  exit 0
fi

# 第一次探测失败：可能是容器正在启动（Docker 起来了但后端还在初始化表）。
# 给 90 秒，别再补一刀。
say "端口 $PORT 未监听，等 90 秒再看"
sleep 90
if port_up; then
  say "已自行恢复（容器启动中），不干预"
  exit 0
fi

# 真的没起：拉起 Docker Desktop
if /usr/bin/pgrep -xq Docker 2>/dev/null || /usr/bin/pgrep -fq "Docker Desktop" 2>/dev/null; then
  say "Docker 进程在但端口不通（容器没起？），尝试 docker start"
  /usr/local/bin/docker start quant-backend >/dev/null 2>&1 || true
else
  say "Docker 未运行，拉起 Docker Desktop"
  /usr/bin/open -a Docker >/dev/null 2>&1 || say "open -a Docker 失败"
fi

# 再给 180 秒观察一轮，把结果写进日志（方便事后判断兜底有没有用）
i=0
while [ "$i" -lt 18 ]; do
  sleep 10
  i=$((i + 1))
  if port_up; then
    say "恢复成功（${i}0 秒后端口通了）"
    exit 0
  fi
done
say "拉起后 180 秒仍未通，需人工检查"
exit 0
