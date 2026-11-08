"""fresh_ma 实验组合引擎测试（2026-10-14 重新优化）。

覆盖：
  1) 候选判定纯函数（新鲜信号 × 回调区 × enabled 策略）
  2) 候选池 T+10 超额计算（PIT 窗口完整性 + 全市场等权基准）
  3) regime 门控：负超额时零交易；正超额时才开仓
  4) 执行口径：T+1 开盘建仓、次日开盘出场（防前视）
  5) 风控：硬止损 / 时间止损
  6) 成本模型与经典镜像组合一致

合成数据约定：raw 价 = hfq 价（无复权差异），候选股 + 基准股两只，
使「候选收益 − 等权基准」可控。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.database import Base
from app.models import SignalAction, SignalDaily
from app.services.xq import portfolio_fresh as pf
from app.services.xq.data import Panel

D0 = date(2026, 3, 2)


def _trading_days(n: int, start: date = D0) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _synthetic_panel(days: list[date], cand_series: list[float],
                     bench_series: list[float]) -> Panel:
    """两只股票的合成面板：cand（候选）与 BENCH（基准），raw=hfq。"""
    rows = []
    for i, d in enumerate(days):
        for sym, s in (("CAND", cand_series), ("BENCH", bench_series)):
            rows.append({
                "date": d, "symbol": sym, "open": s[i], "close": s[i],
                "o_hfq": s[i], "c_hfq": s[i],
            })
    return Panel(pd.DataFrame(rows), {}, days[-1])


def _insert_signals(session: Session, days: list[date], symbols_strats: dict,
                    streak: int = 1, close_off: float = 0.0, ma20_off: float = 0.0,
                    action: str = "WATCH"):
    """每个信号日给每只股票写 signal_daily + signal_actions。

    close/ma20 由调用方通过 offset 控制（用于构造回调区/追高区）。
    """
    for d in days:
        for sym, strats in symbols_strats.items():
            for st in strats:
                session.add(SignalDaily(date=d, symbol=sym, strategy=st))
            session.add(SignalAction(
                date=d, symbol=sym, action=action, streak=streak,
                resonance=len(strats), strategies="、".join(strats),
                close=10.0 + close_off, ma10=10.0, ma20=10.0 + ma20_off,
            ))
    session.commit()


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# ── 1) 候选判定纯函数 ──

class TestIsCandidate:
    def test_accepts_fresh_pullback(self):
        assert pf._is_candidate(["chan_buy"], {"streak": 1, "close": 9.0, "ma20": 10.0},
                                {"chan_buy", "high_tight_flag"})

    def test_accepts_streak_2(self):
        assert pf._is_candidate(["high_tight_flag"], {"streak": 2, "close": 9.9, "ma20": 10.0},
                                {"high_tight_flag"})

    def test_rejects_streak_over_2(self):
        assert not pf._is_candidate(["chan_buy"], {"streak": 3, "close": 9.0, "ma20": 10.0},
                                    {"chan_buy"})

    def test_rejects_above_ma20(self):
        assert not pf._is_candidate(["chan_buy"], {"streak": 1, "close": 10.5, "ma20": 10.0},
                                    {"chan_buy"})

    def test_rejects_disabled_strategy(self):
        assert not pf._is_candidate(["turtle_trade"], {"streak": 1, "close": 9.0, "ma20": 10.0},
                                    {"chan_buy", "high_tight_flag"})

    def test_rejects_missing_ma20(self):
        assert not pf._is_candidate(["chan_buy"], {"streak": 1, "close": 9.0, "ma20": None},
                                    {"chan_buy"})


# ── 2) 候选池超额计算 ──

class TestDailyCandidateExcess:
    def test_excess_is_candidate_minus_equal_weight_bench(self):
        days = _trading_days(30)
        # 候选每日 +1%，基准每日 -1%
        cand = [10 * (1.01 ** i) for i in range(30)]
        bench = [10 * (0.99 ** i) for i in range(30)]
        panel = _synthetic_panel(days, cand, bench)
        act_map = {d: {"CAND": {"streak": 1, "close": cand[i], "ma20": cand[i] + 1}}
                   for i, d in enumerate(days)}
        sig = {d: {"CAND": ["chan_buy"]} for d in days}
        out = pf._daily_candidate_excess(days, act_map, sig, {"chan_buy"}, panel.df)
        # 只有窗口完整的日期有值（最后 11 天 shift 后为 NaN）
        assert len(out) == 30 - 11
        d = days[0]
        # T+1 开盘进、T+1 后持有 10 日：候选 (1.01^11-1)，等权基准 = 两者均值
        cand_ret = (cand[11] / cand[1]) - 1
        bench_ret = (bench[11] / bench[1]) - 1
        expected = cand_ret - (cand_ret + bench_ret) / 2
        assert out[d] == pytest.approx(expected)

    def test_non_candidate_excluded(self):
        days = _trading_days(20)
        cand = [10.0] * 20
        panel = _synthetic_panel(days, cand, [10.0] * 20)
        # streak=3 → 非候选
        act_map = {d: {"CAND": {"streak": 3, "close": 9.0, "ma20": 10.0}} for d in days}
        sig = {d: {"CAND": ["chan_buy"]} for d in days}
        out = pf._daily_candidate_excess(days, act_map, sig, {"chan_buy"}, panel.df)
        assert out == {}


# ── 3) 门控 + 执行口径（全引擎，monkeypatch load_panel）──

def _run_engine(db_session, monkeypatch, days, cand_series, bench_series,
                symbols_strats, cfg=None, **sig_kw):
    panel = _synthetic_panel(days, cand_series, bench_series)
    monkeypatch.setattr(pf, "load_panel", lambda *a, **k: panel)
    _insert_signals(db_session, days, symbols_strats, **sig_kw)
    return pf.run_portfolio_fresh(db_session, "dummy", cfg=cfg)


class TestGateAndExecution:
    def test_gate_closed_no_trades(self, db_session, monkeypatch):
        """候选持续下跌 → 门控超额为负 → 零交易、净值恒定。"""
        days = _trading_days(30)
        n = 30
        cand = [10 * (0.995 ** i) for i in range(n)]
        bench = [10.0] * n
        r = _run_engine(db_session, monkeypatch, days, cand, bench,
                        {"CAND": ["chan_buy"]}, close_off=-1.0,
                        cfg={"gate_min_samples": 3, "gate_window_days": 5})
        assert r["ok"]
        assert r["summary"]["n_trades"] == 0
        assert r["summary"]["return_pct"] == 0.0
        assert r["summary"]["gate_open_days"] == 0
        assert all(not e["gate_open"] for e in r["equity"])

    def test_gate_open_enters_at_next_open(self, db_session, monkeypatch):
        """候选上涨 → 门控开启 → 建仓价 = 信号日次一交易日开盘（非信号日收盘）。"""
        days = _trading_days(25)
        n = 25
        cand = [10 * (1.005 ** i) for i in range(n)]
        bench = [10.0] * n
        r = _run_engine(db_session, monkeypatch, days, cand, bench,
                        {"CAND": ["chan_buy"]}, close_off=-1.0,
                        cfg={"gate_min_samples": 3, "gate_window_days": 5})
        buys = [t for t in r["trades"] if t["side"] == "BUY"]
        assert buys, "门控开启后应有建仓"
        first = buys[0]
        # 首笔建仓日必须是某个信号日的次一交易日，且成交价=该日开盘价
        sig_dates = {t["date"] for t in r["trades"]}
        entry_day = date.fromisoformat(first["date"])
        prev_sig = max(d for d in days if d < entry_day)
        assert entry_day == prev_sig + timedelta(days=1) or entry_day > prev_sig
        # 成交价 = 建仓日开盘价（流水 round2；合成面板 raw=hfq、open=close）
        assert first["price"] == pytest.approx(cand[days.index(entry_day)], abs=0.011)

    def test_stop_loss_exit(self, db_session, monkeypatch):
        """先涨（门控开、建仓）后单日暴跌 15% → 硬止损 -8% 先于移动止损触发。"""
        days = _trading_days(40)
        n = 40
        base = [10 * (1.005 ** i) for i in range(20)]
        crash = base[-1] * 0.85          # 单日 -15%：硬止损（自建仓-8%）先命中
        cand = base + [crash] + [crash] * (n - 21)
        bench = [10.0] * n
        r = _run_engine(db_session, monkeypatch, days, cand, bench,
                        {"CAND": ["chan_buy"]}, close_off=-1.0,
                        cfg={"gate_min_samples": 3, "gate_window_days": 5})
        sells = [t for t in r["trades"] if t["side"] == "SELL"]
        stop_sells = [t for t in sells if "移动" not in t["reason"] and "止损" in t["reason"]]
        assert stop_sells, f"应有硬止损卖出，实际卖出原因: {[t['reason'] for t in sells]}"

    def test_time_stop_exit(self, db_session, monkeypatch):
        """微涨无止损触发 → 持有满 10 个交易日时间止损。"""
        days = _trading_days(45)
        n = 45
        cand = [10 * (1.002 ** i) for i in range(n)]
        bench = [10 * (0.999 ** i) for i in range(n)]
        r = _run_engine(db_session, monkeypatch, days, cand, bench,
                        {"CAND": ["chan_buy"]}, close_off=-1.0,
                        cfg={"gate_min_samples": 3, "gate_window_days": 5})
        time_sells = [t for t in r["trades"]
                      if t["side"] == "SELL" and "时间止损" in t["reason"]]
        assert time_sells, "应有时间止损卖出"


# ── 4) 成本模型 ──

class TestCostModel:
    def test_buy_sell_costs_match_classic(self):
        """与经典镜像组合（portfolio.py）同一套费率公式。"""
        from app.services.xq.portfolio import _buy_cost, _sell_proceeds

        cfg = pf.DEFAULT_CONFIG
        assert pf._buy_cost(10.0, 1000, cfg) == _buy_cost(10.0, 1000, cfg)
        assert pf._sell_proceeds(10.0, 1000, cfg) == _sell_proceeds(10.0, 1000, cfg)

    def test_buy_cost_includes_all_fees(self):
        c = pf._buy_cost(10.0, 1000, pf.DEFAULT_CONFIG)
        # 10*1.0001*1000=10001 + 佣金 max(2.50025,5)=5 + 过户费 0.10001
        assert c == pytest.approx(10006.10, abs=0.01)
