"""大模型通道配置：存储、脱敏、运行时解析与连通性测试。

设计要点
--------
1. **存库而非只读 .env**：``.env`` 在进程启动时读入 ``settings``，改一次必须重启；
   本模块把配置落在 ``llm_providers`` 表，每次调用时现读 → 页面改完立即生效。
2. **单一真源**：HTTP 调用只在 :func:`chat` 一处实现，因子生成与连通性测试共用，
   避免「测试通过但生成失败」这类因两套代码不一致导致的怪问题。
3. **优先级**：DB 中 ``is_default=1`` 的记录 → ``.env``（``QUANT_LLM_API_KEY``）。
   前者存在即完全接管，后者作为向后兼容的兜底，两条通道互不干扰。
4. **不变量**：只要有记录就有且仅有一条 default，由 :func:`_normalize_default` 维护。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime

from sqlalchemy import func, select

from app.config import settings
from app.database import SessionLocal
from app.models import LlmProvider


class LLMUnavailable(RuntimeError):
    """LLM 通道未配置或调用失败（路由层据此降级为 503 并由前端提示）。"""


# ============ 服务商预设 ============
# 只做「填表助手」：选中后自动带出 base_url 与候选模型，全部字段仍可手改。
# 各家的兼容接口路径以其官方文档为准，若上游调整直接在此处更新。
VENDOR_PRESETS: list[dict] = [
    {
        "vendor": "deepseek", "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "note": "OpenAI 兼容，性价比高",
    },
    {
        "vendor": "ark", "label": "火山方舟 · 豆包",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-seed-1-6-250615", "doubao-1-5-pro-32k-250115"],
        "note": "模型名也可填方舟「接入点 ID」（ep- 开头）",
    },
    {
        "vendor": "qwen", "label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-max", "qwen-turbo"],
        "note": "需使用 DashScope 的 OpenAI 兼容模式地址",
    },
    {
        "vendor": "kimi", "label": "Kimi · 月之暗面",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k"],
        "note": "OpenAI 兼容",
    },
    {
        "vendor": "zhipu", "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-flash"],
        "note": "OpenAI 兼容",
    },
    {
        "vendor": "custom", "label": "自定义 / 本地部署",
        "base_url": "",
        "models": [],
        "note": "填任意 OpenAI 兼容端点，如本地 Ollama: http://127.0.0.1:11434/v1",
    },
]

# .env 迁移出来的记录用这个名字，便于用户分辨来源
ENV_PROVIDER_NAME = "来自 .env"


# ============ 脱敏 ============
def mask_key(key: str | None) -> str:
    """只保留前 6 后 4，中间打码。API 一律返回本结果，绝不回明文。"""
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 12:
        return k[:2] + "*" * max(len(k) - 2, 0)
    return f"{k[:6]}{'*' * 6}{k[-4:]}"


# ============ 运行时解析 ============
def _env_cfg() -> dict:
    return {
        "configured": bool((settings.llm_api_key or "").strip()),
        "source": "env",
        "provider_id": None,
        "name": ENV_PROVIDER_NAME,
        "vendor": "custom",
        "base_url": settings.llm_base_url,
        "api_key": settings.llm_api_key or "",
        "model": settings.llm_model,
    }


def resolve_active() -> dict:
    """当前生效的通道配置（含**明文** key，仅供内部调用链使用）。

    每次调用现读数据库 —— 这就是「页面配完立即生效、无需重启」的实现点。
    数据库无记录或读库异常时回退到 .env，异常不向上抛（配置读取不该拖垮主流程）。
    """
    db = SessionLocal()
    try:
        row = db.execute(
            select(LlmProvider).order_by(LlmProvider.is_default.desc(), LlmProvider.id)
        ).scalars().first()
        if row is not None:
            return {
                "configured": bool((row.api_key or "").strip()),
                "source": "db",
                "provider_id": row.id,
                "name": row.name,
                "vendor": row.vendor,
                "base_url": row.base_url,
                "api_key": row.api_key or "",
                "model": row.model,
            }
    except Exception:  # noqa: BLE001 —— 表未建/库锁等情况一律回退 .env
        pass
    finally:
        db.close()
    return _env_cfg()


def llm_configured() -> bool:
    return bool(resolve_active()["configured"])


# ============ 读取（脱敏） ============
def _to_dict(row: LlmProvider) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "vendor": row.vendor,
        "base_url": row.base_url,
        "model": row.model,
        "api_key_masked": mask_key(row.api_key),
        "has_key": bool((row.api_key or "").strip()),
        "is_default": bool(row.is_default),
        "last_test_at": row.last_test_at.isoformat() if row.last_test_at else None,
        "last_test_ok": None if row.last_test_ok is None else bool(row.last_test_ok),
        "last_test_ms": row.last_test_ms,
        "last_test_msg": row.last_test_msg,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_providers(db) -> list[dict]:
    rows = db.execute(
        select(LlmProvider).order_by(LlmProvider.is_default.desc(), LlmProvider.id)
    ).scalars().all()
    return [_to_dict(r) for r in rows]


def active_summary(db) -> dict:
    """给前端的一份「当前生效通道」摘要，同时说明来源是页面配置还是 .env。"""
    cfg = resolve_active()
    return {
        "configured": cfg["configured"],
        "source": cfg["source"] if cfg["configured"] else "",
        "name": cfg["name"] if cfg["configured"] else "",
        "model": cfg["model"] if cfg["configured"] else "",
        "base_url": cfg["base_url"] if cfg["configured"] else "",
        "provider_id": cfg["provider_id"],
        "key_masked": mask_key(cfg["api_key"]),
        "count": db.scalar(select(func.count()).select_from(LlmProvider)) or 0,
        # 页面配置与 .env 同时存在时提示优先级，避免「我改了 .env 怎么没反应」
        "env_shadowed": bool(cfg["source"] == "db" and (settings.llm_api_key or "").strip()),
    }


# ============ 写入 ============
def _normalize_default(db, prefer_id: int | None = None) -> None:
    """维护不变量：有记录 ⇒ 恰好一条 default。"""
    rows = db.execute(select(LlmProvider).order_by(LlmProvider.id)).scalars().all()
    if not rows:
        return
    defaults = [r for r in rows if r.is_default]
    if len(defaults) == 1 and (prefer_id is None or defaults[0].id == prefer_id):
        return
    target = None
    if prefer_id is not None:
        target = next((r for r in rows if r.id == prefer_id), None)
    if target is None:
        target = defaults[0] if defaults else rows[0]
    for r in rows:
        r.is_default = 1 if r.id == target.id else 0


def create_provider(db, *, name: str, vendor: str, base_url: str,
                    api_key: str, model: str, make_default: bool) -> dict:
    row = LlmProvider(
        name=(name or "").strip() or "未命名通道",
        vendor=(vendor or "custom").strip(),
        base_url=(base_url or "").strip(),
        api_key=(api_key or "").strip(),
        model=(model or "").strip(),
        is_default=0,
    )
    # 第一条自动成为默认，避免出现「配了但没生效」的困惑
    is_first = db.execute(select(LlmProvider.id)).first() is None
    db.add(row)
    db.flush()
    if is_first or make_default:
        _normalize_default(db, prefer_id=row.id)
    db.commit()
    db.refresh(row)
    return _to_dict(row)


def update_provider(db, pid: int, patch: dict) -> dict | None:
    row = db.get(LlmProvider, pid)
    if row is None:
        return None
    for field in ("name", "vendor", "base_url", "model"):
        if field in patch and patch[field] is not None:
            setattr(row, field, str(patch[field]).strip())
    # api_key 语义：未提供 / 传 None / 传空串 ⇒ 保持原值（前端不回显明文，无法「原样提交」）
    if patch.get("api_key"):
        row.api_key = str(patch["api_key"]).strip()
        row.last_test_ok = None  # key 换了，旧测试结论作废
        row.last_test_msg = None
    row.updated_at = datetime.now()
    if patch.get("make_default"):
        _normalize_default(db, prefer_id=row.id)
    db.commit()
    db.refresh(row)
    return _to_dict(row)


def delete_provider(db, pid: int) -> bool:
    row = db.get(LlmProvider, pid)
    if row is None:
        return False
    was_default = bool(row.is_default)
    db.delete(row)
    db.flush()
    if was_default:
        _normalize_default(db, prefer_id=None)  # 顺位继承，不留悬空
    db.commit()
    return True


def set_default(db, pid: int) -> bool:
    row = db.get(LlmProvider, pid)
    if row is None:
        return False
    _normalize_default(db, prefer_id=pid)
    db.commit()
    return True


# ============ HTTP 通道（唯一实现） ============
def chat(cfg: dict, messages: list[dict], *, temperature: float = 0.3,
         timeout: int = 90, max_tokens: int = 1200) -> str:
    """调用 OpenAI 兼容的 ``chat/completions``。所有 HTTP 细节集中在此。"""
    if not cfg.get("configured"):
        raise LLMUnavailable("未配置大模型通道：请在因子挖掘页配置，或设置环境变量 QUANT_LLM_API_KEY")
    url = (cfg.get("base_url") or "").rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg.get("model"),
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
            "Authorization": f"Bearer {cfg.get('api_key')}",
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


def test_connection(cfg: dict, *, timeout: int = 20) -> dict:
    """真实发一条最小请求验证通道可用，返回延迟与（失败时的）原始错误。"""
    t0 = time.time()
    try:
        # 不设 temperature 之外的采样要求；用最小 token 数换取最快响应
        msg = chat(cfg, [{"role": "user", "content": "ping"}],
                   temperature=0.0, timeout=timeout, max_tokens=5)
        return {
            "ok": True, "ms": int((time.time() - t0) * 1000),
            "model": cfg.get("model"), "reply": (msg or "").strip()[:80],
        }
    except LLMUnavailable as e:
        return {
            "ok": False, "ms": int((time.time() - t0) * 1000),
            "model": cfg.get("model"), "error": str(e),
        }


def test_provider(db, pid: int, *, timeout: int = 20) -> dict | None:
    """测试指定通道并把结果记回该行，供页面直接展示上次结论。"""
    row = db.get(LlmProvider, pid)
    if row is None:
        return None
    if not (row.api_key or "").strip():
        res = {"ok": False, "ms": 0, "model": row.model, "error": "该通道未填写 API Key"}
    else:
        res = test_connection({
            "configured": True, "base_url": row.base_url,
            "api_key": row.api_key, "model": row.model,
        }, timeout=timeout)
    row.last_test_at = datetime.now()
    row.last_test_ok = 1 if res["ok"] else 0
    row.last_test_ms = res["ms"]
    row.last_test_msg = (res.get("reply") or res.get("error") or "")[:300]
    db.commit()
    return res


# ============ 启动迁移 ============
def ensure_env_provider() -> None:
    """首次启动时把 .env 里已配的 key 落成一条记录，避免升级后配置「消失」。

    只在表为空时执行；已有记录说明用户已在页面上管理，不再插手。
    """
    if not (settings.llm_api_key or "").strip():
        return
    db = SessionLocal()
    try:
        if db.execute(select(LlmProvider.id)).first() is not None:
            return
        db.add(LlmProvider(
            name=ENV_PROVIDER_NAME, vendor="custom",
            base_url=settings.llm_base_url, api_key=settings.llm_api_key,
            model=settings.llm_model, is_default=1,
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    finally:
        db.close()
