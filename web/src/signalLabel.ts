/** 成交信号类型 → 中文标签（未知类型原样返回，避免新增策略后显示英文 key）。

 新增策略若定义了新的 signal_type（如海龟的 turtle_entry/turtle_add），
 在此登记中文名即可，回测/模拟盘成交明细与买卖点提示全自动生效。
*/
const SIGNAL_LABELS: Record<string, string> = {
  // 缠论买卖点
  buy1: "一买", buy2: "二买", buy3: "三买",
  sell1: "一卖", sell2: "二卖", sell3: "三卖",
  // 经典海龟交易法
  turtle_entry: "海龟建仓", turtle_add: "海龟加仓",
  turtle_stop: "海龟止损", turtle_exit: "海龟离场",
  // 唐奇安简化版
  突破买入: "突破买入", 破位卖出: "破位卖出",
  // MACD + 布林
  macd_boll: "MACD布林", boll_break: "跌破下轨",
  // jk 系列（jk001 沪深300 / jk002 全A）：因子选股 + 月频调仓 + 止损 + 趋势仓位
  jk_rebalance: "jk调仓买入", jk_exit: "jk调仓卖出",
  jk_stop_loss: "jk固定止损", jk_trailing_stop: "jk移动止损",
  jk_bearish: "jk看空清仓", jk_vol_trim: "jk波动减仓",
  // 风控
  risk_forced: "风控强平",
  // 通用
  manual: "手动",
};

export function signalLabel(s?: string | null, fallback = "—"): string {
  if (!s) return fallback;
  return SIGNAL_LABELS[s] ?? s;
}
