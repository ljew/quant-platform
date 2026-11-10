"""为「启用且证据过关」的策略自动建模拟盘任务。

老刘 2026-11-09：「如果策略觉得还行，就自动建立模拟盘进行跟踪」。

「还行」的判定不能靠人肉点头 —— 用研究卡自己的门槛：
  state == enabled（已通过研究层审核）
  且最近一次真实回测 超额 > 0、有成交
  且该策略还没有模拟盘任务

为什么要看回测超额：光 enabled 不够。实测 `chan_buy` 卡片是 enabled，
但它最近回测 **超额 -12.72%** —— 这种建了就是自动亏钱，必须挡住，
并在返回里说明原因，不能静默跳过。

为什么不自动删/停任务：反向操作风险大（可能正处于建仓期），只新增不删除。

⚠️ 2026-11-10 起：**自动路径不再直接建盘**。watchdog 用 mode="notify"，
只把候选交出来（并推送邮件），人确认后由页面按钮（mode="create"）或
智能体的 create_paper_task 两段式落地。原因见《智能体架构设计》09 节：
自动建盘等于「机器替人下单」，出事时没有一道闸。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger("paper_auto")

# 自动建的任务统一用这套默认参数；要改就在模拟盘页手动改那一条
AUTO_CASH = 1_000_000.0
AUTO_MIN_TRADES = 5          # 成交太少说明策略几乎不建仓，跟踪没意义


def ensure_paper_for_enabled(db: Session, dry_run: bool = False,
                             mode: str = "create") -> dict:
    """扫一遍「启用的策略卡」，缺任务的按证据决定建不建。

    返回 {"created": [...], "skipped": [...], "existing": [...]}，
    每项带 strategy_key 与原因，方便前端/日志交代清楚。
    """
    from app.models import PaperTask, ResearchCard
    from app.routers.research import _recent_backtest, match_strategy_key
    from app.core.strategies.registry import STRATEGY_REGISTRY

    created, proposals, skipped, existing = [], [], [], []

    cards = db.execute(
        select(ResearchCard).where(
            ResearchCard.card_type == "strategy",
            ResearchCard.state == "enabled",
        ).order_by(ResearchCard.id)
    ).scalars().all()

    for card in cards:
        skey = match_strategy_key(card)
        if not skey:
            skipped.append({"card": card.name, "key": card.key,
                            "reason": "没有对应的注册策略（选股器未封装）"})
            continue
        meta = STRATEGY_REGISTRY.get(skey) or {}

        has = db.execute(
            select(PaperTask).where(PaperTask.strategy_key == skey).limit(1)
        ).scalar_one_or_none()
        if has:
            existing.append({"strategy_key": skey, "task_id": has.id})
            continue

        # ⚠️ 排除 signal_union 池模式的策略（xq_* 系列）。
        # 这类策略的动态池是「全部历史命中标的的并集」，模拟盘要一次性加载
        # 全市场面板 → 实测把 5.5G 容器直接 OOM（exit 137），主进程被杀。
        # 回测能跑是因为它走子进程 + ulimit；模拟盘是在主进程线程里跑的，
        # 没有隔离。要支持得先给 paper 加子进程隔离，不是改个参数的事。
        from app.core.strategies.registry import STRATEGY_REGISTRY as _REG
        if (_REG.get(skey) or {}).get("pool_mode") == "signal_union":
            skipped.append({"strategy_key": skey,
                            "reason": "signal_union 池需全市场面板，模拟盘会 OOM，暂不支持"})
            continue

        bt = _recent_backtest(db, skey)
        if not bt:
            skipped.append({"strategy_key": skey, "reason": "还没跑过回测"})
            continue
        excess = bt.get("excess_return") or 0.0
        trades = bt.get("trade_count") or 0
        if excess <= 0:
            skipped.append({"strategy_key": skey,
                            "reason": f"回测超额 {excess:+.2%}，不达标不建"})
            continue
        if trades < AUTO_MIN_TRADES:
            skipped.append({"strategy_key": skey,
                            "reason": f"回测仅 {trades} 笔成交，样本太少"})
            continue

        # 起点取回测起点：这样模拟盘曲线接着回测往下走，不会从零开始
        start = bt.get("start") or (date.today() - timedelta(days=365)).isoformat()
        if mode == "notify":
            proposals.append({
                "strategy_key": skey,
                "name": f"{meta.get('name') or skey} · 自动跟踪",
                "excess": excess, "trades": trades, "start": start,
                "suggest": {"initial_cash": AUTO_CASH, "commission": 0.0001,
                            "slippage": 0.0015, "adj": "qfq"},
                "reason": f"启用卡 + 回测超额 {excess:+.2%} + {trades} 笔成交",
            })
            continue
        task = PaperTask(
            name=f"{meta.get('name') or skey} · 自动跟踪",
            strategy_key=skey,
            kind="portfolio" if meta.get("multi_asset") else "single",
            symbols=meta.get("index_symbol") or "",
            index_code=meta.get("index_code") or "",
            params_json="{}",
            initial_cash=AUTO_CASH,
            commission=0.0001,
            slippage=0.0015,
            adj="qfq",
            enabled=True,
            start_date=start,
            account_start=start,
        )
        if not dry_run:
            db.add(task)
            db.commit()
            db.refresh(task)
            logger.info("自动建模拟盘：#%s %s（回测超额 %+.2f%%）",
                        task.id, skey, excess * 100)
        created.append({"strategy_key": skey, "task_id": None if dry_run else task.id,
                        "excess": excess, "start": start})

    return {"mode": mode, "created": created, "proposals": proposals,
            "skipped": skipped, "existing": existing}
