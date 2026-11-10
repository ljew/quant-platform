"""平台 API 的唯一出口：一次 GET，一个统一响应包。

单独一层，是因为有三个坑必须在一处解决，不然每个工具都要重复踩：

  ① **代理**：本机环境带 HTTP_PROXY/HTTPS_PROXY，urllib 默认会走代理，
     访问 127.0.0.1 会得到 502（不是服务没起，是请求被代理劫走了）。
     这里显式建一个 ProxyHandler({}) 的 opener，全部工具共用。
  ② **超时**：平台查询可能慢（组合净值要算），但模型等不了太久。
     默认 20s，超时按失败返回，不重试 —— 重试交给 workflow 决定。
  ③ **口径**：每个结果都带 source（打了哪个 URL）和 as_of（什么时候取的）。
     模型很容易把「昨天的数据」说成今天，带上时间戳它能自己发现。

⚠️ 这一层绝不写库、绝不读 SQLite 文件。只读 HTTP。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

API_BASE = os.getenv("QUANT_API", "http://127.0.0.1:8000/api/v1").rstrip("/")
API_TIMEOUT = float(os.getenv("QUANT_API_TIMEOUT", "20"))

# 不走代理。这条是硬要求：走代理访问 localhost 会 502，且报错信息很像「服务挂了」。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get(path: str, params: dict | None = None) -> tuple[Any, str]:
    """GET 一个平台接口，返回 (data, 完整 URL)。异常交给调用方包装。"""
    url = f"{API_BASE}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with _OPENER.open(req, timeout=API_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8")), url


def ok(data: Any, source: str, missing: list[str] | None = None,
       note: str = "") -> dict:
    """成功响应。

    missing 非空表示「调用成功，但有东西没拿到」—— 例如卡片不存在、
    某个字段为空。模型必须把 missing 写进回答，不许自己补。
    """
    out: dict[str, Any] = {
        "ok": True,
        "data": data,
        "source": source,
        "as_of": _now(),
        "missing": missing or [],
    }
    if note:
        out["note"] = note
    return out


def err(e: Exception, source: str,
        hint: str = "调用失败，请在回答里如实说明「没查到」，不要推测数据。") -> dict:
    """失败响应。不抛异常，让模型看见失败并如实转述。"""
    if isinstance(e, urllib.error.HTTPError):
        detail = f"HTTP {e.code}"
    elif isinstance(e, urllib.error.URLError):
        detail = f"连不上（{e.reason}）"
    elif isinstance(e, TimeoutError):
        detail = f"超时（>{API_TIMEOUT:.0f}s）"
    else:
        detail = f"{type(e).__name__}: {e}"
    return {
        "ok": False,
        "error": detail,
        "source": source,
        "as_of": _now(),
        "missing": ["本次调用没有拿到数据"],
        "hint": hint,
    }


def dumps(obj: dict) -> str:
    """统一序列化：中文不转义，便于模型阅读。"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def openapi_paths() -> set[str]:
    """拉平台的 /openapi.json，返回全部路径（含 /api/v1 前缀）。

    第 1 步验收要求「URL 与 /openapi.json 一致」，用这个核对，
    而不是靠人眼看文档 —— 工具表抄错过三次路径。
    """
    root = API_BASE.split("/api/v1", 1)[0]
    req = urllib.request.Request(f"{root}/openapi.json",
                                 headers={"Accept": "application/json"})
    with _OPENER.open(req, timeout=API_TIMEOUT) as r:
        doc = json.loads(r.read().decode("utf-8"))
    return set((doc.get("paths") or {}).keys())
