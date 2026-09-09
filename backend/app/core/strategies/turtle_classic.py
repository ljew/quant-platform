"""经典海龟交易法（Turtle Trading System，完整版）。

与 registry 中简化版 ``turtle``（唐奇安通道突破后直接满仓/清仓）不同，本策略
还原 Richard Dennis 海龟法则的完整四要素：

1. **入场**：收盘价突破前 ``entry`` 日最高价（唐奇安上轨）→ 建首个 Unit；
2. **仓位（N / ATR）**：1 Unit = 账户权益 × ``risk_pct`` ÷ (``stop_n`` × N)，
   其中 N 为 ATR 波动率。波动大的标的自动缩小仓位、波动小的放大，
   实现「风险等权」而非「资金等权」——这是海龟区别于普通突破策略的核心；
3. **金字塔加仓**：价格每上涨 ``add_step`` × N 加 1 个 Unit，最多 ``max_units`` 个；
4. **离场**：
   - 硬止损：跌破「最后一次建仓价 − ``stop_n`` × N」→ 全部离场（海龟 2N 止损）；
   - 通道离场：跌破前 ``exit`` 日最低价 → 清仓（趋势破位）。

无未来函数：唐奇安通道与 ATR 均使用**截至上一根 bar** 的数据计算，
信号在当根收盘价触发并以收盘价成交。

典型参数：entry=20 / exit=10 / atr_period=20 / risk_pct=0.01 / max_units=4 /
add_step=0.5 / stop_n=2.0。
"""
from __future__ import annotations

from app.core.engine import indicators as ind
from app.core.engine.base_strategy import StandardStrategy


class TurtleClassicStrategy(StandardStrategy):
    params = {
        "entry": 20,          # 唐奇安入场通道（日）
        "exit": 10,           # 唐奇安离场通道（日）
        "atr_period": 20,     # N（ATR）周期
        "risk_pct": 0.01,     # 单个 Unit 承担的账户风险比例（1%）
        "max_units": 4,       # 最大加仓单位数（含首仓）
        "add_step": 0.5,      # 加仓间隔（N 的倍数）
        "stop_n": 2.0,        # 止损幅度（N 的倍数）
        "use_exit_channel": 1,  # 是否启用 10 日通道离场（0=仅靠止损）
    }

    def init(self, ctx) -> None:
        self.units = 0          # 当前持有 Unit 数
        self.last_add = 0.0     # 最近一次建仓/加仓价（决定止损位与下次加仓线）
        self.first_entry = 0.0  # 首仓价
        self.stop_price = 0.0   # 当前止损价

    # —— 单 Unit 仓位占比：风险等权推导 ——
    # 1 Unit 每股风险 = stop_n × N；账户可承受风险额 = equity × risk_pct
    # 股数 = equity × risk_pct ÷ (stop_n × N)；市值 = 股数 × price
    # → 仓位占比 = risk_pct × price ÷ (stop_n × N)
    @staticmethod
    def _unit_pct(price: float, risk_pct: float, stop_n: float, n_val: float) -> float:
        if price <= 0 or n_val <= 0 or stop_n <= 0:
            return 0.0
        return max(0.0, min(1.0, risk_pct * price / (stop_n * n_val)))

    def _flat(self) -> None:
        """清仓后复位持仓状态。"""
        self.units = 0
        self.last_add = 0.0
        self.first_entry = 0.0
        self.stop_price = 0.0

    def on_bar(self, ctx, bar) -> None:
        entry = int(ctx.params.get("entry", 20))
        exit_n = int(ctx.params.get("exit", 10))
        atr_p = int(ctx.params.get("atr_period", 20))
        risk = float(ctx.params.get("risk_pct", 0.01))
        max_u = max(1, int(ctx.params.get("max_units", 4)))
        step = float(ctx.params.get("add_step", 0.5))
        stop_n = float(ctx.params.get("stop_n", 2.0))
        use_exit = int(ctx.params.get("use_exit_channel", 1))

        idx = ctx.index
        bars = ctx.bars
        # 需要足够历史：通道 + N（N 本身还要多一根）
        if idx < max(entry, exit_n, atr_p) + 1:
            return
        price = float(bar["close"])
        if price <= 0:
            return

        # N = 截至上一根 bar 的 ATR（不含当日，避免未来函数）
        h = [b["high"] for b in bars[:idx]]
        lo = [b["low"] for b in bars[:idx]]
        cl = [b["close"] for b in bars[:idx]]
        if len(cl) < atr_p + 1:
            return
        n_val = ind.atr(h, lo, cl, atr_p)[-1]
        if n_val is None or n_val <= 0:
            return

        # ============ 持仓中：止损 → 离场通道 → 加仓 ============
        if self.units > 0:
            # ① 2N 硬止损（以最后一次加仓价为基准，等价"最晚建仓单元"先触发止损）
            self.stop_price = self.last_add - stop_n * n_val
            if price <= self.stop_price:
                ctx.order_target_percent(
                    0.0, "turtle_stop",
                    f"止损离场：现价 {price:.2f} 跌破止损位 {self.stop_price:.2f}"
                    f"（末次建仓 {self.last_add:.2f} − {stop_n}N，N={n_val:.2f}），"
                    f"本次持仓 {self.units} 个单元全部平掉。",
                )
                self._flat()
                return

            # ② 唐奇安离场通道：跌破 exit 日最低价
            if use_exit:
                ll = min(bars[i]["low"] for i in range(idx - exit_n, idx))
                if price < ll:
                    ctx.order_target_percent(
                        0.0, "turtle_exit",
                        f"通道离场：现价 {price:.2f} 跌破 {exit_n} 日最低 {ll:.2f}，"
                        f"趋势破位，{self.units} 个单元全部平掉"
                        f"（首仓 {self.first_entry:.2f}）。",
                    )
                    self._flat()
                    return

            # ③ 金字塔加仓：每涨 add_step × N 加一个单元
            if self.units < max_u and price >= self.last_add + step * n_val:
                unit_pct = self._unit_pct(price, risk, stop_n, n_val)
                target = (self.units + 1) * unit_pct
                if target > 1.0:
                    return  # long-only 上限，仓位已满不再加
                prev = self.last_add
                self.units += 1
                self.last_add = price
                self.stop_price = price - stop_n * n_val
                ctx.order_target_percent(
                    target, "turtle_add",
                    f"加仓第 {self.units} 单元：现价 {price:.2f} 较上次建仓 {prev:.2f} "
                    f"上涨 {(price - prev) / n_val:.2f}N（≥{step}N）；"
                    f"N={n_val:.2f}，目标仓位 {target * 100:.1f}%，"
                    f"新止损 {self.stop_price:.2f}。",
                )
            return

        # ============ 空仓：唐奇安上轨突破入场 ============
        hh = max(bars[i]["high"] for i in range(idx - entry, idx))
        if price > hh:
            unit_pct = self._unit_pct(price, risk, stop_n, n_val)
            if unit_pct <= 0:
                return
            self.units = 1
            self.first_entry = price
            self.last_add = price
            self.stop_price = price - stop_n * n_val
            ctx.order_target_percent(
                min(1.0, unit_pct), "turtle_entry",
                f"突破入场：现价 {price:.2f} 上破 {entry} 日最高 {hh:.2f}；"
                f"N(ATR{atr_p})={n_val:.2f}，按 {risk * 100:.1f}% 风险定仓 "
                f"→ 首单元仓位 {unit_pct * 100:.1f}%，止损位 {self.stop_price:.2f}。",
            )
