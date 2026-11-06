"""L5 风控闸门：三层（单笔 / 组合 / 熔断）。

替代 `core/broker/risk.py`——那份是桩：`_today_loss()` 硬编码返回 0.0、
涨跌停按 ±10% 一刀切（没区分 ST 5% / 创业板科创板 20% / 北交所 30%）、
资金检查按订单表推算而非账户实际。实盘不能用。

设计原则：
1. **纯函数**。输入（意图清单 + 账户状态 + 行情 + 行业），输出（放行的 + 拦下的
   + 调整的）。不查库、不写库、不依赖当前时间——这样可以在回放里对历史任意一天
   重算风控结论，也能单测。
2. **能截断就不拒绝**。单票超限 → 减到上限；资金不足 → 减到买得起的整手。
   只有当截断后为 0 才拒绝。理由：日频策略少买一手比整天不动更接近目标组合。
3. **先卖后买**。卖出释放的资金才计入买入预算，与 apply_open 顺序一致。

熔断是最后一道，也是最粗的一道：策略可以错，不能让它在错的路上加速。
fresh_ma 无门控时回撤 -29.7%，这个数字就是熔断线存在的原因。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.live.intent import IntentSpec, lot_size
# 涨跌停幅度已下沉到 core/rules，回测与实盘共用同一份实现。
# 两侧各写一套曾在回测里退化为固定 9.5% 一刀切，导致净值与实盘背离。
from app.core.rules.limit import limit_pct

# 涨停幅度按板块区分（2026 规则）——实现见 app/core/rules/limit.py。
# 此处 re-export，保持 `from app.live.guard import limit_pct` 的老用法可用。


@dataclass
class GuardConfig:
    max_single_pct: float = 0.05        # 单票市值 / 总资产
    max_positions: int = 20
    max_industry_pct: float = 0.25      # 单行业市值 / 总资产
    min_cash_pct: float = 0.03          # 事后现金下限
    max_turnover_pct: float = 0.40      # 单日成交额 / 总资产（调仓日）
    build_turnover_pct: float = 1.00    # 初始建仓日豁免：空仓到满仓本就是 100% 换手
    price_deviation_pct: float = 0.21   # 参考价偏离昨收上限：须 > 最大涨跌停(20%)，
                                        # 否则涨停股会被误判为脏数据
    drawdown_stop_pct: float = -0.15    # 组合自峰值回撤 → 停止新开仓
    daily_loss_stop_pct: float = -0.03  # 单日亏损 → 停止当日所有交易


@dataclass
class AccountState:
    total_asset: float                  # 总资产（现金 + 持仓市值）
    cash: float
    peak_asset: float                   # 历史峰值（算回撤用）
    prev_close_asset: float | None = None   # 昨收总资产（算单日亏损用）
    positions: dict[str, int] = field(default_factory=dict)   # symbol -> shares
    position_value: dict[str, float] = field(default_factory=dict)
    industries: dict[str, str] = field(default_factory=dict)  # symbol -> 行业


@dataclass
class GuardResult:
    allowed: list[IntentSpec] = field(default_factory=list)
    blocked: list[tuple[IntentSpec, str]] = field(default_factory=list)
    adjusted: list[tuple[IntentSpec, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    halted: bool = False                # 熔断触发
    halt_reason: str = ""

    @property
    def n_orders(self) -> int:
        return len(self.allowed)


def _clip_qty(symbol: str, qty: int, price: float, budget: float) -> int:
    """按预算截断到整手（科创板 200 起）。"""
    if price <= 0 or budget <= 0:
        return 0
    lot = lot_size(symbol)
    by_budget = int(budget / price // lot * lot)
    return max(min(qty, by_budget), 0)


def check(
    items: list[IntentSpec],
    account: AccountState,
    prev_close: dict[str, float],
    cfg: GuardConfig | None = None,
) -> GuardResult:
    """对一整批意图做风控。返回放行/拦截/调整三份清单。

    prev_close: symbol -> 昨收（用于涨停校验与价格偏离校验）
    """
    cfg = cfg or GuardConfig()
    res = GuardResult()
    total = max(account.total_asset, 1.0)

    # ── 熔断层：先判断还要不要交易 ──
    dd = account.total_asset / account.peak_asset - 1.0 if account.peak_asset else 0.0
    if dd <= cfg.drawdown_stop_pct:
        res.halted = True
        res.halt_reason = f"组合自峰值回撤 {dd:.1%} ≤ {cfg.drawdown_stop_pct:.0%}，停止新开仓"
    if account.prev_close_asset:
        day = account.total_asset / account.prev_close_asset - 1.0
        if day <= cfg.daily_loss_stop_pct:
            res.halted = True
            res.halt_reason = f"单日亏损 {day:.1%} ≤ {cfg.daily_loss_stop_pct:.0%}，停止当日交易"

    cash = account.cash
    pos_count = sum(1 for q in account.positions.values() if q > 0)
    # 初始建仓日：空仓 → 满仓天然是 100% 换手，用调仓日的 40% 上限会把仓位
    # 卡在半路（实测 40 万只建到 9 只）。换成豁免档，但仅在「无持仓且无卖出」时生效。
    is_initial_build = pos_count == 0 and not any(i.side == "SELL" for i in items)
    turnover_cap = total * (cfg.build_turnover_pct if is_initial_build
                            else cfg.max_turnover_pct)
    pos_value = dict(account.position_value)
    ind_value: dict[str, float] = {}
    for s, v in pos_value.items():
        ind = account.industries.get(s) or "未知"
        ind_value[ind] = ind_value.get(ind, 0.0) + v

    turnover = 0.0

    sells = [i for i in items if i.side == "SELL"]
    buys = [i for i in items if i.side == "BUY"]

    # ── 卖出：熔断不拦卖出（只拦新开仓），但拦涨跌停与无持仓 ──
    for it in sells:
        held = account.positions.get(it.symbol, 0)
        if held <= 0:
            res.blocked.append((it, "无持仓可卖"))
            continue
        it.qty = min(it.qty, held)
        pc = prev_close.get(it.symbol, 0.0)
        if pc and it.ref_price and it.ref_price <= pc * (1 - limit_pct(it.symbol, it.name) + 0.005):
            res.blocked.append((it, f"跌停（参考价 {it.ref_price:.2f} ≤ 跌停价）卖不出"))
            continue
        cash += it.est_amount * (1 - 0.001)
        turnover += it.est_amount
        pos_count -= 1
        res.allowed.append(it)

    # ── 买入 ──
    for it in buys:
        if res.halted:
            res.blocked.append((it, f"熔断：{res.halt_reason}"))
            continue

        pc = prev_close.get(it.symbol, 0.0)
        if it.ref_price <= 0:
            res.blocked.append((it, "无有效参考价"))
            continue
        # 顺序：① 超出涨跌停范围 → 脏数据/除权；② 落在涨停区间 → 买不进。
        # 阈值 price_deviation_pct(21%) 必须大于最大涨跌停(20%)，否则主板涨停
        # 会被先判成脏数据；而涨停判断用 >=，若不先排除越界值，+25% 也会被
        # 归成「涨停」——理由错，且会掩盖真正的数据问题。
        if pc and abs(it.ref_price / pc - 1) > cfg.price_deviation_pct:
            res.blocked.append((
                it, f"参考价 {it.ref_price:.2f} 偏离昨收 {pc:.2f} 达 "
                    f"{(it.ref_price/pc-1)*100:+.1f}%（超出涨跌停范围，疑似除权或脏数据）"))
            continue
        if pc and it.ref_price >= pc * (1 + limit_pct(it.symbol, it.name) - 0.005):
            res.blocked.append((it, f"涨停（参考价 {it.ref_price:.2f} ≥ 涨停价）买不进"))
            continue

        # 组合层：持仓数
        if pos_count >= cfg.max_positions:
            res.blocked.append((it, f"持仓数已达上限 {cfg.max_positions}"))
            continue

        # 组合层：单票上限（已持有则该票不新建，避免加仓偏离等权）
        cur_v = pos_value.get(it.symbol, 0.0)
        cap_single = total * cfg.max_single_pct
        room_single = max(cap_single - cur_v, 0.0)

        # 组合层：行业上限
        ind = account.industries.get(it.symbol) or "未知"
        cap_ind = total * cfg.max_industry_pct
        room_ind = max(cap_ind - ind_value.get(ind, 0.0), 0.0)

        # 组合层：现金下限（事后现金不低于 min_cash_pct）
        room_cash = max(cash - total * cfg.min_cash_pct, 0.0)

        # 组合层：单日换手
        room_turnover = max(turnover_cap - turnover, 0.0)

        budget = min(room_single, room_ind, room_cash, room_turnover,
                     it.est_amount if it.est_amount > 0 else cap_single)
        new_qty = _clip_qty(it.symbol, it.qty, it.ref_price, budget)

        if new_qty <= 0:
            why = ("单票额度已满" if room_single <= 0 else
                   "行业额度已满" if room_ind <= 0 else
                   "现金不足" if room_cash <= 0 else
                   "换手额度已满" if room_turnover <= 0 else
                   f"预算 {budget:,.0f} 买不满最小手")
            res.blocked.append((it, why))
            continue

        if new_qty < it.qty:
            old = it.qty
            it.qty = new_qty
            it.est_amount = round(new_qty * it.ref_price, 2)
            res.adjusted.append((it, f"数量 {old} → {new_qty}（受额度约束）"))

        cash -= it.est_amount
        turnover += it.est_amount
        pos_count += 1
        pos_value[it.symbol] = pos_value.get(it.symbol, 0.0) + it.est_amount
        ind_value[ind] = ind_value.get(ind, 0.0) + it.est_amount
        res.allowed.append(it)

    if cash < total * cfg.min_cash_pct:
        res.warnings.append(f"事后现金 {cash:,.0f} 低于下限 {total*cfg.min_cash_pct:,.0f}")
    if turnover > turnover_cap:
        res.warnings.append(f"单日换手 {turnover/total:.1%} 超上限 {turnover_cap/total:.0%}")

    return res
