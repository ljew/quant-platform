#!/usr/bin/env python3
"""迁移脚本：把现有信号策略与注册表因子建成研究卡（纯数据，不动逻辑）。

幂等：同 (card_type, key) 已存在则跳过。重复运行安全。
用法：python scripts/migrate_research_cards.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import FactorRegistry, ResearchCard  # noqa: E402
from app.services.xq.strategies import STRATEGY_NAME, STRATEGY_QUALITY  # noqa: E402

SPEC_SIGNAL = "T+1 开盘建仓 / T+10 收盘卖出 / 超额 vs 全市场等权（xq/stats.py 口径）"
SPEC_FACTOR = "截面 Rank IC / ICIR / 分层单调性（factor_mining 口径，forward=20 日）"

# 质量档 → 卡片状态
TIER_STATE = {"strong": "enabled", "good": "enabled", "neutral": "candidate", "weak": "downgraded"}

# 策略档案（假设/失效条件/窗口证据：240 日校准 = 2026-09-30 复测；90 日 = 2026-10-03 复盘）
STRATEGY_CARDS: dict[str, dict] = {
    "high_tight_flag": {
        "hypothesis": "放量突破长期整理平台后的强势股有动量延续",
        "evidence": {"w240": 1.77, "w120": 1.66, "w60": 1.80, "d90": 2.14, "n": 349},
        "cost": "信号稀疏（≈4 条/日），换手低，扣成本后仍为正",
        "crowd": "信号稀疏，天然不拥挤",
        "fail": "任一滚动窗口转负即降级复核",
    },
    "chan_buy": {
        "hypothesis": "缠论结构买点捕捉趋势拐点",
        "evidence": {"w240": None, "w120": None, "w60": None, "d90": 0.75, "n": 17274},
        "cost": "信号密集（≈190 条/日），换手高；T+5 超额 −0.03% 提示短期无肉",
        "crowd": "密度偏高，待加确认门槛过滤",
        "fail": "90 日窗口转负",
    },
    "dual_param": {
        "hypothesis": "双周期动量共振捕捉趋势",
        "evidence": {"w240": 1.49, "w120": 2.92, "w60": None, "d90": -0.69, "n": 3607},
        "cost": "", "crowd": "", "fail": "已触发：90 日窗口转负",
    },
    "rps_breakout": {
        "hypothesis": "相对强度榜前列突破后有延续",
        "evidence": {"w240": 2.45, "w120": None, "w60": None, "d90": -1.21, "n": 8994},
        "cost": "", "crowd": "", "fail": "已触发：90 日窗口转负（240 日 +2.45% 仍为正，重点复核）",
    },
    "ma_volume": {
        "hypothesis": "均线上穿伴放量确认趋势",
        "evidence": {"w240": -0.62, "w120": None, "w60": None, "d90": -0.93, "n": 2857},
        "cost": "", "crowd": "", "fail": "已触发：两窗口皆负；连续 4 周未恢复进退役评估",
    },
    "turtle_trade": {
        "hypothesis": "突破 N 日高点后趋势延续",
        "evidence": {"w240": -0.24, "w120": None, "w60": None, "d90": -2.66, "n": 11908},
        "cost": "", "crowd": "", "fail": "已触发：大样本持续为负；退役评估候选",
    },
    "limit_up_shakeout": {
        "hypothesis": "涨停后洗盘不破是主力控盘信号",
        "evidence": {"w240": -1.15, "w120": None, "w60": None, "d90": -0.63, "n": 132},
        "cost": "", "crowd": "样本稀疏（n=132），证据强度不足", "fail": "已触发",
    },
    "private_placement": {
        "hypothesis": "定增落地后存在事件驱动超额",
        "evidence": {"w240": 0.38, "w120": None, "w60": None, "d90": -0.38, "n": 421},
        "cost": "", "crowd": "", "fail": "已触发：90 日窗口转负",
    },
    "uptrend_limit_down": {
        "hypothesis": "上升趋势中的跌停是错杀反弹机会",
        "evidence": {"w240": -1.88, "w120": None, "w60": None, "d90": -0.34, "n": 236},
        "cost": "", "crowd": "样本稀疏（n=236）", "fail": "已触发",
    },
}

TIER_LABEL = {"strong": "strong 启用档", "neutral": "neutral 观察档", "weak": "weak 不建仓档"}


def migrate() -> None:
    init_db()
    db = SessionLocal()
    created, skipped = 0, 0
    try:
        # —— 策略卡 ——
        for key, tier in STRATEGY_QUALITY.items():
            exist = db.execute(select(ResearchCard).where(
                ResearchCard.card_type == "strategy", ResearchCard.key == key)).scalar()
            if exist:
                skipped += 1
                continue
            info = STRATEGY_CARDS.get(key, {})
            db.add(ResearchCard(
                card_type="strategy", key=key,
                name=STRATEGY_NAME.get(key, key),
                hypothesis=info.get("hypothesis", "待补充"),
                data_spec=SPEC_SIGNAL,
                evidence_json=json.dumps(info.get("evidence", {}), ensure_ascii=False),
                cost_note=info.get("cost", "待评估"),
                crowd_note=info.get("crowd", ""),
                fail_condition=info.get("fail", "90 日窗口转负"),
                state=TIER_STATE.get(tier, "watch"),
                history_json=json.dumps([{
                    "date": "2026-10-08", "from": None, "to": TIER_STATE.get(tier, "watch"),
                    "reason": f"研究卡迁移：当前质量档 {TIER_LABEL.get(tier, tier)}",
                }], ensure_ascii=False),
                created_at=datetime.now(), updated_at=datetime.now(),
            ))
            created += 1

        # —— 因子卡 ——
        FACTOR_STATE = {"enabled": "enabled", "candidate": "candidate",
                        "disabled": "retired", "archived": "retired"}
        for f in db.execute(select(FactorRegistry)).scalars().all():
            exist = db.execute(select(ResearchCard).where(
                ResearchCard.card_type == "factor", ResearchCard.key == f.name)).scalar()
            if exist:
                skipped += 1
                continue
            db.add(ResearchCard(
                card_type="factor", key=f.name, name=f.name,
                hypothesis="待补充（GP 挖掘产物，可经 LLM 释义回填）",
                data_spec=SPEC_FACTOR,
                evidence_json=json.dumps({"ic_mean": f.ic_mean}, ensure_ascii=False),
                cost_note="", crowd_note="",
                fail_condition="IC 滚动窗口转负或与现有因子相关性 ≥0.8",
                state=FACTOR_STATE.get(f.status, "watch"),
                history_json=json.dumps([{
                    "date": "2026-10-08", "from": None,
                    "to": FACTOR_STATE.get(f.status, "watch"),
                    "reason": f"研究卡迁移：注册表状态 {f.status}",
                }], ensure_ascii=False),
                created_at=datetime.now(), updated_at=datetime.now(),
            ))
            created += 1

        db.commit()
        print(f"迁移完成：新建 {created} 张卡，跳过已有 {skipped} 张")
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
