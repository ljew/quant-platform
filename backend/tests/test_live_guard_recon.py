"""L5 风控 / L7 对账测试。

风控的验收标准不是「代码跑通」，而是「构造的违规场景全部被拦，且拦得有理由」。
对账的验收标准是「持仓只能由成交推出，且卖出无持仓必须报错而不是静默吞掉」。
"""
import sys
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.database import Base                                     # noqa: E402
from app.live.guard import (AccountState, GuardConfig, check,     # noqa: E402
                            limit_pct)
from app.live.intent import IntentSpec                            # noqa: E402
from app.live.recon import apply_fill, reconcile                  # noqa: E402
from app.models import LiveFill, LiveOrderIntent, LivePosition    # noqa: E402

D = date(2026, 10, 28)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def buy(sym, qty, px, name=""):
    return IntentSpec(symbol=sym, side="BUY", target_shares=qty, actual_shares=0,
                      qty=qty, ref_price=px, limit_price=round(px * 1.02, 2),
                      est_amount=qty * px, reason="test", priority=100, name=name)


def sell(sym, qty, px, name=""):
    return IntentSpec(symbol=sym, side="SELL", target_shares=0, actual_shares=qty,
                      qty=qty, ref_price=px, limit_price=round(px * 0.98, 2),
                      est_amount=qty * px, reason="test", priority=0, name=name)


def acct(total=400_000, cash=400_000, peak=None, prev=None, positions=None,
         position_value=None, industries=None):
    return AccountState(
        total_asset=total, cash=cash, peak_asset=peak or total,
        prev_close_asset=prev, positions=positions or {},
        position_value=position_value or {}, industries=industries or {})


class TestLimitPct:
    def test_board_specific_limits(self):
        assert limit_pct("sh600000") == 0.10
        assert limit_pct("sz000001") == 0.10
        assert limit_pct("sh688001") == 0.20
        assert limit_pct("sz300750") == 0.20
        assert limit_pct("bj430047") == 0.30

    def test_st_is_five_percent(self):
        assert limit_pct("sh600000", "*ST某某") == 0.05


class TestGuardBlocks:
    def test_single_position_capped(self):
        """单票 5% 上限：想买 4 万（10%）→ 截断到 2 万。"""
        r = check([buy("sh600000", 4000, 10.0)], acct(), {"sh600000": 10.0})
        assert r.n_orders == 1
        assert r.allowed[0].qty == 2000          # 20000 / 10
        assert r.adjusted, "应记录截断"

    def test_position_count_limit(self):
        """持仓已 20 只 → 第 21 只拒绝。"""
        a = acct(positions={f"sh60000{i}": 100 for i in range(20)})
        a.position_value = {k: 1000.0 for k in a.positions}
        r = check([buy("sh601111", 100, 10.0)], a, {"sh601111": 10.0})
        assert r.n_orders == 0
        assert "持仓数" in r.blocked[0][1]

    def test_industry_concentration(self):
        """单行业已占 24% → 再买同行业应被截断到 ≤25%。"""
        a = acct(total=400_000, cash=100_000,
                 positions={"sh600001": 100},
                 position_value={"sh600001": 96_000.0},
                 industries={"sh600001": "煤炭", "sh600002": "煤炭"})
        r = check([buy("sh600002", 2000, 10.0)], a, {"sh600002": 10.0})
        allowed = sum(i.qty * i.ref_price for i in r.allowed)
        assert allowed <= 400_000 * 0.25 - 96_000 + 1, "行业额度应被卡住"

    def test_limit_up_cannot_buy(self):
        """涨停（主板 +10%）买不进。"""
        r = check([buy("sh600000", 1000, 11.0)], acct(), {"sh600000": 10.0})
        assert r.n_orders == 0
        assert "涨停" in r.blocked[0][1]

    def test_st_limit_up_at_five_percent(self):
        """ST 涨 5% 即涨停，按 10% 判断会漏掉——这里验板块区分生效。"""
        r = check([buy("sh600000", 1000, 10.5, name="*ST某某")],
                  acct(), {"sh600000": 10.0})
        assert r.n_orders == 0 and "涨停" in r.blocked[0][1]

    def test_limit_down_cannot_sell(self):
        r = check([sell("sh600000", 1000, 9.0)],
                  acct(cash=0, total=10_000, positions={"sh600000": 1000},
                       position_value={"sh600000": 10_000.0}),
                  {"sh600000": 10.0})
        assert r.n_orders == 0 and "跌停" in r.blocked[0][1]

    def test_sell_without_position_blocked(self):
        r = check([sell("sh600000", 100, 10.0)], acct(), {"sh600000": 10.0})
        assert r.n_orders == 0 and "无持仓" in r.blocked[0][1]

    def test_cash_reserve_respected(self):
        """现金只剩 5% 时买入应被截断，保留 3% 下限。"""
        a = acct(total=400_000, cash=20_000)
        r = check([buy("sh600000", 2000, 10.0)], a, {"sh600000": 10.0})
        assert sum(i.est_amount for i in r.allowed) <= 20_000 - 12_000 + 1

    def test_drawdown_circuit_breaker(self):
        """组合自峰值回撤 20% → 停止新开仓，但不拦卖出。"""
        a = acct(total=320_000, cash=320_000, peak=400_000,
                 positions={"sh600001": 100}, position_value={"sh600001": 1000.0})
        r = check([buy("sh600000", 100, 10.0), sell("sh600001", 100, 10.0)],
                  a, {"sh600000": 10.0, "sh600001": 10.0})
        assert r.halted and "回撤" in r.halt_reason
        assert any(i.side == "SELL" for i in r.allowed), "熔断不应阻止卖出"

    def test_daily_loss_circuit_breaker(self):
        a = acct(total=380_000, cash=380_000, prev=400_000)
        r = check([buy("sh600000", 100, 10.0)], a, {"sh600000": 10.0})
        assert r.halted and "单日亏损" in r.halt_reason

    def test_price_deviation_guard(self):
        """超出涨跌停范围（+25%）→ 判为除权或脏数据，不出单。"""
        r = check([buy("sh600000", 100, 12.5)], acct(), {"sh600000": 10.0})
        assert r.n_orders == 0
        assert "偏离" in r.blocked[0][1]

    def test_limit_up_beats_deviation(self):
        """涨停必须报「涨停」而不是「脏数据」——顺序回归测试。"""
        r = check([buy("sh600000", 100, 11.0)], acct(), {"sh600000": 10.0})
        assert "涨停" in r.blocked[0][1]

    def test_turnover_cap(self):
        """单日换手 40% 上限：一次买满 40% 后再买应被卡。"""
        r = check([buy("sh600000", 16000, 10.0), buy("sh600001", 16000, 10.0)],
                  acct(cash=400_000, total=400_000),
                  {"sh600000": 10.0, "sh600001": 10.0})
        total_buy = sum(i.est_amount for i in r.allowed)
        assert total_buy <= 400_000 * 0.40 + 1

    def test_cash_after_exact(self):
        """全部放行时，事后现金不小于 3%——这是 min_cash_pct 的直接验证。"""
        items = [buy(f"sh60000{i}", 2000, 10.0) for i in range(5)]
        a = acct(total=400_000, cash=400_000)
        r = check(items, a, {f"sh60000{i}": 10.0 for i in range(5)})
        spent = sum(i.est_amount for i in r.allowed)
        assert 400_000 - spent >= 400_000 * 0.03 - 1


class TestRecon:
    def test_buy_creates_position(self, db):
        apply_fill(db, trade_date=D, symbol="sh600000", side="BUY",
                   qty=1000, price=10.0, commission=5.0)
        db.commit()
        p = db.scalars(select(LivePosition)).one()
        assert p.shares == 1000 and p.avg_cost == 10.0 and p.entry_date == D

    def test_second_buy_weights_cost(self, db):
        apply_fill(db, trade_date=D, symbol="sh600000", side="BUY", qty=1000, price=10.0)
        apply_fill(db, trade_date=D, symbol="sh600000", side="BUY", qty=1000, price=12.0)
        db.commit()
        p = db.scalars(select(LivePosition)).one()
        assert p.shares == 2000
        assert p.avg_cost == pytest.approx(11.0)

    def test_partial_sell_keeps_cost(self, db):
        apply_fill(db, trade_date=D, symbol="sh600000", side="BUY", qty=1000, price=10.0)
        apply_fill(db, trade_date=D, symbol="sh600000", side="SELL", qty=400, price=15.0)
        db.commit()
        p = db.scalars(select(LivePosition)).one()
        assert p.shares == 600
        assert p.avg_cost == 10.0, "卖出不应改变剩余仓位的成本基准"

    def test_full_sell_removes_position(self, db):
        apply_fill(db, trade_date=D, symbol="sh600000", side="BUY", qty=500, price=10.0)
        apply_fill(db, trade_date=D, symbol="sh600000", side="SELL", qty=500, price=11.0)
        db.commit()
        assert db.scalars(select(LivePosition)).all() == []

    def test_sell_without_position_raises(self, db):
        with pytest.raises(ValueError, match="无持仓"):
            apply_fill(db, trade_date=D, symbol="sh600000", side="SELL",
                       qty=100, price=10.0)

    def test_fill_backfills_intent(self, db):
        intent = LiveOrderIntent(trade_date=D, symbol="sh600000", side="BUY",
                                 qty=1000, ref_price=10.0, status="SENT")
        db.add(intent)
        db.flush()
        apply_fill(db, trade_date=D, symbol="sh600000", side="BUY", qty=600,
                   price=10.05, intent_id=intent.id)
        db.commit()
        db.refresh(intent)
        assert intent.status == "PARTIAL" and intent.filled_qty == 600

    def test_reconcile_slippage_and_fill_rate(self, db):
        db.add_all([
            LiveOrderIntent(trade_date=D, symbol="sh600000", side="BUY",
                            qty=1000, ref_price=10.0, status="SENT"),
            LiveOrderIntent(trade_date=D, symbol="sh600001", side="BUY",
                            qty=1000, ref_price=20.0, status="SENT"),
        ])
        db.commit()
        apply_fill(db, trade_date=D, symbol="sh600000", side="BUY",
                   qty=1000, price=10.2)      # 滑点 +2%
        apply_fill(db, trade_date=D, symbol="sh600001", side="BUY",
                   qty=300, price=20.0)       # 只成交 30%
        db.commit()

        r = reconcile(db, D)
        assert r["n_intents"] == 2
        assert r["fill_rate"] == pytest.approx((1.0 + 0.3) / 2, abs=1e-4)
        assert r["slippage_mean_pct"] == pytest.approx(1.0, abs=0.1)
        assert r["slippage_worst_pct"] == pytest.approx(0.0, abs=0.1)
        assert any(m["symbol"] == "sh600001" for m in r["missed"])
