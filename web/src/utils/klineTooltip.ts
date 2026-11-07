/**
 * K 线浮框 / 价格轴统一规范（2026-11-07）
 *
 * - 浮框只显示：日期 + 高 / 开 / 低 / 收 + 成交量 + 成交额。
 *   均线只画在主图上，MACD / 成交量柱也不进浮框；避免默认 axis tooltip 把
 *   所有 series（含 MA 的浮点长尾、DIF/DEA）一股脑列出来。
 * - 价格一律 2 位小数；成交量按平台口径为整数「手」（无小数点）。
 * - 成交额接口原值是 **千元**（tushare 口径），展示层自动换算成 亿元 / 万元 / 千元，
 *   保留 2 位小数（与同花顺对照）；指数等无成交额数据的标的显示「—」。
 * - 行情看板与首页 K 线抽屉共用此规范，防止两处口径漂移。
 */
import type { KlineBar } from "../api/client";
import type { ThemeColors } from "../theme";

/** 价格统一 2 位小数，可用于 tooltip 与 y 轴刻度。 */
export const formatPrice2 = (v: number): string => Number(v).toFixed(2);

/** 成交额：入参单位为千元，自动换算成 亿元 / 万元 / 千元（2 位小数）。
 *  1 亿元 = 100000 千元，1 万元 = 10 千元。0 或缺失显示「—」，不伪装成 0 成交。 */
export function formatAmount(amount: number | null | undefined): string {
  const v = Number(amount ?? 0);
  if (!Number.isFinite(v) || v <= 0) return "—";
  if (v >= 100000) return `${(v / 100000).toFixed(2)} 亿元`;
  if (v >= 10) return `${(v / 10).toFixed(2)} 万元`;
  return `${formatPrice2(v)} 千元`;
}

const ESC: Record<string, string> = {
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
};
const esc = (s: string): string => s.replace(/[&<>"]/g, (c) => ESC[c] ?? c);

/** 构造 echarts tooltip 配置。formatter 直接按 dataIndex 取 bars，
 *  不依赖 params 里的 series 值，因此 legend 开关、均线 null 值都不会影响浮框。 */
export function klineTooltip(bars: KlineBar[], colors: ThemeColors) {
  const cell = (label: string, value: string, color: string = colors.text) =>
    `<span style="display:inline-flex;justify-content:space-between;gap:14px;min-width:128px">` +
    `<span style="color:${colors.muted}">${label}</span>` +
    `<span style="font-variant-numeric:tabular-nums;font-weight:600;color:${color}">${value}</span>` +
    `</span>`;

  return {
    trigger: "axis" as const,
    axisPointer: { type: "cross" as const },
    confine: true,
    backgroundColor: colors.card,
    borderColor: colors.border,
    borderWidth: 1,
    padding: [8, 12],
    textStyle: { color: colors.text, fontSize: 12 },
    extraCssText: "border-radius:8px;box-shadow:0 6px 20px rgba(0,0,0,.16);",
    formatter: (params: unknown): string => {
      const list = (Array.isArray(params) ? params : [params]) as { dataIndex?: number }[];
      const i = list[0]?.dataIndex;
      const b = typeof i === "number" ? bars[i] : undefined;
      if (!b) return "";
      // A 股红涨绿跌：收盘价按当日阴阳着色，其余保持中性色。
      const tone = b.close >= b.open ? colors.up : colors.down;
      return (
        `<div style="min-width:280px">` +
        `<div style="font-weight:600;margin-bottom:6px">${esc(String(b.date))}</div>` +
        `<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 14px">` +
        cell("最高", formatPrice2(b.high)) +
        cell("开盘", formatPrice2(b.open)) +
        cell("最低", formatPrice2(b.low)) +
        cell("收盘", formatPrice2(b.close), tone) +
        `</div>` +
        `<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 14px;margin-top:6px;padding-top:6px;border-top:1px solid ${colors.border}">` +
        cell("成交量", `${Math.round(b.volume).toLocaleString("zh-CN")} 手`) +
        cell("成交额", formatAmount(b.amount)) +
        `</div></div>`
      );
    },
  };
}
