"""W47-1 模拟盘体验包：详情 cost_profile 回显 + 前端校验文案护栏。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.cost_defaults import COMMISSION, SLIPPAGE
from app.core.engine.paper_engine import get_paper_task_detail
from app.core.eval_profile import build_cost_profile


def _empty_db():
    db = MagicMock()
    # snaps / trades 两次 execute 都返回空列表
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db.execute.return_value = result
    return db


def _task(**kw):
    base = dict(
        id=7,
        name="ux-demo",
        strategy_key="chan",
        kind="single",
        symbols="sh600519",
        index_code="",
        enabled=False,
        last_run_at=None,
        account_start=None,
        last_bar_date=None,
        error_msg=None,
        initial_cash=1_000_000.0,
        commission=COMMISSION,
        slippage=SLIPPAGE,
        equity_curve_json=None,
        state_json="{}",
        factor_analysis_json=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestPaperDetailCostEcho:
    def test_detail_includes_cost_profile_default(self):
        detail = get_paper_task_detail(_empty_db(), _task())
        assert detail["commission"] == COMMISSION == 0.00025
        assert detail["slippage"] == SLIPPAGE == 0.0001
        cp = detail["cost_profile"]
        assert cp["source"] == "default"
        assert cp["commission"] == COMMISSION
        assert cp["slippage"] == SLIPPAGE
        assert "万2.5" in cp["label"]
        assert detail["fees_paid_commission"] == 0.0

    def test_detail_user_cost_source(self):
        detail = get_paper_task_detail(
            _empty_db(), _task(commission=0.0003, slippage=0.0)
        )
        assert detail["commission"] == 0.0003
        assert detail["slippage"] == 0.0
        assert detail["cost_profile"]["source"] == "user"
        expect = build_cost_profile(0.0003, 0.0, source="user")
        assert detail["cost_profile"] == expect

    def test_fees_paid_commission_sums_trades(self):
        db = MagicMock()
        snaps = MagicMock()
        snaps.scalars.return_value.all.return_value = []
        t1 = SimpleNamespace(
            date="2026-01-02", symbol="sh600519", side="BUY", price=100.0,
            shares=100, cash_after=0, commission=5.0, pnl=0.0,
            signal_type="", signal_reason="",
        )
        t2 = SimpleNamespace(
            date="2026-01-03", symbol="sh600519", side="SELL", price=101.0,
            shares=100, cash_after=0, commission=5.5, pnl=0.0,
            signal_type="", signal_reason="",
        )
        trades = MagicMock()
        trades.scalars.return_value.all.return_value = [t1, t2]
        db.execute.side_effect = [snaps, trades]
        detail = get_paper_task_detail(db, _task())
        assert detail["fees_paid_commission"] == 10.5
        assert len(detail["trades"]) == 2


class TestPaperPageCopyGuardrails:
    """前端四条校验文案 + 口径句必须原文出现在 PaperPage。"""

    def test_validation_and_disclaimer_strings(self):
        src = Path(__file__).resolve().parents[2] / "web" / "src" / "pages" / "PaperPage.tsx"
        if not src.exists():
            # 容器里只挂载 backend/，前端源码不在 → 这条文案检查无处可读。
            # 之前是直接 FileNotFoundError，导致容器内跑 pytest 永远挂一条，
            # 把「真的坏了」和「环境缺文件」混在一起。
            pytest.skip(f"前端源码未挂载进容器：{src}")
        text = src.read_text(encoding="utf-8")
        assert "请先选择策略（策略列表加载中或尚未选择）" in text
        assert "请填写标的代码，例如 sh600519" in text
        assert "请填写指数代码，例如 000906" in text
        assert "初始资金须为正数" in text
        assert "与回测默认成本同契约，非券商实盘" in text
        assert "DEFAULT_COST_PROFILE" in text
        assert "cost_profile={detail.cost_profile" in text or "cost_profile={costProfile" in text
