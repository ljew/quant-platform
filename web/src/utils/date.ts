/**
 * 本地日历日期工具。
 *
 * 禁止用 `new Date().toISOString().slice(0, 10)`：那是 UTC 日期，
 * 北京时间 00:00–08:00 会退回前一天，导致 K 线请求的 end 少一天、
 * 最新一根 K 线画不出来（2026-11-07 修）。
 */
export function localDate(daysAgo = 0): string {
  const d = new Date();
  d.setDate(d.getDate() - daysAgo);
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}
