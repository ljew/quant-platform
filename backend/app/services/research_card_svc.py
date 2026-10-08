"""研究卡 → 生产逻辑的解析层（研究卡机制第 2 步）。

设计原则（docs/research-card-v0.1.md）：
- **卡片状态是唯一真相**：信号定档、因子计算门禁都以 research_cards.state 为准。
- **旧配置作冗余校验**：STRATEGY_QUALITY（代码常量）与 factor_registry.status
  仍保留，逐条比对，不一致时 logger.warning —— 不静默分叉。
- **无卡回退**：某策略/因子还没有卡时，回退到旧配置，行为与改造前完全一致。

状态 ↔ 质量档映射（与迁移脚本一致）：
  enabled → strong（可建仓） / candidate → neutral（可建仓，确认中）
  downgraded / retired → weak（仅观察，不建仓） / watch → weak
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ResearchCard

logger = logging.getLogger(__name__)

STATE_TO_TIER: dict[str, str] = {
    "enabled": "strong",
    "candidate": "neutral",
    "watch": "weak",
    "downgraded": "weak",
    "retired": "weak",
}
TIER_TO_STATE: dict[str, str] = {
    "strong": "enabled",
    "good": "enabled",
    "neutral": "candidate",
    "weak": "downgraded",
}


def resolve_strategy_quality(db: Session) -> dict[str, str]:
    """解析当前生效的策略质量档：卡片为准，STRATEGY_QUALITY 为冗余/回退。

    返回 {strategy_key: tier}，tier ∈ strong/good/neutral/weak（与 engine 口径一致）。
    """
    from app.services.xq.strategies import STRATEGY_QUALITY

    quality = dict(STRATEGY_QUALITY)
    cards = db.execute(
        select(ResearchCard).where(ResearchCard.card_type == "strategy")
    ).scalars().all()
    for c in cards:
        tier = STATE_TO_TIER.get(c.state, "neutral")
        old = quality.get(c.key)
        if old is not None and old != tier:
            logger.warning(
                "研究卡与 STRATEGY_QUALITY 不一致：%s 卡状态=%s→档=%s，常量档=%s；以卡为准",
                c.key, c.state, tier, old,
            )
        quality[c.key] = tier
    return quality


def enabled_factor_names(db: Session) -> list[str]:
    """解析当前需要每日计算的注册因子：卡片状态 enabled 才算数。

    factor_registry.status=='enabled' 且（无卡 或 卡状态 enabled）→ 计算；
    有卡但非 enabled → 跳过；无卡 → 旧行为保底（计算）并 warning 提示补卡。
    """
    from app.models import FactorRegistry

    rows = db.execute(
        select(FactorRegistry).where(FactorRegistry.status == "enabled")
    ).scalars().all()
    if not rows:
        return []
    cards = {
        c.key: c.state
        for c in db.execute(
            select(ResearchCard).where(ResearchCard.card_type == "factor")
        ).scalars().all()
    }
    keep: list[str] = []
    for f in rows:
        state = cards.get(f.name)
        if state is None:
            logger.warning("因子 %s 注册表 enabled 但无研究卡，按旧行为计算；建议补卡", f.name)
            keep.append(f.name)
        elif state == "enabled":
            keep.append(f.name)
        else:
            logger.info("因子 %s 研究卡状态=%s，跳过每日计算", f.name, state)
    return keep
