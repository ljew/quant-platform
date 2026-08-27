"""情绪打分器 v2：DeepSeek LLM 打分（与词典 v1 同接口可对比）。

配置来源优先级：
1. 环境变量 QUANT_LLM_API_KEY / QUANT_LLM_BASE_URL / QUANT_LLM_MODEL
2. XiaoAn .env 的 MODEL_KEY_DEEPSEEK（key），BASE 固定 https://api.deepseek.com

调用为 OpenAI 兼容 chat/completions；单条失败重试一次；整体异常返回中性分。
"""
from __future__ import annotations

import json
import os
import time

import pandas as pd
import requests

from .base import SentimentScorer

_DEFAULT_BASE = "https://api.deepseek.com"
_XIAOAN_ENV = "/Users/happyljew/Desktop/xiaoanhelper/.env"

SYSTEM_PROMPT = (
    "你是A股财经文本情绪分析引擎。对给定文章输出 JSON："
    '{"bull": <看多词义强度0-3>, "bear": <看空强度0-3>, "net": <-1~1 净情绪>}。'
    "只输出 JSON，不要解释。中性内容 net=0。"
)


def _resolve_key() -> str:
    k = os.getenv("QUANT_LLM_API_KEY")
    if k:
        return k
    try:
        with open(_XIAOAN_ENV, encoding="utf-8") as f:
            for line in f:
                if line.startswith("MODEL_KEY_DEEPSEEK="):
                    v = line.split("=", 1)[1].strip()
                    if v:
                        return v
    except OSError:
        pass
    return ""


class LlmScorerV2(SentimentScorer):
    version = "llm_v2"

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 timeout: int = 25, max_chars: int = 800, sleep: float = 0.15):
        self.key = _resolve_key()
        self.base_url = (base_url or os.getenv("QUANT_LLM_BASE_URL") or _DEFAULT_BASE).rstrip("/")
        self.model = model or os.getenv("QUANT_LLM_MODEL") or "deepseek-chat"
        self.timeout = timeout
        self.max_chars = max_chars
        self.sleep = sleep
        self.available = bool(self.key)

    @staticmethod
    def _parse(text: str) -> tuple[int, int, float] | None:
        t = text.strip()
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            return None
        try:
            d = json.loads(t[i: j + 1])
            bull = int(max(0, min(3, float(d.get("bull", 0)))))
            bear = int(max(0, min(3, float(d.get("bear", 0)))))
            net = float(d.get("net", 0))
            net = max(-1.0, min(1.0, net))
            return bull, bear, round(net, 4)
        except Exception:  # noqa: BLE001
            return None

    def score(self, texts: list[str]) -> pd.DataFrame:
        if not self.available:
            raise RuntimeError("LLM API key 未配置")
        out = []
        for t in texts:
            body = {"bull": 0, "bear": 0, "net": 0.0}
            for attempt in range(2):
                try:
                    resp = requests.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.key}",
                                 "Content-Type": "application/json"},
                        json={"model": self.model,
                              "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                           {"role": "user",
                                            "content": t[: self.max_chars]}],
                              "temperature": 0.1, "max_tokens": 64},
                        timeout=self.timeout)
                    if resp.status_code == 200:
                        content = resp.json()["choices"][0]["message"]["content"]
                        parsed = self._parse(content)
                        if parsed:
                            body = {"bull": parsed[0], "bear": parsed[1], "net": parsed[2]}
                            break
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.5)
            nets = body["net"]
            out.append({"bull": body["bull"], "bear": body["bear"], "net": round(nets, 4),
                        "score_version": self.version})
            time.sleep(self.sleep)
        return pd.DataFrame(out)
