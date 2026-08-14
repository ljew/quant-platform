import { useCallback, useEffect, useState } from "react";
import EChart from "../components/EChart";
import { api, BacktestResult, StrategyInfo } from "../api/client";

/** 策略回测（React 版，异步任务 + 进度轮询）。 */
export default function BacktestPage() {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [key, setKey] = useState("dual_ma");
  const [symbol, setSymbol] = useState("sh600519");
  const [start, setStart] = useState("2023-01-01");
  const [end, setEnd] = useState("2025-06-30");
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [progress, setProgress] = useState("");
  const [running, setRunning] = useState(false);

  useEffect(() => {
    api.strategies().then((s) => {
      setStrategies(s);
      if (s.length) setKey(s[0].key);
    });
  }, []);

  const run = useCallback(async () => {
    setRunning(true);
    setProgress("提交任务…");
    setResult(null);
    try {
      const meta = strategies.find((s) => s.key === key);
      const params: Record<string, unknown> = {};
      (meta?.param_schema || []).forEach((f) => (params[f.key] = f.default));
      const { task_id } = await api.backtestAsync({
        symbol,
        start,
        end,
        strategy: key,
        params,
        initial_cash: 1000000,
      });
      let t = await api.taskStatus(task_id);
      for (let i = 0; i < 300 && t.status === "running"; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        t = await api.taskStatus(task_id);
        setProgress(`进度 ${Math.round((t.progress || 0) * 100)}% · ${t.message || "引擎计算中…"}`);
      }
      if (t.status === "done" && t.result_id) {
        const res = await api.backtestDetail(t.result_id);
        setResult(res);
        setProgress("回测完成 ✓");
      } else {
        setProgress(`失败: ${t.error || "超时"}`);
      }
    } catch (e) {
      setProgress(`失败: ${(e as Error).message}`);
    } finally {
      setRunning(false);
    }
  }, [key, symbol, start, end, strategies]);

  const equityOption = {
    backgroundColor: "transparent",
    tooltip: { trigger: "axis" },
    legend: { data: ["组合净值", "基准"], top: 0 },
    grid: { left: 60, right: 20, top: 40, bottom: 40 },
    xAxis: { type: "category", data: (result?.equity_curve || []).map((p) => p.date) },
    yAxis: { type: "value", scale: true },
    series: [
      {
        name: "组合净值",
        type: "line",
        data: (result?.equity_curve || []).map((p) => p.equity),
        showSymbol: false,
        lineStyle: { color: "#cf1322", width: 1.5 },
      },
    ],
  };

  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(180px,1fr))", gap: 10, marginBottom: 12 }}>
        <label>
          策略
          <select value={key} onChange={(e) => setKey(e.target.value)} style={inputStyle}>
            {strategies.map((s) => (
              <option key={s.key} value={s.key}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          标的
          <input value={symbol} onChange={(e) => setSymbol(e.target.value)} style={inputStyle} />
        </label>
        <label>
          开始
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)} style={inputStyle} />
        </label>
        <label>
          结束
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} style={inputStyle} />
        </label>
      </div>
      <button onClick={run} disabled={running} style={{ padding: "8px 28px", borderRadius: 6, border: 0, background: "#1668dc", color: "#fff", cursor: "pointer" }}>
        {running ? "运行中…" : "运行回测（异步）"}
      </button>
      <div style={{ margin: "10px 0", color: "#888" }}>{progress}</div>

      {result && (
        <div>
          <div style={{ display: "flex", gap: 24, flexWrap: "wrap", marginBottom: 16 }}>
            <Metric label="总收益" value={`${(result.total_return * 100).toFixed(2)}%`} />
            <Metric label="年化" value={`${(result.annual_return * 100).toFixed(2)}%`} />
            <Metric label="夏普" value={result.sharpe.toFixed(2)} />
            <Metric label="最大回撤" value={`${(result.max_drawdown * 100).toFixed(2)}%`} />
            <Metric label="超额收益" value={`${((result.excess_return || 0) * 100).toFixed(2)}%`} />
          </div>
          {(result.equity_curve || []).length > 0 && <EChart option={equityOption as never} height={380} />}
          {(result.risk_limits || {}) && Object.keys(result.risk_limits || {}).length > 0 && (
            <div style={{ marginTop: 10, color: "#333" }}>
              风险硬上限: {JSON.stringify(result.risk_limits)} · 截断 {result.risk_clamps?.length || 0} 次
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const inputStyle = {
  width: "100%",
  padding: "6px 10px",
  borderRadius: 6,
  border: "1px solid #d9d9d9",
  marginTop: 4,
  boxSizing: "border-box" as const,
};

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ background: "#f6f8fa", borderRadius: 8, padding: "10px 18px", minWidth: 110 }}>
      <div style={{ fontSize: 12, color: "#888" }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 600 }}>{value}</div>
    </div>
  );
}
