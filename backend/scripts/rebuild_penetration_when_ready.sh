#!/bin/sh
# 全量持仓拉数完成后，自动重算「持仓补齐 + 行业穿透」并刷新页面缓存。
#
# 背景：fetch_fund_holdings.py 逐只基金拉取约需 40+ 分钟，期间页面展示的是
# 已完成部分（缓存里带 fetch_progress，不会谎报全量）。锁文件释放即代表拉数结束。
#
# 也可手动执行（若后台任务被宿主回收）：
#   cd ~/quant-platform && \
#     /Users/happyljew/WorkBuddy/2026-11-06-13-53-26/.venv/bin/python \
#       backend/scripts/build_fund_penetration.py
cd /Users/happyljew/quant-platform || exit 1
PY=/Users/happyljew/WorkBuddy/2026-11-06-13-53-26/.venv/bin/python
LOG=data/holdings/penetration.log
echo "[$(date '+%F %T')] 等待全量拉数完成（监听 fetch.lock）..." >> "$LOG"
while [ -f data/holdings/fetch.lock ]; do sleep 20; done
sleep 5
echo "[$(date '+%F %T')] 开始全量重算" >> "$LOG"
"$PY" backend/scripts/build_fund_penetration.py >> "$LOG" 2>&1
RC=$?
# 穿透数据已拆到独立 data/fund_research.db（宿主机独占写、容器只读），
# 不再触碰 bind mount 上的 quant_dev.db，因此无需重启容器重建 WAL 视图。
echo "[$(date '+%F %T')] 全量重算结束 exit=$RC" >> "$LOG"
exit $RC
