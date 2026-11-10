"""任务单：Planner（大模型）与编排器（代码）之间唯一的接口。

这个文件是整个架构的分界线：
  · 往上 —— Planner 用大模型理解人的话，产出「任务单」
  · 往下 —— 代码校验任务单、派发、执行

为什么要中间这层 JSON，而不是让模型直接调工具：
  ① **可校验**：agent 名、工具名必须在白名单里，越权的直接拒绝
  ② **可拒绝**：模型想调没授权的工具 → 编排器挡掉，不靠它自觉
  ③ **可重放**：任务单落库，同样的输入可以跑出同样的路径
  ④ **可降级**：模型挂了，用规则模板也能产出基础任务单

这就是「编排层确定性、决策层概率性」的落地形态。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import personas
from .llm import LLMError, chat_json
from .tools import REGISTRY

MAX_TASKS = 4


@dataclass
class Task:
    agent: str
    goal: str
    need: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"agent": self.agent, "goal": self.goal, "need": self.need}


@dataclass
class Plan:
    intent: str
    summary: str
    tasks: list[Task]
    rejected: list[str] = field(default_factory=list)  # 被挡掉的越权请求

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "summary": self.summary,
            "tasks": [t.to_dict() for t in self.tasks],
            "rejected": self.rejected,
        }


def _planner_prompt() -> str:
    roster = []
    for k, p in personas.PERSONAS.items():
        roster.append(f"- {k}（{p['name']}，{p['level']} 级）：{p['tools']}")
    roster_text = "\n".join(roster)
    tools_text = "\n".join(f"- {n}：{REGISTRY[n]['desc']}" for n in REGISTRY)
    return f"""\
你是一个量化投研平台的调度员。你的唯一工作是把用户的问题拆成 1~{MAX_TASKS} 个子任务，
分派给下面这些智能体。**你自己不查数据、不下结论。**

可用智能体（名字 → 能用的工具）：
{roster_text}

可用工具的说明：
{tools_text}

分派原则：
1. 只派真正需要的。一个简单问题派 1 个就够了，不要为了"全面"硬凑。
2. 每个子任务说清楚要它干什么（goal），以及大概要用哪个工具（need，可留空）。
3. need 里只能写该智能体白名单里的工具 —— 写错了会被系统剔除。
4. 如果问题需要"历史表现 + 当前是否失效 + 外部原因"三方面，通常派 3 个：
   strategy（历史）+ risk（去留判断）+ news（事件背景）。
5. 涉及下单/建仓/改配置的问题，派 portfolio 或 trader，它们只会给建议。

只输出 JSON，不要任何解释：
{{
  "intent": "一句话说明用户到底想问什么",
  "summary": "你对这个问题的理解（30 字内）",
  "tasks": [
    {{"agent": "strategy", "goal": "具体要它做什么", "need": ["get_backtests"]}}
  ]
}}
"""


def make_plan(question: str) -> Plan:
    """调大模型产出任务单（不做校验）。"""
    msgs = [
        {"role": "system", "content": _planner_prompt()},
        {"role": "user", "content": f"用户问题：{question}"},
    ]
    raw = chat_json(msgs, temperature=0.1, max_tokens=800)
    tasks_raw = raw.get("tasks") or []
    tasks = [
        Task(
            agent=str(t.get("agent") or "").strip(),
            goal=str(t.get("goal") or "").strip(),
            need=list(t.get("need") or []),
        )
        for t in tasks_raw
        if isinstance(t, dict)
    ]
    return Plan(
        intent=str(raw.get("intent") or question)[:200],
        summary=str(raw.get("summary") or "")[:120],
        tasks=tasks,
    )


def validate(plan: Plan) -> Plan:
    """校验并修正任务单。这是「代码的地盘」—— 不调用任何大模型。

    修正规则（都是静默修正 + 记录，不抛异常，保证流程能走下去）：
      · agent 不存在 → 丢弃该任务
      · 任务数超限 → 截断
      · 同一 agent 出现多次 → 合并 goal
      · need 里有越权工具 → 剔除并记入 rejected
      · need 为空 → 填该 agent 的全部白名单（让它自己决定用哪个）
    """
    kept: dict[str, Task] = {}

    for t in plan.tasks[:MAX_TASKS * 2]:  # 先宽进，后面再截
        p = personas.get(t.agent)
        if not p:
            plan.rejected.append(f"未知智能体：{t.agent}")
            continue

        allowed = set(p["tools"])
        bad = [n for n in t.need if n not in allowed]
        for n in bad:
            plan.rejected.append(f"{t.agent} 无权使用 {n}（已剔除）")

        need = [n for n in t.need if n in allowed] or list(p["tools"])

        if t.agent in kept:
            kept[t.agent].goal += "；" + t.goal
        else:
            kept[t.agent] = Task(agent=t.agent, goal=t.goal, need=need)

    plan.tasks = list(kept.values())[:MAX_TASKS]

    if not plan.tasks:
        # 兜底：至少派一个风控（它是读权限最全、判断最保守的一个）
        plan.tasks = [Task(agent="risk", goal=plan.intent, need=list(personas.PERSONAS["risk"]["tools"]))]
        plan.rejected.append("原始任务单为空或全部非法，已兜底派给风控")

    return plan


def _rule_based_plan(question: str) -> Plan:
    """降级方案：大模型不可用时，用关键词命中一套固定模板。

    这不是「智能」，但保证链路不断 —— 对应架构里的「可降级」。
    """
    q = question.lower()
    if any(k in q for k in ("数据", "健康", "缺", "断")):
        return Plan("data_health", "数据健康检查", [
            Task("data-eng", "检查五层健康，列出异常", ["get_framework_health"]),
        ])
    if any(k in q for k in ("模拟盘", "组合", "仓位")):
        return Plan("portfolio", "组合与模拟盘状态", [
            Task("trader", "报告模拟盘净值与运行状态", ["list_paper_tasks"]),
            Task("risk", "评估在跑策略的风险", ["get_signal_stats"]),
        ])
    # 默认：当策略评估处理
    return Plan("evaluate", question[:80], [
        Task("strategy", "查历史回测与近期表现", ["get_backtests", "get_signal_stats"]),
        Task("risk", "判断该升级/降级/退役", ["get_research_card", "get_backtests"]),
        Task("news", "看近期有无相关事件", ["get_latest_digest"]),
    ])


def plan(question: str, use_llm: bool = True) -> Plan:
    """完整流程：产出 → 校验 → 返回。LLM 失败时降级到规则模板。"""
    if not use_llm:
        return validate(_rule_based_plan(question))
    try:
        p = make_plan(question)
    except LLMError as e:
        p = _rule_based_plan(question)
        p.rejected.append(f"Planner 调用失败（{str(e)[:60]}），已降级到规则模板")
    return validate(p)


if __name__ == "__main__":  # 自测：不调 LLM，只看规则模板
    for q in ("缠论还值不值得跟", "数据有没有问题", "模拟盘怎么样"):
        p = validate(_rule_based_plan(q))
        print(q, "→", json.dumps(p.to_dict(), ensure_ascii=False))
