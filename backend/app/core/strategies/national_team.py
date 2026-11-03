"""国家队介入强度指数（NSI）择时策略。

**这是什么**

用宽基 ETF 份额的日频变化衡量「有组织承接资金」的介入强度，输出 0–100 的信号分，
在基础仓位上做加法：触发时 100%，其余时间 70%。**只加不减**。

研究结论与规格见 `docs/NSI-SPEC.md`，研究卡 `research_cards` id=13
（key=`national_team_index_nsi`）。

**为什么标的是指数而不是 ETF**

平台的 `kline_daily` 只收个股、`index_kline_daily` 收指数，没有 ETF 行情。
本策略在 `sh000300` 上验证信号的择时价值（不含 ETF 跟踪误差与买卖价差）；
实盘执行时再换成 510300 等宽基 ETF。

**PIT（无前视）**

信号由 T 日收盘后的 ETF 份额算出，而引擎按 T 日收盘价撮合。
因此 `on_bar(T)` 只能读取**严格早于 T** 的信号，与 `xq_signal` 的约定一致。
若拿当日信号按当日收盘价成交即构成前视（实测虚增 0.14pp 年化）。
"""
from __future__ import annotations

import csv
import os
from bisect import bisect_left
from datetime import date, datetime
from typing import Any

from app.core.engine.base_strategy import StandardStrategy

DEFAULT_CSV = os.environ.get("NSI_SIGNAL_CSV", "/data/research/nsi_signal.csv")


def _to_date(v: Any) -> date | None:
    """把各种日期表示统一成 date。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v)[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


class NationalTeamStrategy(StandardStrategy):
    """国家队介入强度择时（单标的、多空仅多头、仓位两档）。

    信号默认读 `nsi_signal.csv`（由 `scripts/research/nsi_daily.py` 每日产出）。
    若传入 `threshold`>0，则用 csv 里的 `score` 列按该阈值重算仓位，
    便于在前端做阈值敏感性实验；否则直接用 csv 已算好的 `position` 列。
    """

    params: dict = {
        "signal_csv": DEFAULT_CSV,
        "base_position": 0.70,
        "boost_position": 1.00,
        "hold_days": 20,
        "threshold": 0.0,          # 0 = 用 csv 的 position 列；>0 = 用 score 重算
        "hhi_threshold": 0.0,      # 0 = 关闭；>0 = 申购集中度高于此值时不加仓
        "warmup_days": 0,
    }

    # ———————————————— 初始化 ————————————————
    def init(self, ctx) -> None:
        p = ctx.params or {}
        self.csv_path = str(p.get("signal_csv") or DEFAULT_CSV)
        self.base = min(1.0, max(0.0, float(p.get("base_position", 0.70))))
        self.boost = min(1.0, max(0.0, float(p.get("boost_position", 1.00))))
        self.hold_days = max(1, int(p.get("hold_days", 20)))
        self.threshold = float(p.get("threshold", 0.0) or 0.0)
        self.hhi_threshold = float(p.get("hhi_threshold", 0.0) or 0.0)

        self.scores: dict[date, float] = {}
        self.positions: dict[date, float] = {}
        self.hhis: dict[date, float] = {}
        self._dates: list[date] = []
        self._last_trigger_at: int | None = None
        self._load()

    def _load(self) -> None:
        """把信号序列读进内存（约 2000 行，开跑前一次性加载）。"""
        if not os.path.exists(self.csv_path):
            # 没有信号文件时退化为「恒基础仓位」，让回测照跑但结果无意义，
            # 避免抛异常把整条回测链路打断（前端会看到净值=基准×仓位）。
            self._warned = True
            return

        with open(self.csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            # 首列可能是 date / Unnamed: 0（pandas to_csv 的索引列）
            key = reader.fieldnames[0] if reader.fieldnames else "date"
            for row in reader:
                d = _to_date(row.get(key))
                if d is None:
                    continue
                try:
                    sc = row.get("score")
                    ps = row.get("position")
                    if sc not in (None, "", "nan"):
                        self.scores[d] = float(sc)
                    if ps not in (None, "", "nan"):
                        self.positions[d] = float(ps)
                    hs_ = row.get("hhi")
                    if hs_ not in (None, "", "nan"):
                        self.hhis[d] = float(hs_)
                except (TypeError, ValueError):
                    continue
        self._dates = sorted(set(self.scores) | set(self.positions))

    # ———————————————— 信号查询（PIT） ————————————————
    def _prev_index(self, d: date) -> int:
        """返回严格早于 d 的最后一个信号下标；无则 -1。"""
        if not self._dates:
            return -1
        i = bisect_left(self._dates, d)   # 第一个 >= d 的位置
        return i - 1

    def _lookup(self, d: date) -> tuple[float | None, float | None, float | None]:
        i = self._prev_index(d)
        if i < 0:
            return None, None, None
        dd = self._dates[i]
        return self.scores.get(dd), self.positions.get(dd), self.hhis.get(dd)

    # ———————————————— 逐 bar ————————————————
    def on_bar(self, ctx, bar) -> None:
        d = _to_date(bar.get("date"))
        if d is None:
            return

        if self.threshold > 0:
            # 用 score 重算：阈值来自前端参数，状态机在策略内维护
            score, _, hhi = self._lookup(d)
            if score is None:
                return
            if score >= self.threshold:
                self._last_trigger_at = ctx.index
            holding = (ctx.index - self._last_trigger_at) if self._last_trigger_at is not None else 10**9
            target = self.boost if holding <= self.hold_days else self.base
            if self._last_trigger_at == ctx.index:
                reason = (f"NSI 触发：score={score:.1f} ≥ 阈值 {self.threshold:.1f}，"
                          f"国家队承接迹象强，加仓至 {target*100:.0f}%。")
            else:
                reason = (f"NSI 未触发：score={score:.1f} < 阈值 {self.threshold:.1f}，"
                          f"维持 {target*100:.0f}% 仓位。")
        else:
            # 用 csv 已算好的 position 判定「是否加仓」，但**实际仓位按前端参数映射**。
            # 若直接下发 csv 的 0.7/1.0，base_position / boost_position 会完全失效
            # （2026-11-03 验证时踩到：两组参数跑出完全相同的净值）。
            _, pos, hhi = self._lookup(d)
            if pos is None:
                return
            # csv 的 position 是「研究口径下的二元档位」，>0.85 视为加仓档
            target = self.boost if pos > 0.85 else self.base
            nxt = "加仓（国家队托底）" if target > self.base + 1e-9 else "基础仓位"
            reason = f"NSI 信号 {pos:.2f} → {target*100:.0f}%，{nxt}。"

        # 拥挤度过滤（2026-11-03）：申购过度集中于单只 ETF 时，流入更像套利/单一机构，
        # 不是多只同步的国家队行为 —— 此时不加仓。实测低 HHI 组 IC +0.128、高 HHI 组 -0.068。
        if self.hhi_threshold > 0 and hhi is not None and hhi > self.hhi_threshold \
                and target > self.base + 1e-9:
            reason += f"（HHI {hhi:.3f} > {self.hhi_threshold:.3f}，申购集中于单只，取消加仓）"
            target = self.base

        if abs(target - getattr(self, "_last_target", -1.0)) > 1e-9:
            self._last_target = target
            ctx.order_target_percent(target, "NSI", reason)
