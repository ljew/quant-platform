import { useCallback, useEffect, useMemo, useState } from "react";
import EChart from "../components/EChart";
import { api, KlineBar } from "../api/client";
import { useTheme } from "../theme";

const SYMBOLS = ["sh600519", "sz000858", "sh000906", "sz300750", "sh688166"];

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
  const { colors } = useTheme();

  const load = useCallback(async (sym: string) => {
    setLoading(true);
    setError("");
    try {
      const data = await api.kline(sym, "2023-01-01", "2025-06-30");
      setBars(data);
    } catch (e) {
      setError((e as Error).message);
      setBars([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(symbol);
  }, [symbol, load]);

  const option = useMemo(() => {
    const dates = bars.map((b) => b.date);
    const closes = bars.map((b) => b.close);
    const { dif, dea, hist } = macd(closes);
    const ma10 = closes.map((_, i) =>
      i < 9 ? null : closes.slice(i - 9, i + 1).reduce((a, b) => a + b, 0) / 10
    );
    const ma30 = closes.map((_, i) =>
      i < 29 ? null : closes.slice(i - 29, i + 1).reduce((a, b) => a + b, 0) / 30
    );
    const up = colors.up;
    const down = colors.down;
    return {
      tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
      legend: { data: ["K线", "MA10", "MA30", "成交量", "MACD"], top: 0 },
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
        { gridIndex: 0, scale: true },
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
        { name: "MA10", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: ma10, smooth: true, showSymbol: false, lineStyle: { width: 1, color: "#f0a020" } },
        { name: "MA30", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: ma30, smooth: true, showSymbol: false, lineStyle: { width: 1, color: "#8e5cf0" } },
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
    <div style={{ padding: 16 }}>
      <div style={{ marginBottom: 12, display: "flex", gap: 8, alignItems: "center" }}>
        {SYMBOLS.map((s) => (
          <button
            key={s}
            onClick={() => setSymbol(s)}
            style={{
              padding: "6px 14px",
              borderRadius: 6,
              border: `1px solid ${colors.border}`,
              background: s === symbol ? colors.accent : colors.card,
              color: s === symbol ? "#fff" : colors.text,
              cursor: "pointer",
            }}
          >
            {s}
          </button>
        ))}
      </div>
      {error && <div style={{ color: colors.up, marginBottom: 8 }}>{error}</div>}
      {loading && <div style={{ color: colors.muted, marginBottom: 8 }}>加载中…</div>}
      {bars.length > 0 && <EChart option={option as never} height={560} />}
    </div>
  );
}
