import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import EChart from "../components/EChart";
import { api, KlineBar, SpotQuote, StockItem } from "../api/client";
import { localDate } from "../utils/date";
import { formatPrice2, klineTooltip } from "../utils/klineTooltip";
import { useTheme } from "../theme";
import { Card, PageHeader } from "../components/ui";

const SYMBOLS = ["sh600519", "sz000858", "sh000906", "sz300750", "sh688166"];

/** K 线窗口（自然日）。end 恒为本地「今天」，绝不写死结束日期——
 *  原实现把 end 写死成 2025-06-30，导致 688166 等标的的图永远停在
 *  一年多前（53.71），与同花顺 11-06 收盘 30.65 完全对不上（2026-11-07 修）。 */
const RANGES = [
  { label: "近1年", days: 365 },
  { label: "近2年", days: 730 },
  { label: "近5年", days: 1825 },
];

/** 主图均线周期与配色。均线只画在主图上，不进浮框。 */
const MA_LINES = [
  { name: "MA5", period: 5, color: "#e8901a" },
  { name: "MA20", period: 20, color: "#8e5cf0" },
  { name: "MA60", period: 60, color: "#2f86d6" },
];

/** 简单移动平均：不足窗口返回 null；结果收敛到 2 位，消除浮点长尾。 */
function sma(vals: number[], period: number): (number | null)[] {
  const out: (number | null)[] = [];
  let sum = 0;
  for (let i = 0; i < vals.length; i++) {
    sum += vals[i];
    if (i >= period) sum -= vals[i - period];
    out.push(i >= period - 1 ? Math.round((sum / period) * 100) / 100 : null);
  }
  return out;
}

function ema(vals: number[], period: number): (number | null)[] {
  const out: (number | null)[] = [];
  if (!vals.length) return out;
  const k = 2 / (period + 1);
  let prev = vals[0];
  vals.forEach((v, i) => {
    prev = i === 0 ? v : v * k + prev * (1 - k);
    out.push(i >= period - 1 ? prev : null);
  });
  return out;
}

function macd(vals: number[], fast = 12, slow = 26, signal = 9) {
  const ef = ema(vals, fast);
  const es = ema(vals, slow);
  const dif = vals.map((_, i) => (ef[i] != null && es[i] != null ? (ef[i] as number) - (es[i] as number) : null));
  const valid = dif.filter((x): x is number => x != null);
  const deaRaw = ema(valid, signal);
  const dea = dif.map((v) => (v != null ? (deaRaw.shift() ?? null) : null));
  const hist = dif.map((v, i) => (v != null && dea[i] != null ? (v - (dea[i] as number)) * 2 : null));
  return { dif, dea, hist };
}

/** 行情看板：K线+MA 主图 / 成交量 / MACD 副图（dataZoom 联动），A股红涨绿跌。 */
export default function MarketPage() {
  const [symbol, setSymbol] = useState("sh600519");
  const [bars, setBars] = useState<KlineBar[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [quote, setQuote] = useState<SpotQuote | null>(null);
  const [rangeDays, setRangeDays] = useState(730);
  const { colors } = useTheme();

  // —— 股票搜索（代码 / 简称 / 名称，防抖 250ms）——
  const [kw, setKw] = useState("");
  const [hits, setHits] = useState<StockItem[]>([]);
  const [searching, setSearching] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  const pick = useCallback((sym: string) => {
    setSymbol(sym);
    setKw("");
    setHits([]);
  }, []);

  useEffect(() => {
    const q = kw.trim();
    if (!q) { setHits([]); return; }
    const t = setTimeout(async () => {
      setSearching(true);
      try {
        const r = await api.stocks(q, 8);
        setHits(r);
      } catch {
        setHits([]);
      } finally {
        setSearching(false);
      }
    }, 250);
    return () => clearTimeout(t);
  }, [kw]);

  // 点击外部关闭下拉
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setHits([]);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  const load = useCallback(async (sym: string, days: number) => {
    setLoading(true);
    setError("");
    setQuote(null);
    try {
      // 实时快照与 K 线并行取；快照失败不影响画图。
      // limit 显式放宽到 2000：后端默认 500 根，近5年区间会被静默截断。
      const [data, snap] = await Promise.all([
        api.kline(sym, localDate(days), localDate(), 2000),
        api.quote(sym).catch(() => null),
      ]);
      setBars(data);
      setQuote(snap);
    } catch (e) {
      setError((e as Error).message);
      setBars([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(symbol, rangeDays);
  }, [symbol, rangeDays, load]);

  const option = useMemo(() => {
    const dates = bars.map((b) => b.date);
    const closes = bars.map((b) => b.close);
    const { dif, dea, hist } = macd(closes);
    const up = colors.up;
    const down = colors.down;
    return {
      tooltip: klineTooltip(bars, colors),
      legend: { data: ["K线", ...MA_LINES.map((m) => m.name), "成交量", "MACD"], top: 0 },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [
        { left: 64, right: 20, top: 36, height: "46%" },
        { left: 64, right: 20, top: "58%", height: "14%" },
        { left: 64, right: 20, top: "76%", height: "18%" },
      ],
      xAxis: [
        { type: "category", data: dates, gridIndex: 0, boundaryGap: false, axisLabel: { show: false } },
        { type: "category", data: dates, gridIndex: 1, boundaryGap: false, axisLabel: { show: false } },
        { type: "category", data: dates, gridIndex: 2, boundaryGap: false },
      ],
      yAxis: [
        { gridIndex: 0, scale: true, axisLabel: { formatter: formatPrice2 } },
        { gridIndex: 1, scale: true, axisLabel: { show: false } },
        { gridIndex: 2, scale: true, axisLabel: { show: false } },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1, 2], start: 40, end: 100 },
        { type: "slider", xAxisIndex: [0, 1, 2], height: 16, bottom: 4, start: 40, end: 100 },
      ],
      series: [
        {
          name: "K线", type: "candlestick", xAxisIndex: 0, yAxisIndex: 0,
          data: bars.map((b) => [b.open, b.close, b.low, b.high]),
          itemStyle: { color: up, color0: down, borderColor: up, borderColor0: down },
        },
        ...MA_LINES.map((m) => ({
          name: m.name, type: "line", xAxisIndex: 0, yAxisIndex: 0,
          data: sma(closes, m.period), smooth: true, showSymbol: false,
          itemStyle: { color: m.color }, lineStyle: { width: 1.2, color: m.color },
        })),
        {
          name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1,
          data: bars.map((b, i) => ({
            value: b.volume,
            itemStyle: { color: i > 0 ? (b.close >= bars[i - 1].close ? up : down) : up, opacity: 0.8 },
          })),
        },
        {
          name: "MACD", type: "bar", xAxisIndex: 2, yAxisIndex: 2,
          data: hist.map((v) => ({ value: v, itemStyle: { color: v != null && v >= 0 ? up : down } })),
        },
        { name: "DIF", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: dif, showSymbol: false, lineStyle: { width: 1 } },
        { name: "DEA", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: dea, showSymbol: false, lineStyle: { width: 1 } },
      ],
    };
  }, [bars, colors]);

  return (
    <div style={{ maxWidth: 1280, margin: "0 auto" }}>
      <PageHeader title="行情看板" desc="K线 + MA5/20/60 主图 · 成交量 / MACD 副图联动 · 默认前复权（同花顺口径）· 数据截至最近交易日"
        actions={
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            {/* 股票搜索：代码 / 简称 / 名称 */}
            <div ref={boxRef} style={{ position: "relative" }}>
              <input
                value={kw}
                onChange={(e) => setKw(e.target.value)}
                placeholder="搜股票：代码 / 简称 / 名称"
                spellCheck={false}
                style={{
                  width: 230, padding: "7px 12px", borderRadius: 999, border: `1px solid ${colors.border}`,
                  background: colors.card, color: colors.text, fontSize: 13, outline: "none",
                }}
              />
              {kw.trim() !== "" && (
                <div
                  style={{
                    position: "absolute", top: "calc(100% + 6px)", left: 0, right: 0, zIndex: 30,
                    background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 12,
                    boxShadow: "0 8px 24px rgba(0,0,0,.12)", overflow: "hidden", maxHeight: 320, overflowY: "auto",
                  }}
                >
                  {searching && hits.length === 0 && (
                    <div style={{ padding: "10px 14px", fontSize: 12.5, color: colors.muted }}>搜索中…</div>
                  )}
                  {!searching && hits.length === 0 && (
                    <div style={{ padding: "10px 14px", fontSize: 12.5, color: colors.muted }}>无匹配（试试 600519 / 茅台 / 招行）</div>
                  )}
                  {hits.map((h) => (
                    <button
                      key={h.symbol}
                      onClick={() => pick(h.symbol)}
                      style={{
                        display: "flex", justifyContent: "space-between", alignItems: "center", width: "100%",
                        padding: "8px 14px", border: 0, borderBottom: `1px solid ${colors.border}`, cursor: "pointer",
                        background: "transparent", color: colors.text, fontSize: 13, textAlign: "left",
                      }}
                      onMouseEnter={(e) => { e.currentTarget.style.background = colors.accent; e.currentTarget.style.color = "#fff"; }}
                      onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = colors.text; }}
                    >
                      <span style={{ fontWeight: 600 }}>{h.name}</span>
                      <span style={{ color: colors.muted, fontSize: 12 }}>{h.symbol.replace(/^(sh|sz|bj)/, "")}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
            {/* 快捷切换 */}
            <div style={{ display: "flex", gap: 6, background: colors.card, borderRadius: 999, padding: 4, border: `1px solid ${colors.border}` }}>
            {SYMBOLS.map((s) => (
              <button
                key={s}
                onClick={() => setSymbol(s)}
                className="num"
                style={{
                  padding: "5px 14px",
                  borderRadius: 999,
                  border: 0,
                  background: s === symbol ? colors.accent : "transparent",
                  color: s === symbol ? "#fff" : colors.muted,
                  cursor: "pointer",
                  fontSize: 12.5,
                  fontWeight: s === symbol ? 600 : 400,
                  transition: "background .15s",
                }}
              >
                {s}
              </button>
            ))}
            </div>
          </div>
        }
      />
      {error && <div style={{ color: colors.up, marginBottom: 8 }}>{error}</div>}
      {loading && <div style={{ color: colors.muted, marginBottom: 8 }}>加载中…</div>}
      {bars.length > 0 && (
        <Card
          colors={colors}
          pad={6}
          title={
            <span style={{ display: "inline-flex", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
              <span>{quote ? `${quote.name} · ${symbol}` : `${symbol} 日线`}</span>
              {quote && (
                <span className="num" style={{ color: quote.change_pct >= 0 ? colors.up : colors.down }}>
                  {quote.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  <span style={{ marginLeft: 6 }}>
                    {quote.change_pct >= 0 ? "+" : ""}{quote.change_pct.toFixed(2)}%
                  </span>
                </span>
              )}
              <span style={{ fontSize: 12, fontWeight: 400, color: colors.muted }}>
                {/^(sh000|sz399)/.test(symbol) ? "不复权" : "前复权"}
                {" · K线截至 "}{bars[bars.length - 1].date}{" · "}{bars.length}{" 根"}
              </span>
            </span>
          }
          extra={
            <div style={{ display: "flex", gap: 4 }}>
              {RANGES.map((r) => (
                <button
                  key={r.days}
                  onClick={() => setRangeDays(r.days)}
                  className="num"
                  style={{
                    padding: "4px 12px", borderRadius: 999, fontSize: 12, cursor: "pointer",
                    border: `1px solid ${colors.border}`,
                    background: r.days === rangeDays ? colors.accent : "transparent",
                    color: r.days === rangeDays ? "#fff" : colors.muted,
                  }}
                >
                  {r.label}
                </button>
              ))}
            </div>
          }
        >
          <EChart option={option as never} height={560} />
        </Card>
      )}
      {!loading && !error && bars.length === 0 && (
        <div style={{ color: colors.muted, fontSize: 13 }}>该标的在所选区间内无 K 线数据。</div>
      )}
    </div>
  );
}
