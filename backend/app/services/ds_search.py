"""DeepSeek 网页搜索 / 抓取客户端。

## 一次误判的更正（2026-11-07）

当天早些时候我断言「DeepSeek API 没有联网能力」，依据是：
  - `tools: [{"type": "web_search"}]` 返回 422（`expected function`）
  - 试了 `/search`、`/v1/search`、`/chat/completions/search` 全部 404

这两条都不构成证据。`tools` 里确实只有 function calling，但联网能力做成了
**独立的 tools 端点**，路径是 `/v1/tools/search` 和 `/v1/tools/fetch` ——
我试的三个路径都不是它。老刘给了文档链接后实测：两个接口都 200 且质量很高。

教训：**「我试了几条路径都 404」不等于「功能不存在」**，要查文档再下结论。

## 接口

    POST {root}/v1/tools/search   {query, count, ori_question, need_content}
    POST {root}/v1/tools/fetch    {url}

- 计费：每 1000 次成功请求 5 元（失败/无结果不计费），Search 与 Fetch 同价
- 限速：Search QPS 100、Fetch QPS 25（账号级）
- 返回字段：title / url / snippet / summary(600~800字) / content / site_name /
  date / authority(0~4, -1未知) / rerank_score(0~3, ≥1.5 为优质)

## 注意

`llm_config` 里的 `base_url` 通常带 `/v1` 后缀（OpenAI 兼容写法），
而 tools 接口在 root 下 —— 直接用会拼成 `/v1/v1/tools/search` 而 404。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("ds_search")

DEFAULT_ROOT = "https://api.deepseek.com"


def _config() -> tuple[str, str]:
    """返回 (root, api_key)。root 已剥掉 `/v1` 后缀。"""
    from app.services.llm_config import resolve_active

    cfg = resolve_active()
    key = cfg.get("api_key") or ""
    raw = (cfg.get("base_url") or DEFAULT_ROOT).rstrip("/")
    root = raw[:-3] if raw.endswith("/v1") else raw
    return root, key


def available() -> bool:
    """当前配置是否能用 DeepSeek 搜索。

    只有 base_url 指向 deepseek 才有这两个端点；换成 ARK / Qwen 等
    兼容服务时必须关掉，否则每次调用都是 404 白花钱。
    """
    if os.getenv("QUANT_DS_SEARCH", "1") == "0":
        return False
    root, key = _config()
    return bool(key) and "deepseek" in root.lower()


def _post(path: str, body: dict, timeout: int = 30) -> dict:
    root, key = _config()
    req = urllib.request.Request(
        root + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def search(query: str, count: int = 6, need_content: bool = False,
           ori_question: str = "", timeout: int = 30) -> list[dict]:
    """搜网页，返回归一化后的结果列表；失败返回 []（不抛，简报不该被搜索拖垮）。

    `need_content=False`：`summary` 已有 600~800 字，足够模型判断，
    再要正文只是白占带宽和上下文。
    """
    if not available():
        return []
    body = {"query": (query or "").strip()[:128], "count": max(1, min(50, count))}
    if need_content:
        body["need_content"] = True
    if ori_question:
        body["ori_question"] = ori_question[:128]
    try:
        d = _post("/v1/tools/search", body, timeout=timeout)
    except urllib.error.HTTPError as e:
        # 401 key 失效 / 402 余额不足 / 429 超 QPS / 5xx 服务端 —— 都只记日志，
        # 让调用方继续用「一手快讯」那条腿跑，不整份简报失败。
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:160]
        except Exception:  # noqa: BLE001
            pass
        logger.warning("DeepSeek 搜索失败 %s: %s %s", query[:24], e.code, detail)
        return []
    except Exception as e:  # noqa: BLE001
        logger.warning("DeepSeek 搜索异常 %s: %s", query[:24], e)
        return []

    out = []
    for it in (d.get("results") or []):
        if not isinstance(it, dict):
            continue
        score = it.get("rerank_score")
        try:
            score = float(score) if score is not None else 0.0
        except Exception:  # noqa: BLE001
            score = 0.0
        # 文档口径：≥1.5 为优质。低于 1.0 的基本是关键词误命中，直接丢。
        if score and score < 1.0:
            continue
        out.append({
            "title": str(it.get("title") or "")[:120],
            "url": str(it.get("url") or ""),
            "site": str(it.get("site_name") or "")[:24],
            "summary": str(it.get("summary") or it.get("snippet") or "")[:900],
            "date": str(it.get("date") or "")[:10],
            "score": round(score, 2),
            "authority": int(it.get("authority") if it.get("authority") is not None else -1),
        })
    return out


def fetch(url: str, timeout: int = 30) -> dict:
    """抓单页正文（Markdown）。失败返回 {"error": ...}。"""
    if not available():
        return {"error": "搜索通道不可用"}
    try:
        d = _post("/v1/tools/fetch", {"url": url[:8192]}, timeout=timeout)
        return {"title": d.get("title") or "", "content": d.get("content") or ""}
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return {"error": "被站点安全策略拒绝"}
        if e.code == 404:
            return {"error": "页面无可提取正文"}
        return {"error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:80]}"}


# 每小时检索的主题。相对固定的好处是结果可比、可回溯；
# 要改就改这里，或在 hot_brief 里传 queries。
DEFAULT_QUERIES = (
    "A股 政策 部委 发布",
    "行业 产量 价格 数据 景气",
    "券商 研报 观点 A股 策略",
)
