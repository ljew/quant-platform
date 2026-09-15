"""jk001 熊市防御实验：验证两条修复路径。

诊断发现（scripts/diag_drawdown.py）：
  - 最大回撤 -42.84% 是 2021-02-10 → 2024-01-22 长达 715 交易日的阴跌，不是急跌；
  - 全程 1449 日里 bearish 只触发 5 天，neutral 1188 天 → 趋势风控形同虚设；
  - 波动率目标约束（vol_target）实测无效：降仓幅度小于收益损失，Calmar 全线下降。

本脚本对比：
  A baseline        原版行为
  B ma_bear         MA 空头排列（last<ma20<ma60<ma120）额外 -2 分 → bearish 可触发
  C ma_bear_soft    空头排列 -1 分 + last<ma250 再 -1 分（更连续）
  D bench_dd        基准（沪深300）自峰值回撤 >20% 降仓 50%、>30% 清仓
  E ma_bear+bdd     B + D 组合
"""
from __future__ import annotations

import argparse
import logging
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)

from app.database import SessionLocal, init_db            # noqa: E402
from app.core.strategies.registry import STRATEGY_REGISTRY  # noqa: E402
from app.routers.strategy import _run_portfolio            # noqa: E402
from app.schemas import BacktestRequest                    # noqa: E402

KEY = "jk001"


def _trend_factory(mode: str):
    """生成 _update_trend 的变体实现。"""
    def _update_trend(self, ctx, date: str) -> None:
        wk = self._week_key(date)
        if wk is not None and self._last_trend_week == wk:
            return
        self._last_trend_week = wk

        c = ctx.benchmark_history(260)
        if not c or len(c) < 200:
            return
        last = c[-1]
        ma20 = statistics.fmean(c[-20:])
        ma60 = statistics.fmean(c[-60:])
        ma120 = statistics.fmean(c[-120:])
        ma250 = statistics.fmean(c[-250:])

        score = 0
        if last > ma20 > ma60 > ma120:
            score += 3
        elif last > ma20 and ma20 > ma60:
            score += 2
        elif last > ma60:
            score += 1
        if last > ma250 * 1.2:
            score += 2
        elif last < ma250 * 0.8:
            score -= 2
        if last / c[-21] - 1 > 0.05:
            score += 1
        if len(c) >= 61 and last / c[-61] - 1 > 0.10:
            score += 2
        rets = [c[i] / c[i - 1] - 1 for i in range(len(c) - 60, len(c))]
        if len(rets) >= 20:
            vol60 = statistics.stdev(rets) * (252 ** 0.5)
            if vol60 < 0.15:
                score += 1
            elif vol60 > 0.35:
                score -= 1

        # —— 变体：让空头排列真的能压到 bearish ——
        if mode in ("ma_bear", "ma_bear_soft", "combo"):
            if last < ma20 < ma60 < ma120:
                score -= 2 if mode != "ma_bear_soft" else 1
            if mode == "ma_bear_soft" and last < ma250:
                score -= 1

        if score >= 4:
            self.market_trend, self._trend_position = "bullish", self.bullish_position
        elif score <= -2:
            self.market_trend, self._trend_position = "bearish", self.bearish_position
        else:
            self.market_trend, self._trend_position = "neutral", self.neutral_position

        # —— 变体：基准自峰值回撤降仓 ——
        if mode in ("bench_dd", "combo"):
            peak = max(c[-250:])
            dd = last / peak - 1
            if dd <= -0.30:
                self._trend_position = min(self._trend_position, self.bearish_position)
            elif dd <= -0.20:
                self._trend_position = min(self._trend_position,
                                           self.neutral_position * 0.5)
        self._recalc_position()
    return _update_trend


MODES = ["baseline", "ma_bear", "ma_bear_soft", "bench_dd", "combo"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-02")
    ap.add_argument("--end", default="2024-12-20")
    ap.add_argument("--slippage", type=float, default=0.0)
    ap.add_argument("--modes", default=",".join(MODES))
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    meta = STRATEGY_REGISTRY[KEY]
    base = dict(meta["default_params"])
    cls = meta["cls"]
    orig_trend = cls._update_trend

    import app.routers.strategy as S
    Orig = S.PortfolioBacktestEngine
    holder = {}

    class Spy(Orig):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            holder["e"] = self

    S.PortfolioBacktestEngine = Spy

    print(f"区间 {args.start} ~ {args.end}  滑点 {args.slippage}")
    print(f"{'模式':>13} | {'收益':>8} {'回撤':>8} {'夏普':>6} {'Calmar':>7} "
          f"{'基准':>8} | 趋势分布(bull/neu/bear)")
    print("-" * 92)

    for mode in args.modes.split(","):
        mode = mode.strip()
        if not mode:
            continue
        rec = []
        if mode != "baseline":
            cls._update_trend = _trend_factory(mode)
        o_ob, o_reb = cls.on_bar, cls.rebalance

        def ob(self, ctx, date):
            r = o_ob(self, ctx, date); _snap(date); return r

        def reb(self, ctx, date):
            r = o_reb(self, ctx, date); _snap(date); return r

        def _snap(date):
            e = holder.get("e")
            if not e:
                return
            eq = e._equity_today()
            mv = sum(sh * (e._last_close.get(s) or 0)
                     for s, sh in e.positions.items() if sh > 0)
            rec.append((date, eq, mv / eq if eq else 0.0,
                        getattr(self_holder.get("s"), "market_trend", "?")))

        self_holder = {}
        o_init = cls.init

        def init(self, ctx):
            o_init(self, ctx)
            self_holder["s"] = self

        cls.init, cls.on_bar, cls.rebalance = init, ob, reb
        try:
            req = BacktestRequest(symbol=meta.get("index_symbol", "sh000300"),
                                  start=args.start, end=args.end, strategy=KEY,
                                  params=base, initial_cash=1_000_000,
                                  commission=0.0003, slippage=args.slippage, adj="qfq")
            r = _run_portfolio(db, req, meta, base)
            dist = {}
            for _, _, _, t in rec:
                dist[t] = dist.get(t, 0) + 1
            calmar = (r.total_return or 0) / abs(r.max_drawdown or 1)
            print(f"{mode:>13} | {(r.total_return or 0) * 100:>7.2f}% "
                  f"{(r.max_drawdown or 0) * 100:>7.2f}% {r.sharpe or 0:>6.3f} "
                  f"{calmar:>7.2f} {(r.benchmark_total_return or 0) * 100:>7.2f}% | "
                  f"{dist.get('bullish', 0)}/{dist.get('neutral', 0)}/{dist.get('bearish', 0)}")
        except Exception as e:  # noqa: BLE001
            print(f"{mode:>13} | ERROR {e}")
        finally:
            cls.init, cls.on_bar, cls.rebalance = o_init, o_ob, o_reb
            cls._update_trend = orig_trend

    return 0


if __name__ == "__main__":
    sys.exit(main())
