#!/bin/sh
# 基金历史日频净值拉完后，自动完成「入库 → 全量净值回归 → 刷新页面缓存」。
#
# 独立性：所有写入都落在 data/fund_research.db（宿主机独占写、容器只读），
# 不触碰 bind mount 上的 quant_dev.db，因此不需要重启 quant-backend。
#
# 手动兜底：
#   cd ~/quant-platform && \
#     /Users/happyljew/WorkBuddy/2026-11-06-13-53-26/.venv/bin/python \
#       backend/scripts/import_fund_nav.py && \
#     /Users/happyljew/WorkBuddy/2026-11-06-13-53-26/.venv/bin/python \
#       backend/scripts/build_fund_regression.py
cd /Users/happyljew/quant-platform || exit 1
PY=/Users/happyljew/WorkBuddy/2026-11-06-13-53-26/.venv/bin/python
LOG=data/research/regression.log
echo "[$(date '+%F %T')] 等待净值拉取完成（监听 fetch_nav.lock）..." >> "$LOG"
while [ -f data/research/fetch_nav.lock ]; do sleep 20; done
sleep 5
echo "[$(date '+%F %T')] 导入净值到独立研究库" >> "$LOG"
"$PY" backend/scripts/import_fund_nav.py >> "$LOG" 2>&1 || exit 1
echo "[$(date '+%F %T')] 全量约束 WLS 净值回归" >> "$LOG"
"$PY" backend/scripts/build_fund_regression.py >> "$LOG" 2>&1
RC=$?
echo "[$(date '+%F %T')] 回归隐含个股持仓（第五轨）" >> "$LOG"
"$PY" backend/scripts/build_regression_holdings.py >> "$LOG" 2>&1
RC=$((RC + $?))
echo "[$(date '+%F %T')] FOF 可穿透性诊断" >> "$LOG"
"$PY" backend/scripts/build_fof_diagnosis.py >> "$LOG" 2>&1
RC=$((RC + $?))
echo "[$(date '+%F %T')] 回归链路结束 exit=$RC" >> "$LOG"
exit $RC
