"""大模型通道配置 API。

- GET    /llm/providers              列表（key 脱敏）+ 当前生效通道摘要
- GET    /llm/presets                服务商预设（前端「一键填充」）
- POST   /llm/providers              新增（第一条自动成为默认）
- PATCH  /llm/providers/{id}         修改（api_key 留空 = 保持原值）
- DELETE /llm/providers/{id}         删除（若删的是默认，顺位继承）
- POST   /llm/providers/{id}/default 设为默认
- POST   /llm/providers/{id}/test    用库中配置做连通性测试，结果记回该行
- POST   /llm/test                   用**表单当前值**测试（保存前先验证），
                                     api_key 留空且有 id 时复用库中已存的 key

配置写入后立即生效：``resolve_active()`` 每次调用现读数据库，无需重启后端。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import llm_config as lc

router = APIRouter(prefix="/llm", tags=["llm"])


class ProviderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    vendor: str = Field("custom", max_length=24)
    base_url: str = Field(..., min_length=1, max_length=255)
    api_key: str = Field("", max_length=600)
    model: str = Field(..., min_length=1, max_length=120)
    make_default: bool = True


class ProviderPatch(BaseModel):
    name: str | None = Field(None, max_length=64)
    vendor: str | None = Field(None, max_length=24)
    base_url: str | None = Field(None, max_length=255)
    # 传空 / 不传 = 不修改（前端无法回显明文 key，故不能要求原样提交）
    api_key: str | None = Field(None, max_length=600)
    model: str | None = Field(None, max_length=120)
    make_default: bool = False


class TestPayload(BaseModel):
    """测试一组配置；api_key 留空时按 id 复用库中已存的 key。"""

    id: int | None = None
    base_url: str = Field(..., min_length=1, max_length=255)
    model: str = Field(..., min_length=1, max_length=120)
    api_key: str = Field("", max_length=600)


def _check_url(url: str) -> str:
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="接口地址需以 http:// 或 https:// 开头")
    return u.rstrip("/")


@router.get("/providers")
def list_providers(db: Session = Depends(get_db)):
    """通道列表 + 当前生效配置。key 一律脱敏返回。"""
    return {"active": lc.active_summary(db), "items": lc.list_providers(db)}


@router.get("/presets")
def presets():
    """服务商预设：选中后前端自动带出接口地址与候选模型。"""
    return {"presets": lc.VENDOR_PRESETS}


@router.post("/providers")
def create_provider(payload: ProviderCreate, db: Session = Depends(get_db)):
    return lc.create_provider(
        db,
        name=payload.name, vendor=payload.vendor,
        base_url=_check_url(payload.base_url), api_key=payload.api_key,
        model=payload.model, make_default=payload.make_default,
    )


@router.patch("/providers/{pid}")
def update_provider(pid: int, payload: ProviderPatch, db: Session = Depends(get_db)):
    patch = payload.model_dump(exclude_unset=True)
    if patch.get("base_url"):
        patch["base_url"] = _check_url(patch["base_url"])
    row = lc.update_provider(db, pid, patch)
    if row is None:
        raise HTTPException(status_code=404, detail="通道不存在")
    return row


@router.delete("/providers/{pid}")
def delete_provider(pid: int, db: Session = Depends(get_db)):
    if not lc.delete_provider(db, pid):
        raise HTTPException(status_code=404, detail="通道不存在")
    return {"ok": True}


@router.post("/providers/{pid}/default")
def set_default(pid: int, db: Session = Depends(get_db)):
    if not lc.set_default(db, pid):
        raise HTTPException(status_code=404, detail="通道不存在")
    return {"ok": True}


@router.post("/providers/{pid}/test")
def test_saved(pid: int, db: Session = Depends(get_db)):
    """用库中已保存的配置做连通性测试（真实发一条最小请求）。"""
    res = lc.test_provider(db, pid)
    if res is None:
        raise HTTPException(status_code=404, detail="通道不存在")
    return res


@router.post("/test")
def test_draft(payload: TestPayload, db: Session = Depends(get_db)):
    """测试尚未保存的表单配置，避免「先存一条错的再删」。

    api_key 留空时从 payload.id 指向的记录取（编辑场景前端不回显明文 key）。
    """
    key = (payload.api_key or "").strip()
    if not key and payload.id:
        row = db.get(lc.LlmProvider, payload.id)
        key = (row.api_key or "").strip() if row else ""
    if not key:
        return {"ok": False, "ms": 0, "model": payload.model,
                "error": "未填写 API Key"}
    return lc.test_connection({
        "configured": True,
        "base_url": _check_url(payload.base_url),
        "api_key": key,
        "model": payload.model,
    })
