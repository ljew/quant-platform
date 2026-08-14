import { useCallback, useEffect, useState } from "react";
import EChart from "../components/EChart";
import { api, KlineBar } from "../api/client";

const SYMBOLS = ["sh600519", "sz000858", "sh000906", "sz300750", "sh688166"];

/** 行情看板（K线 + 均线），A股红涨绿跌。 */
export default function MarketPage() {
  const [symbol, setSymbol] = useState("sh600519");
  const [bars, setBars] = useState<KlineBar[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async (sym: string) => {
    setLoading(true);
    setError("");
    try {
      const end = "2025-06-30";
      const start = "2024-01-01";
      const data = await api.kline(sym, start, end);
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

  const option = {
    backgroundColor: "transparent",
    tooltip: { trigger: "axis" },
    legend: { data: ["K线", "MA10", "MA30"], top: 0 },
    grid: { left: 60, right: 20, top: 40, bottom: 60 },
    xAxis: {
      type: "category",
      data: bars.map((b) => b.date),
      boundaryGap: false,
    },
    yAxis: { type: "value", scale: true },
    dataZoom: [
      { type: "inside", start: 0, end: 100 },
      { type: "slider", height: 18, bottom: 10 },
    ],
    series: [
      {
        name: "K线",
        type: "candlestick",
        data: bars.map((b) => [b.open, b.close, b.low, b.high]),
        itemStyle: { color: "#cf1322", color0: "#237804", borderColor: "#cf1322", borderColor0: "#237804" },
      },
      {
        name: "MA10",
        type: "line",
        data: bars.map((_, i) => {
          if (i < 9) return null;
          const s = bars.slice(i - 9, i + 1);
          return s.reduce((a, b) => a + b.close, 0) / 10;
        }),
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 1 },
      },
      {
        name: "MA30",
        type: "line",
        data: bars.map((_, i) => {
          if (i < 29) return null;
          const s = bars.slice(i - 29, i + 1);
          return s.reduce((a, b) => a + b.close, 0) / 30;
        }),
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 1 },
      },
    ],
  };

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
              border: "1px solid #d9d9d9",
              background: s === symbol ? "#1668dc" : "#fff",
              color: s === symbol ? "#fff" : "#333",
              cursor: "pointer",
            }}
          >
            {s}
          </button>
        ))}
      </div>
      {error && <div style={{ color: "#cf1322", marginBottom: 8 }}>{error}</div>}
      {loading && <div style={{ color: "#888", marginBottom: 8 }}>加载中…</div>}
      {bars.length > 0 && <EChart option={option as never} height={480} />}
    </div>
  );
}
