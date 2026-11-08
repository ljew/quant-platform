#!/bin/sh
# 自动完成「导入 → 重算主口径 → 重跑净值回归 → 回归隐含个股 → FOF 诊断 → 刷新缓存」。
# 手动兜底顺序不能颠倒（主算法会重写缓存，回归脚本随后恢复 regression 字段）：
# 所有写入落在 data/fund_research.db（宿主机独占写、容器只读），不需要重启容器。
#
# 手动兜底：
#   cd ~/quant-platform && \
#     <venv>/bin/python backend/scripts/import_csrc_industry.py && \
#     <venv>/bin/python backend/scripts/build_fund_penetration.py && \
#     <venv>/bin/python backend/scripts/build_fund_regression.py && \
#     <venv>/bin/python backend/scripts/build_regression_holdings.py && \
#     <venv>/bin/python backend/scripts/build_fof_diagnosis.py
cd /Users/happyljew/quant-platform || exit 1
PY=/Users/happyljew/WorkBuddy/2026-11-06-13-53-26/.venv/bin/python
LOG=data/research/regulatory.log
TOTAL=$("$PY" -c "
import sqlite3
con=sqlite3.connect('file:data/fund_research.db?mode=ro',uri=True)
n=con.execute('select count(distinct base_code) from fund_hold_coverage').fetchone()[0]
con.close()
print(n*11)
")
echo "[$(date '+%F %T')] 等待监管行业配置拉取完成（任务总数 $TOTAL）..." >> "$LOG"
while true; do
    N=$("$PY" -c "
import json
try: print(len(json.load(open('data/research/progress_csrc.json'))['done']))
except Exception: print(0)
" 2>/dev/null)
    [ "${N:-0}" -ge "$TOTAL" ] && break
    sleep 20
done
sleep 5
echo "[$(date '+%F %T')] 导入监管数据" >> "$LOG"
"$PY" backend/scripts/import_csrc_industry.py >> "$LOG" 2>&1 || exit 1
echo "[$(date '+%F %T')] 用全量监管仓位重算主口径" >> "$LOG"
"$PY" backend/scripts/build_fund_penetration.py >> "$LOG" 2>&1 || exit 1
echo "[$(date '+%F %T')] 重跑净值回归校验" >> "$LOG"
"$PY" backend/scripts/build_fund_regression.py >> "$LOG" 2>&1
RC=$?
echo "[$(date '+%F %T')] 回归隐含个股持仓（第五轨）" >> "$LOG"
"$PY" backend/scripts/build_regression_holdings.py >> "$LOG" 2>&1
RC=$((RC + $?))
echo "[$(date '+%F %T')] FOF 可穿透性诊断" >> "$LOG"
"$PY" backend/scripts/build_fof_diagnosis.py >> "$LOG" 2>&1
RC=$((RC + $?))
echo "[$(date '+%F %T')] 监管链路结束 exit=$RC" >> "$LOG"
exit $RC
