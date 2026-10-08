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

import json
import logging
from datetime import datetime

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


# ──────────────────────── 每周复盘：证据更新 + 状态机 ────────────────────────

MIN_SAMPLES = 20          # 证据有效的最小样本量（与复盘「走弱」口径一致）
RETIRE_NEG_WEEKS = 4      # 降级卡连续负超额周数达到该值 → 建议退役


def _append_history(card: ResearchCard, to_state: str, reason: str, actor: str) -> None:
    hist = json.loads(card.history_json) if card.history_json else []
    hist.append({
        "at": f"{datetime.now():%Y-%m-%d %H:%M}",
        "from": card.state,
        "to": to_state,
        "reason": reason,
        "actor": actor,
    })
    card.history_json = json.dumps(hist, ensure_ascii=False)


def weekly_review_cards(db: Session, strat_rows: list[dict]) -> dict:
    """每周复盘驱动研究卡：更新证据字段 + 按状态机评估迁移。

    安全边界（docs/research-card-v0.1.md）：**升级自动执行，降级/退役只出建议由人确认**。
    可机检的自动迁移：
      - watch → candidate：90 日超额 > 0 且样本 ≥ 20
      - downgraded → candidate（恢复）：90 日超额转正且样本 ≥ 20
    建议类迁移（进报告，不动状态）：
      - enabled → downgraded：90 日超额 < 0 且样本 ≥ 20（触发卡片失效条件）
      - downgraded → retired：连续 4 周负超额（周数记在证据字段 negative_weeks）
    返回 {"auto": [...], "suggest": [...]}。
    """
    cards = db.execute(
        select(ResearchCard).where(ResearchCard.card_type == "strategy")
    ).scalars().all()
    rows_by_key = {r["strategy"]: r for r in strat_rows}
    auto: list[str] = []
    suggest: list[str] = []
    today = f"{datetime.now():%Y-%m-%d}"

    for card in cards:
        ev = json.loads(card.evidence_json or "{}")
        # 幂等：同一天重复复盘（手动补跑/重跑）不重复记快照、不累加 negative_weeks
        if ev.get("evidence_updated_at") == today:
            continue
        row = rows_by_key.get(card.key)
        now = datetime.now()

        # 1) 证据字段刷新（每周快照，最多留 52 周）
        if row:
            snap = {
                "date": f"{now:%Y-%m-%d}", "window_days": 90,
                "exc_10": row.get("exc_10"), "exc_5": row.get("exc_5"),
                "n_10": row.get("n_10", 0), "win_10": row.get("win_10"),
                "t_10": row.get("t_10"),
            }
            for k in ("exc_10", "exc_5", "n_10", "win_10", "t_10"):
                ev[k] = snap[k]
            ev["evidence_updated_at"] = snap["date"]
            weekly = ev.get("weekly", [])
            weekly.append(snap)
            ev["weekly"] = weekly[-52:]
            card.evidence_json = json.dumps(ev, ensure_ascii=False)
            card.updated_at = now

        exc = (row or {}).get("exc_10")
        n = (row or {}).get("n_10") or 0
        enough = exc is not None and n >= MIN_SAMPLES

        # 2) 状态机评估
        if card.state == "watch" and enough and exc > 0:
            _append_history(card, "candidate",
                            f"90 日 T+10 超额 {exc:+.2f}%（n={n}）转正且样本达标，自动升级", "auto-review")
            card.state = "candidate"
            auto.append(f"{card.name}：观察 → 候选（90 日 {exc:+.2f}%，n={n}）")

        elif card.state == "downgraded":
            if enough and exc > 0:
                ev["negative_weeks"] = 0
                card.evidence_json = json.dumps(ev, ensure_ascii=False)
                _append_history(card, "candidate",
                                f"90 日 T+10 超额 {exc:+.2f}%（n={n}）恢复为正，自动升回候选", "auto-review")
                card.state = "candidate"
                auto.append(f"{card.name}：降级 → 候选（恢复，90 日 {exc:+.2f}%，n={n}）")
            elif enough and exc < 0:
                neg = int(ev.get("negative_weeks", 0)) + 1
                ev["negative_weeks"] = neg
                card.evidence_json = json.dumps(ev, ensure_ascii=False)
                if neg >= RETIRE_NEG_WEEKS:
                    suggest.append(
                        f"信号「{card.name}」降级后已连续 {neg} 周负超额"
                        f"（本周 {exc:+.2f}%，n={n}），达到退役条件，建议人工确认退役")

        elif card.state == "enabled" and enough and exc < 0:
            suggest.append(
                f"信号「{card.name}」触发失效条件：90 日 T+10 超额 {exc:+.2f}%"
                f"（n={n}）转负，建议人工确认降级")

        elif card.state == "candidate" and enough and exc > 0:
            suggest.append(
                f"信号「{card.name}」90 日超额 {exc:+.2f}%（n={n}，t={row.get('t_10')}）："
                f"可评估升「启用」——需补齐三窗口/成本/相关性证据后人工确认")

        elif card.state == "candidate" and enough and exc < 0:
            suggest.append(
                f"信号「{card.name}」候选期 90 日 T+10 超额 {exc:+.2f}%（n={n}）转负，"
                f"建议人工确认降为「观察」（候选档允许建仓，不宜放任）")

    db.commit()
    return {"auto": auto, "suggest": suggest}


def sync_factor_card(db: Session, *, reg, rating: str,
                     ic_mean: float | None, icir: float | None) -> ResearchCard:
    """挖矿入库时同步因子研究卡（无卡建卡；候选→启用跟随注册表自动升级）。

    已被人工降级/退役的卡不自动复活，只告警——人的决定优先于机器。
    """
    card = db.execute(select(ResearchCard).where(
        ResearchCard.card_type == "factor", ResearchCard.key == reg.name)).scalar()
    ev = {"ic_mean": ic_mean, "icir": icir, "rating": rating,
          "evidence_updated_at": f"{datetime.now():%Y-%m-%d}"}
    if card is None:
        state = "enabled" if reg.status == "enabled" else "candidate"
        card = ResearchCard(
            card_type="factor", key=reg.name, name=reg.name,
            hypothesis=f"GP 遗传规划挖掘：训练窗口 IC 显著（统计假设，经济含义待人工标注），表达式 {reg.expr}",
            data_spec="每日截面 × 全市场，写 factor_mined_daily；口径见 datahub 管道",
            evidence_json=json.dumps(ev, ensure_ascii=False),
            fail_condition="周度 IC 连续 4 周低于 0.02 或转负 → 降级评估",
            state=state,
            history_json=json.dumps([{
                "at": f"{datetime.now():%Y-%m-%d %H:%M}", "from": "-", "to": state,
                "reason": f"自动挖矿入库（评级 {rating}，IC={ic_mean}，ICIR={icir}）",
                "actor": "auto-mining"}], ensure_ascii=False),
        )
        db.add(card)
        db.commit()
        db.refresh(card)
        return card

    old_ev = json.loads(card.evidence_json or "{}")
    old_ev.update(ev)
    card.evidence_json = json.dumps(old_ev, ensure_ascii=False)
    card.updated_at = datetime.now()
    if reg.status == "enabled" and card.state == "candidate":
        _append_history(card, "enabled",
                        f"注册表启用且评级 {rating}（IC={ic_mean}），自动升级", "auto-mining")
        card.state = "enabled"
    elif reg.status == "enabled" and card.state in ("downgraded", "retired"):
        logger.warning("因子 %s 注册表 enabled 但研究卡为 %s（人工降级/退役），不自动复活",
                       reg.name, card.state)
    db.commit()
    return card
