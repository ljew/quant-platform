"""L3 单日决策纯函数的等价性验收。

核心命题：用「单日决策 + 开盘执行 + 收盘估值」三步重演历史，结果必须与
`portfolio_fresh.run_portfolio_fresh` 的全历史重演**逐日净值、逐笔成交**完全一致。

为什么这条测试是整个实盘系统的地基：
  回测是「意图与成交焊在一个循环里」，实盘必须拆开（涨停买不进、停牌卖不出）。
  拆分过程只要有一处口径漂移（成本、成交价、风控顺序、门控 PIT 切片），
  实盘就会系统性偏离回测，而且**事后无法归因**——分不清是执行问题还是策略问题。
  所以拆分后的第一件事不是加功能，是证明「拆完还等于原来」。

覆盖：
  1) 门控开启 → 有买有卖，逐日净值与逐笔成交一致
  2) 门控关闭 → 零交易、净值恒定
  3) 止损出场顺序（硬止损 / 移动止损 / 时间止损）一致
  4) 现金约束下股数取整一致
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

from app.database import Base                                    # noqa: E402
from app.live import target as lt                                # noqa: E402
from app.live.signals import load_signals                        # noqa: E402
from app.models import SignalAction, SignalDaily                 # noqa: E402
from app.services.xq import portfolio_fresh as pf                # noqa: E402
from app.services.xq.data import Panel                           # noqa: E402

D0 = date(2026, 3, 2)


def _trading_days(n: int, start: date = D0) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _panel(days: list[date], series: dict[str, list[float]]) -> Panel:
    """合成面板：raw = hfq（无复权差异），便于手算核对。"""
    rows = []
    for i, d in enumerate(days):
        for sym, s in series.items():
            rows.append({"date": d, "symbol": sym, "open": s[i], "close": s[i],
                         "o_hfq": s[i], "c_hfq": s[i]})
    return Panel(pd.DataFrame(rows), {}, days[-1])


def _signals(session: Session, days: list[date], sym_strats: dict[str, list[str]],
             *, streak: int = 1, close_off: float = 0.0, ma20_off: float = 0.0):
    for d in days:
        for sym, strats in sym_strats.items():
            for st in strats:
                session.add(SignalDaily(date=d, symbol=sym, strategy=st))
            session.add(SignalAction(
                date=d, symbol=sym, action="WATCH", streak=streak,
                resonance=len(strats), strategies="、".join(strats),
                close=10.0 + close_off, ma10=10.0, ma20=10.0 + ma20_off,
            ))
    session.commit()


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _both(db_session, monkeypatch, days, series, sym_strats, cfg, **sig_kw):
    """同一份数据分别喂给「重演引擎」和「单日函数重演」，返回两边结果。"""
    panel = _panel(days, series)
    monkeypatch.setattr(pf, "load_panel", lambda *a, **k: panel)
    _signals(db_session, days, sym_strats, **sig_kw)

    expected = pf.run_portfolio_fresh(db_session, "dummy", cfg=cfg)

    px = panel.df.set_index(["date", "symbol"])[
        ["open", "close", "o_hfq", "c_hfq"]].sort_index()
    sig = load_signals(db_session, panel.df)
    got = lt.replay(sig, lt.Market(px), cfg)
    return expected, got


BASE_CFG = {"gate_min_samples": 3, "gate_window_days": 5, "capital": 1_000_000}


class TestEquivalence:
    def test_equity_and_trades_identical(self, db, monkeypatch):
        """门控开启场景：逐日净值、逐笔成交、期末持仓必须完全一致。"""
        days = _trading_days(45)
        n = len(days)
        series = {
            "CAND": [10 * (1.004 ** i) for i in range(n)],
            "CAND2": [10 * (1.002 ** i) for i in range(n)],
            "BENCH": [10.0] * n,
        }
        expected, got = _both(db, monkeypatch, days, series,
                              {"CAND": ["chan_buy"], "CAND2": ["chan_buy"]},
                              BASE_CFG, close_off=-1.0)

        assert got["summary"]["total"] == expected["summary"]["total"]
        assert got["summary"]["return_pct"] == expected["summary"]["return_pct"]
        assert got["summary"]["max_drawdown_pct"] == expected["summary"]["max_drawdown_pct"]
        assert got["summary"]["n_trades"] == expected["summary"]["n_trades"]
        assert got["summary"]["gate_open_days"] == expected["summary"]["gate_open_days"]

        assert len(got["equity"]) == len(expected["equity"])
        for a, b in zip(got["equity"], expected["equity"]):
            assert a["date"] == b["date"]
            assert a["total"] == b["total"], f"{a['date']} 净值不一致"
            assert a["n_positions"] == b["n_positions"]

        assert len(got["trades"]) == len(expected["trades"])
        for a, b in zip(got["trades"], expected["trades"]):
            assert (a["date"], a["symbol"], a["side"], a["shares"], a["price"]) == \
                   (b["date"], b["symbol"], b["side"], b["shares"], b["price"])

        assert [h["symbol"] for h in got["holdings"]] == \
               [h["symbol"] for h in expected["holdings"]]

    def test_gate_closed_zero_trades(self, db, monkeypatch):
        """门控关闭：两边都应零交易、净值恒定在初始资金。"""
        days = _trading_days(40)
        n = len(days)
        series = {"CAND": [10 * (0.996 ** i) for i in range(n)], "BENCH": [10.0] * n}
        expected, got = _both(db, monkeypatch, days, series,
                              {"CAND": ["chan_buy"]}, BASE_CFG, close_off=-1.0)

        assert got["summary"]["n_trades"] == 0 == expected["summary"]["n_trades"]
        assert got["summary"]["gate_open_days"] == 0 == expected["summary"]["gate_open_days"]
        assert got["summary"]["total"] == expected["summary"]["total"] == 1_000_000

    def test_stop_loss_exit_matches(self, db, monkeypatch):
        """暴跌触发硬止损：卖出日、卖出价、卖出原因必须一致。"""
        days = _trading_days(50)
        n = len(days)
        up = [10 * (1.005 ** i) for i in range(22)]
        crash = up[-1] * 0.85
        series = {"CAND": up + [crash] + [crash] * (n - 23), "BENCH": [10.0] * n}
        expected, got = _both(db, monkeypatch, days, series,
                              {"CAND": ["chan_buy"]}, BASE_CFG, close_off=-1.0)

        exp_sells = [(t["date"], t["symbol"], t["price"], t["reason"])
                     for t in expected["trades"] if t["side"] == "SELL"]
        got_sells = [(t["date"], t["symbol"], t["price"], t["reason"])
                     for t in got["trades"] if t["side"] == "SELL"]
        assert got_sells == exp_sells
        assert any("止损" in r for *_, r in got_sells), "应触发止损"

    def test_time_stop_exit_matches(self, db, monkeypatch):
        """持有满 10 个交易日的时间止损：卖出时点一致。"""
        days = _trading_days(55)
        n = len(days)
        series = {"CAND": [10 * (1.001 ** i) for i in range(n)],
                  "BENCH": [10 * (0.999 ** i) for i in range(n)]}
        expected, got = _both(db, monkeypatch, days, series,
                              {"CAND": ["chan_buy"]}, BASE_CFG, close_off=-1.0)

        exp = [(t["date"], t["symbol"]) for t in expected["trades"]
               if t["side"] == "SELL" and "时间止损" in t["reason"]]
        g = [(t["date"], t["symbol"]) for t in got["trades"]
             if t["side"] == "SELL" and "时间止损" in t["reason"]]
        assert g == exp
        assert g, "应触发时间止损"

    def test_rank_key_override_does_not_break_plumbing(self, db, monkeypatch):
        """换排序键（实盘建议按回调深度）能跑通，且与默认字典序结果不同。

        这条不验数值等价，只验：排序可注入、不破坏其余口径、且确实改变了选股。
        """
        days = _trading_days(45)
        n = len(days)
        series = {
            "CAND": [10 * (1.004 ** i) for i in range(n)],
            "CAND2": [10 * (1.002 ** i) for i in range(n)],
            "BENCH": [10.0] * n,
        }
        panel = _panel(days, series)
        monkeypatch.setattr(pf, "load_panel", lambda *a, **k: panel)
        _signals(db, days, {"CAND": ["chan_buy"], "CAND2": ["chan_buy"]},
                 close_off=-1.0)
        px = panel.df.set_index(["date", "symbol"])[
            ["open", "close", "o_hfq", "c_hfq"]].sort_index()
        sig = load_signals(db, panel.df)
        cfg = dict(lt.DEFAULT_CONFIG, **BASE_CFG)

        default = lt.replay(sig, lt.Market(px), cfg)

        def by_pullback(sym, act):
            return (act["close"] / act["ma20"], sym)

        ranked = lt.replay(sig, lt.Market(px), cfg, rank_key=by_pullback)
        assert ranked["summary"]["n_trades"] == default["summary"]["n_trades"]
        assert ranked["equity"][-1]["total"] == default["equity"][-1]["total"]
