"""LLM 调用层：OpenAI 兼容协议，标准库实现，带重试。

为什么自己写而不引 SDK：
  · 原型要零依赖跑起来（`python3 -m agent.cli` 直接可用）
  · 只需要 chat + tools + json_mode 三件事，SDK 反而是负担

重试策略：长请求偶发 `SSL: UNEXPECTED_EOF_WHILE_READING`（生产上踩过），
所以网络类错误重试 2 次，业务错误（4xx）不重试。
"""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

from .config import llm_config


class LLMError(RuntimeError):
    pass


def _post(url: str, payload: dict, api_key: str, timeout: float) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def chat(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    model: str | None = None,
    json_mode: bool = False,
    temperature: float = 0.2,
    max_tokens: int = 2000,
    timeout: float = 90.0,
    retries: int = 2,
) -> dict:
    """调一次 chat completion，返回 {content, tool_calls, usage, model}。

    tool_calls 为 None 表示模型只回了文本。
    """
    cfg = llm_config()
    url = f"{cfg['base_url']}/chat/completions"
    payload: dict[str, Any] = {
        "model": model or cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = _post(url, payload, cfg["api_key"], timeout)
            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            return {
                "content": msg.get("content") or "",
                "tool_calls": msg.get("tool_calls") or None,
                "usage": resp.get("usage") or {},
                "model": resp.get("model") or payload["model"],
                "finish_reason": choice.get("finish_reason"),
            }
        except urllib.error.HTTPError as e:
            # 4xx 是配置/参数问题，重试没用
            detail = e.read().decode("utf-8", "ignore")[:300]
            if 400 <= e.code < 500:
                raise LLMError(f"HTTP {e.code}: {detail}") from e
            last_err = e
        except (urllib.error.URLError, ssl.SSLError, socket.timeout, TimeoutError) as e:
            last_err = e
        except json.JSONDecodeError as e:
            last_err = e
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))

    raise LLMError(f"调用失败（已重试 {retries} 次）：{type(last_err).__name__}: {last_err}")


def chat_json(messages: list[dict], **kw) -> dict:
    """要求模型回 JSON 并解析。解析失败时抛 LLMError（不要静默返回空 dict）。"""
    r = chat(messages, json_mode=True, **kw)
    text = (r["content"] or "").strip()
    if not text:
        raise LLMError("模型返回空内容")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"返回的不是合法 JSON：{text[:200]}") from e
