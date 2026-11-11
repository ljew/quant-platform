#!/bin/zsh
# 会话备份：~/quant-agent/sessions → ~/quant-agent/backups/sessions_<ts>.tar.gz
#
# 为什么要有这个：2026-11-11 我（阿策）连着两次用 `ls -t | head -N` 批量删
# 测试会话，第二次把老刘三个真实会话一起删了 —— `rm -rf` 不可恢复，dsh 也没有
# 回收站。会话是 append 写的 zstd，删了就是没了。
#
# 所以：dsh 每次启动前先打一份包，保留最近 10 份（约 10 次重启的跨度）。
# 手工需要时也可以直接跑本脚本。
set -u

SRC="$HOME/quant-agent/sessions"
DST="$HOME/quant-agent/backups"
KEEP=10

[ -d "$SRC" ] || { echo "没有 $SRC，跳过"; exit 0; }

# 会话总量很小（几 MB），不必先判断大小
mkdir -p "$DST" || exit 0
ts=$(date +%Y%m%d_%H%M%S)
out="$DST/sessions_$ts.tar.gz"

# 排除 .lock（无意义）与正在写的临时文件
if tar -czf "$out" \
      --exclude='*.lock' \
      -C "$HOME/quant-agent" sessions 2>/dev/null; then
  echo "会话已备份：$out（$(du -h "$out" | cut -f1)）"
else
  echo "备份失败（会话可能正在写），跳过" >&2
  rm -f "$out"
  exit 0
fi

# 只留最近 KEEP 份
ls -t "$DST"/sessions_*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do
  rm -f "$f"
done
