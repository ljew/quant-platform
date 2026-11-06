"""xq（Sequoia 迁移）选股信号 → 组合回测适配器。

**为什么需要这一层**

`app/services/xq/strategies.py` 里的 9 个选股器产出的是「某个交易日命中了哪些
股票」，落在 `signal_daily`；而「策略回测」页走的是 `PortfolioBacktestEngine`
（按调仓周期调仓、以净值为核心、带基准对比 / 回撤 / 夏普 / 参数寻优）。
两者的数据形态不同 —— 结果就是：这 9 个策略此前只能在「信号中心」看名单，
没法用平台的标准回测设施评估「如果一直跟着它做，净值会怎样」。

本模块把 `signal_daily` 当作**时点选股事实**喂给组合引擎：
每个调仓日读取『不晚于该日的最新一期信号』，等权建仓，不在名单里的清仓。

**PIT（无前视）说明**

适配器取的是 `signal_date < 调仓日` 的最近一期信号（严格早于，不留当天）。
选股信号由当日收盘数据算出，若拿当日信号再按当日收盘价成交即构成前视；
滞后一日后，引擎按调仓日收盘价撮合就没有信息优势了 —— 这也是平台其它
组合策略的统一约定（引擎 `run()` 先 append 当日 close 再调 `rebalance`）。
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from datetime import date, datetime, timedelta

from app.core.engine.base_strategy import PortfolioStrategy

# 频次排序的回看窗口（自然日）：近 20 个自然日 ≈ 近 14 个交易日。
# 用自然日而非交易日是为了不引入交易日历依赖 —— 排序键只需要「相对多少」，
# 不需要精确到日。
_FREQ_WINDOW_DAYS = 20


def _to_date(v) -> date:
    """统一转 datetime.date（DB 取出的 DATE 列在不同驱动下可能是 date/datetime/str）。"""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


class XqSignalStrategy(PortfolioStrategy):
    """按 xq 选股信号等权持有、定期调仓的组合策略。

    不继承 StandardStrategy：它不做逐 bar 的技术指标判断，而是消费已经算好的
    时点选股结果，因此天然是横截面（多标的）策略。
    """

    params: dict = {}

    # ———————————————— 初始化 ————————————————
    def init(self, ctx) -> None:
        p = ctx.params or {}
        # signal_key 由策略注册表固化（每个 xq_* 策略对应一个选股器），不进 UI 参数表单
        self.key = str(p.get("signal_key") or "")
        self.max_holdings = max(1, int(p.get("max_holdings", 20) or 20))
        self.max_age = max(1, int(p.get("signal_max_age", 10) or 10))
        self.gross = min(1.0, max(0.0, float(p.get("gross_exposure", 1.0) or 1.0)))

        self.by_date: dict[date, list[str]] = {}
        self._dates: list[date] = []
        self._by_symbol: dict[str, list[date]] = defaultdict(list)
        self._load_signals()

    def _load_signals(self) -> None:
        """一次性把该策略的全部历史信号读进内存（几十万行，一次查询足够）。"""
        from sqlalchemy import select

        from app.database import SessionLocal
        from app.models import SignalDaily

        db = SessionLocal()
        try:
            rows = db.execute(
                select(SignalDaily.date, SignalDaily.symbol)
                .where(SignalDaily.strategy == self.key)
                .order_by(SignalDaily.date)
            ).all()
        finally:
            db.close()

        for d, sym in rows:
            dd = _to_date(d)
            self.by_date.setdefault(dd, []).append(sym)
            self._by_symbol[sym].append(dd)
        self._dates = sorted(self.by_date)

    # ———————————————— 选股 ————————————————
    def _freq(self, symbol: str, d: date) -> int:
        """该标的在 (d-20天, d) 窗口内被本策略命中的天数（排序键）。"""
        lst = self._by_symbol.get(symbol)
        if not lst:
            return 0
        lo = d - timedelta(days=_FREQ_WINDOW_DAYS)
        return bisect.bisect_left(lst, d) - bisect.bisect_left(lst, lo)

    def _targets(self, d: date) -> list[str]:
        """调仓日 d 的目标持仓（按信号强度截断到 max_holdings）。"""
        i = bisect.bisect_left(self._dates, d) - 1  # 严格早于 d 的最近信号日
        if i < 0:
            return []
        sig_date = self._dates[i]
        if (d - sig_date).days > self.max_age:
            return []  # 信号已过期 → 空仓，不拿旧名单硬撑
        syms = list(dict.fromkeys(self.by_date[sig_date]))  # 去重且保序
        if len(syms) <= self.max_holdings:
            return syms
        # 按近 20 日出现频次降序；同频按代码升序（先排序再用稳定排序做二级键）
        return sorted(sorted(syms), key=lambda s: self._freq(s, d), reverse=True)[: self.max_holdings]

    # ———————————————— 调仓 ————————————————
    def rebalance(self, ctx, date_str: str) -> None:
        d = _to_date(date_str)
        target = self._targets(d)

        held = ctx.positions()
        tset = set(target)
        for sym in held:
            if sym not in tset:
                ctx.order_target_percent(sym, 0.0, "xq_exit", "信号退出")

        if not target:
            return
        w = self.gross / len(target)
        for sym in target:
            ctx.order_target_percent(
                sym, w, "xq_entry", f"{self.key} 信号（近 20 日出现 {self._freq(sym, d)} 次）"
            )
