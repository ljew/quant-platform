"""因子表达式 × 大模型：自然语言 ↔ 表达式。

对外能力：
- nl_to_expr(text)    自然语言想法 → 因子表达式（生成 → 校验 → 失败回灌重修）
- explain_expr(expr)  表达式 → 中文逻辑说明（读懂 GP 挖出的天书）

LLM 通道走 OpenAI 兼容协议（DeepSeek / 火山方舟 / DashScope 兼容模式均适用），
用标准库 urllib 发起请求，不新增第三方依赖。

三条设计红线：
1. **契约动态化**：提示词里的函数表由 factor_expr.FUNCS 反射生成、变量表取自
   ns_vars.var_names()。引擎增删函数或变量时提示词自动跟随，杜绝「提示词说没有、
   实际支持」或反过来的错位 —— 这类错位会让模型稳定地写出非法表达式。
2. **生成必过闸**：模型输出一律先过 validate_expr（AST 白名单 + 真实试算），
   未通过就把真实报错原文回灌让它重写。宁可多花一轮，也不放行未验证的表达式。
3. **不编造数据**：平台没有的字段（换手率、北向资金、龙虎榜等）要求模型在
   unsupported 里明说，禁止用相近字段冒充 —— 编出来的因子比没有因子更危险。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from app.config import settings


class LLMUnavailable(RuntimeError):
    """LLM 通道未配置或调用失败（路由层据此降级为 503 并由前端提示）。"""


def llm_configured() -> bool:
    return bool((settings.llm_api_key or "").strip())


def llm_info() -> dict:
    """供前端探测 AI 模式是否可用（不泄露 key）。"""
    return {
        "configured": llm_configured(),
        "model": settings.llm_model if llm_configured() else "",
        "base_url": settings.llm_base_url if llm_configured() else "",
    }


# ============ 提示词契约（从引擎反射生成，永不过期） ============
# 少数内置函数（如 math.log）无法反射签名，手工兜底
_SIG_FALLBACK = {"log": "(x)"}


def _function_contract() -> str:
    """反射生成函数签名表：只保留参数名与默认值，剥离类型注解（对模型是噪声）。"""
    import inspect

    from app.core.engine.factor_expr import FUNCS

    parts = []
    for name in sorted(FUNCS):
        try:
            sig = inspect.signature(FUNCS[name])
        except (TypeError, ValueError):
            parts.append(f"{name}{_SIG_FALLBACK.get(name, '(...)')}")
            continue
        args = []
        for p in sig.parameters.values():
            if p.kind is inspect.Parameter.VAR_POSITIONAL:
                args.append(f"*{p.name}")
            elif p.default is inspect.Parameter.empty:
                args.append(p.name)
            elif isinstance(p.default, float):
                args.append(f"{p.name}={p.default:g}")
            else:
                args.append(f"{p.name}={p.default}")
        parts.append(f"{name}({', '.join(args)})")
    return " | ".join(parts)


def _variable_contract() -> str:
    from app.datahub.ns_vars import VAR_DOC, var_names

    return "\n".join(f"- {v}: {VAR_DOC.get(v, '')}" for v in sorted(var_names()))


_SYSTEM_TEMPLATE = """你是量化平台的因子表达式生成器。把用户的自然语言投资想法，翻译成平台可执行的因子表达式。

# 表达式语法
- 只能使用下面列出的函数与变量；禁止属性访问（.xxx），禁止 import / lambda / 推导式 / 赋值语句。
- 表达式求值结果是一个数，**数值越大代表越符合用户想要的股票**（选股按因子值降序）。
- 支持 Python 表达式语法：算术 + - * /、括号、数字常量、序列切片（如 amt_v[-20:]）。

# 可用函数（签名）
<<FUNCS>>

# 可用变量
<<VARS>>

# 语义要点
- 序列按窗口分三族：c_* 收盘价、vol_* 成交量、amt_* 成交额。后缀 m/r/v/b/t 分别对应
  126/26/61/126/121 日窗口。**同后缀的序列长度相同**，可逐点配合，例如
  corr(returns(c_v), returns(vol_v))。
- 不同后缀的序列长度不同，不要逐点相减或相乘；跨窗口比较时先用 mean/std 等聚合成标量。
- 想要「越小越好」的因子（低估值、低波动、缩量），记得给整个表达式取负号，例如 -std(returns(c_v))。
- 估值用 safe_inv(pe_ttm, 0, 1000)。market_cap 单位是亿元，ocf/capex/total_assets 单位是元；
  做比值优先使用已归一化的 fcf_yield / ocf_to_assets / capex_intensity，避免量纲错误。
- 用 div0(x, y) 而不是直接相除，可以避免分母为 0 时整条因子失效。

# 参考示例
- 低估值 → safe_inv(pe_ttm, 0, 1000)
- 短期反转 → -roc(c_r, 5)
- 低波动 → -std(returns(c_v))
- 量价背离（价涨量缩） → -corr(returns(c_v), returns(vol_v))
- 近期放量 → mean(amt_v[-20:]) / mean(amt_v) - 1
- 便宜的现金流 → fcf_yield
- 缩量低波 → -std(returns(c_v)) * mean(vol_r) / mean(vol_v)

# 硬性约束
1. 若用户的想法依赖平台没有的数据（如换手率、北向资金、龙虎榜、股东户数、机构评级、
   分钟级数据），**不要编造变量名，也不要用相近字段冒充**。照常给出最接近的可用表达式，
   并在 unsupported 字段里明确写出缺什么数据。
2. 变量名必须与上面的列表完全一致，不要自创（例如没有 turnover、volume、close 这些名字）。
3. 只输出 JSON，不要输出解释文字，不要加 Markdown 代码围栏。

# 输出 JSON 字段
{"expr": "表达式", "name": "中文因子名(不超过10字)", "logic": "一句话说明这个因子在赌什么逻辑",
 "direction": "越大越优 或 越小越优", "unsupported": "缺失的数据字段，没有则空字符串",
 "confidence": 0.0到1.0之间的数字}
"""

_EXPLAIN_TEMPLATE = """你是量化研究员。用户会给你一条因子表达式，请用中文解释它在做什么。

# 可用函数（签名）
<<FUNCS>>

# 可用变量
<<VARS>>

只输出 JSON，不要 Markdown 围栏：
{"name": "中文因子名(不超过10字)", "logic": "这个因子在赌什么逻辑(1-2句)",
 "direction": "越大越优 或 越小越优", "caveats": "什么市场环境下容易失效(1句)"}
"""


def _fill(template: str) -> str:
    return template.replace("<<FUNCS>>", _function_contract()).replace("<<VARS>>", _variable_contract())


def system_prompt() -> str:
    """生成用系统提示词（导出以便调试与测试）。"""
    return _fill(_SYSTEM_TEMPLATE)


def explain_prompt() -> str:
    return _fill(_EXPLAIN_TEMPLATE)


# ============ 通道 ============
def _chat(messages: list[dict], *, temperature: float = 0.3,
          timeout: int = 90, max_tokens: int = 1200) -> str:
    """调用 OpenAI 兼容的 chat/completions。"""
    if not llm_configured():
        raise LLMUnavailable("未配置大模型：请在 .env 设置 QUANT_LLM_API_KEY")
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.llm_api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise LLMUnavailable(f"模型接口返回 {e.code}：{detail}") from e
    except urllib.error.URLError as e:
        raise LLMUnavailable(f"无法连接模型服务：{e.reason}") from e
    except Exception as e:  # noqa: BLE001
        raise LLMUnavailable(f"模型调用失败：{type(e).__name__}: {e}") from e
    try:
        return body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise LLMUnavailable(f"模型返回结构异常：{str(body)[:200]}") from e


def _slice_braces(s: str) -> str:
    i, j = s.find("{"), s.rfind("}")
    return s[i:j + 1] if 0 <= i < j else ""


def parse_json(text: str) -> dict:
    """从模型输出里稳健地取出 JSON 对象（容忍代码围栏与前后废话）。"""
    if not text:
        return {}
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`").strip()
        if s[:4].lower() == "json":
            s = s[4:].strip()
    for cand in (s, _slice_braces(s)):
        if not cand:
            continue
        try:
            data = json.loads(cand)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict):
            return data
    return {}


# ============ 对外能力 ============
def nl_to_expr(text: str, *, max_retry: int = 3, timeout: int = 90) -> dict:
    """自然语言 → 因子表达式，带「校验失败自动重修」闭环。

    max_retry 为重修轮数：总尝试次数 = 1 + max_retry。
    返回 ok=True 时 expr 一定已通过 validate_expr。
    """
    from app.services.factor_mining import validate_expr

    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "请输入因子想法"}
    if not llm_configured():
        raise LLMUnavailable("未配置大模型：请在 .env 设置 QUANT_LLM_API_KEY")

    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": f"用户的因子想法：{text}"},
    ]
    fixes: list[dict] = []
    for attempt in range(1, max_retry + 2):
        raw = _chat(messages, timeout=timeout)
        data = parse_json(raw)
        expr = str(data.get("expr") or "").strip()
        err = ""
        sample = None
        if not expr:
            err = "模型未返回 expr 字段"
        else:
            ok, verr, sample = validate_expr(expr)
            if not ok:
                err = verr
        if expr and not err:
            return {
                "ok": True,
                "expr": expr,
                "name": (str(data.get("name") or "").strip() or "AI 因子")[:20],
                "logic": str(data.get("logic") or "").strip(),
                "direction": str(data.get("direction") or "").strip(),
                "unsupported": str(data.get("unsupported") or "").strip(),
                "confidence": data.get("confidence"),
                "sample_value": sample,
                "attempts": attempt,
                "fixes": fixes,
                "model": settings.llm_model,
            }
        fixes.append({"attempt": attempt, "expr": expr, "error": err})
        if attempt > max_retry:
            break
        messages.append({"role": "assistant", "content": raw[:2000]})
        messages.append({
            "role": "user",
            "content": (
                f"这条表达式没有通过平台校验，报错是：{err}\n"
                "请修正后重新输出 JSON。变量名必须与给定列表完全一致，"
                "函数只能用列表里的，禁止属性访问。"
            ),
        })
    return {
        "ok": False,
        "error": f"连续 {max_retry + 1} 次生成均未通过校验",
        "fixes": fixes,
        "hint": "可以换一种更具体的说法，或直接手写表达式",
    }


def explain_expr(expr: str, *, timeout: int = 60) -> dict:
    """表达式 → 中文逻辑说明。"""
    from app.services.factor_mining import validate_expr

    expr = (expr or "").strip()
    if not expr:
        return {"ok": False, "error": "表达式为空"}
    ok, err, _ = validate_expr(expr)
    if not ok:
        return {"ok": False, "error": f"表达式无效：{err}"}
    if not llm_configured():
        raise LLMUnavailable("未配置大模型：请在 .env 设置 QUANT_LLM_API_KEY")

    raw = _chat([
        {"role": "system", "content": explain_prompt()},
        {"role": "user", "content": expr},
    ], temperature=0.2, timeout=timeout, max_tokens=600)
    data = parse_json(raw)
    if not data:
        return {"ok": False, "error": "模型未返回可解析的 JSON", "raw": raw[:300]}
    return {
        "ok": True,
        "expr": expr,
        "name": (str(data.get("name") or "").strip() or "未命名")[:20],
        "logic": str(data.get("logic") or "").strip(),
        "direction": str(data.get("direction") or "").strip(),
        "caveats": str(data.get("caveats") or "").strip(),
        "model": settings.llm_model,
    }
