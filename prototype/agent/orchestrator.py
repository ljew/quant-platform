"""编排器：校验过的任务单 → 并行执行 → 汇总。

这是「代码的地盘」：派发、并行、超时、重试、落审计，一条 LLM 调用都没有。
大模型只出现在两头 —— Planner（理解问题）和 Aggregator（组织语言）。
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import audit, personas
from .llm import LLMError, chat, chat_json
from .plan import Plan, Task
from .tools import REGISTRY, call_tool, tool_specs

MAX_TOOL_ROUNDS = 4          # 单个 agent 最多几轮工具调用
AGENT_TIMEOUT_HINT = 60      # 单个 agent 的软预算（秒），超了由上层决定是否放弃

OUTPUT_SPEC = """

【输出格式 · 必须遵守】
当信息足够（或确实查不到）时，**直接输出一个 JSON**，不要 markdown 代码块、不要解释：
{
  "finding": "一句话结论（不超过 60 字）",
  "evidence": [
    {"claim": "结论里的某个具体说法", "source": "接口名或回测 id", "value": "具体数值"}
  ],
  "confidence": "high | medium | low",
  "missing": "没查到的部分；全都查到了就填空字符串"
}
"""


def _tool_args(tc: dict) -> dict:
    raw = (tc.get("function") or {}).get("arguments") or "{}"
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


def _strip_fence(text: str) -> str:
    """模型偶尔会套 ```json ... ```，剥掉。"""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
    if m:
        t = m.group(1).strip()
    # 还不行就取第一个 { 到最后一个 }
    if not t.startswith("{"):
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i:j + 1]
    return t


def run_agent(run_id: int, task: Task, context: str = "") -> dict:
    """跑一个智能体：给它任务 + 它的工具白名单，产出结构化小结。"""
    p = personas.get(task.agent)
    if not p:
        return {"agent": task.agent, "ok": False, "error": "未知智能体"}

    # ⚠️ 工具给**全部白名单**，不是 task.need。
    # 踩过的坑：Planner 的 need 只写了两三个工具，agent 就只能用这几个，
    # 而它的 system 里规定"查失效条件"—— 结果它如实报告"该工具不在我的列表内"，
    # 白白浪费一轮。need 是**优先级提示**，不是权限边界；权限边界是 persona.tools。
    specs = tool_specs(p["tools"])
    focus = [n for n in task.need if n in p["tools"]]
    msgs = [
        {"role": "system", "content": p["system"] + personas.COMMON_RULES + OUTPUT_SPEC},
        {"role": "user", "content": (
            f"你的任务：{task.goal}\n"
            f"可用工具（全部）：{', '.join(p['tools'])}\n"
            + (f"建议优先用：{', '.join(focus)}\n" if focus else "")
            + (f"背景：{context}\n" if context else "")
        )},
    ]

    calls = 0
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    t0 = time.time()

    for _ in range(MAX_TOOL_ROUNDS):
        r = chat(msgs, tools=specs or None, temperature=0.2, max_tokens=1500)
        u = r.get("usage") or {}
        usage["prompt_tokens"] += u.get("prompt_tokens") or 0
        usage["completion_tokens"] += u.get("completion_tokens") or 0

        if not r["tool_calls"]:
            # 没有工具调用了 → 它给了最终答复
            text = _strip_fence(r["content"])
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {"finding": r["content"][:300], "evidence": [],
                        "confidence": "low", "missing": "模型未按 JSON 输出"}
            data.update({
                "agent": task.agent,
                "name": p["name"],
                "ok": True,
                "calls": calls,
                "ms": int((time.time() - t0) * 1000),
                "usage": usage,
            })
            return data

        # 有工具调用 → 执行，把结果喂回去
        msgs.append({
            "role": "assistant",
            "content": r["content"] or "",
            "tool_calls": r["tool_calls"],
        })
        for tc in r["tool_calls"]:
            name = (tc.get("function") or {}).get("name") or ""
            args = _tool_args(tc)
            st = time.time()
            result = call_tool(name, args)      # ← 白名单在这里生效
            ms = int((time.time() - st) * 1000)
            calls += 1
            audit.log_action(run_id, task.agent, name, args, result, ms)
            msgs.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or name,
                "content": json.dumps(result, ensure_ascii=False)[:4000],
            })

    # 轮次用尽还没收敛
    return {
        "agent": task.agent, "name": p["name"], "ok": False,
        "finding": "工具调用轮次用尽，未能得出结论",
        "evidence": [], "confidence": "low", "missing": "需要更多轮次",
        "calls": calls, "ms": int((time.time() - t0) * 1000), "usage": usage,
    }


def _aggregator_prompt() -> str:
    return """\
你是这家投研平台的「总研」。下面几个专业智能体已经各自查完了数据，你要把它们的结果
组织成一个给人看的回答。

【铁律】
1. 你**不能引入任何新事实**。只能引用下面这些小结里已经出现的内容。
2. 小结之间矛盾时，如实指出矛盾，不要自己调和成"综合来看"。
3. 某个智能体说 missing 非空（没查到），你要在回答里明确写出这个缺口。
4. 不要写"综上所述""总体而言"这类空话。结论要具体到数字。

【回答结构】
第一段：结论（明确、可执行，不超过 3 句）
第二段：依据（分条列，每条带数字和出处）
第三段：你可以怎么做（给 2~3 个具体选项，不是"继续观察"这种废话）

如果证据不足以支撑结论，第一段就写「证据不足，无法判断」，并说明缺什么。
"""


def aggregate(question: str, results: list[dict]) -> tuple[str, dict]:
    """把多个 agent 的 JSON 小结合成一段回答。返回 (answer, usage)。"""
    payload = json.dumps(results, ensure_ascii=False, indent=2)
    msgs = [
        {"role": "system", "content": _aggregator_prompt()},
        {"role": "user", "content": f"用户的问题：{question}\n\n各智能体的结果：\n{payload}"},
    ]
    r = chat(msgs, temperature=0.3, max_tokens=1600)
    return r["content"].strip(), (r.get("usage") or {})


def run(question: str, *, use_llm: bool = True, verbose: bool = True) -> dict:
    """完整流程：规划 → 并行执行 → 汇总。返回可直接打印的结果字典。"""
    from .plan import plan as make

    t_start = time.time()
    p: Plan = make(question, use_llm=use_llm)
    run_id = audit.start_run(question, p.to_dict())

    if verbose:
        print(f"\n〔任务单〕{p.intent}")
        for t in p.tasks:
            print(f"   → {personas.PERSONAS[t.agent]['name']}({t.agent}) · {t.goal}")
        for r in p.rejected:
            print(f"   ⚠ {r}")
        print()

    # 并行跑所有 agent
    results: list[dict] = []
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    with ThreadPoolExecutor(max_workers=len(p.tasks) or 1) as pool:
        futs = {pool.submit(run_agent, run_id, t, p.summary): t for t in p.tasks}
        for f in as_completed(futs):
            t = futs[f]
            try:
                r = f.result()
            except Exception as e:  # noqa: BLE001
                r = {"agent": t.agent, "ok": False, "finding": f"执行异常：{e}",
                     "evidence": [], "confidence": "low", "missing": str(e)}
            results.append(r)
            u = r.get("usage") or {}
            for k in total_usage:
                total_usage[k] += u.get(k) or 0
            if verbose:
                flag = "✓" if r.get("ok") else "✗"
                print(f"   {flag} {r.get('name', t.agent):8s} {r.get('calls', 0)} 次工具 · "
                      f"{int(r.get('ms', 0)) / 1000:.1f}s · {str(r.get('finding'))[:52]}")

    # 按任务单顺序排（as_completed 是乱序的）
    order = {t.agent: i for i, t in enumerate(p.tasks)}
    results.sort(key=lambda x: order.get(x.get("agent"), 99))

    answer, agg_usage = aggregate(question, results)
    for k in total_usage:
        total_usage[k] += agg_usage.get(k) or 0

    total_ms = int((time.time() - t_start) * 1000)
    n_calls = sum(r.get("calls", 0) for r in results)
    audit.finish_run(run_id, answer=answer, n_agents=len(results), n_calls=n_calls,
                     total_ms=total_ms, usage=total_usage)

    return {
        "run_id": run_id,
        "question": question,
        "plan": p.to_dict(),
        "agents": results,
        "answer": answer,
        "usage": total_usage,
        "total_ms": total_ms,
        "n_calls": n_calls,
    }
