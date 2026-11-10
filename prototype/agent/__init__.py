"""量化投研平台 · 多智能体原型（只读）

跑法：
    cd ~/quant-platform/prototype
    python3 -m agent.cli "缠论还值不值得跟"

设计要点（对应设计文档 PART 2）：
    plan.py         任务单 = 大模型与代码的分界线
    personas.py     8 个数字人：人格 + 工具白名单 + 硬约束
    tools.py        9 个只读工具，每个结果带 source
    orchestrator.py 并行执行 + 汇总（零 LLM 派发）
    audit.py        每次工具调用落库，可倒查
"""

__version__ = "0.1.0"
